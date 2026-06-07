from __future__ import annotations

from app.agents.writer_agent import WriterAgent
from app.core.config import get_config
from app.core.models import (
    AnalysisFieldResult,
    AnalysisSchemaField,
    CompetitorAnalysisRecord,
    Evidence,
    RunState,
)


class _DummyLLM:
    config = type('Cfg', (), {'agent_llm_retry_count': 0, 'openai_model': 'test-model'})()

    def invoke_json(self, *args, **kwargs):
        raise AssertionError('invoke_json should not be called in this test')


def _build_state(long_text: str) -> tuple[RunState, list[CompetitorAnalysisRecord]]:
    records = [
        CompetitorAnalysisRecord(
            product_name='alpha',
            fields=[
                AnalysisFieldResult(field_name='feature_tree', summary=long_text, evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                AnalysisFieldResult(field_name='strengths', summary=long_text, evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                AnalysisFieldResult(field_name='weaknesses', summary=long_text, evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                AnalysisFieldResult(field_name='pricing_model', summary=long_text, evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
            ],
        )
    ]
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='strengths', priority=2),
            AnalysisSchemaField(field_name='weaknesses', priority=3),
            AnalysisSchemaField(field_name='pricing_model', priority=4),
        ],
        competitor_analyses=records,
        evidences=[Evidence(source_url='https://example.com/1', snippet='evidence', evidence_id='ev1')],
    )
    return state, records


def test_comparison_matrix_labels_direct_and_substitute_competitors() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='collaboration_software',
        competitors=['A', 'B'],
        planner_meta={
            'candidate_groups': {
                'direct': [{'name': 'A'}],
                'substitute': [{'name': 'B'}],
            }
        },
    )
    records = [
        CompetitorAnalysisRecord(product_name='A', fields=[AnalysisFieldResult(field_name='feature_tree', summary='x', evidence_refs=[], confidence=0.8, normalized_value={})]),
        CompetitorAnalysisRecord(product_name='B', fields=[AnalysisFieldResult(field_name='feature_tree', summary='y', evidence_refs=[], confidence=0.8, normalized_value={})]),
    ]
    matrix = agent._comparison_matrix(state, records)
    assert '直接竞品' in matrix[0]['product']
    assert '间接竞品' in matrix[1]['product']


def test_dynamic_field_section_uses_normalized_value_when_summary_unknown() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='meeting_software',
        competitors=['腾讯会议'],
        evidences=[],
    )
    records = [
        CompetitorAnalysisRecord(
            product_name='腾讯会议',
            fields=[
                AnalysisFieldResult(
                    field_name='feature_tree',
                    summary='unknown',
                    evidence_refs=[],
                    confidence=0.7,
                    normalized_value={'nodes': [{'name': '会议', 'capability': '音视频协作'}, {'name': '录制', 'capability': '云录制'}]},
                )
            ],
        )
    ]
    text = agent._dynamic_field_section_text(state, records, 'feature_tree')
    assert '腾讯会议' in text
    assert '核心能力包括会议：音视频协作；录制：云录制。' in text


def test_dynamic_field_section_uses_pricing_normalized_value_when_summary_unknown() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='meeting_software',
        competitors=['飞书'],
        evidences=[],
    )
    records = [
        CompetitorAnalysisRecord(
            product_name='飞书',
            fields=[
                AnalysisFieldResult(
                    field_name='pricing_model',
                    summary='unknown',
                    evidence_refs=[],
                    confidence=0.7,
                    normalized_value={
                        'model_type': 'subscription',
                        'free_tier': True,
                        'tiers': [{'name': '商业版'}, {'name': '企业版'}],
                    },
                )
            ],
        )
    ]
    text = agent._dynamic_field_section_text(state, records, 'pricing_model')
    assert '定价模式为 subscription；存在免费层；可观察到的套餐包括 商业版、企业版。' in text


def test_schema_field_labels_use_local_mapping_without_translation_call() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='meeting_software',
        competitors=['腾讯会议'],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
            AnalysisSchemaField(field_name='部署方式', priority=3),
        ],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name='腾讯会议',
                fields=[
                    AnalysisFieldResult(field_name='feature_tree', summary='能力完整', evidence_refs=[], confidence=0.7, normalized_value={}),
                    AnalysisFieldResult(field_name='部署方式', summary='支持公有云与私有化', evidence_refs=[], confidence=0.7, normalized_value={}),
                ],
            )
        ],
    )

    agent._refresh_dynamic_schema_labels(state)

    assert agent._schema_field_label('product') == '产品'
    assert agent._schema_field_label('feature_tree') == '功能体系'
    assert agent._schema_field_label('strengths') == '优势'
    assert agent._schema_field_label('weaknesses') == '劣势'
    assert agent._schema_field_label('pricing_model') == '定价模式'
    assert agent._schema_field_label('user_feedback') == '用户反馈'
    assert agent._schema_field_label('部署方式') == '部署方式'


def test_report_text_not_truncated_by_default() -> None:
    cfg = get_config()
    old_enabled = cfg.report_truncation_enabled
    old_limits_json = cfg.report_truncation_limits_json
    cfg.report_truncation_enabled = False
    cfg.report_truncation_limits_json = ''
    try:
        agent = WriterAgent(llm=_DummyLLM())
        long_text = 'A' * 240
        state, records = _build_state(long_text)

        matrix = agent._comparison_matrix(state, records)
        overview = agent._comparison_overview_text(records)
        strengths_weaknesses = agent._strengths_weaknesses_text(state, records)
        actions = agent._opportunity_bullets(records)

        assert matrix[0]['feature_tree'] == long_text
        assert '…' not in matrix[0]['feature_tree']
        assert long_text in overview
        assert '…' not in overview
        assert long_text in strengths_weaknesses
        assert '…' not in strengths_weaknesses
        assert long_text in '\n'.join(actions)
        assert '…' not in '\n'.join(actions)
    finally:
        cfg.report_truncation_enabled = old_enabled
        cfg.report_truncation_limits_json = old_limits_json


def test_report_text_truncated_when_enabled_with_custom_limits() -> None:
    cfg = get_config()
    old_enabled = cfg.report_truncation_enabled
    old_limits_json = cfg.report_truncation_limits_json
    cfg.report_truncation_enabled = True
    cfg.report_truncation_limits_json = '{"matrix_cell":20,"comparison_overview":18,"opportunity":16,"strength_weakness":14,"matrix_highlight":12}'
    try:
        agent = WriterAgent(llm=_DummyLLM())
        long_text = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        state, records = _build_state(long_text)

        matrix = agent._comparison_matrix(state, records)
        overview = agent._comparison_overview_text(records)
        strengths_weaknesses = agent._strengths_weaknesses_text(state, records)
        actions = agent._opportunity_bullets(records)
        highlights = agent._matrix_overview_bullets([{'product': 'alpha', 'feature_tree': long_text, 'pricing_model': long_text}])

        assert matrix[0]['feature_tree'].endswith('…')
        assert len(matrix[0]['feature_tree']) <= 20
        assert '…' in overview
        assert '…' in strengths_weaknesses
        assert '…' in '\n'.join(actions)
        assert '…' in '\n'.join(highlights)
    finally:
        cfg.report_truncation_enabled = old_enabled
        cfg.report_truncation_limits_json = old_limits_json
