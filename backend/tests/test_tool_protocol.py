from __future__ import annotations

import pytest

from harness.tools.protocol import parse_tool_call_turn
from harness.tools.loop import ToolLoopError, ToolLoopExecutor
from harness.tools import ToolRegistry, ToolRequest, ToolResult, ToolRouter, ToolSpec


def test_parse_tool_call_turn() -> None:
    payload = {
        'tool_calls': [{'name': 'web.search', 'arguments': {'q': 'x'}}],
        'final_output': None,
    }
    turn = parse_tool_call_turn(payload)
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == 'web.search'
    assert turn.tool_calls[0].arguments['q'] == 'x'
    assert turn.final_output is None


class _ActionTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(name='action.plan_scope', group='action', description='plan')

    def handle(self, request: ToolRequest) -> ToolResult:
        return ToolResult(ok=True, output={'status': 'completed'})


def test_tool_loop_requires_action_tool_prefix() -> None:
    registry = ToolRegistry()
    registry.register(spec=_ActionTool().spec(), handler=_ActionTool())
    router = ToolRouter(registry)

    def _fake_model(**kwargs):
        return {'tool_calls': [], 'final_output': {'decision': {'action_type': 'plan_scope'}}}

    with pytest.raises(ToolLoopError) as exc_info:
        ToolLoopExecutor(router).run(
            invoke_model=_fake_model,
            trace_name='manager.test.required_action',
            system_prompt='test',
            user_payload={'task': 'plan'},
            metadata={'agent_name': 'ManagerAgent'},
            tool_names=['action.plan_scope'],
            fallback_to_plain_json=False,
            required_tool_prefixes=['action.'],
        )

    assert exc_info.value.code == 'required_tool_not_called'
