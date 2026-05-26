from __future__ import annotations

from app.agents.writer_agent import WriterAgent
from app.core.models import (
    AnalysisFieldResult,
    AnalysisSchemaField,
    CompetitorAnalysisRecord,
    Evidence,
    Report,
    ReportClaim,
    ReportSection,
    RunState,
)


class _DummyLLM:
    config = type('Cfg', (), {'agent_llm_retry_count': 0, 'openai_model': 'test-model'})()

    def invoke_json(self, *args, **kwargs):
        raise AssertionError('invoke_json should not be called in this test')


class _QueuedLLM:
    config = type('Cfg', (), {'agent_llm_retry_count': 0, 'openai_model': 'test-model'})()

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def invoke_json(self, *args, **kwargs):
        if not self._responses:
            raise AssertionError('No queued LLM response left')
        self.calls.append(kwargs)
        return self._responses.pop(0)


def test_template_sections_include_dynamic_schema_fields() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='meeting_software',
        competitors=['腾讯会议'],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='compliance_certifications', priority=2),
            AnalysisSchemaField(field_name='ai_meeting_native_features', priority=3),
        ],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name='腾讯会议',
                fields=[
                    AnalysisFieldResult(field_name='feature_tree', summary='支持音视频会议', evidence_refs=['ev1'], confidence=0.8, normalized_value={'nodes': [{'name': '会议', 'capability': '音视频会议'}]}),
                    AnalysisFieldResult(field_name='compliance_certifications', summary='支持等保与合规要求', evidence_refs=['ev2'], confidence=0.75, normalized_value={'key_observations': ['等保', '数据驻留'], 'value': '合规能力较完整'}),
                ],
            )
        ],
        evidences=[
            Evidence(source_url='https://example.com/1', snippet='feature evidence', evidence_id='ev1'),
            Evidence(source_url='https://example.com/2', snippet='compliance evidence', evidence_id='ev2'),
        ],
    )
    sections = agent._template_sections(state, state.competitor_analyses)
    dynamic_ids = [section.section_id for section in sections if section.section_id.startswith('dynamic_')]
    assert 'dynamic_compliance_certifications' in dynamic_ids


def test_ensure_report_consistency_normalizes_invalid_claim_refs() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='meeting_software',
        competitors=['腾讯会议'],
        analysis_schema_plan=[AnalysisSchemaField(field_name='feature_tree', priority=1)],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name='腾讯会议',
                fields=[AnalysisFieldResult(field_name='feature_tree', summary='支持音视频会议', evidence_refs=['ev1'], confidence=0.8, normalized_value={})],
            )
        ],
        evidences=[Evidence(source_url='https://example.com/1', snippet='feature evidence', evidence_id='ev1')],
    )
    drafted = type('Drafted', (), {})()
    drafted.report = Report(
        executive_summary='',
        comparison_matrix=[],
        sections=[
            ReportSection(
                section_id='capability_comparison',
                title='四、核心能力与产品形态',
                field_name='feature_tree',
                claims=[ReportClaim(statement='腾讯会议: 支持音视频会议', evidence_refs=['bad_ref'], confidence=0.8)],
                content_markdown='',
            )
        ],
        opportunities=[],
        appendix_sources=[],
        markdown='',
        html='',
    )
    normalized = agent._ensure_report_consistency(drafted, state=state)
    section = next(item for item in normalized.report.sections if item.section_id == 'capability_comparison')
    assert section.claims
    assert section.claims[0].evidence_refs == ['ev1']


def test_markdown_does_not_duplicate_action_section() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='meeting_software',
        competitors=['腾讯会议'],
        analysis_schema_plan=[AnalysisSchemaField(field_name='feature_tree', priority=1)],
    )
    report = Report(
        executive_summary='摘要',
        comparison_matrix=[],
        sections=[
            ReportSection(
                section_id='action_recommendations',
                title='八、建议动作',
                field_name='',
                content_markdown='- 建议一',
            )
        ],
        opportunities=['建议一', '建议二'],
        appendix_sources=['https://example.com'],
    )
    markdown = agent._markdown_from_template(state, report)
    assert '## 八、建议动作' in markdown
    assert '## 建议行动' not in markdown


def test_run_llm_synthesizes_overview_sections_in_second_pass() -> None:
    llm = _QueuedLLM(
        [
            {
                'report': {
                    'executive_summary': '',
                    'comparison_matrix': [],
                    'sections': [
                        {
                            'section_id': 'comparison_overview',
                            'title': '三、竞品对比总览',
                            'field_name': '',
                            'claims': [],
                            'content_markdown': '- 腾讯会议偏会议协同\n- 飞书偏一体化协同',
                        }
                    ],
                    'appendix_sources': [],
                    'opportunities': [],
                    'markdown': '',
                    'html': '',
                }
            },
            {
                'background_goal': '本次研究聚焦协作办公软件，重点比较产品能力、商业化与用户采用信号。',
                'conclusion_advice': '当前市场呈现会议协同与综合协同两类路径，我方应优先聚焦目标场景并补强关键短板。',
                'executive_summary': '协作办公赛道的主要分化点在产品形态与商业化深度。我方应围绕目标场景做差异化取舍。',
            },
        ]
    )
    agent = WriterAgent(llm=llm)
    state = RunState(
        industry='meeting_software',
        competitors=['腾讯会议', '飞书'],
        user_prompt='帮我做一个协作办公软件的竞品分析',
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
        ],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name='腾讯会议',
                fields=[
                    AnalysisFieldResult(field_name='feature_tree', summary='聚焦视频会议', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='pricing_model', summary='按席位收费', evidence_refs=['ev1'], confidence=0.75, normalized_value={}),
                ],
            ),
            CompetitorAnalysisRecord(
                product_name='飞书',
                fields=[
                    AnalysisFieldResult(field_name='feature_tree', summary='综合协同办公', evidence_refs=['ev2'], confidence=0.82, normalized_value={}),
                    AnalysisFieldResult(field_name='pricing_model', summary='分层订阅', evidence_refs=['ev2'], confidence=0.78, normalized_value={}),
                ],
            ),
        ],
        evidences=[
            Evidence(source_url='https://example.com/1', snippet='meeting', evidence_id='ev1'),
            Evidence(source_url='https://example.com/2', snippet='suite', evidence_id='ev2'),
        ],
    )
    drafted = agent.run_llm(state)
    section_ids = [section.section_id for section in drafted.report.sections]
    assert section_ids[:2] == ['background_goal', 'conclusion_advice']
    assert drafted.report.executive_summary.startswith('协作办公赛道的主要分化点')
    assert '## 建议行动' not in drafted.report.markdown
    overview_payload = llm.calls[1]['user_payload']
    assert 'comparison_matrix' in overview_payload
    assert 'sections' not in overview_payload
    assert '竞品对比矩阵' in overview_payload.get('task', '')


def test_run_fallback_still_generates_non_meta_overview_sections() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='meeting_software',
        competitors=['腾讯会议', '飞书'],
        user_prompt='帮我做一个协作办公软件的竞品分析',
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
        ],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name='腾讯会议',
                fields=[
                    AnalysisFieldResult(field_name='feature_tree', summary='聚焦视频会议', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='pricing_model', summary='按席位收费', evidence_refs=['ev1'], confidence=0.75, normalized_value={}),
                ],
            ),
            CompetitorAnalysisRecord(
                product_name='飞书',
                fields=[
                    AnalysisFieldResult(field_name='feature_tree', summary='综合协同办公', evidence_refs=['ev2'], confidence=0.82, normalized_value={}),
                    AnalysisFieldResult(field_name='pricing_model', summary='分层订阅', evidence_refs=['ev2'], confidence=0.78, normalized_value={}),
                ],
            ),
        ],
        evidences=[
            Evidence(source_url='https://example.com/1', snippet='meeting', evidence_id='ev1'),
            Evidence(source_url='https://example.com/2', snippet='suite', evidence_id='ev2'),
        ],
    )
    drafted = agent.run_fallback(state)
    assert '本报告围绕 2 个竞品' not in drafted.report.markdown
    assert '## 建议行动' not in drafted.report.markdown
    assert drafted.report.sections[0].section_id == 'background_goal'
    assert drafted.report.sections[1].section_id == 'conclusion_advice'


def test_dynamic_field_section_contains_provenance_links() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='meeting_software',
        competitors=['腾讯会议'],
        evidences=[
            Evidence(
                source_url='https://example.com/product',
                title='腾讯会议产品页',
                snippet='支持视频会议',
                evidence_id='ev1',
            )
        ],
    )
    records = [
        CompetitorAnalysisRecord(
            product_name='腾讯会议',
            fields=[
                AnalysisFieldResult(
                    field_name='feature_tree',
                    summary='支持视频会议与协作',
                    evidence_refs=['ev1'],
                    confidence=0.8,
                    normalized_value={},
                )
            ],
        )
    ]
    text = agent._dynamic_field_section_text(state, records, 'feature_tree')
    assert '溯源' in text
    assert '[腾讯会议产品页](https://example.com/product)' in text


def test_markdownish_to_html_renders_inline_links() -> None:
    html = WriterAgent._markdownish_to_html('- 溯源：[腾讯会议产品页](https://example.com/product)')
    assert '<a href="https://example.com/product"' in html
    assert '腾讯会议产品页</a>' in html


def test_comparison_matrix_labels_direct_and_substitute_competitors() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='collaboration_software',
        competitors=['钉钉', '石墨文档'],
        planner_meta={
            'candidate_groups': {
                'direct': [{'name': '钉钉'}],
                'substitute': [{'name': '石墨文档'}],
            }
        },
    )
    records = [
        CompetitorAnalysisRecord(product_name='钉钉', fields=[AnalysisFieldResult(field_name='feature_tree', summary='企业协同', evidence_refs=[], confidence=0.8, normalized_value={})]),
        CompetitorAnalysisRecord(product_name='石墨文档', fields=[AnalysisFieldResult(field_name='feature_tree', summary='文档协作', evidence_refs=[], confidence=0.8, normalized_value={})]),
    ]
    matrix = agent._comparison_matrix(state, records)
    assert matrix[0]['product'] == '钉钉（直接竞品）'
    assert matrix[1]['product'] == '石墨文档（替代竞品）'
