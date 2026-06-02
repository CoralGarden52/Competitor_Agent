from __future__ import annotations

from app.core.agent_llm import AgentLLMClient
from app.core.config import AppConfig
from app.core.tools import ToolRegistry, ToolRequest, ToolResult, ToolRouter, ToolSpec


class _EchoTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(name='web.search', group='web', description='search', schema={'q': 'string'})

    def handle(self, request: ToolRequest) -> ToolResult:
        q = str(request.args.get('q', ''))
        return ToolResult(ok=True, output={'hits': [f'hit:{q}']})


def test_invoke_json_with_tools_roundtrip(monkeypatch) -> None:
    reg = ToolRegistry()
    reg.register(_EchoTool())
    router = ToolRouter(reg)
    client = AgentLLMClient(
        AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m'),
        tool_router=router,
    )

    calls = {'n': 0}

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, network_retries=None):
        calls['n'] += 1
        if calls['n'] == 1:
            return {'tool_calls': [{'name': 'web.search', 'arguments': {'q': 'notion'}}], 'final_output': None}
        history = user_payload.get('tool_history', [])
        hits = history[0]['tool_calls'][0]['output']['hits']
        return {'tool_calls': [], 'final_output': {'summary': f"done:{hits[0]}"}}

    monkeypatch.setattr(client, 'invoke_json', _fake_invoke_json)

    result = client.invoke_json_with_tools(
        trace_name='agent.test.tool_protocol',
        system_prompt='test',
        user_payload={'question': 'q'},
        metadata={'run_id': 'r1'},
        tool_names=['web.search'],
    )

    assert result['summary'] == 'done:hit:notion'
    assert calls['n'] == 2


def test_invoke_json_with_tools_role_forbidden_tool(monkeypatch) -> None:
    reg = ToolRegistry()
    reg.register(_EchoTool())
    router = ToolRouter(reg)
    client = AgentLLMClient(
        AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m'),
        tool_router=router,
    )

    calls = {'n': 0}

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, network_retries=None):
        calls['n'] += 1
        if calls['n'] == 1:
            return {'tool_calls': [{'name': 'web.fetch', 'arguments': {'url': 'https://example.com'}}], 'final_output': None}
        history = user_payload.get('tool_history', [])
        err = history[0]['tool_calls'][0]['error_code']
        return {'tool_calls': [], 'final_output': {'error_code': err}}

    monkeypatch.setattr(client, 'invoke_json', _fake_invoke_json)
    result = client.invoke_json_with_tools(
        trace_name='agent.test.tool_protocol.forbidden',
        system_prompt='test',
        user_payload={'question': 'q'},
        metadata={'run_id': 'r1', 'agent_name': 'WriterAgent', 'node_name': 'draft'},
        tool_names=['web.search'],
    )
    assert result['error_code'] == 'forbidden_tool'
