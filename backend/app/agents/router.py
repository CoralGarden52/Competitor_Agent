from __future__ import annotations

from dataclasses import dataclass

from app.core.models import QAOutput, StageName


@dataclass(frozen=True)
class RouteDecision:
    action: str  # finalize | retry | fail
    route_back_stage: StageName | None = None
    reason: str = ''


def route_after_qa(*, qa_result: QAOutput, iteration: int, max_rework_iterations: int) -> RouteDecision:
    if qa_result.passed:
        return RouteDecision(action='finalize', reason='qa_passed')

    if iteration > max_rework_iterations:
        return RouteDecision(action='fail', reason='max_iterations_reached')

    stage_map = {
        'Collect': StageName.collect,
        'Analyze': StageName.analyze,
        'Draft': StageName.draft,
    }
    route_stage = stage_map.get(qa_result.target_agent or 'Draft', StageName.draft)
    return RouteDecision(action='retry', route_back_stage=route_stage, reason='qa_failed_retry')
