from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import QAOutput, RecoveryState, RunState, StageName, TransitionReason


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

        if stage == StageName.finalize or run_state.status == 'completed':
            return TransitionDecision(
                next_stage=None,
                transition_reason=TransitionReason.completed,
                recovery_state=RecoveryState.none,
                terminal_status='completed',
            )
        if (
            stage == StageName.plan
            and run_state.plan_confirmation.status.value == 'awaiting_user_confirmation'
        ):
            return TransitionDecision(
                next_stage=StageName.confirm_plan,
                transition_reason=TransitionReason.stage_succeeded,
                recovery_state=RecoveryState.none,
            )
        return TransitionDecision(
            next_stage=stage,
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
