from __future__ import annotations

from app.core.agent_llm import LLMCallError
from app.core.report_conversation import ReportContextCompactor, ReportMemoryCompactionAgent, select_report_chunks, split_report_chunks


class _FakeSummaryLLM:
    def __init__(self, result: dict | None = None, exc: Exception | None = None) -> None:
        self.result = result or {
            'mid_summary': 'LLM rolling summary: price question resolved',
            'next_work_memory': 'review price risks',
        }
        self.exc = exc
        self.calls: list[dict] = []

    def invoke_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.result


def test_memory_compaction_uses_llm_summary_for_mid_window() -> None:
    messages = [
        {'message_id': f'msg_{idx}', 'turn_id': f'turn_{idx}', 'role': 'user', 'content': f'question {idx}'}
        for idx in range(6)
    ]
    llm = _FakeSummaryLLM()

    memory = ReportMemoryCompactionAgent(llm=llm, short_window_limit=2).compact(
        run_id='run_1',
        conversation_id='conv_1',
        messages=messages,
        next_work_memory='supplement pricing section',
    )

    assert [item['message_id'] for item in memory['short_window']] == ['msg_4', 'msg_5']
    assert memory['mid_summary'] == 'LLM rolling summary: price question resolved'
    assert memory['next_work_memory'] == 'review price risks'
    assert llm.calls[0]['trace_name'] == 'report_conversation_memory_compact'
    assert llm.calls[0]['user_payload']['messages_to_summarize'][0]['content'] == 'question 0'


def test_context_compactor_archives_long_window_refs() -> None:
    messages = [
        {'message_id': f'msg_{idx}', 'turn_id': f'turn_{idx}', 'role': 'assistant', 'content': f'answer {idx}'}
        for idx in range(32)
    ]

    memory = ReportContextCompactor(short_window_limit=4).compact(messages=messages)

    assert len(memory['short_window']) == 4
    assert any(item['message_id'] == 'msg_0' for item in memory['long_archive_refs'])


def test_context_compactor_deduplicates_existing_archive_refs() -> None:
    messages = [
        {'message_id': f'msg_{idx}', 'turn_id': f'turn_{idx}', 'role': 'assistant', 'content': f'answer {idx}'}
        for idx in range(32)
    ]
    existing_memory = {
        'long_archive_refs': [{'message_id': 'msg_0', 'turn_id': 'old_turn', 'role': 'assistant'}],
        'mid_summary': 'previous summary',
    }

    memory = ReportContextCompactor(short_window_limit=4).compact(messages=messages, existing_memory=existing_memory)

    archived_ids = [item['message_id'] for item in memory['long_archive_refs']]
    assert archived_ids.count('msg_0') == 1


def test_memory_compaction_falls_back_when_llm_fails() -> None:
    messages = [
        {'message_id': f'msg_{idx}', 'turn_id': f'turn_{idx}', 'role': 'user', 'content': f'question {idx}'}
        for idx in range(6)
    ]
    llm = _FakeSummaryLLM(exc=LLMCallError(reason='disabled_for_test', message='disabled_for_test'))

    memory = ReportMemoryCompactionAgent(llm=llm, short_window_limit=2).compact(
        run_id='run_1',
        conversation_id='conv_1',
        messages=messages,
        next_work_memory='supplement pricing section',
    )

    assert [item['message_id'] for item in memory['short_window']] == ['msg_4', 'msg_5']
    assert 'question 0' in memory['mid_summary']
    assert memory['next_work_memory'] == 'supplement pricing section'
    assert memory['_compaction_fallback'] is True


def test_report_chunk_selection_matches_heading_or_content() -> None:
    chunks = split_report_chunks('# Overview\none line\n\n## Pricing\nseat based\n\n## Deployment\nprivate deployment supported')
    selected = select_report_chunks(chunks, 'how is pricing charged', limit=1)

    assert selected
    assert 'Pricing' in selected[0].heading_path
