from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agents.router import RouteDecision, route_after_qa
from app.core.models import QAOutput, RunState, StageName
from app.core.planner_llm import CORE_DYNAMIC_FIELDS, PlannerLLMClient


StageHandler = Callable[[RunState], None]
QAGateHandler = Callable[[RunState], QAOutput]


@dataclass
class OrchestratorAgent:
    max_rework_iterations: int
    planner: PlannerLLMClient | None = None

    def execute_attempt(
        self,
        state: RunState,
        *,
        plan_handler: StageHandler,
        collect_handler: StageHandler,
        normalize_handler: StageHandler,
        analyze_handler: StageHandler,
        draft_handler: StageHandler,
        qa_handler: QAGateHandler,
    ) -> QAOutput:
        plan_handler(state)
        collect_handler(state)
        normalize_handler(state)
        analyze_handler(state)
        qa_result = qa_handler(state)
        if qa_result.passed:
            draft_handler(state)
        return qa_result

    def route(self, *, qa_result: QAOutput, iteration: int) -> RouteDecision:
        return route_after_qa(qa_result=qa_result, iteration=iteration, max_rework_iterations=self.max_rework_iterations)

    def generate_dynamic_plan(
        self,
        *,
        prompt: str | None = None,
        industry: str | None = None,
        competitors: list[str] | None = None,
        industry_hint: str | None = None,
        competitor_hints: list[str] | None = None,
        aspect_hints: list[str] | None = None,
        max_direct: int = 2,
        max_substitute: int = 3,
    ) -> dict[str, Any]:
        raw_competitors = competitors or competitor_hints or []
        base = self._dedupe_competitors(raw_competitors)
        prompt_text = (prompt or '').strip()
        industry_label = str(industry or industry_hint or '').strip().lower()
        product_profile: dict[str, Any] = {}
        discover_result: dict[str, Any] = {}

        if self.planner is None:
            candidate_groups = {
                'direct': [{'name': x, 'fit_type': 'direct', 'reason': 'provided input', 'confidence': 0.7} for x in base],
                'substitute': [],
            }
            schema = [
                {'field_name': field_name, 'query_templates': [f'{{product}} {field_name}'], 'recommended_sources': ['public_web'], 'priority': index + 1}
                for index, field_name in enumerate(CORE_DYNAMIC_FIELDS)
            ]
            inferred_industry = industry_label or 'general'
            product_profile = {
                'target_product': '',
                'target_product_description': inferred_industry or '目标产品',
                'intent_summary': prompt_text[:160],
                'product_category': inferred_industry or 'general',
                'primary_use_cases': [],
            }
        else:
            inferred_industry = industry_label or self.planner.infer_industry_from_prompt(prompt=prompt_text, industry_hint=industry_hint or industry)
            product_profile = self.planner.infer_product_profile(
                prompt=prompt_text,
                industry=inferred_industry,
                competitor_hints=base,
            )
            discover_result = self.planner.discover_competitors_grouped(
                prompt=prompt_text,
                industry=inferred_industry,
                competitor_hints=base,
                max_direct=max_direct,
                max_substitute=max_substitute,
            )
            candidate_groups = discover_result.get('competitors', {'direct': [], 'substitute': []})
            comparison_decision = discover_result.get('comparison_decision', {})
            if isinstance(comparison_decision, dict):
                authoritative_groups = {
                    'direct': (
                        list(comparison_decision.get('direct', []))[:max_direct]
                        if isinstance(comparison_decision.get('direct', []), list)
                        else []
                    ),
                    'substitute': (
                        list(comparison_decision.get('substitute', []))[:max_substitute]
                        if isinstance(comparison_decision.get('substitute', []), list)
                        else []
                    ),
                }
                if authoritative_groups['direct'] or authoritative_groups['substitute']:
                    candidate_groups = authoritative_groups
            discovered_profile = discover_result.get('product_profile', {})
            if isinstance(discovered_profile, dict):
                merged_profile = dict(discovered_profile)
                merged_profile.update({key: value for key, value in product_profile.items() if value})
                product_profile = merged_profile
            planned_names = [
                str(item.get('name', '')).strip()
                for item in candidate_groups.get('direct', [])
                if isinstance(item, dict) and str(item.get('name', '')).strip()
            ]
            schema_seed = self.planner._normalize_dynamic_schema(
                self.planner._core_schema_plan_only() + list(discover_result.get('comparison_schema_fields', []))
            )
            target_product = str(product_profile.get('target_product', '') or '').strip()
            if target_product and planned_names:
                schema = self.planner._normalize_dynamic_schema(
                    self.planner.plan_schema(
                        industry=inferred_industry,
                        target_product=target_product,
                        competitors=planned_names,
                    ) + schema_seed
                )
            else:
                schema = schema_seed

        direct = [str(item.get('name', '')).strip() for item in candidate_groups.get('direct', []) if str(item.get('name', '')).strip()]
        substitute = [str(item.get('name', '')).strip() for item in candidate_groups.get('substitute', []) if str(item.get('name', '')).strip()]
        planned = self._dedupe_competitors(direct + substitute)
        target_product_name = str(product_profile.get('target_product', '') or '').strip()
        if target_product_name:
            candidate_groups = {
                **candidate_groups,
                'target': {
                    'name': target_product_name,
                    'fit_type': 'target',
                    'reason': 'user_target_product',
                    'confidence': 0.95,
                },
            }

        user_aspect_fields = self._aspect_hints_to_schema_fields(aspect_hints or [])
        merged_schema = self._merge_schema_plan(schema, user_aspect_fields)
        final_refine_status = 'skipped'

        planner_meta: dict[str, Any] = {}
        if self.planner is not None:
            planner_meta['llm_call_status'] = self.planner.last_call_status()
            planner_meta['llm_call_status_by_step'] = self.planner.step_call_status()
            planner_meta['candidate_policy'] = 'direct_only_analysis'
            planner_meta['comparison_search_plan'] = discover_result.get('comparison_search_plan', {})
            planner_meta['comparison_corpus_count'] = len(discover_result.get('comparison_corpus', []))
            planner_meta['comparison_corpus_target_count'] = '6 (>=3 timely within 18 months)'
            planner_meta['comparison_corpus_saved_count'] = len(discover_result.get('comparison_corpus', []))
            planner_meta['comparison_corpus_summarized_count'] = len(discover_result.get('comparison_corpus', []))
            planner_meta['comparison_corpus_timely_count'] = len(
                [
                    item for item in discover_result.get('comparison_corpus', [])
                    if str(item.get('date_confidence', '') or '').strip() in {'parsed', 'fallback_18m'}
                ]
            )
            planner_meta['dynamic_field_target_count'] = '5-7'
            planner_meta['dynamic_field_actual_count'] = max(0, len([x for x in merged_schema if x.get('field_name') not in CORE_DYNAMIC_FIELDS]))
            planner_meta['plan_pipeline_version'] = 'plan_v2_corpus_reduce'
            planner_meta['plan_fallback_reason'] = str(discover_result.get('fallback_reason', '') or '')
        else:
            planner_meta['llm_enabled'] = False
            planner_meta['reason'] = 'planner_missing'

        return {
            'planned_competitors': direct or base,
            'candidate_groups': candidate_groups,
            'analysis_schema_plan': merged_schema,
            'inferred_industry': inferred_industry,
            'target_product': str(product_profile.get('target_product', '') or '').strip(),
            'target_product_description': str(product_profile.get('target_product_description', '') or '').strip(),
            'user_intent_summary': str(product_profile.get('intent_summary', '') or '').strip(),
            'product_profile': product_profile,
            'planner_meta': planner_meta,
            'final_refine_status': final_refine_status,
            'comparison_search_plan': discover_result.get('comparison_search_plan', {}) if self.planner is not None else {},
            'comparison_corpus': discover_result.get('comparison_corpus', []) if self.planner is not None else [],
            'comparison_decision_evidence_refs': discover_result.get('comparison_decision_evidence_refs', []) if self.planner is not None else [],
        }

    @staticmethod
    def _dedupe_competitors(items: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for item in items:
            name = item.strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            output.append(name)
        return output

    @staticmethod
    def _normalize_field_name(value: str) -> str:
        token = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]+', '_', str(value or '').strip().lower())
        token = re.sub(r'_+', '_', token).strip('_')
        return token

    def _aspect_hints_to_schema_fields(self, hints: list[str]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, hint in enumerate(hints, 1):
            normalized = self._normalize_field_name(hint)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            output.append(
                {
                    'field_name': normalized,
                    'query_templates': [f'{{product}} {hint}', f'{{product}} {hint} 对比'],
                    'recommended_sources': ['official', 'public_web', 'community'],
                    'priority': 100 + index,
                }
            )
        return output

    def _merge_schema_plan(self, base_schema: list[dict[str, Any]], extra_schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.planner is None:
            return base_schema + extra_schema
        return self.planner._normalize_dynamic_schema(list(base_schema) + list(extra_schema))

    @staticmethod
    def stage_order() -> list[StageName]:
        return [StageName.plan, StageName.confirm_plan, StageName.collect, StageName.normalize, StageName.analyze, StageName.qa, StageName.draft, StageName.finalize]
