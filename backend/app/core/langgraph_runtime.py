from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.core.models import QAOutput, RecoveryState, RunState, StageName, TransitionReason
from app.core.transition_policy import TransitionDecision, TransitionPolicy


class GraphExecState(TypedDict):
    run_state: RunState
    should_continue: bool


class WorkflowLangGraphRuntime:
    def __init__(self, service: Any):
        self.service = service
        self.graph = self._build_graph()
        self._stage_handlers: dict[StageName, Any] = {
            StageName.plan: self.service._plan,
            StageName.collect: self.service._collect,
            StageName.normalize: self.service._normalize,
            StageName.analyze: self.service._analyze,
            StageName.qa: self.service._qa,
            StageName.draft: self.service._draft,
            StageName.finalize: self.service._finalize,
        }
        self._stage_agents: dict[StageName, str] = {
            StageName.plan: 'OrchestratorAgent',
            StageName.collect: 'CollectorAgent',
            StageName.normalize: 'Normalizer',
            StageName.analyze: 'AnalystAgent',
            StageName.qa: 'QACriticAgent',
            StageName.draft: 'WriterAgent',
            StageName.finalize: 'Finalizer',
        }

    def _build_graph(self):
        graph = StateGraph(GraphExecState)
        graph.add_node('turn', self._node_turn)
        graph.set_entry_point('turn')
        graph.add_conditional_edges(
            'turn',
            self._route_after_turn,
            {
                'continue': 'turn',
                'end': END,
            },
        )
        return graph.compile()

    def execute(self, run_state: RunState) -> RunState:
        state: GraphExecState = {'run_state': run_state, 'should_continue': True}
        result = self.graph.invoke(state)
        return result['run_state']

    def _trace(self, node_name: str, run_state: RunState, fn):
        import time

        start_time = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] START: {node_name} (run_id={run_state.run_id[:8]})")

        trace_id = self.service.store.trace_node_started(run_id=run_state.run_id, node_name=node_name, attempt=run_state.attempt)
        self.service.store.trace_node_input(
            run_id=run_state.run_id,
            node_name=node_name,
            input_payload={
                'industry': run_state.industry,
                'competitors': run_state.competitors,
                'user_prompt': run_state.user_prompt,
                'planned_competitors': run_state.planned_competitors,
                'status': run_state.status,
                'turn_count': run_state.turn_count,
                'recovery_state': run_state.recovery_state.value if isinstance(run_state.recovery_state, RecoveryState) else str(run_state.recovery_state),
            },
        )
        try:
            result = fn(run_state)
            elapsed = time.time() - start_time
            print(f"[{time.strftime('%H:%M:%S')}] END:   {node_name} (elapsed={elapsed:.2f}s, evidences={len(run_state.evidences)}, profiles={len(run_state.profiles)})")

            self.service.store.trace_node_completed(
                trace_id=trace_id,
                run_id=run_state.run_id,
                node_name=node_name,
                output_payload={
                    'status': run_state.status,
                    'evidence_count': len(run_state.evidences),
                    'profile_count': len(run_state.profiles),
                    'finding_count': len(run_state.findings),
                    'has_report': run_state.report is not None,
                    'turn_count': run_state.turn_count,
                },
            )
            self.service.store.save_checkpoint(
                run_id=run_state.run_id,
                node_name=node_name,
                attempt=run_state.attempt,
                state=run_state,
            )
            return result
        except Exception as exc:
            elapsed = time.time() - start_time
            print(f"[{time.strftime('%H:%M:%S')}] FAIL:  {node_name} (elapsed={elapsed:.2f}s, error={str(exc)})")
            self.service.store.trace_node_failed(trace_id=trace_id, error_text=str(exc))
            raise

    def _node_turn(self, state: GraphExecState) -> GraphExecState:
        run_state = state['run_state']
        if run_state.status in ('completed', 'failed'):
            return {'run_state': run_state, 'should_continue': False}

        run_state.turn_count += 1
        active_stage = run_state.current_stage
        if run_state.turn_count > run_state.max_turns:
            run_state.status = 'failed'
            run_state.transition_reason = TransitionReason.max_turns_reached
            run_state.recovery_state = RecoveryState.halted
            run_state.last_error = {'reason': 'max_turns_reached', 'turn_count': run_state.turn_count, 'max_turns': run_state.max_turns}
            self.service._save_and_event(
                run_state,
                active_stage,
                'runtime.turn.terminated',
                {
                    'turn': run_state.turn_count,
                    'from_stage': active_stage.value,
                    'to_stage': None,
                    'transition_reason': 'max_turns_reached',
                    'recovery_state': RecoveryState.halted.value,
                    'error': run_state.last_error,
                },
            )
            return {'run_state': run_state, 'should_continue': False}

        self.service._save_and_event(
            run_state,
            active_stage,
            'runtime.turn.started',
            {
                'turn': run_state.turn_count,
                'from_stage': active_stage.value,
                'recovery_state': run_state.recovery_state.value if isinstance(run_state.recovery_state, RecoveryState) else str(run_state.recovery_state),
            },
        )

        stage_result: Any | None = None
        stage_error: Exception | None = None
        try:
            decision, stage_result = self.service._manager_act(run_state)
            active_stage = self.service._stage_for_action(decision.action_type)
            run_state.current_stage = active_stage
            self._call_optional('mark_todo_stage_started', run_state, active_stage, decision.target_agent)
            stage_result = self._trace(active_stage.value, run_state, lambda _state: stage_result)
            self._call_optional('mark_todo_stage_completed', run_state, active_stage, decision.target_agent)
            self._call_optional(
                '_emit_hook',
                'after_stage',
                {
                    'run_id': run_state.run_id,
                    'attempt': run_state.attempt,
                    'stage': active_stage.value,
                    'agent_name': decision.target_agent,
                    'payload': {'status': run_state.status, 'turn': run_state.turn_count},
                },
            )
        except Exception as exc:  # noqa: BLE001
            stage_error = exc
            self._call_optional('mark_todo_stage_blocked', run_state, active_stage, str(exc), self._stage_agents.get(active_stage, active_stage.value))
            self._call_optional(
                '_emit_hook',
                'on_error',
                {
                    'run_id': run_state.run_id,
                    'attempt': run_state.attempt,
                    'stage': active_stage.value,
                    'agent_name': self._stage_agents.get(active_stage, active_stage.value),
                    'error': {'type': exc.__class__.__name__, 'message': str(exc)},
                },
            )

        decision = TransitionPolicy.decide(
            run_state=run_state,
            stage=active_stage,
            stage_result=stage_result,
            error=stage_error,
        )
        self._apply_transition(
            run_state=run_state,
            stage=active_stage,
            stage_result=stage_result,
            stage_error=stage_error,
            decision=decision,
        )

        should_continue = run_state.status not in ('completed', 'failed')
        return {'run_state': run_state, 'should_continue': should_continue}

    def _apply_transition(
        self,
        *,
        run_state: RunState,
        stage: StageName,
        stage_result: Any | None,
        stage_error: Exception | None,
        decision: TransitionDecision,
    ) -> None:
        if decision.apply_rework_ticket and isinstance(stage_result, QAOutput):
            self.service._apply_rework_ticket(run_state, stage_result)
        if decision.transition_reason.value == 'qa_recollect_skipped':
            self.service._save_and_event(
                run_state,
                StageName.qa,
                'qa.recollect.skipped',
                {'reason': 'max_single_recollect_reached', 'attempt': run_state.attempt},
            )

        run_state.transition_reason = decision.transition_reason
        run_state.recovery_state = decision.recovery_state
        run_state.next_stage = decision.next_stage
        if decision.next_stage is not None:
            run_state.current_stage = decision.next_stage
        if decision.terminal_status is not None:
            run_state.status = decision.terminal_status  # type: ignore[assignment]
        if stage_error is not None:
            run_state.last_error = {
                'type': stage_error.__class__.__name__,
                'message': str(stage_error),
                'reason': str(getattr(stage_error, 'reason', '') or ''),
            }
        elif decision.terminal_status == 'completed':
            run_state.last_error = {}

        self.service._save_and_event(
            run_state,
            stage,
            'runtime.turn.transitioned',
            {
                'turn': run_state.turn_count,
                'from_stage': stage.value,
                'to_stage': decision.next_stage.value if decision.next_stage is not None else None,
                'transition_reason': decision.transition_reason.value,
                'recovery_state': decision.recovery_state.value,
                'error': run_state.last_error if stage_error is not None else None,
            },
        )

        if decision.terminal_status is not None:
            self.service._save_and_event(
                run_state,
                stage,
                'runtime.turn.terminated',
                {
                    'turn': run_state.turn_count,
                    'from_stage': stage.value,
                    'to_stage': decision.next_stage.value if decision.next_stage is not None else None,
                    'transition_reason': decision.transition_reason.value,
                    'recovery_state': decision.recovery_state.value,
                    'status': decision.terminal_status,
                    'error': run_state.last_error if stage_error is not None else None,
                },
            )

    @staticmethod
    def _route_after_turn(state: GraphExecState) -> str:
        return 'continue' if state.get('should_continue', False) else 'end'

    def _call_optional(self, method_name: str, *args: Any) -> None:
        callback = getattr(self.service, method_name, None)
        if callable(callback):
            callback(*args)
