from __future__ import annotations

from app.core.models import QAOutput, RecoveryState, RunState, StageName, TransitionReason
from app.core.transition_policy import TransitionPolicy


def _state(*, attempt: int = 1, turn_count: int = 0, max_turns: int = 40) -> RunState:
    return RunState(
        industry='saas',
        competitors=['alpha'],
        attempt=attempt,
        turn_count=turn_count,
        max_turns=max_turns,
    )


def test_policy_stage_success_continues_manager_loop_on_current_stage() -> None:
    state = _state()
    decision = TransitionPolicy.decide(run_state=state, stage=StageName.plan, stage_result=None, error=None)
    assert decision.next_stage == StageName.plan
    assert decision.transition_reason == TransitionReason.stage_succeeded
    assert decision.recovery_state == RecoveryState.none
    assert decision.terminal_status is None


def test_policy_qa_pass_keeps_manager_loop_running() -> None:
    state = _state()
    qa = QAOutput(passed=True, issues=[], target_agent=None, ticket=None, collect_plan=None)
    decision = TransitionPolicy.decide(run_state=state, stage=StageName.qa, stage_result=qa, error=None)
    assert decision.next_stage == StageName.qa
    assert decision.transition_reason == TransitionReason.stage_succeeded


def test_policy_qa_fail_keeps_manager_loop_running() -> None:
    state = _state(attempt=1)
    qa = QAOutput.model_validate(
        {
            'passed': False,
            'issues': [{'code': 'x', 'message': 'm', 'stage': 'collect'}],
            'target_agent': 'Collect',
            'collect_plan': {'enabled': True, 'items': [{'competitor': 'alpha', 'field_name': 'pricing_model', 'reason': 'missing', 'query_list': ['a', 'b'], 'priority': 1}]},
        }
    )
    decision = TransitionPolicy.decide(run_state=state, stage=StageName.qa, stage_result=qa, error=None)
    assert decision.next_stage == StageName.qa
    assert decision.transition_reason == TransitionReason.stage_succeeded
    assert decision.recovery_state == RecoveryState.none
    assert decision.apply_rework_ticket is False


def test_policy_retryable_error_stays_on_stage() -> None:
    state = _state()

    class _RetryableError(Exception):
        def __init__(self):
            super().__init__('retry later')
            self.reason = 'rate_limit'

    decision = TransitionPolicy.decide(run_state=state, stage=StageName.analyze, stage_result=None, error=_RetryableError())
    assert decision.next_stage == StageName.analyze
    assert decision.transition_reason == TransitionReason.retryable_error
    assert decision.recovery_state == RecoveryState.retrying


def test_policy_terminal_error_fails() -> None:
    state = _state()
    decision = TransitionPolicy.decide(run_state=state, stage=StageName.collect, stage_result=None, error=ValueError('boom'))
    assert decision.next_stage is None
    assert decision.transition_reason == TransitionReason.terminal_error
    assert decision.terminal_status == 'failed'


def test_policy_max_turns_reached() -> None:
    state = _state(turn_count=5, max_turns=5)
    decision = TransitionPolicy.decide(run_state=state, stage=StageName.plan, stage_result=None, error=None)
    assert decision.next_stage is None
    assert decision.transition_reason == TransitionReason.max_turns_reached
    assert decision.terminal_status == 'failed'
