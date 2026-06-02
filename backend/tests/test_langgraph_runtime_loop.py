from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.langgraph_runtime import WorkflowLangGraphRuntime
from app.core.models import QAOutput, RunState, StageName


@dataclass
class _FakeStore:
    traces: list[dict[str, Any]] = field(default_factory=list)

    def trace_node_started(self, *, run_id: str, node_name: str, attempt: int) -> int:
        self.traces.append({'run_id': run_id, 'node_name': node_name, 'attempt': attempt, 'status': 'running'})
        return len(self.traces)

    def trace_node_input(self, *, run_id: str, node_name: str, input_payload: dict[str, Any]) -> None:  # noqa: ARG002
        return

    def trace_node_completed(self, *, trace_id: int, run_id: str, node_name: str, output_payload: dict[str, Any]) -> None:  # noqa: ARG002
        self.traces[trace_id - 1]['status'] = 'completed'
        self.traces[trace_id - 1]['output'] = output_payload

    def trace_node_failed(self, *, trace_id: int, error_text: str) -> None:
        self.traces[trace_id - 1]['status'] = 'failed'
        self.traces[trace_id - 1]['error'] = error_text

    def save_checkpoint(self, *, run_id: str, node_name: str, attempt: int, state: RunState) -> None:  # noqa: ARG002
        return

    def save_state(self, state: RunState) -> None:  # noqa: ARG002
        return


class _FakeService:
    def __init__(self):
        self.store = _FakeStore()
        self.calls: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.qa_count = 0

    def _save_and_event(self, state: RunState, stage: StageName, event_type: str, payload: dict[str, Any]) -> None:  # noqa: ARG002
        self.events.append({'stage': stage.value, 'event_type': event_type, 'payload': payload, 'created_at': datetime.now(UTC).isoformat()})

    def _plan(self, state: RunState) -> None:  # noqa: ARG002
        self.calls.append('plan')

    def _collect(self, state: RunState) -> None:  # noqa: ARG002
        self.calls.append('collect')

    def _normalize(self, state: RunState) -> None:  # noqa: ARG002
        self.calls.append('normalize')

    def _analyze(self, state: RunState) -> None:  # noqa: ARG002
        self.calls.append('analyze')

    def _draft(self, state: RunState) -> None:  # noqa: ARG002
        self.calls.append('draft')

    def _finalize(self, state: RunState) -> None:  # noqa: ARG002
        self.calls.append('finalize')

    def _qa(self, state: RunState) -> QAOutput:  # noqa: ARG002
        self.calls.append('qa')
        self.qa_count += 1
        if self.qa_count == 1:
            return QAOutput.model_validate(
                {
                    'passed': False,
                    'issues': [{'code': 'missing_alpha_pricing', 'message': 'need recollect', 'stage': 'collect'}],
                    'target_agent': 'Collect',
                    'collect_plan': {'enabled': True, 'items': [{'competitor': 'alpha', 'field_name': 'pricing_model', 'reason': 'missing', 'query_list': ['a', 'b'], 'priority': 1}]},
                }
            )
        return QAOutput(passed=True, issues=[], target_agent=None, ticket=None, collect_plan=None)

    def _apply_rework_ticket(self, state: RunState, result: QAOutput) -> None:  # noqa: ARG002
        state.parent_attempt = state.attempt
        state.attempt += 1
        state.ticket_id = 'ticket_1'


def test_runtime_loop_executes_single_turn_node_until_end() -> None:
    service = _FakeService()
    runtime = WorkflowLangGraphRuntime(service)
    run_state = RunState(industry='saas', competitors=['alpha'], user_prompt='x')

    result = runtime.execute(run_state)

    assert result.status == 'completed'
    assert result.turn_count == 11
    assert result.attempt == 2
    assert result.current_stage == StageName.finalize
    assert result.transition_reason.value == 'completed'
    assert result.recovery_state.value == 'none'

    # First QA fails then recollects; second QA passes.
    assert service.calls == [
        'plan',
        'collect',
        'normalize',
        'analyze',
        'qa',
        'collect',
        'normalize',
        'analyze',
        'qa',
        'draft',
        'finalize',
    ]

    event_types = [event['event_type'] for event in service.events]
    assert 'runtime.turn.started' in event_types
    assert 'runtime.turn.transitioned' in event_types
    assert 'runtime.turn.terminated' in event_types


def test_runtime_loop_stops_on_max_turns_before_stage_execution() -> None:
    service = _FakeService()
    runtime = WorkflowLangGraphRuntime(service)
    run_state = RunState(industry='saas', competitors=['alpha'], user_prompt='x', max_turns=0)

    result = runtime.execute(run_state)
    assert result.status == 'failed'
    assert result.turn_count == 1
    assert result.transition_reason.value == 'max_turns_reached'
    assert service.calls == []
