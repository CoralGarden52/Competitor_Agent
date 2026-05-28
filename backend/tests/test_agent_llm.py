from __future__ import annotations

import json

from app.core.agent_llm import AgentLLMClient
from app.core.config import AppConfig


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _DummyStore:
    def __init__(self) -> None:
        self.calls: list = []

    def save_llm_call(self, trace) -> None:
        self.calls.append(trace)


def test_invoke_json_repairs_bad_json_response(monkeypatch) -> None:
    calls = []

    def _fake_urlopen(req, timeout=0):
        payload = json.loads(req.data.decode('utf-8'))
        calls.append(payload)
        if len(calls) == 1:
            return _FakeResponse(
                {
                    'choices': [
                        {
                            'message': {
                                'content': '{"summary":"ok"}{"extra":1}'
                            }
                        }
                    ]
                }
            )
        return _FakeResponse(
            {
                'choices': [
                    {
                        'message': {
                            'content': '{"summary":"ok"}'
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr('urllib.request.urlopen', _fake_urlopen)

    client = AgentLLMClient(
        AppConfig(
            openai_api_key='k',
            openai_base_url='https://example.com/v1',
            openai_model='test-model',
        )
    )

    result = client.invoke_json(
        trace_name='test.json.repair',
        system_prompt='返回 JSON',
        user_payload={'foo': 'bar'},
        metadata={},
    )

    assert result == {'summary': 'ok'}
    assert len(calls) == 2
    repair_messages = calls[1]['messages']
    assert 'JSON 修复助手' in repair_messages[0]['content']


def test_invoke_json_persists_usage_trace(monkeypatch) -> None:
    store = _DummyStore()

    def _fake_urlopen(req, timeout=0):
        return _FakeResponse(
            {
                'choices': [
                    {
                        'finish_reason': 'stop',
                        'message': {
                            'content': '{"ok": true}',
                        },
                    }
                ],
                'usage': {
                    'prompt_tokens': 12,
                    'completion_tokens': 7,
                    'total_tokens': 19,
                    'completion_tokens_details': {'reasoning_tokens': 3},
                },
            }
        )

    monkeypatch.setattr('urllib.request.urlopen', _fake_urlopen)

    client = AgentLLMClient(
        AppConfig(
            openai_api_key='k',
            openai_base_url='https://example.com/v1',
            openai_model='test-model',
        ),
        store,
    )

    result = client.invoke_json(
        trace_name='test.usage',
        system_prompt='返回 JSON',
        user_payload={'foo': 'bar'},
        metadata={'run_id': 'run_1', 'attempt': 1, 'node_name': 'analyze', 'agent_name': 'AnalystAgent'},
    )

    assert result == {'ok': True}
    assert len(store.calls) == 1
    trace = store.calls[0]
    assert trace.prompt_tokens == 12
    assert trace.completion_tokens == 7
    assert trace.total_tokens == 19
    assert trace.usage_source == 'provider'
    assert trace.usage_details['completion_tokens_details']['reasoning_tokens'] == 3
