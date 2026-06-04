from __future__ import annotations

from typing import Any

from app.core.agent_llm import AgentLLMClient
from app.core.models import ActionExecutionResult, ActionTarget, DecisionContextSnapshot, ManagerDecision
from app.core.prompts.agent_prompts import MANAGER_ACT_SYSTEM_PROMPT, MANAGER_SYSTEM_PROMPT
from harness.tools.loop import ToolLoopExecutor, ToolLoopError
from harness.tools.router import ToolRouter


class ManagerAgent:
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
            'instruction': '你已经拿到了完整 context。请只输出下一步最合适的单个动作决策，不要调用任何工具。',
        }
        result = self.llm.invoke_json(
            trace_name='agent.manager.decide_action',
            system_prompt=MANAGER_SYSTEM_PROMPT,
            user_payload=payload,
            metadata=metadata,
        )
        parsed = {
            'turn': context.turn_count,
            'action_type': result.get('action_type', 'plan_scope'),
            'target_agent': result.get('target_agent', 'OrchestratorAgent'),
            'targets': result.get('targets', {}),
            'reason': result.get('reason', ''),
            'expected_outcome': result.get('expected_outcome', ''),
            'success_criteria': result.get('success_criteria', []),
            'priority': result.get('priority', 1),
            'metadata': result.get('metadata', {}),
        }
        decision = ManagerDecision.model_validate(parsed)
        if not decision.targets:
            decision.targets = ActionTarget()
        return decision

    def fallback_decide(self, *, context: DecisionContextSnapshot) -> ManagerDecision:
        if not context.planned_competitors or not context.schema_fields:
            action_type = 'plan_scope'
            target_agent = 'OrchestratorAgent'
            reason = 'missing_competitor_scope_or_schema'
        elif context.evidence_count <= max(2, len(context.planned_competitors)):
            action_type = 'collect_initial'
            target_agent = 'CollectorAgent'
            reason = 'evidence_insufficient'
        elif context.finding_count == 0:
            action_type = 'reanalyze_targets'
            target_agent = 'AnalystAgent'
            reason = 'findings_missing'
        elif not context.report_ready:
            action_type = 'redraft_report'
            target_agent = 'WriterAgent'
            reason = 'report_missing'
        else:
            action_type = 'finalize_run'
            target_agent = 'Finalizer'
            reason = 'report_ready'
        return ManagerDecision(
            turn=context.turn_count,
            action_type=action_type,
            target_agent=target_agent,
            targets=ActionTarget(competitors=context.planned_competitors),
            reason=reason,
            expected_outcome='advance_run_state',
            success_criteria=['action completes without terminal error'],
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
