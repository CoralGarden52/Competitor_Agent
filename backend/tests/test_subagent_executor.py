from __future__ import annotations

import json

from app.core.agent_llm import AgentLLMClient
from app.core.config import AppConfig
from app.core.storage import SQLiteStore
from harness.tools import ToolRegistry, ToolRequest, ToolResult, ToolRouter, ToolSpec
from harness.subagents import (
    SubagentBudget,
    SubagentExecutor,
    SubagentRequest,
    SubagentTokenTracker,
    collector_deep_dive_role,
)


class _SearchTool:
    def spec(self) -> ToolSpec:
        return ToolSpec(name='web.search', group='web', description='search')

    def handle(self, request: ToolRequest) -> ToolResult:
        query = str(request.args.get('query', '') or '')
        return ToolResult(
            ok=True,
            output={
                'hits': [
                    {
                        'query': query,
                        'title': 'official pricing',
                        'url': 'https://official.example/pricing',
                        'snippet': 'confirmed price',
                        'source_provider': 'test',
                    }
                ]
            },
        )


class _SearchThenFinishLLM:
    def __init__(self) -> None:
        self.payloads = []

    def invoke_json(self, **kwargs):
        self.payloads.append(kwargs['user_payload'])
        if len(self.payloads) == 1:
            return {'tool_calls': [{'name': 'web.search', 'arguments': {'query': 'alpha pricing'}}], 'final_output': None}
        return {
            'tool_calls': [],
            'final_output': {
                'sources': [
                    {'url': 'https://official.example/pricing'},
                    {'url': 'https://invented.example/not-visited'},
                ],
                'verification_claims': ['price confirmed'],
                'verification_conflicts': [],
                'verification_gaps': [],
            },
        }


class _TooManyToolsLLM:
    def invoke_json(self, **kwargs):
        return {
            'tool_calls': [
                {'name': 'web.search', 'arguments': {'query': 'one'}},
                {'name': 'web.search', 'arguments': {'query': 'two'}},
            ],
            'final_output': None,
        }


def _executor(tmp_path, llm):
    registry = ToolRegistry()
    tool = _SearchTool()
    registry.register(spec=tool.spec(), handler=tool)
    store = SQLiteStore(tmp_path / 'subagent.db')
    return SubagentExecutor(llm=llm, tool_router=ToolRouter(registry), store=store), store


def _request() -> SubagentRequest:
    return SubagentRequest(
        parent_run_id='run_subagent',
        attempt=1,
        industry='saas',
        competitor='alpha',
        field_name='pricing_model',
        objective='verify pricing',
    )


def test_subagent_uses_isolated_history_and_rejects_unvisited_sources(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        'harness.subagents.tracing.get_tracing_runtime',
        lambda: type('Runtime', (), {'langsmith_enabled': False, 'client': None})(),
    )
    llm = _SearchThenFinishLLM()
    executor, store = _executor(tmp_path, llm)
    result = executor.run(request=_request(), role=collector_deep_dive_role(), budget=SubagentBudget())

    assert result.status == 'completed'
    assert llm.payloads[0]['tool_history'] == []
    assert len(result.new_evidences) == 1
    assert result.new_evidences[0]['source_url'] == 'https://official.example/pricing'
    rows = store.list_subagent_runs('run_subagent')
    assert len(rows) == 1
    assert rows[0]['status'] == 'completed'
    assert rows[0]['tool_history'][0]['tool_calls'][0]['name'] == 'web.search'


def test_subagent_stops_when_tool_budget_is_exhausted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        'harness.subagents.tracing.get_tracing_runtime',
        lambda: type('Runtime', (), {'langsmith_enabled': False, 'client': None})(),
    )
    executor, store = _executor(tmp_path, _TooManyToolsLLM())
    result = executor.run(
        request=_request(),
        role=collector_deep_dive_role(),
        budget=SubagentBudget(max_rounds=3, max_tool_calls=1, max_tokens=4000, timeout_s=90),
    )

    assert result.status == 'budget_exhausted'
    assert result.usage.tool_calls == 1
    assert store.list_subagent_runs('run_subagent')[0]['status'] == 'budget_exhausted'


def test_agent_llm_applies_subagent_token_budget(monkeypatch) -> None:
    captured = {}

    class _Response:
        def read(self):
            return json.dumps(
                {
                    'choices': [{'message': {'content': '{"ok": true}'}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
                }
            ).encode('utf-8')

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _urlopen(request, timeout=0):
        captured.update(json.loads(request.data.decode('utf-8')))
        return _Response()

    monkeypatch.setattr('urllib.request.urlopen', _urlopen)
    tracker = SubagentTokenTracker(max_tokens=4000)
    client = AgentLLMClient(AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m'))
    result = client.invoke_json(
        trace_name='subagent.token.test',
        system_prompt='return json',
        user_payload={'task': 'verify'},
        metadata={'_via_tool': True},
        token_tracker=tracker,
    )

    assert result == {'ok': True}
    assert 0 < captured['max_tokens'] < 4000
    assert tracker.total_tokens == 15
