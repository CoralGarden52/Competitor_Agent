from __future__ import annotations

from typing import Any

from app.core.agent_llm import AgentLLMClient
from app.core.models import ActionExecutionResult, ActionTarget, DecisionContextSnapshot, ManagerDecision
from app.core.prompts.agent_prompts import MANAGER_ACT_SYSTEM_PROMPT, MANAGER_SYSTEM_PROMPT
from harness.tools.loop import ToolLoopExecutor, ToolLoopError
from harness.tools.protocol import parse_tool_call_turn
from harness.tools.router import ToolRouter


class ManagerAgent:
    _STATE_TOOLS = [
        'state.get_run_snapshot',
        'state.get_gap_summary',
        'state.get_report_status',
    ]
    _ACTION_TOOLS = [
        'action.plan_scope',
        'action.collect_initial',
        'action.collect_gap',
        'action.normalize_evidence',
        'action.reanalyze_targets',
        'action.redraft_report',
        'action.run_qa',
        'action.finalize_run',
    ]

    def __init__(self, llm: AgentLLMClient, tool_router: ToolRouter) -> None:
        self.llm = llm
        self.tool_router = tool_router

    def decide_and_act(
        self,
        *,
        context: DecisionContextSnapshot,
        metadata: dict[str, Any],
    ) -> tuple[ManagerDecision, ActionExecutionResult]:
        payload = {
            'context': context.model_dump(mode='json'),
            'instruction': '你已经拿到了完整 context。禁止读取 state.*；必须先执行且只执行一个 action.* 工具，然后再返回 decision 和 action_result。',
        }
        observed_action: dict[str, Any] = {}

        def _after_tool(tool_name: str, arguments: dict[str, Any], result: Any) -> None:
            if tool_name.startswith('action.'):
                observed_action['name'] = tool_name
                observed_action['arguments'] = arguments
                observed_action['output'] = result.output

        tool_names = list(self._ACTION_TOOLS)
        loop_result = ToolLoopExecutor(self.tool_router).run(
            invoke_model=self.llm.invoke_json,
            trace_name='agent.manager.decide_and_act',
            system_prompt=MANAGER_ACT_SYSTEM_PROMPT,
            user_payload=payload,
            metadata=metadata,
            tool_names=tool_names,
            max_tool_rounds=5,
            max_tool_calls=6,
            fallback_to_plain_json=False,
            after_tool=_after_tool,
            required_tool_prefixes=['action.'],
        )
        final_output = loop_result.final_output
        raw_decision = final_output.get('decision', {}) if isinstance(final_output, dict) else {}
        parsed = {
            'turn': context.turn_count,
            'action_type': raw_decision.get('action_type', self._infer_action_type_from_tool(observed_action.get('name', ''))),
            'target_agent': raw_decision.get('target_agent', 'ManagerAgent'),
            'targets': raw_decision.get('targets', {}),
            'reason': raw_decision.get('reason', ''),
            'expected_outcome': raw_decision.get('expected_outcome', ''),
            'success_criteria': raw_decision.get('success_criteria', []),
            'priority': raw_decision.get('priority', 1),
            'metadata': {
                **(raw_decision.get('metadata', {}) if isinstance(raw_decision.get('metadata', {}), dict) else {}),
                'tool_rounds': loop_result.rounds,
                'tool_calls': loop_result.tool_calls,
            },
        }
        decision = ManagerDecision.model_validate(parsed)
        if not decision.targets:
            decision.targets = ActionTarget()
        result_payload = final_output.get('action_result', {}) if isinstance(final_output, dict) else {}
        if not result_payload and observed_action.get('output'):
            result_payload = observed_action['output']
        action_result = ActionExecutionResult.model_validate(
            {
                'action_type': decision.action_type,
                'target_agent': decision.target_agent,
                'status': result_payload.get('status', 'completed'),
                'summary': result_payload.get('summary', ''),
                'changed_fields': result_payload.get('changed_fields', []),
                'artifacts': result_payload.get('artifacts', {}),
                'next_hints': result_payload.get('next_hints', []),
            }
        )
        return decision, action_result

    def decide(self, *, context: DecisionContextSnapshot, metadata: dict[str, Any]) -> ManagerDecision:
        payload = {
            'context': context.model_dump(mode='json'),
            'instruction': '你已经拿到了完整 context。可以按需调用 state.* 工具补充判断。禁止调用 action.* 工具；只输出下一步最合适的单个动作决策。',
        }
        try:
            result = ToolLoopExecutor(self.tool_router).run(
                invoke_model=self.llm.invoke_json,
                trace_name='agent.manager.decide_action',
                system_prompt=MANAGER_SYSTEM_PROMPT,
                user_payload=payload,
                metadata=metadata,
                tool_names=list(self._STATE_TOOLS),
                max_tool_rounds=3,
                max_tool_calls=4,
                fallback_to_plain_json=False,
            )
            final_output = result.final_output
            tool_rounds = result.rounds
            tool_calls = result.tool_calls
            repaired = False
        except ToolLoopError as exc:
            if exc.code != 'tool_protocol_error':
                raise
            final_output = self._repair_protocol_final_output(
                context=context,
                metadata=metadata,
                history=exc.history,
            )
            tool_rounds = max(1, exc.rounds)
            tool_calls = exc.tool_calls
            repaired = True
        parsed = {
            'turn': context.turn_count,
            'action_type': final_output.get('action_type', 'plan_scope'),
            'target_agent': final_output.get('target_agent', 'OrchestratorAgent'),
            'targets': final_output.get('targets', {}),
            'reason': final_output.get('reason', ''),
            'expected_outcome': final_output.get('expected_outcome', ''),
            'success_criteria': final_output.get('success_criteria', []),
            'priority': final_output.get('priority', 1),
            'decision_basis': final_output.get('decision_basis', []),
            'rejected_actions': final_output.get('rejected_actions', []),
            'confidence': final_output.get('confidence', 0.5),
            'metadata': {
                **(final_output.get('metadata', {}) if isinstance(final_output.get('metadata', {}), dict) else {}),
                'tool_rounds': tool_rounds,
                'tool_calls': tool_calls,
                'protocol_repaired': repaired,
            },
        }
        decision = ManagerDecision.model_validate(parsed)
        if not decision.targets:
            decision.targets = ActionTarget()
        return decision

    def _repair_protocol_final_output(
        self,
        *,
        context: DecisionContextSnapshot,
        metadata: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        repair_prompt = (
            f"{MANAGER_SYSTEM_PROMPT}\n\n"
            "你上一轮输出不符合工具协议：既没有有效 tool_calls，也没有 final_output。"
            "现在禁止再调用任何工具。"
            "请直接输出严格 JSON final_output 对象本身，且只能是一个 JSON 对象，字段必须包含："
            '{"action_type":"...","target_agent":"...","targets":{"competitors":[],"fields":[],"sections":[],"ticket_ids":[]},'
            '"reason":"...","expected_outcome":"...","success_criteria":[],"priority":1,'
            '"decision_basis":[],"rejected_actions":[],"confidence":0.0,"metadata":{}}。'
        )
        repair_payload = {
            'context': context.model_dump(mode='json'),
            'tool_history': history,
            'instruction': '不要输出 tool_calls 包装层，不要输出 final_output 包装层，只返回最终决策 JSON 对象。',
        }
        repaired = self.llm.invoke_json(
            trace_name='agent.manager.decide_action.protocol_repair',
            system_prompt=repair_prompt,
            user_payload=repair_payload,
            metadata={**metadata, '_via_tool': True, 'tool_protocol_repair': True},
        )
        turn = parse_tool_call_turn(repaired)
        if turn.final_output is not None and not turn.tool_calls:
            return turn.final_output
        if isinstance(repaired, dict) and str(repaired.get('action_type', '') or '').strip():
            return repaired
        raise ToolLoopError(
            'tool_protocol_error',
            'manager protocol repair failed to produce final_output',
            rounds=0,
            tool_calls=0,
            history=history,
        )

    def fallback_decide(self, *, context: DecisionContextSnapshot) -> ManagerDecision:
        if not context.plan_ready:
            action_type = 'plan_scope'
            target_agent = 'OrchestratorAgent'
            reason = 'missing_competitor_scope_or_schema'
        elif context.report_ready:
            if context.qa_reviewed and context.qa_passed:
                action_type = 'finalize_run'
                target_agent = 'Finalizer'
                reason = 'qa_approved_for_delivery'
            elif context.qa_reanalyze_pending:
                action_type = 'reanalyze_targets'
                target_agent = 'AnalystAgent'
                reason = 'qa_reanalyze_pending'
            elif context.qa_collect_pending and context.qa_collect_allowed:
                action_type = 'collect_gap'
                target_agent = 'CollectorAgent'
                reason = 'qa_collect_plan_pending'
            elif context.finalize_with_risk_eligible:
                action_type = 'finalize_run'
                target_agent = 'Finalizer'
                reason = 'collect_gap_blocked_finalize_with_risk'
            elif bool(context.quality_gate.get('finalize_eligible', False)):
                action_type = 'finalize_run'
                target_agent = 'Finalizer'
                reason = 'quality_gate_finalize_eligible'
            elif not context.qa_reviewed:
                action_type = 'run_qa'
                target_agent = 'QACriticAgent'
                reason = 'qa_review_needed_for_report_quality'
            elif context.qa_failure_kind == 'report_gap':
                action_type = 'redraft_report'
                target_agent = 'WriterAgent'
                reason = 'report_quality_gap_after_qa'
            else:
                action_type = 'finalize_run'
                target_agent = 'Finalizer'
                reason = 'qa_failed_without_collect_path_finalize_with_risk'
        elif context.analyze_ready:
            action_type = 'redraft_report'
            target_agent = 'WriterAgent'
            reason = 'report_artifact_missing_after_analysis_ready'
        elif context.collect_ready:
            action_type = 'reanalyze_targets'
            target_agent = 'AnalystAgent'
            reason = 'analysis_artifact_missing_after_collect_ready'
        else:
            action_type = 'collect_initial'
            target_agent = 'CollectorAgent'
            reason = 'collect_artifact_missing_after_plan_ready'
        return ManagerDecision(
            turn=context.turn_count,
            action_type=action_type,
            target_agent=target_agent,
            targets=ActionTarget(competitors=context.planned_competitors),
            reason=reason,
            expected_outcome='advance_run_state',
            success_criteria=['action completes without terminal error'],
            decision_basis=[
                'fallback_path',
                f'plan_ready={context.plan_ready}',
                f'collect_ready={context.collect_ready}',
                f'analyze_ready={context.analyze_ready}',
                f'report_ready={context.report_ready}',
                f'qa_ready={context.qa_ready}',
                f'quality_gate_finalize_eligible={bool(context.quality_gate.get("finalize_eligible", False))}',
            ],
            confidence=0.4,
            metadata={'fallback': True},
        )

    @staticmethod
    def _infer_action_type_from_tool(tool_name: str) -> str:
        mapping = {
            'action.plan_scope': 'plan_scope',
            'action.collect_initial': 'collect_initial',
            'action.collect_gap': 'collect_gap',
            'action.normalize_evidence': 'normalize_evidence',
            'action.reanalyze_targets': 'reanalyze_targets',
            'action.redraft_report': 'redraft_report',
            'action.run_qa': 'run_qa',
            'action.finalize_run': 'finalize_run',
        }
        return mapping.get(tool_name, 'plan_scope')

    def _invoke_llm_json(
        self,
        *,
        trace_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        metadata: dict[str, Any],
        tool_names: list[str],
    ) -> dict[str, Any]:
        if hasattr(self.llm, 'invoke_json_with_tools'):
            return self.llm.invoke_json_with_tools(
                trace_name=trace_name,
                system_prompt=system_prompt,
                user_payload=user_payload,
                metadata=metadata,
                tool_names=tool_names,
            )
        return self.llm.invoke_json(
            trace_name=trace_name,
            system_prompt=system_prompt,
            user_payload=user_payload,
            metadata=metadata,
        )
