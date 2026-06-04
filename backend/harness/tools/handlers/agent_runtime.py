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
        return ToolResult(
            ok=True,
            output={
                'run': {
                    'run_id': state.run_id,
                    'status': state.status,
                    'turn_count': state.turn_count,
                    'current_stage': state.current_stage.value,
                    'planned_competitors': state.planned_competitors,
                    'schema_fields': [item.field_name for item in state.analysis_schema_plan],
                    'evidence_count': len(state.evidences),
                    'finding_count': len(state.findings),
                    'report_ready': state.report is not None,
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
