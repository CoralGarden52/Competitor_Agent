from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agents.router import RouteDecision, route_after_qa
from app.core.planner_llm import PlannerLLMClient, CORE_DYNAMIC_FIELDS
from app.core.models import QAOutput, RunState, StageName


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
        draft_handler(state)
        return qa_handler(state)

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
    ) -> dict[str, Any]:
        raw_competitors = competitors or competitor_hints or []
        base = self._dedupe_competitors(raw_competitors)
        prompt_text = (prompt or '').strip()
        if self.planner is None:
            candidate_groups = {'direct': [{'name': x, 'fit_type': 'direct', 'reason': 'provided input', 'confidence': 0.7} for x in base], 'substitute': []}
            schema = [
                {'field_name': field_name, 'query_templates': [f'{{product}} {field_name}'], 'recommended_sources': ['public_web'], 'priority': i + 1}
                for i, field_name in enumerate(CORE_DYNAMIC_FIELDS)
            ]
            inferred_industry = (industry or industry_hint or 'general').strip().lower()
        else:
            inferred_industry = self.planner.infer_industry_from_prompt(
                prompt=prompt_text or (industry or 'general'),
                industry_hint=industry_hint or industry,
            )
            candidate_groups = self.planner.discover_competitors_grouped(prompt=prompt_text or inferred_industry, competitor_hints=base)
            schema = self.planner.plan_dynamic_schema(
                prompt=prompt_text or inferred_industry,
                industry=inferred_industry,
                candidates=[item['name'] for item in candidate_groups.get('direct', []) + candidate_groups.get('substitute', [])],
            )
        direct = [str(item.get('name', '')).strip() for item in candidate_groups.get('direct', []) if str(item.get('name', '')).strip()]
        substitute = [str(item.get('name', '')).strip() for item in candidate_groups.get('substitute', []) if str(item.get('name', '')).strip()]
        planned = self._dedupe_competitors(direct + substitute)
        planner_meta = (
            self.planner.planner_meta(industry=inferred_industry, competitors=planned, schema_plan=schema)
            if self.planner is not None
            else {'llm_enabled': False, 'reason': 'planner_missing'}
        )
        if self.planner is not None:
            planner_meta['llm_call_status'] = self.planner.last_call_status()
            planner_meta['llm_call_status_by_step'] = self.planner.step_call_status()
        planner_meta['candidate_policy'] = 'direct_substitute_only'
        return {
            'planned_competitors': planned or base,
            'candidate_groups': candidate_groups,
            'analysis_schema_plan': schema,
            'inferred_industry': inferred_industry,
            'planner_meta': planner_meta,
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
    def stage_order() -> list[StageName]:
        return [StageName.plan, StageName.collect, StageName.normalize, StageName.analyze, StageName.draft, StageName.qa, StageName.finalize]
