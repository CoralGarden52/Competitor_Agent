from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harness.tools.types import ToolRequest, ToolResult


ServiceGetter = Callable[[], Any]


class StateSnapshotHandler:
    def __init__(self, get_service: ServiceGetter) -> None:
        self.get_service = get_service

    def handle(self, request: ToolRequest) -> ToolResult:
        service = self.get_service()
        state = service.store.get_run_state(str(request.args.get('run_id', '') or request.metadata.get('run_id', '') or ''))
        if state is None:
            return ToolResult(ok=True, output={'run': {}})
        coverage = service._calc_analyze_coverage(state)
        planned_competitors = state.planned_competitors or state.competitors
        schema_fields = [item.field_name for item in state.analysis_schema_plan]
        record_map = {record.product_name: record for record in state.competitor_analyses}
        missing_competitors = [competitor for competitor in planned_competitors if competitor not in record_map]
        missing_schema_fields: list[str] = []
        for field_name in schema_fields:
            if any(field_name not in {field.field_name for field in (record_map.get(competitor).fields if record_map.get(competitor) else [])} for competitor in planned_competitors):
                missing_schema_fields.append(field_name)
        latest_ticket = state.tickets[-1] if state.tickets else None
        qa_collect_round_used = bool(state.planner_meta.get('qa_collect_round_used', False))
        qa_collect_plan = state.planner_meta.get('qa_collect_plan') if isinstance(state.planner_meta, dict) else None
        qa_collect_items = qa_collect_plan.get('items', []) if isinstance(qa_collect_plan, dict) else []
        qa_collect_pending = (
            isinstance(qa_collect_plan, dict)
            and bool(qa_collect_plan.get('enabled', False))
            and isinstance(qa_collect_items, list)
            and bool(qa_collect_items)
            and not qa_collect_round_used
        )
        qa_collect_allowed = bool(
            (
                latest_ticket is not None
                and latest_ticket.target_agent == 'Collect'
            )
            or qa_collect_pending
        ) and not qa_collect_round_used
        coverage_value = float(coverage.get('coverage', 0.0) or 0.0) if isinstance(coverage, dict) else 0.0
        gap_count = len(service._build_decision_context(state).gap_summary)
        report_ready = bool(state.report is not None and bool(str(state.report.markdown).strip()) if state.report else False)
        analyze_ready = bool(state.competitor_analyses) and bool(state.findings)
        qa_delivery_approved = report_ready and analyze_ready and bool(state.planner_meta.get('last_qa_checked', False)) and bool(state.planner_meta.get('last_qa_passed', False))
        static_quality_approved = report_ready and analyze_ready and coverage_value >= 0.8 and gap_count == 0
        quality_gate = {
            'coverage_ok': coverage_value >= 0.8,
            'coverage_threshold': 0.8,
            'coverage': coverage_value,
            'critical_gaps_count': gap_count,
            'qa_delivery_approved': qa_delivery_approved,
            'static_quality_approved': static_quality_approved,
            'finalize_eligible': qa_delivery_approved or static_quality_approved,
        }
        qa_recommendation = 'collect_gap' if qa_collect_pending else ('finalize_run' if quality_gate['finalize_eligible'] else 'run_qa')
        if bool(state.planner_meta.get('qa_reanalyze_targets', {})):
            qa_recommendation = 'reanalyze_targets'
        if bool(state.planner_meta.get('last_qa_checked', False)) and not bool(state.planner_meta.get('last_qa_passed', False)) and not qa_collect_pending:
            qa_recommendation = 'collect_gap'
        return ToolResult(
            ok=True,
            output={
                'run': {
                    'run_id': state.run_id,
                    'status': state.status,
                    'turn_count': state.turn_count,
                    'current_stage': state.current_stage.value,
                    'planned_competitors': planned_competitors,
                    'schema_fields': schema_fields,
                    'plan_ready': bool(planned_competitors) and bool(schema_fields),
                    'evidence_count': len(state.evidences),
                    'collect_ready': bool(state.evidences),
                    'competitor_analysis_count': len(state.competitor_analyses),
                    'finding_count': len(state.findings),
                    'analyze_ready': bool(state.competitor_analyses) and bool(state.findings),
                    'report_ready': state.report is not None and bool(str(state.report.markdown).strip()) if state.report else False,
                    'report_section_count': len(state.report.sections) if state.report else 0,
                    'missing_competitors': missing_competitors,
                    'missing_schema_fields': missing_schema_fields,
                    'qa_collect_allowed': qa_collect_allowed,
                    'qa_reviewed': bool(state.planner_meta.get('last_qa_checked', False)),
                    'qa_passed': bool(state.planner_meta.get('last_qa_passed', False)),
                    'qa_issue_count': int(state.planner_meta.get('last_qa_issue_count', 0) or 0),
                    'qa_collect_pending': qa_collect_pending,
                    'qa_collect_item_count': len(qa_collect_items) if isinstance(qa_collect_items, list) else 0,
                    'qa_recommendation': qa_recommendation,
                    'quality_gate': quality_gate,
                    'coverage': coverage,
                }
            },
        )


class CoverageSummaryHandler:
    def __init__(self, get_service: ServiceGetter) -> None:
        self.get_service = get_service

    def handle(self, request: ToolRequest) -> ToolResult:
        service = self.get_service()
        state = service.store.get_run_state(str(request.args.get('run_id', '') or request.metadata.get('run_id', '') or ''))
        if state is None:
            return ToolResult(ok=True, output={'coverage': {}})
        coverage = service._calc_analyze_coverage(state)
        return ToolResult(ok=True, output={'coverage': coverage})


class GapSummaryHandler:
    def __init__(self, get_service: ServiceGetter) -> None:
        self.get_service = get_service

    def handle(self, request: ToolRequest) -> ToolResult:
        service = self.get_service()
        state = service.store.get_run_state(str(request.args.get('run_id', '') or request.metadata.get('run_id', '') or ''))
        if state is None:
            return ToolResult(ok=True, output={'gaps': []})
        gaps = []
        for record in state.competitor_analyses:
            for field in record.fields:
                if field.evidence_gaps:
                    gaps.append({'competitor': record.product_name, 'field_name': field.field_name, 'gaps': field.evidence_gaps})
        return ToolResult(ok=True, output={'gaps': gaps[:20]})


class ReportStatusHandler:
    def __init__(self, get_service: ServiceGetter) -> None:
        self.get_service = get_service

    def handle(self, request: ToolRequest) -> ToolResult:
        service = self.get_service()
        state = service.store.get_run_state(str(request.args.get('run_id', '') or request.metadata.get('run_id', '') or ''))
        if state is None:
            return ToolResult(ok=True, output={'report': {'ready': False}})
        markdown = state.report.markdown if state.report else ''
        return ToolResult(
            ok=True,
            output={'report': {'ready': state.report is not None, 'has_markdown': bool(str(markdown).strip()), 'source_count': len(state.report.appendix_sources) if state.report else 0}},
        )


class WorkflowActionHandler:
    def __init__(self, get_service: ServiceGetter, action_name: str) -> None:
        self.get_service = get_service
        self.action_name = action_name

    def handle(self, request: ToolRequest) -> ToolResult:
        service = self.get_service()
        result = service._run_action_tool(self.action_name, request.args, request.metadata)
        return ToolResult(ok=True, output=result)
