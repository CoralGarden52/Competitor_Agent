from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.core.agent_llm import LLMCallError
from app.core.deps import get_service
from app.core.models import Report, RunState
from app.core.report_conversation import ReportMemoryCompactionAgent
from app.main import create_app
from harness.tools import ToolResult


class _FakeWebRouter:
    def __init__(self, *, fail_fetch: bool = False) -> None:
        self.fail_fetch = fail_fetch
        self.calls: list[tuple[str, dict]] = []

    def invoke(self, request):
        self.calls.append((request.name, request.args))
        if request.name == 'web.search':
            return ToolResult(
                ok=True,
                provider='fake-search',
                output={
                    'hits': [
                        {
                            'title': 'Alpha official pricing',
                            'url': 'https://example.com/alpha-pricing',
                            'snippet': 'Official pricing page for Alpha.',
                            'provider': 'fake-search',
                        }
                    ],
                    'trace': [],
                },
            )
        if request.name == 'web.fetch':
            if self.fail_fetch:
                return ToolResult(ok=False, error_code='network_error', error_message='fetch failed')
            return ToolResult(ok=True, provider='fake-fetch', output={'content': '# Alpha pricing\nAlpha now publishes team pricing.'})
        if request.name == 'web.extract':
            return ToolResult(ok=True, output={'sanitized': 'Alpha official pricing says team pricing is published.', 'extract_fields': {}})
        return ToolResult(ok=False, error_code='unknown_tool', error_message=request.name)


def _disable_chat_llm(monkeypatch) -> None:
    service = get_service()

    def _raise(*args, **kwargs):
        raise LLMCallError(reason='disabled_for_test', message='disabled_for_test')

    monkeypatch.setattr(service.agent_llm, 'invoke_json', _raise)


def _wait_turn(client: TestClient, run_id: str, turn_id: str) -> dict:
    body = {}
    for _ in range(40):
        response = client.get(f'/runs/{run_id}/chat/{turn_id}')
        assert response.status_code == 200
        body = response.json()
        if body['status'] in ('completed', 'failed'):
            break
        time.sleep(0.05)
    return body


def test_chat_endpoint_404_for_missing_run() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.post('/runs/run_missing/chat', json={'message': 'hello'})

    assert response.status_code == 404


def test_chat_turn_persists_user_message_and_answer(monkeypatch) -> None:
    _disable_chat_llm(monkeypatch)
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='summary', markdown='# Report\n\n## Pricing\nSeat based pricing.'),
        status='completed',
    )
    service.store.save_state(state)

    response = client.post(f'/runs/{state.run_id}/chat', json={'message': 'How is pricing charged?', 'mode': 'answer_only'})

    assert response.status_code == 200
    turn_id = response.json()['turn_id']
    body = _wait_turn(client, state.run_id, turn_id)
    assert body['status'] == 'completed'
    assert body['assistant_answer']
    assert body['report_updated'] is False
    assert 'report.get_chunks' in body['actions_taken']

    history = client.get(f'/runs/{state.run_id}/chat').json()
    assert len(history['messages']) >= 2
    assert history['memory']['short_window']


def test_chat_turn_saves_llm_compacted_memory(monkeypatch) -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, **kwargs):  # noqa: ARG001
        if trace_name == 'report_conversation_memory_compact':
            return {
                'mid_summary': 'LLM compacted memory: user asked about pricing and received a sourced answer.',
                'next_work_memory': 'If the user asks for edits, update the pricing section conservatively.',
            }
        if trace_name == 'report_conversation_web_collect_decision':
            return {'needs_web_collect': False, 'queries': [], 'reason': 'report chunk is sufficient'}
        return {
            'intent': 'answer_only',
            'assistant_answer': 'Pricing is seat based in the current report.',
            'report_updated': False,
            'revised_markdown': '',
            'patch_summary': '',
            'actions_taken': ['report.get_chunks'],
            'source_refs': [],
            'next_work_memory': 'Remember pricing follow-up.',
        }

    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)
    service.report_conversation.compactor = ReportMemoryCompactionAgent(llm=service.agent_llm, short_window_limit=1)
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='summary', markdown='# Report\n\n## Pricing\nSeat based pricing.'),
        status='completed',
    )
    service.store.save_state(state)

    response = client.post(f'/runs/{state.run_id}/chat', json={'message': 'How is pricing charged?', 'mode': 'answer_only'})

    assert response.status_code == 200
    body = _wait_turn(client, state.run_id, response.json()['turn_id'])
    assert body['status'] == 'completed'
    assert body['memory_snapshot']['short_window']
    assert body['memory_snapshot']['mid_summary'] == 'LLM compacted memory: user asked about pricing and received a sourced answer.'
    assert body['memory_snapshot']['next_work_memory'] == 'If the user asks for edits, update the pricing section conservatively.'

    history = client.get(f'/runs/{state.run_id}/chat').json()
    assert history['memory']['mid_summary'] == 'LLM compacted memory: user asked about pricing and received a sourced answer.'


def test_chat_turn_collects_web_refs_when_context_is_insufficient(monkeypatch) -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    router = _FakeWebRouter()
    captured_payloads: list[dict] = []

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, **kwargs):  # noqa: ARG001
        if trace_name == 'report_conversation_memory_compact':
            return {'mid_summary': 'memory', 'next_work_memory': ''}
        if trace_name == 'report_conversation_web_collect_decision':
            captured_payloads.append(user_payload)
            return {'needs_web_collect': True, 'queries': ['Alpha official pricing'], 'reason': 'user asks for latest official source'}
        captured_payloads.append(user_payload)
        return {
            'intent': 'answer_only',
            'assistant_answer': 'Based on the official web page, Alpha publishes team pricing.',
            'report_updated': False,
            'revised_markdown': '',
            'patch_summary': '',
            'actions_taken': 'already searched the web',
            'source_refs': ['https://example.com/alpha-pricing'],
            'next_work_memory': 'Remember official pricing source.',
        }

    monkeypatch.setattr(service, 'tool_router', router)
    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='summary', markdown='# Report\n\n## Deployment\nCloud deployment.'),
        status='completed',
    )
    service.store.save_state(state)

    response = client.post(f'/runs/{state.run_id}/chat', json={'message': 'What is the latest official source for Alpha pricing?', 'mode': 'answer_only'})

    assert response.status_code == 200
    body = _wait_turn(client, state.run_id, response.json()['turn_id'])
    assert body['status'] == 'completed'
    assert {'web.search', 'web.fetch', 'web.extract'}.issubset(set(body['actions_taken']))
    assert 'https://example.com/alpha-pricing' in body['source_refs']
    assert router.calls[0][0] == 'web.search'
    assert [name for name, _args in router.calls] == ['web.search', 'web.fetch', 'web.extract']
    assert captured_payloads
    assert captured_payloads[-1]['web_refs'][0]['source_url'] == 'https://example.com/alpha-pricing'
    assert captured_payloads[-1]['evidence_policy']['public_facts_must_come_from_corpus_or_web_refs'] is True
    assert all(action in {'report.get_chunks', 'corpus.search', 'web.search', 'web.fetch', 'web.extract', 'report.apply_patch'} for action in body['actions_taken'])
    assert '本轮依据：' in body['assistant_answer']
    assert '- 新采集网页：1 条' in body['assistant_answer']
    assert '新采集网页：\n1. https://example.com/alpha-pricing' in body['assistant_answer']


def test_chat_turn_skips_web_when_disabled(monkeypatch) -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    router = _FakeWebRouter()

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, **kwargs):  # noqa: ARG001
        if trace_name == 'report_conversation_memory_compact':
            return {'mid_summary': 'memory', 'next_work_memory': ''}
        if trace_name == 'report_conversation_web_collect_decision':
            raise AssertionError('web decision should not call LLM when web collection is disabled')
        return {
            'intent': 'answer_only',
            'assistant_answer': 'No web collection was used.',
            'report_updated': False,
            'revised_markdown': '',
            'patch_summary': '',
            'actions_taken': [],
            'source_refs': [],
            'next_work_memory': '',
        }

    monkeypatch.setattr(service, 'tool_router', router)
    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='summary', markdown='# Report\n\n## Deployment\nCloud deployment.'),
        status='completed',
    )
    service.store.save_state(state)

    response = client.post(
        f'/runs/{state.run_id}/chat',
        json={'message': 'What is the latest official source for Alpha pricing?', 'mode': 'answer_only', 'allow_web_collect': False},
    )

    assert response.status_code == 200
    body = _wait_turn(client, state.run_id, response.json()['turn_id'])
    assert body['status'] == 'completed'
    assert not router.calls
    assert 'web.search' not in body['actions_taken']


def test_chat_turn_web_failure_does_not_fail_turn(monkeypatch) -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    router = _FakeWebRouter(fail_fetch=True)

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, **kwargs):  # noqa: ARG001
        if trace_name == 'report_conversation_memory_compact':
            return {'mid_summary': 'memory', 'next_work_memory': ''}
        if trace_name == 'report_conversation_web_collect_decision':
            return {'needs_web_collect': True, 'queries': ['Alpha pricing source'], 'reason': 'user asks for source'}
        assert user_payload['web_refs'] == []
        return {
            'intent': 'answer_only',
            'assistant_answer': 'I could not fetch external web evidence, so I answered from available context.',
            'report_updated': False,
            'revised_markdown': '',
            'patch_summary': '',
            'actions_taken': [],
            'source_refs': [],
            'next_work_memory': '',
        }

    monkeypatch.setattr(service, 'tool_router', router)
    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='summary', markdown='# Report\n\n## Deployment\nCloud deployment.'),
        status='completed',
    )
    service.store.save_state(state)

    response = client.post(f'/runs/{state.run_id}/chat', json={'message': 'Find latest source for Alpha pricing', 'mode': 'answer_only'})

    assert response.status_code == 200
    body = _wait_turn(client, state.run_id, response.json()['turn_id'])
    assert body['status'] == 'completed'
    assert [name for name, _args in router.calls] == ['web.search', 'web.fetch']
    assert 'web.search' not in body['actions_taken']
    assert 'https://example.com/alpha-pricing' not in body['source_refs']


def test_chat_turn_uses_manager_llm_to_decide_web_collect_for_more_advantages(monkeypatch) -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    router = _FakeWebRouter()
    decision_payloads: list[dict] = []
    answer_payloads: list[dict] = []

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, **kwargs):  # noqa: ARG001
        if trace_name == 'report_conversation_memory_compact':
            return {'mid_summary': '用户要求补充腾讯会议优势，已采集公开网页证据回答。', 'next_work_memory': ''}
        if trace_name == 'report_conversation_web_collect_decision':
            decision_payloads.append(user_payload)
            return {
                'needs_web_collect': True,
                'queries': ['腾讯会议 优势 官方 公开资料'],
                'reason': '用户要求补充更多优势，现有报告片段不足以覆盖新增公开事实。',
            }
        answer_payloads.append(user_payload)
        return {
            'intent': 'answer_only',
            'assistant_answer': '可以补充为：腾讯会议除现有优势外，还可从全端接入、会议协作和大型会议能力展开，但新增表述需要绑定采集到的网页证据。',
            'report_updated': False,
            'revised_markdown': '',
            'patch_summary': '',
            'actions_taken': ['report.get_chunks', 'web.search', 'web.fetch', 'web.extract'],
            'source_refs': ['https://example.com/alpha-pricing'],
            'next_work_memory': '',
        }

    monkeypatch.setattr(service, 'tool_router', router)
    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)
    state = RunState(
        industry='在线会议',
        competitors=['腾讯会议'],
        report=Report(executive_summary='summary', markdown='# Report\n\n## 腾讯会议优势\n当前报告已记录4项核心优势。'),
        status='completed',
    )
    service.store.save_state(state)

    response = client.post(f'/runs/{state.run_id}/chat', json={'message': '补充多一些腾讯会议的优势', 'mode': 'answer_only'})

    assert response.status_code == 200
    body = _wait_turn(client, state.run_id, response.json()['turn_id'])
    assert body['status'] == 'completed'
    assert [name for name, _args in router.calls] == ['web.search', 'web.fetch', 'web.extract']
    assert decision_payloads
    assert decision_payloads[0]['user_message'] == '补充多一些腾讯会议的优势'
    assert 'memory' in decision_payloads[0]
    assert decision_payloads[0]['report_chunks']
    assert answer_payloads[0]['web_refs']
    assert '可以补充为' in body['assistant_answer']


def test_answer_only_followup_does_not_patch_report_when_llm_requests_update(monkeypatch) -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, **kwargs):  # noqa: ARG001
        if trace_name == 'report_conversation_memory_compact':
            return {'mid_summary': 'memory', 'next_work_memory': ''}
        if trace_name == 'report_conversation_web_collect_decision':
            return {'needs_web_collect': False, 'queries': [], 'reason': 'report chunks are enough'}
        assert user_payload['allow_report_update'] is False
        assert user_payload['current_report_markdown'] == ''
        return {
            'intent': 'report_edit',
            'assistant_answer': 'Here are extra advantages to mention directly in chat: cross-platform access and collaboration controls.',
            'report_updated': True,
            'revised_markdown': '# Report\n\nWrong update',
            'patch_summary': 'should not apply',
            'actions_taken': ['report.get_chunks', 'report.apply_patch'],
            'source_refs': [],
            'next_work_memory': '',
        }

    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)
    original_markdown = '# Report\n\n## Advantages\nCurrent report text.'
    state = RunState(
        industry='meeting software',
        competitors=['Tencent Meeting'],
        report=Report(executive_summary='summary', markdown=original_markdown),
        status='completed',
    )
    service.store.save_state(state)

    response = client.post(f'/runs/{state.run_id}/chat', json={'message': 'Add a few more Tencent Meeting advantages', 'mode': 'answer_only', 'auto_apply': False})

    assert response.status_code == 200
    body = _wait_turn(client, state.run_id, response.json()['turn_id'])
    assert body['status'] == 'completed'
    assert body['report_updated'] is False
    assert 'report.apply_patch' not in body['actions_taken']
    assert '报告已更新' not in body['assistant_answer']
    assert '报告未自动覆盖原文' not in body['assistant_answer']
    assert 'extra advantages' in body['assistant_answer']
    persisted = client.get(f'/runs/{state.run_id}').json()['state']
    assert persisted['report']['markdown'] == original_markdown


def test_needs_web_collect_rule() -> None:
    app = create_app()
    service = get_service()

    assert service.report_conversation._needs_web_collect(
        message='请找最新官网公开证据',
        selected_chunks=[],
        corpus_refs=[],
        allow_web_collect=True,
    ) is True
    assert service.report_conversation._needs_web_collect(
        message='总结报告里的定价',
        selected_chunks=[],
        corpus_refs=[],
        allow_web_collect=False,
    ) is False
    assert service.report_conversation._needs_web_collect(
        message='总结报告里的定价',
        selected_chunks=[object()],  # type: ignore[list-item]
        corpus_refs=[{'summary': 'known'}],
        allow_web_collect=True,
    ) is False


def test_chat_report_edit_updates_markdown_and_resets_qa(monkeypatch) -> None:
    _disable_chat_llm(monkeypatch)
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='summary', markdown='# Report\n\n## Pricing\nSeat based pricing.'),
        planner_meta={'last_qa_checked': True, 'last_qa_passed': True, 'last_qa_issue_count': 0},
        status='completed',
    )
    service.store.save_state(state)

    response = client.post(
        f'/runs/{state.run_id}/chat',
        json={'message': 'Please add risk notes to the pricing section', 'mode': 'edit_report', 'auto_apply': True},
    )

    assert response.status_code == 200
    turn_id = response.json()['turn_id']
    body = _wait_turn(client, state.run_id, turn_id)
    assert body['status'] == 'completed'
    assert body['report_updated'] is True
    assert body['report_revision_id']
    assert 'report.apply_patch' in body['actions_taken']

    updated = client.get(f'/runs/{state.run_id}').json()['state']
    assert '## ' in updated['report']['markdown']
    assert updated['planner_meta']['last_qa_checked'] is False
    assert updated['planner_meta']['last_qa_passed'] is False


def test_chat_edit_without_report_completes_without_patch(monkeypatch) -> None:
    _disable_chat_llm(monkeypatch)
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(industry='saas', competitors=['alpha'], status='completed')
    service.store.save_state(state)

    response = client.post(f'/runs/{state.run_id}/chat', json={'message': 'Please supplement the report', 'mode': 'edit_report'})

    assert response.status_code == 200
    body = _wait_turn(client, state.run_id, response.json()['turn_id'])
    assert body['status'] == 'completed'
    assert body['report_updated'] is False
    assert body['assistant_answer']
