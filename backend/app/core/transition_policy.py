from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import QAOutput, RecoveryState, RunState, StageName, TransitionReason


_DEFAULT_STAGE_FLOW: dict[StageName, StageName | None] = {
    StageName.plan: StageName.collect,
    StageName.collect: StageName.normalize,
    StageName.normalize: StageName.analyze,
    StageName.analyze: StageName.qa,
    StageName.qa: StageName.draft,
    StageName.draft: StageName.finalize,
    StageName.finalize: None,
}

_RETRYABLE_REASONS = {
    'network_error',
    'rate_limit',
    'timeout',
    'provider_error',
    'internal_error',
}


@dataclass
class TransitionDecision:
    next_stage: StageName | None
    transition_reason: TransitionReason
    recovery_state: RecoveryState
    terminal_status: str | None = None
    apply_rework_ticket: bool = False


class TransitionPolicy:
    @classmethod
    def decide(
        cls,
        *,
        run_state: RunState,
        stage: StageName,
        stage_result: Any | None,
        error: Exception | None,
    ) -> TransitionDecision:
        if run_state.turn_count >= run_state.max_turns:
            return TransitionDecision(
                next_stage=None,
                transition_reason=TransitionReason.max_turns_reached,
                recovery_state=RecoveryState.halted,
                terminal_status='failed',
            )

        if error is not None:
            if cls._is_retryable_error(error):
                return TransitionDecision(
                    next_stage=stage,
                    transition_reason=TransitionReason.retryable_error,
                    recovery_state=RecoveryState.retrying,
                )
            return TransitionDecision(
                next_stage=None,
                transition_reason=TransitionReason.terminal_error,
                recovery_state=RecoveryState.halted,
                terminal_status='failed',
            )

        if stage == StageName.qa and isinstance(stage_result, QAOutput):
            if stage_result.passed:
                return TransitionDecision(
                    next_stage=StageName.draft,
                    transition_reason=TransitionReason.qa_passed,
                    recovery_state=RecoveryState.none,
                )
            if run_state.attempt <= 1 and stage_result.target_agent == 'Collect':
                return TransitionDecision(
                    next_stage=StageName.collect,
                    transition_reason=TransitionReason.qa_rework_collect,
                    recovery_state=RecoveryState.reworking,
                    apply_rework_ticket=True,
                )
            return TransitionDecision(
                next_stage=StageName.draft,
                transition_reason=TransitionReason.qa_recollect_skipped,
                recovery_state=RecoveryState.none,
            )

        next_stage = _DEFAULT_STAGE_FLOW.get(stage)
        if stage == StageName.finalize:
            return TransitionDecision(
                next_stage=None,
                transition_reason=TransitionReason.completed,
                recovery_state=RecoveryState.none,
                terminal_status='completed',
            )
        return TransitionDecision(
            next_stage=next_stage,
            transition_reason=TransitionReason.stage_succeeded,
            recovery_state=RecoveryState.none,
        )

    @staticmethod
    def _is_retryable_error(error: Exception) -> bool:
        reason = str(getattr(error, 'reason', '') or '').strip().lower()
        if reason in _RETRYABLE_REASONS:
            return True
        if isinstance(error, (TimeoutError, ConnectionError)):
            return True
        error_name = error.__class__.__name__.lower()
        return 'timeout' in error_name or 'connection' in error_name

