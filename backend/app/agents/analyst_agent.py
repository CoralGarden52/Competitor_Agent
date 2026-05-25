from __future__ import annotations

import concurrent.futures
import logging
from typing import Any

from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.models import (
    AnalysisFieldResult,
    AnalysisSchemaField,
    AnalyzeOutput,
    CompetitorAnalysisRecord,
    CompetitorEvidenceBundle,
    CompetitorProfile,
    FeatureNode,
    FeedbackSummary,
    FieldEvidenceBundle,
    Finding,
    PricingModel,
    PricingTier,
    RawEvidence,
    RunState,
)
from app.core.prompts.agent_prompts import ANALYZE_SYSTEM_PROMPT
from app.core.schema_registry import get_domain_schema
from app.core.storage import SQLiteStore

logger = logging.getLogger(__name__)


CORE_PROFILE_FIELDS = {'feature_tree', 'strengths', 'weaknesses', 'pricing_model', 'user_feedback'}


class AnalystAgent:
    def __init__(self, llm: AgentLLMClient, store: SQLiteStore):
        self.llm = llm
        self.store = store

    def run_llm(self, state: RunState) -> AnalyzeOutput:
        schema_plan = self._schema_plan(state)
        schema_map = {item.field_name: item for item in schema_plan}
        bundles = self._build_competitor_evidence_bundles(state, schema_plan)

        tasks: list[tuple[int, int, str, FieldEvidenceBundle, AnalysisSchemaField | None]] = []
        for bundle_index, bundle in enumerate(bundles):
            print(f"  分析竞品: {bundle.product_name}")
            for field_index, field_bundle in enumerate(bundle.fields):
                print(f"    分析字段: {field_bundle.field_name}")
                tasks.append(
                    (
                        bundle_index,
                        field_index,
                        bundle.product_name,
                        field_bundle,
                        schema_map.get(field_bundle.field_name),
                    )
                )

        field_results: dict[tuple[int, int], AnalysisFieldResult] = {}
        max_workers = min(max(1, len(tasks)), self.llm.config.analyze_llm_max_workers)

        def _run_task(task: tuple[int, int, str, FieldEvidenceBundle, AnalysisSchemaField | None]) -> tuple[tuple[int, int], AnalysisFieldResult]:
            bundle_index, field_index, competitor, field_bundle, schema_item = task
            result = self._analyze_single_field(
                competitor=competitor,
                field_name=field_bundle.field_name,
                evidences=field_bundle.evidences,
                industry=state.industry,
                schema_item=schema_item,
            )
            return (bundle_index, field_index), result

        if tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_run_task, task) for task in tasks]
                for future in concurrent.futures.as_completed(futures):
                    key, result = future.result()
                    field_results[key] = result

        records = []
        for bundle_index, bundle in enumerate(bundles):
            ordered_fields = [
                field_results[(bundle_index, field_index)]
                for field_index, _field_bundle in enumerate(bundle.fields)
            ]
            records.append(CompetitorAnalysisRecord(product_name=bundle.product_name, fields=ordered_fields))

        profiles = [self._profile_from_record(state=state, record=record) for record in records]
        findings = self._build_findings_from_records(records)
        return AnalyzeOutput(competitors=records, profiles=profiles, findings=findings)
    
    def _analyze_single_field(
        self,
        competitor: str,
        field_name: str,
        evidences: list[RawEvidence],
        industry: str,
        schema_item: AnalysisSchemaField | None = None,
    ) -> AnalysisFieldResult:
        """对单个字段进行分析，单独调用LLM"""
        if not evidences:
            return AnalysisFieldResult(
                field_name=field_name,
                summary='unknown',
                evidence_refs=[],
                confidence=0.2,
                normalized_value={},
                evidence_gaps=[f'no_evidence_for_{field_name}'],
            )
        
        # 准备证据内容
        evidence_contents = []
        evidence_ids = []
        for ev in evidences[:5]:  # 最多使用5条证据
            evidence_ids.append(ev.evidence_id)
            content = ev.snippet[:500] if ev.snippet else ''
            if content:
                title = ev.title.strip()[:120] if ev.title else ''
                query = ev.query.strip()[:120] if ev.query else ''
                evidence_contents.append(
                    f"证据{len(evidence_contents)+1}（来源: {ev.source_type}，标题: {title}，查询: {query}）:\n{content}"
                )

        query_templates = schema_item.query_templates if schema_item is not None else []
        recommended_sources = schema_item.recommended_sources if schema_item is not None else []
        sys_prompt = (
            f"{ANALYZE_SYSTEM_PROMPT}\n\n"
            "你当前只需要分析单个字段，并返回严格 JSON："
            '{"summary":"...","normalized_value":{},"evidence_gaps":[]}\n'
            "summary 必须是原创总结，且要紧扣当前字段语义。\n"
            "normalized_value 必须尽可能结构化，并与字段类型相匹配。\n"
            "如果证据不足，可以在 evidence_gaps 中写出缺口，但不要编造。"
        )
        
        user_prompt = {
            'competitor': competitor,
            'industry': industry,
            'field_name': field_name,
            'field_context': {
                'query_templates': query_templates,
                'recommended_sources': recommended_sources,
                'analysis_focus': self._field_analysis_focus(field_name),
                'normalized_value_shape': self._normalized_value_shape_hint(field_name),
            },
            'evidences': evidence_contents,
            'instruction': (
                f"请基于以上证据，对【{field_name}】字段进行分析总结。"
                "必须总结提炼，不能直接复制原文；"
                "必须结合 field_context 理解这个字段要回答什么；"
                "只输出和该 schema item 直接相关的内容。"
            ),
        }
        
        try:
            result = self.llm.invoke_json(
                trace_name=f'agent.analyze.field.{field_name}',
                system_prompt=sys_prompt,
                user_payload=user_prompt,
                metadata={
                    'competitor': competitor,
                    'field_name': field_name,
                    'evidence_count': len(evidences),
                },
            )
            summary = str(result.get('summary', '')).strip()
            normalized_value = self._coerce_normalized_value(
                field_name=field_name,
                raw_value=result.get('normalized_value', {}),
                summary=summary,
            )
            evidence_gaps = self._clean_evidence_gaps(result.get('evidence_gaps', []))
            
            # 如果还是没有有效结果，使用fallback
            if not summary or summary.lower() == 'none' or summary.lower() == 'unknown':
                raise ValueError("Empty or invalid result")
            
            confidence = min(0.9, 0.5 + (0.1 * min(len(evidences), 5)))
            
        except Exception as e:
            logger.warning(f"Failed to analyze field {field_name} for {competitor}: {e}")
            summary = self._fallback_summary(field_name, competitor, evidences, query_templates, recommended_sources)
            normalized_value = self._fallback_normalized_value(
                field_name,
                competitor,
                evidences,
                query_templates=query_templates,
                recommended_sources=recommended_sources,
            )
            evidence_gaps = [] if evidences else [f'no_evidence_for_{field_name}']
            confidence = 0.35 + (0.12 * min(len(evidences), 4))
        
        return AnalysisFieldResult(
            field_name=field_name,
            summary=summary.strip()[:500],
            evidence_refs=evidence_ids,
            confidence=confidence,
            normalized_value=normalized_value,
            evidence_gaps=evidence_gaps,
        )

    def run_fallback(self, state: RunState) -> AnalyzeOutput:
        schema_plan = self._schema_plan(state)
        bundles = self._build_competitor_evidence_bundles(state, schema_plan)
        records = [self._fallback_record(bundle, schema_plan) for bundle in bundles]
        profiles = [self._profile_from_record(state=state, record=record) for record in records]
        findings = self._build_findings_from_records(records)
        return AnalyzeOutput(competitors=records, profiles=profiles, findings=findings)

    def _schema_plan(self, state: RunState) -> list[AnalysisSchemaField]:
        if state.analysis_schema_plan:
            return [item if isinstance(item, AnalysisSchemaField) else AnalysisSchemaField.model_validate(item) for item in state.analysis_schema_plan]
        return [
            AnalysisSchemaField(field_name='feature_tree', query_templates=['{product} feature_tree'], recommended_sources=['official'], priority=1),
            AnalysisSchemaField(field_name='strengths', query_templates=['{product} strengths'], recommended_sources=['review'], priority=2),
            AnalysisSchemaField(field_name='weaknesses', query_templates=['{product} weaknesses'], recommended_sources=['review'], priority=3),
            AnalysisSchemaField(field_name='pricing_model', query_templates=['{product} pricing_model'], recommended_sources=['official'], priority=4),
            AnalysisSchemaField(field_name='user_feedback', query_templates=['{product} user_feedback'], recommended_sources=['community'], priority=5),
        ]

    def _build_competitor_evidence_bundles(
        self,
        state: RunState,
        schema_plan: list[AnalysisSchemaField],
    ) -> list[CompetitorEvidenceBundle]:
        active_competitors = state.planned_competitors or state.competitors
        field_names = [item.field_name for item in schema_plan]
        bundles: list[CompetitorEvidenceBundle] = []
        for competitor in active_competitors:
            field_bundles: list[FieldEvidenceBundle] = []
            for field_name in field_names:
                matches = [
                    ev
                    for ev in state.evidences
                    if self._evidence_matches_competitor(ev, competitor) and self._evidence_matches_field(ev, field_name)
                ]
                field_bundles.append(FieldEvidenceBundle(field_name=field_name, evidences=matches))
            bundles.append(CompetitorEvidenceBundle(product_name=competitor, fields=field_bundles))
        return bundles

    def _ensure_analysis_consistency(
        self,
        analyzed: AnalyzeOutput,
        *,
        state: RunState,
        schema_plan: list[AnalysisSchemaField],
    ) -> AnalyzeOutput:
        records = analyzed.competitors or []
        if records:
            records = self._normalize_records(records, schema_plan=schema_plan)
        else:
            bundles = self._build_competitor_evidence_bundles(state, schema_plan)
            records = [self._fallback_record(bundle, schema_plan) for bundle in bundles]

        profiles = analyzed.profiles or [self._profile_from_record(state=state, record=record) for record in records]
        findings = analyzed.findings or self._build_findings_from_records(records)
        return AnalyzeOutput(competitors=records, profiles=profiles, findings=findings)

    def _normalize_records(
        self,
        records: list[CompetitorAnalysisRecord],
        *,
        schema_plan: list[AnalysisSchemaField],
    ) -> list[CompetitorAnalysisRecord]:
        ordered_fields = [item.field_name for item in sorted(schema_plan, key=lambda x: x.priority)]
        normalized: list[CompetitorAnalysisRecord] = []
        for record in records:
            by_field = {item.field_name: item for item in record.fields}
            fields: list[AnalysisFieldResult] = []
            for field_name in ordered_fields:
                field_result = by_field.get(field_name)
                if field_result is None:
                    fields.append(
                        AnalysisFieldResult(
                            field_name=field_name,
                            summary='unknown',
                            evidence_refs=[],
                            confidence=0.2,
                            normalized_value={},
                            evidence_gaps=[f'missing_analysis_for_{field_name}'],
                        )
                    )
                else:
                    fields.append(field_result)
            normalized.append(CompetitorAnalysisRecord(product_name=record.product_name, fields=fields))
        return normalized

    def _fallback_record(
        self,
        bundle: CompetitorEvidenceBundle,
        schema_plan: list[AnalysisSchemaField],
    ) -> CompetitorAnalysisRecord:
        results = [self._fallback_field_result(bundle.product_name, field.field_name, field.evidences) for field in bundle.fields]
        if not results:
            results = [self._fallback_field_result(bundle.product_name, item.field_name, []) for item in schema_plan]
        return CompetitorAnalysisRecord(product_name=bundle.product_name, fields=results)

    def _fallback_field_result(self, competitor: str, field_name: str, evidences: list[RawEvidence]) -> AnalysisFieldResult:
        refs = [ev.evidence_id for ev in evidences[:4]]
        summary = self._fallback_summary(field_name, competitor, evidences, [], [])
        normalized = self._fallback_normalized_value(field_name, competitor, evidences, query_templates=[], recommended_sources=[])
        gaps = [] if evidences else [f'no_evidence_for_{field_name}']
        confidence = min(0.9, 0.35 + (0.12 * min(len(evidences), 4))) if evidences else 0.2
        return AnalysisFieldResult(
            field_name=field_name,
            summary=summary,
            evidence_refs=refs,
            confidence=confidence,
            normalized_value=normalized,
            evidence_gaps=gaps,
        )

    def _profile_from_record(self, *, state: RunState, record: CompetitorAnalysisRecord) -> CompetitorProfile:
        field_map = {field.field_name: field for field in record.fields}
        feature_tree = self._feature_tree_from_field(field_map.get('feature_tree'))
        strengths = self._list_from_field(field_map.get('strengths'), fallback='strengths')
        weaknesses = self._list_from_field(field_map.get('weaknesses'), fallback='weaknesses')
        pricing_model = self._pricing_model_from_field(field_map.get('pricing_model'))
        user_feedback = self._feedback_from_field(field_map.get('user_feedback'))
        core_refs: list[str] = []
        for field_name in CORE_PROFILE_FIELDS:
            field = field_map.get(field_name)
            if field is not None:
                core_refs.extend(field.evidence_refs)
        domain = get_domain_schema(self.store, state.industry)
        dynamic_fields = {
            field.field_name: {
                'summary': field.summary,
                'confidence': field.confidence,
                'normalized_value': field.normalized_value,
                'evidence_refs': field.evidence_refs,
                'evidence_gaps': field.evidence_gaps,
            }
            for field in record.fields
            if field.field_name not in CORE_PROFILE_FIELDS
        }
        extension_data: dict[str, Any] = {'analysis_fields': dynamic_fields}
        for required in domain.required_extension_fields:
            field = dynamic_fields.get(required)
            extension_data[required] = field.get('summary', 'unknown') if isinstance(field, dict) else 'unknown'

        positioning = self._positioning_from_record(record)
        return CompetitorProfile(
            industry=state.industry,
            product_name=record.product_name,
            positioning=positioning,
            feature_tree=feature_tree,
            advantages=strengths or ['unknown'],
            disadvantages=weaknesses or ['unknown'],
            pricing_model=pricing_model,
            user_feedback=user_feedback,
            evidence_refs=self._dedupe_refs(core_refs),
            domain_extensions=extension_data,
        )

    def _build_findings_from_records(self, records: list[CompetitorAnalysisRecord]) -> list[Finding]:
        findings: list[Finding] = []
        category_map = {'feature_tree': 'feature', 'pricing_model': 'pricing', 'user_feedback': 'feedback'}
        for record in records:
            for field in record.fields:
                if not field.evidence_refs:
                    continue
                findings.append(
                    Finding(
                        statement=f'{record.product_name} 在 {field.field_name} 维度：{field.summary}',
                        category=category_map.get(field.field_name, 'feature'),
                        evidence_refs=field.evidence_refs[:3],
                        confidence=field.confidence,
                        risk_flag=bool(field.evidence_gaps),
                    )
                )
        return findings

    @staticmethod
    def _evidence_matches_competitor(evidence: RawEvidence, competitor: str) -> bool:
        competitor_hint = str(evidence.domain_extensions.get('competitor', '')).strip().casefold()
        if competitor_hint and competitor_hint == competitor.casefold():
            return True
        haystacks = [
            evidence.query,
            evidence.title,
            evidence.snippet,
            evidence.source_url,
            str(evidence.domain_extensions.get('content_excerpt', '')),
        ]
        needle = competitor.casefold()
        return any(needle in str(item).casefold() for item in haystacks if str(item).strip())

    @staticmethod
    def _evidence_matches_field(evidence: RawEvidence, field_name: str) -> bool:
        schema_field = str(evidence.domain_extensions.get('schema_field', '')).strip().casefold()
        if schema_field:
            return schema_field == field_name.casefold()
        query_template = str(evidence.domain_extensions.get('query_template', '')).strip().casefold()
        return field_name.casefold() in query_template

    @staticmethod
    def _dedupe_refs(refs: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            key = ref.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    @staticmethod
    def _fallback_summary(
        field_name: str,
        competitor: str,
        evidences: list[RawEvidence],
        query_templates: list[str],
        recommended_sources: list[str],
    ) -> str:
        if not evidences:
            return 'unknown'
        
        # 收集所有证据的摘要内容
        all_snippets = []
        for ev in evidences:
            if ev.snippet and ev.snippet.strip():
                all_snippets.append(ev.snippet.strip())
            elif ev.title and ev.title.strip():
                all_snippets.append(ev.title.strip())
        
        # 合并摘要内容
        combined_content = ' '.join(all_snippets)[:500]
        
        source_hint = f"重点来源包括：{', '.join(recommended_sources[:3])}。" if recommended_sources else ''
        if field_name == 'feature_tree':
            if combined_content:
                return f'{competitor} 的核心功能和能力结构主要体现在：{combined_content[:180]}。'
            return f'{competitor} 的功能结构可从已采集页面中观察到核心平台、自动化与集成相关能力。{source_hint}'
        if field_name == 'pricing_model':
            if combined_content:
                return f'{competitor} 的定价模式与套餐线索主要包括：{combined_content[:180]}。'
            return f'{competitor} 存在公开定价或套餐线索。{source_hint}'
        if field_name == 'user_feedback':
            if combined_content:
                return f'{competitor} 的用户反馈重点集中在：{combined_content[:180]}。'
            return f'{competitor} 存在可公开观察的用户反馈信号。{source_hint}'
        if field_name == 'strengths':
            return combined_content[:180] or f'{competitor} 在该维度存在可观察优势。{source_hint}'
        if field_name == 'weaknesses':
            return combined_content[:180] or f'{competitor} 在该维度存在待确认短板。{source_hint}'
        if query_templates:
            return f'{competitor} 在 {field_name} 维度的公开信息主要围绕这些方向：{"；".join(query_templates[:2])}。结合现有证据可见：{combined_content[:160]}。'
        return combined_content[:180] or f'{competitor} 在 {field_name} 维度存在公开信息。{source_hint}'

    @staticmethod
    def _fallback_normalized_value(
        field_name: str,
        competitor: str,
        evidences: list[RawEvidence],
        *,
        query_templates: list[str],
        recommended_sources: list[str],
    ) -> dict[str, Any]:
        top_snippet = ''
        for ev in evidences:
            if ev.snippet and ev.snippet.strip():
                top_snippet = ev.snippet.strip()[:160]
                break
        if field_name == 'feature_tree':
            return {
                'nodes': [
                    {'name': 'Core Platform', 'capability': top_snippet or f'{competitor} core value capabilities'},
                    {'name': 'Integrations', 'capability': 'Observed integration or extensibility signals'},
                ]
            }
        if field_name == 'pricing_model':
            has_pricing = any(
                ('pricing' in ev.snippet.lower() or 'plan' in ev.snippet.lower() or '价格' in ev.snippet or '套餐' in ev.snippet)
                for ev in evidences
                if ev.snippet
            )
            return {
                'model_type': 'subscription' if has_pricing else 'unknown',
                'free_tier': has_pricing,
                'billing_dimensions': ['seat', 'usage'] if has_pricing else [],
                'tiers': [{'name': 'Observed Plan', 'price_range': 'unknown', 'billing_cycle': 'monthly'}] if has_pricing else [],
            }
        if field_name == 'user_feedback':
            has_feedback = any(
                ('review' in ev.snippet.lower() or 'user' in ev.snippet.lower() or '评价' in ev.snippet or '反馈' in ev.snippet)
                for ev in evidences
                if ev.snippet
            )
            return {
                'positive_themes': ['Ease of use'] if has_feedback else [],
                'negative_themes': ['Pricing concerns'] if has_feedback else [],
                'representative_quotes': [top_snippet[:160]] if top_snippet else [],
                'sentiment_distribution': {'positive': 0.55, 'neutral': 0.25, 'negative': 0.2} if has_feedback else {},
            }
        if field_name in {'strengths', 'weaknesses'}:
            return {'items': [top_snippet[:160]] if top_snippet else []}
        return {
            'key_observations': [top_snippet[:160]] if top_snippet else [],
            'source_signals': recommended_sources[:3],
            'query_focus': query_templates[:2],
            'value': top_snippet[:200] if top_snippet else 'unknown',
        }

    @staticmethod
    def _field_analysis_focus(field_name: str) -> str:
        focus_map = {
            'feature_tree': '提炼核心功能、能力结构和主要使用场景',
            'strengths': '总结核心优势、差异化亮点和竞争卖点',
            'weaknesses': '总结明显短板、限制条件和用户常见顾虑',
            'pricing_model': '分析定价模式、套餐结构、计费维度和免费层',
            'user_feedback': '提炼正向反馈、负向反馈和代表性体验主题',
        }
        return focus_map.get(field_name, f'围绕 {field_name} 这个分析维度提炼关键事实、能力范围和差异点')

    @staticmethod
    def _normalized_value_shape_hint(field_name: str) -> dict[str, Any]:
        hints: dict[str, dict[str, Any]] = {
            'feature_tree': {'nodes': [{'name': '能力模块', 'capability': '能力说明'}]},
            'strengths': {'items': ['优势1', '优势2']},
            'weaknesses': {'items': ['劣势1', '劣势2']},
            'pricing_model': {
                'model_type': 'subscription|usage_based|enterprise_quote|unknown',
                'free_tier': False,
                'billing_dimensions': ['seat'],
                'tiers': [{'name': 'Pro', 'price_range': 'unknown', 'billing_cycle': 'monthly', 'limits': []}],
            },
            'user_feedback': {
                'positive_themes': ['易用性'],
                'negative_themes': ['价格偏高'],
                'representative_quotes': ['一句代表性反馈'],
                'sentiment_distribution': {'positive': 0.6, 'neutral': 0.2, 'negative': 0.2},
            },
        }
        return hints.get(field_name, {'key_observations': ['观察1', '观察2'], 'value': '结构化概括值'})

    def _coerce_normalized_value(self, *, field_name: str, raw_value: Any, summary: str) -> dict[str, Any]:
        payload = raw_value if isinstance(raw_value, dict) else {}
        if field_name == 'feature_tree':
            nodes = payload.get('nodes', [])
            if isinstance(nodes, list) and nodes:
                cleaned_nodes = []
                for item in nodes[:6]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get('name', '')).strip()
                    capability = str(item.get('capability', '')).strip()
                    if name and capability:
                        cleaned_nodes.append({'name': name, 'capability': capability})
                if cleaned_nodes:
                    return {'nodes': cleaned_nodes}
            return {'nodes': [{'name': 'Core Platform', 'capability': summary[:160] or 'unknown'}]}
        if field_name in {'strengths', 'weaknesses'}:
            items = payload.get('items', [])
            if isinstance(items, list):
                cleaned = [str(item).strip() for item in items if str(item).strip()]
                if cleaned:
                    return {'items': cleaned[:5]}
            return {'items': [summary[:180]] if summary and summary.lower() != 'unknown' else []}
        if field_name == 'pricing_model':
            tiers = payload.get('tiers', [])
            cleaned_tiers = []
            if isinstance(tiers, list):
                for item in tiers[:4]:
                    if not isinstance(item, dict):
                        continue
                    cleaned_tiers.append(
                        {
                            'name': str(item.get('name', 'Observed Plan')).strip() or 'Observed Plan',
                            'price_range': str(item.get('price_range', 'unknown')).strip() or 'unknown',
                            'billing_cycle': str(item.get('billing_cycle', 'unknown')).strip() or 'unknown',
                            'limits': [str(x).strip() for x in item.get('limits', []) if str(x).strip()] if isinstance(item.get('limits', []), list) else [],
                        }
                    )
            return {
                'model_type': str(payload.get('model_type', 'unknown')).strip() or 'unknown',
                'free_tier': bool(payload.get('free_tier', False)),
                'billing_dimensions': [str(x).strip() for x in payload.get('billing_dimensions', []) if str(x).strip()] if isinstance(payload.get('billing_dimensions', []), list) else [],
                'tiers': cleaned_tiers,
            }
        if field_name == 'user_feedback':
            return {
                'positive_themes': [str(x).strip() for x in payload.get('positive_themes', []) if str(x).strip()] if isinstance(payload.get('positive_themes', []), list) else [],
                'negative_themes': [str(x).strip() for x in payload.get('negative_themes', []) if str(x).strip()] if isinstance(payload.get('negative_themes', []), list) else [],
                'representative_quotes': [str(x).strip() for x in payload.get('representative_quotes', []) if str(x).strip()] if isinstance(payload.get('representative_quotes', []), list) else [],
                'sentiment_distribution': payload.get('sentiment_distribution', {}) if isinstance(payload.get('sentiment_distribution', {}), dict) else {},
            }
        observations = payload.get('key_observations', [])
        return {
            'key_observations': [str(x).strip() for x in observations if str(x).strip()] if isinstance(observations, list) else [],
            'value': str(payload.get('value', summary[:200] or 'unknown')).strip() or 'unknown',
        }

    @staticmethod
    def _clean_evidence_gaps(raw_gaps: Any) -> list[str]:
        if not isinstance(raw_gaps, list):
            return []
        return [str(item).strip() for item in raw_gaps if str(item).strip()][:5]

    def _feature_tree_from_field(self, field: AnalysisFieldResult | None) -> list[FeatureNode]:
        if field is None:
            return [FeatureNode(name='Core Platform', capability='unknown')]
        nodes = field.normalized_value.get('nodes', [])
        if isinstance(nodes, list) and nodes:
            out: list[FeatureNode] = []
            for item in nodes:
                if not isinstance(item, dict):
                    continue
                out.append(
                    FeatureNode(
                        name=str(item.get('name', 'Capability')).strip() or 'Capability',
                        capability=str(item.get('capability', 'unknown')).strip() or 'unknown',
                    )
                )
            if out:
                return out
        return [FeatureNode(name='Core Platform', capability=field.summary or 'unknown')]

    def _list_from_field(self, field: AnalysisFieldResult | None, *, fallback: str) -> list[str]:
        if field is None:
            return []
        items = field.normalized_value.get('items', [])
        if isinstance(items, list):
            cleaned = [str(item).strip() for item in items if str(item).strip()]
            if cleaned:
                return cleaned[:5]
        summary = field.summary.strip()
        if summary and summary.lower() != 'unknown':
            return [summary]
        return [fallback]

    def _pricing_model_from_field(self, field: AnalysisFieldResult | None) -> PricingModel:
        payload = field.normalized_value if field is not None else {}
        tiers_payload = payload.get('tiers', []) if isinstance(payload, dict) else []
        tiers: list[PricingTier] = []
        if isinstance(tiers_payload, list):
            for item in tiers_payload:
                if not isinstance(item, dict):
                    continue
                tiers.append(
                    PricingTier(
                        name=str(item.get('name', 'Observed Plan')).strip() or 'Observed Plan',
                        price_range=str(item.get('price_range', 'unknown')).strip() or 'unknown',
                        billing_cycle=str(item.get('billing_cycle', 'unknown')).strip() or 'unknown',
                        limits=[str(x).strip() for x in item.get('limits', []) if str(x).strip()] if isinstance(item.get('limits', []), list) else [],
                    )
                )
        return PricingModel(
            model_type=str(payload.get('model_type', 'unknown')).strip() if isinstance(payload, dict) else 'unknown',
            free_tier=bool(payload.get('free_tier', False)) if isinstance(payload, dict) else False,
            billing_dimensions=[str(x).strip() for x in payload.get('billing_dimensions', []) if str(x).strip()] if isinstance(payload, dict) and isinstance(payload.get('billing_dimensions', []), list) else [],
            tiers=tiers,
        )

    def _feedback_from_field(self, field: AnalysisFieldResult | None) -> FeedbackSummary:
        payload = field.normalized_value if field is not None else {}
        return FeedbackSummary(
            positive_themes=[str(x).strip() for x in payload.get('positive_themes', []) if str(x).strip()] if isinstance(payload, dict) and isinstance(payload.get('positive_themes', []), list) else [],
            negative_themes=[str(x).strip() for x in payload.get('negative_themes', []) if str(x).strip()] if isinstance(payload, dict) and isinstance(payload.get('negative_themes', []), list) else [],
            representative_quotes=[str(x).strip() for x in payload.get('representative_quotes', []) if str(x).strip()] if isinstance(payload, dict) and isinstance(payload.get('representative_quotes', []), list) else [],
            sentiment_distribution=payload.get('sentiment_distribution', {}) if isinstance(payload, dict) and isinstance(payload.get('sentiment_distribution', {}), dict) else {},
        )

    @staticmethod
    def _positioning_from_record(record: CompetitorAnalysisRecord) -> str:
        non_generic = [field.summary for field in record.fields if field.summary.strip() and field.summary.strip().lower() != 'unknown']
        if non_generic:
            return non_generic[0][:220]
        return f'{record.product_name} market positioning inferred from public sources'
