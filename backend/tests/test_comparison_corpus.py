from __future__ import annotations

from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService
from app.core.planner_llm import PlannerLLMClient
from harness.tools import ToolRequest
from harness.tools.bootstrap import build_tool_runtime
from app.core.config import AppConfig


def _document(*, content_hash: str = 'abc123', date_confidence: str = 'parsed') -> dict:
    return {
        'corpus_id': f'corpus_{content_hash}',
        'source_url': f'https://example.com/{content_hash}',
        'title': '2026 meeting software comparison',
        'topic_key': 'meeting_software',
        'industry': 'collaboration',
        'keywords': ['meeting software', 'video meeting'],
        'query': 'recent meeting software comparison',
        'summary': 'Zoom and Teams are compared for enterprise meetings.',
        'content': 'Zoom and Teams are compared for enterprise meetings and collaboration.',
        'content_hash': content_hash,
        'published_at': '2026-03-01',
        'date_confidence': date_confidence,
        'source_provider': 'test',
        'llm_extract': {
            'mentioned_competitors': ['Zoom', 'Teams'],
            'comparison_dimensions': ['meeting_capacity'],
        },
    }


def test_comparison_corpus_is_persisted_and_retrievable(tmp_path) -> None:
    store = SQLiteStore(tmp_path / 'test.db')
    corpus_id = store.upsert_comparison_corpus_document(_document())
    store.link_run_comparison_corpus(run_id='run_a', corpus_id=corpus_id)

    results = store.search_comparison_corpus(
        topic_key='meeting_software',
        industry='collaboration',
        keywords=['meeting'],
    )

    assert [item['corpus_id'] for item in results] == ['corpus_abc123']
    assert results[0]['llm_extract']['mentioned_competitors'] == ['Zoom', 'Teams']


def test_comparison_corpus_evidence_excludes_out_of_range_documents() -> None:
    evidences = CompetitorWorkflowService._comparison_corpus_evidences(
        [_document(content_hash='fresh'), _document(content_hash='old', date_confidence='out_of_range')]
    )

    assert [item.domain_extensions['corpus_id'] for item in evidences] == ['corpus_fresh']
    assert evidences[0].domain_extensions['scope'] == 'cross_competitor'


def test_comparison_corpus_unknown_date_is_kept_with_lower_recency() -> None:
    evidences = CompetitorWorkflowService._comparison_corpus_evidences(
        [_document(content_hash='unknown', date_confidence='unknown')]
    )

    assert len(evidences) == 1
    assert evidences[0].recency_score == 0.45


def test_comparison_corpus_fallback_18m_is_kept_as_timely() -> None:
    evidences = CompetitorWorkflowService._comparison_corpus_evidences(
        [_document(content_hash='fallback18m', date_confidence='fallback_18m')]
    )

    assert len(evidences) == 1
    assert evidences[0].recency_score == 0.8


def test_extract_published_at_uses_18_month_fallback_window() -> None:
    published_at, confidence = PlannerLLMClient._extract_published_at('发布时间：2025-02-01')

    assert published_at == '2025-02-01'
    assert confidence == 'fallback_18m'


def test_select_comparison_corpus_documents_targets_six_with_three_timely() -> None:
    documents = [
        _document(content_hash='fresh1', date_confidence='parsed'),
        _document(content_hash='fresh2', date_confidence='parsed'),
        _document(content_hash='fresh3', date_confidence='fallback_18m'),
        _document(content_hash='unknown1', date_confidence='unknown'),
        _document(content_hash='unknown2', date_confidence='unknown'),
        _document(content_hash='old1', date_confidence='out_of_range'),
        _document(content_hash='old2', date_confidence='out_of_range'),
    ]

    selected = PlannerLLMClient._select_comparison_corpus_documents(
        documents,
        target_docs=6,
        min_timely_docs=3,
    )

    assert len(selected) == 6
    timely_count = sum(1 for item in selected if PlannerLLMClient._is_timely_comparison_document(item))
    assert timely_count >= 3
    assert sum(1 for item in selected if item['date_confidence'] == 'out_of_range') == 1


def test_corpus_search_tool_reads_persisted_documents(tmp_path) -> None:
    store = SQLiteStore(tmp_path / 'test.db')
    store.upsert_comparison_corpus_document(_document())
    runtime = build_tool_runtime(AppConfig(), store=store)

    result = runtime.router.invoke(
        ToolRequest(
            name='corpus.search',
            args={'topic_key': 'meeting_software', 'industry': 'collaboration', 'keywords': ['meeting']},
        )
    )

    assert result.ok is True
    assert result.output['documents'][0]['corpus_id'] == 'corpus_abc123'
