from __future__ import annotations

import concurrent.futures
import logging
import re
from pathlib import Path
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
        self._runtime_run_id = ''
        self._runtime_attempt = 0

    def run_llm(
        self,
        state: RunState,
        *,
        reanalyze_targets: dict[str, set[str]] | None = None,
        previous_records: list[CompetitorAnalysisRecord] | None = None,
    ) -> AnalyzeOutput:
        schema_plan = self._schema_plan(state)
        schema_map = {item.field_name: item for item in schema_plan}
        bundles = self._build_competitor_evidence_bundles(state, schema_plan)
        previous_map = {item.product_name: item for item in (previous_records or [])}
        should_reanalyze = bool(reanalyze_targets)

        tasks: list[tuple[int, int, str, FieldEvidenceBundle, AnalysisSchemaField | None]] = []
        self._runtime_run_id = state.run_id
        self._runtime_attempt = state.attempt
        for bundle_index, bundle in enumerate(bundles):
            print(f"  分析竞品: {bundle.product_name}")
            for field_index, field_bundle in enumerate(bundle.fields):
                if should_reanalyze:
                    target_fields = reanalyze_targets.get(bundle.product_name, set()) if reanalyze_targets else set()
                    if field_bundle.field_name not in target_fields:
                        continue
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
            previous_field_map = {
                item.field_name: item
                for item in (previous_map.get(bundle.product_name).fields if bundle.product_name in previous_map else [])
            }
            ordered_fields: list[AnalysisFieldResult] = []
            for field_index, field_bundle in enumerate(bundle.fields):
                key = (bundle_index, field_index)
                if key in field_results:
                    ordered_fields.append(field_results[key])
                    continue
                reused = previous_field_map.get(field_bundle.field_name)
                if reused is not None:
                    ordered_fields.append(reused)
                    continue
                ordered_fields.append(
                    self._fallback_field_result(
                        bundle.product_name,
                        field_bundle.field_name,
                        field_bundle.evidences,
                    )
                )
            records.append(CompetitorAnalysisRecord(product_name=bundle.product_name, fields=ordered_fields))

        profiles = [self._profile_from_record(state=state, record=record) for record in records]
        findings = self._build_findings_from_records(records)
        self._runtime_run_id = ''
        self._runtime_attempt = 0
        return AnalyzeOutput(competitors=records, profiles=profiles, findings=findings)
    def _analyze_single_field(
        self,
        competitor: str,
        field_name: str,
        evidences: list[RawEvidence],
        industry: str,
        schema_item: AnalysisSchemaField | None = None,
        run_id: str = '',
        attempt: int = 0,
    ) -> AnalysisFieldResult:
        """对单个字段进行分析，单独调用 LLM。"""
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
        for ev in evidences[:5]:  # 最多使用 5 条证据
            evidence_ids.append(ev.evidence_id)
            content = ev.snippet[:500] if ev.snippet else ''
            if content:
                title = ev.title.strip()[:120] if ev.title else ''
                query = ev.query.strip()[:120] if ev.query else ''
                evidence_contents.append(
                    f"证据{len(evidence_contents)+1}（来源: {ev.source_type}，标题: {title}，查询: {query}）\n{content}"
                )

        query_templates = schema_item.query_templates if schema_item is not None else []
        recommended_sources = schema_item.recommended_sources if schema_item is not None else []

        if field_name == 'pricing_model':
            pricing_result = self._analyze_pricing_model_with_chunks(
                competitor=competitor,
                evidences=evidences,
                industry=industry,
                schema_item=schema_item,
                evidence_ids=evidence_ids,
                run_id=run_id or str(getattr(self, '_runtime_run_id', '') or ''),
                attempt=attempt or int(getattr(self, '_runtime_attempt', 0) or 0),
            )
            if pricing_result is not None:
                return pricing_result

        sys_prompt = (
            f"{ANALYZE_SYSTEM_PROMPT}\n\n"
            "只分析一个 schema 字段，并且只返回严格 JSON：\n"
            '{"summary":"...","normalized_value":{},"evidence_gaps":[]}\n'
            "请根据证据进行归纳，不要复制原始文本。输出内容必须聚焦当前字段。"
            "如果证据不完整，请优先给出已确认事实，并列出剩余缺口。"
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
                f"请基于以上证据分析字段 [{field_name}]。"
                "使用 field_context 聚焦归纳内容，避免编造，并且只返回符合 JSON 结构的输出。"
            ),
        }
        
        try:
            result = self._invoke_llm_json(
                trace_name=f'agent.analyze.field.{field_name}',
                system_prompt=sys_prompt,
                user_payload=user_prompt,
                metadata={
                    'run_id': run_id,
                    'attempt': attempt,
                    'model': str(getattr(getattr(self.llm, 'config', None), 'openai_model', '')),
                    'competitor': competitor,
                    'field_name': field_name,
                    'evidence_count': len(evidences),
                    'agent_name': 'AnalystAgent',
                    'node_name': 'analyze',
                },
                tool_names=['web.search', 'web.fetch', 'web.extract'],
            )
            summary = str(result.get('summary', '')).strip()
            normalized_value = self._coerce_normalized_value(
                field_name=field_name,
                raw_value=result.get('normalized_value', {}),
                summary=summary,
            )
            evidence_gaps = self._clean_evidence_gaps(result.get('evidence_gaps', []))
            
            # 只要 summary / normalized_value / evidence_gaps 任一部分有有效信息，就保留结果
            if not self._has_meaningful_field_output(
                summary=summary,
                normalized_value=normalized_value,
                evidence_gaps=evidence_gaps,
            ):
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

    def _analyze_pricing_model_with_chunks(
        self,
        *,
        competitor: str,
        evidences: list[RawEvidence],
        industry: str,
        schema_item: AnalysisSchemaField | None,
        evidence_ids: list[str],
        run_id: str,
        attempt: int,
    ) -> AnalysisFieldResult | None:
        evidence_blocks = self._build_pricing_evidence_blocks(evidences)
        if not evidence_blocks:
            return None

        chunks = [evidence_blocks[index : index + 3] for index in range(0, min(len(evidence_blocks), 9), 3)]
        chunk_results: list[dict[str, Any]] = []

        for chunk_index, chunk in enumerate(chunks, start=1):
            try:
                result = self._invoke_llm_json(
                    trace_name='agent.analyze.field.pricing_model.chunk',
                    system_prompt=(
                        "你是企业软件定价分析助手。"
                        "请从当前证据分片中提取所有定价事实，并且只返回 JSON。"
                        "保留不完整但已确认的事实；不要编造。"
                        "{\"summary\":\"...\",\"normalized_value\":{\"model_type\":\"...\",\"free_tier\":false,"
                        "\"billing_dimensions\":[],\"tiers\":[{\"name\":\"...\",\"price_range\":\"...\","
                        "\"billing_cycle\":\"...\",\"limits\":[]}]},\"evidence_gaps\":[]}"
                    ),
                    user_payload={
                        'competitor': competitor,
                        'industry': industry,
                        'field_name': 'pricing_model',
                        'chunk_index': chunk_index,
                        'chunk_count': len(chunks),
                        'field_context': {
                            'query_templates': schema_item.query_templates if schema_item is not None else [],
                            'recommended_sources': schema_item.recommended_sources if schema_item is not None else [],
                            'analysis_focus': self._field_analysis_focus('pricing_model'),
                            'normalized_value_shape': self._normalized_value_shape_hint('pricing_model'),
                        },
                        'evidences': chunk,
                        'instruction': (
                            "提取当前分片中所有与定价相关的事实，包括不完整信息，"
                            "例如套餐名称、计费周期、席位限制、免费套餐或任何明确价格。"
                        ),
                    },
                    metadata={
                        'run_id': run_id,
                        'attempt': attempt,
                        'model': str(getattr(getattr(self.llm, 'config', None), 'openai_model', '')),
                        'competitor': competitor,
                        'field_name': 'pricing_model',
                        'chunk_index': chunk_index,
                        'chunk_size': len(chunk),
                        'evidence_count': len(evidences),
                        'agent_name': 'AnalystAgent',
                        'node_name': 'analyze',
                    },
                    tool_names=['web.search', 'web.fetch', 'web.extract'],
                )
                chunk_results.append(
                    {
                        'chunk_index': chunk_index,
                        'summary': str(result.get('summary', '')).strip(),
                        'normalized_value': result.get('normalized_value', {}),
                        'evidence_gaps': self._clean_evidence_gaps(result.get('evidence_gaps', [])),
                    }
                )
            except Exception as exc:
                logger.warning("Chunk pricing extraction failed for %s chunk %s: %s", competitor, chunk_index, exc)

        if not chunk_results:
            return None

        try:
            final_result = self._invoke_llm_json(
                trace_name='agent.analyze.field.pricing_model.reduce',
                system_prompt=(
                    "你是企业软件定价分析助手。"
                    "请将各证据分片的提取结果合并为最终的 pricing_model JSON。"
                    "保留已确认事实，避免编造。"
                    "{\"summary\":\"...\",\"normalized_value\":{\"model_type\":\"...\",\"free_tier\":false,"
                    "\"billing_dimensions\":[],\"tiers\":[{\"name\":\"...\",\"price_range\":\"...\","
                    "\"billing_cycle\":\"...\",\"limits\":[]}]},\"evidence_gaps\":[]}"
                ),
                user_payload={
                    'competitor': competitor,
                    'industry': industry,
                    'field_name': 'pricing_model',
                    'chunk_results': chunk_results,
                    'instruction': (
                        "合并各分片结果，并保留所有已确认的套餐、层级和定价事实，即使信息不完整。"
                    ),
                },
                metadata={
                    'run_id': run_id,
                    'attempt': attempt,
                    'model': str(getattr(getattr(self.llm, 'config', None), 'openai_model', '')),
                    'competitor': competitor,
                    'field_name': 'pricing_model',
                    'chunk_count': len(chunk_results),
                    'evidence_count': len(evidences),
                    'agent_name': 'AnalystAgent',
                    'node_name': 'analyze',
                },
                tool_names=['web.search', 'web.fetch', 'web.extract'],
            )
            summary = str(final_result.get('summary', '')).strip()
            normalized_value = self._coerce_normalized_value(
                field_name='pricing_model',
                raw_value=final_result.get('normalized_value', {}),
                summary=summary,
            )
            evidence_gaps = self._clean_evidence_gaps(final_result.get('evidence_gaps', []))
            if not self._has_meaningful_field_output(
                summary=summary,
                normalized_value=normalized_value,
                evidence_gaps=evidence_gaps,
            ):
                raise ValueError('empty_pricing_reduce_result')
            confidence = min(0.92, 0.58 + (0.06 * min(len(chunk_results), 3)) + (0.04 * min(len(evidences), 6)))
            return AnalysisFieldResult(
                field_name='pricing_model',
                summary=(summary or 'unknown')[:500],
                evidence_refs=evidence_ids,
                confidence=confidence,
                normalized_value=normalized_value,
                evidence_gaps=evidence_gaps,
            )
        except Exception as exc:
            logger.warning("Pricing reduce extraction failed for %s: %s", competitor, exc)
            merged_summary = '; '.join(
                one['summary'] for one in chunk_results if str(one.get('summary', '')).strip() and str(one.get('summary', '')).strip().lower() != 'unknown'
            )[:500]
            merged_gaps: list[str] = []
            for one in chunk_results:
                for gap in one.get('evidence_gaps', []):
                    gap_text = str(gap).strip()
                    if gap_text and gap_text not in merged_gaps:
                        merged_gaps.append(gap_text)
            normalized_value = self._coerce_normalized_value(
                field_name='pricing_model',
                raw_value=next((one.get('normalized_value', {}) for one in chunk_results if self._normalized_value_has_signal(one.get('normalized_value', {}))), {}),
                summary=merged_summary,
            )
            return AnalysisFieldResult(
                field_name='pricing_model',
                summary=(merged_summary or 'unknown')[:500],
                evidence_refs=evidence_ids,
                confidence=0.52,
                normalized_value=normalized_value,
                evidence_gaps=merged_gaps,
            )

    def _invoke_llm_json(
        self,
        *,
        trace_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        metadata: dict[str, Any],
        tool_names: list[str],
    ) -> dict[str, Any]:
        if hasattr(self.llm, 'invoke_json_with_tools'):
            return self.llm.invoke_json_with_tools(
                trace_name=trace_name,
                system_prompt=system_prompt,
                user_payload=user_payload,
                metadata=metadata,
                tool_names=tool_names,
            )
        return self.llm.invoke_json(
            trace_name=trace_name,
            system_prompt=system_prompt,
            user_payload=user_payload,
            metadata=metadata,
        )

    def _build_pricing_evidence_blocks(self, evidences: list[RawEvidence]) -> list[str]:
        blocks: list[str] = []
        for index, ev in enumerate(evidences[:8], start=1):
            body = self._load_pricing_evidence_text(ev)
            snippet = str(ev.snippet or '').strip()
            title = str(ev.title or '').strip()[:160]
            query = str(ev.query or '').strip()[:160]
            if not body and not snippet:
                continue
            body_excerpt = body[:2600] if body else ''
            merged = (
                f"证据{index}\n"
                f"URL: {ev.source_url}\n"
                f"来源类型: {ev.source_type}\n"
                f"标题: {title}\n"
                f"查询: {query}\n"
                f"摘要片段: {snippet[:700]}\n"
                f"正文节选:\n{body_excerpt}"
            ).strip()
            blocks.append(merged)
        return blocks

    def _load_pricing_evidence_text(self, ev: RawEvidence) -> str:
        project_root = Path(__file__).resolve().parents[3]
        parts: list[str] = []
        ext = ev.domain_extensions if isinstance(ev.domain_extensions, dict) else {}
        raw_path = str(ev.raw_content_path or '').strip()
        if raw_path:
            file_path = project_root / raw_path
            if file_path.exists():
                parts.append(file_path.read_text(encoding='utf-8', errors='ignore'))
        content_excerpt = str(ext.get('content_excerpt', '') or '').strip()
        if content_excerpt:
            parts.append(content_excerpt)
        merged = '\n\n'.join(part.strip() for part in parts if part and part.strip())
        return re.sub(r'\n{3,}', '\n\n', merged)

    @staticmethod
    def _has_meaningful_field_output(
        *,
        summary: str,
        normalized_value: dict[str, Any],
        evidence_gaps: list[str],
    ) -> bool:
        normalized_summary = str(summary or '').strip()
        if normalized_summary and normalized_summary.lower() not in {'none', 'unknown'}:
            return True
        if AnalystAgent._normalized_value_has_signal(normalized_value):
            return True
        return bool(evidence_gaps)

    @staticmethod
    def _normalized_value_has_signal(value: Any) -> bool:
        if isinstance(value, dict):
            for one in value.values():
                if AnalystAgent._normalized_value_has_signal(one):
                    return True
            return False
        if isinstance(value, list):
            return any(AnalystAgent._normalized_value_has_signal(one) for one in value)
        if isinstance(value, bool):
            return True
        if value is None:
            return False
        text = str(value).strip()
        return bool(text) and text.lower() not in {'unknown', 'none', '{}', '[]'}

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
                        statement=f'{record.product_name} in {field.field_name}: {field.summary}',
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

        all_snippets = []
        for ev in evidences:
            if ev.snippet and ev.snippet.strip():
                all_snippets.append(ev.snippet.strip())
            elif ev.title and ev.title.strip():
                all_snippets.append(ev.title.strip())

        combined_content = ' '.join(all_snippets)[:500]
        source_hint = f" Key sources: {', '.join(recommended_sources[:3])}." if recommended_sources else ''
        if field_name == 'feature_tree':
            if combined_content:
                return f'{competitor} feature/capability structure is reflected by: {combined_content[:180]}.'
            return f'{competitor} has observable core platform and integration capability signals.{source_hint}'
        if field_name == 'pricing_model':
            if combined_content:
                return f'{competitor} pricing model and tier clues include: {combined_content[:180]}.'
            return f'{competitor} has public pricing or plan-related signals.{source_hint}'
        if field_name == 'user_feedback':
            if combined_content:
                return f'{competitor} user feedback mainly mentions: {combined_content[:180]}.'
            return f'{competitor} has observable user feedback signals.{source_hint}'
        if field_name == 'strengths':
            return combined_content[:180] or f'{competitor} shows strengths on this dimension.{source_hint}'
        if field_name == 'weaknesses':
            return combined_content[:180] or f'{competitor} shows weaknesses on this dimension.{source_hint}'
        if query_templates:
            return f'{competitor} public info on {field_name} focuses on: {"; ".join(query_templates[:2])}. Observed: {combined_content[:160]}.'
        return combined_content[:180] or f'{competitor} has public information in {field_name}.{source_hint}'

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
                ('pricing' in ev.snippet.lower() or 'plan' in ev.snippet.lower() or '浠锋牸' in ev.snippet or '濂楅' in ev.snippet)
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
            'feature_tree': 'Extract core capabilities, structure, and major use cases.',
            'strengths': 'Summarize differentiation and core strengths.',
            'weaknesses': 'Summarize limits, risks, and common concerns.',
            'pricing_model': 'Analyze pricing structure, tiers, billing dimensions, and free tier.',
            'user_feedback': 'Extract positive/negative themes and representative feedback.',
        }
        return focus_map.get(field_name, f'Extract key facts and differences around {field_name}.')

    @staticmethod
    def _normalized_value_shape_hint(field_name: str) -> dict[str, Any]:
        hints: dict[str, dict[str, Any]] = {
            'feature_tree': {'nodes': [{'name': 'Capability', 'capability': 'Description'}]},
            'strengths': {'items': ['strength_1', 'strength_2']},
            'weaknesses': {'items': ['weakness_1', 'weakness_2']},
            'pricing_model': {
                'model_type': 'subscription|usage_based|enterprise_quote|unknown',
                'free_tier': False,
                'billing_dimensions': ['seat'],
                'tiers': [{'name': 'Pro', 'price_range': 'unknown', 'billing_cycle': 'monthly', 'limits': []}],
            },
            'user_feedback': {
                'positive_themes': ['ease_of_use'],
                'negative_themes': ['pricing_concern'],
                'representative_quotes': ['quote'],
                'sentiment_distribution': {'positive': 0.6, 'neutral': 0.2, 'negative': 0.2},
            },
        }
        return hints.get(field_name, {'key_observations': ['observation_1', 'observation_2'], 'value': 'structured_value'})

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
        raw_distribution = payload.get('sentiment_distribution', {}) if isinstance(payload, dict) and isinstance(payload.get('sentiment_distribution', {}), dict) else {}

        def _to_float(value: Any, default: float = 0.0) -> float:
            if value is None:
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        sentiment_distribution = {
            'positive': _to_float(raw_distribution.get('positive', 0.0)),
            'neutral': _to_float(raw_distribution.get('neutral', 0.0)),
            'negative': _to_float(raw_distribution.get('negative', 0.0)),
        }
        return FeedbackSummary(
            positive_themes=[str(x).strip() for x in payload.get('positive_themes', []) if str(x).strip()] if isinstance(payload, dict) and isinstance(payload.get('positive_themes', []), list) else [],
            negative_themes=[str(x).strip() for x in payload.get('negative_themes', []) if str(x).strip()] if isinstance(payload, dict) and isinstance(payload.get('negative_themes', []), list) else [],
            representative_quotes=[str(x).strip() for x in payload.get('representative_quotes', []) if str(x).strip()] if isinstance(payload, dict) and isinstance(payload.get('representative_quotes', []), list) else [],
            sentiment_distribution=sentiment_distribution,
        )

    @staticmethod
    def _positioning_from_record(record: CompetitorAnalysisRecord) -> str:
        non_generic = [field.summary for field in record.fields if field.summary.strip() and field.summary.strip().lower() != 'unknown']
        if non_generic:
            return non_generic[0][:220]
        return f'{record.product_name} market positioning inferred from public sources'

