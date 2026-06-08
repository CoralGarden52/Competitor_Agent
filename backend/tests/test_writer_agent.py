from __future__ import annotations

from app.agents.writer_agent import WriterAgent
from app.core.config import get_config
from app.core.models import (
    AnalysisFieldResult,
    AnalysisSchemaField,
    CompetitorAnalysisRecord,
    Evidence,
    Finding,
    RunState,
)


class _DummyLLM:
    config = type('Cfg', (), {'agent_llm_retry_count': 0, 'openai_model': 'test-model'})()

    def invoke_json(self, *args, **kwargs):
        raise AssertionError('invoke_json should not be called in this test')

    def invoke_text_stream(self, *args, **kwargs):
        if False:
            yield ""


class _ParallelLLM:
    config = type('Cfg', (), {'agent_llm_retry_count': 0, 'openai_model': 'test-model'})()

    def invoke_json_with_tools(self, *args, **kwargs):
        payload = kwargs['user_payload']
        trace_name = kwargs.get('trace_name', '')
        if 'swot' in trace_name:
            target = payload['target_product']
            peer_name = payload['peer_products'][0]['product_name'] if payload.get('peer_products') else 'peer'
            return {
                'product_name': target,
                'strengths': [{'text': f'{target} 核心能力聚焦', 'evidence_refs': ['ev1']}],
                'weaknesses': [{'text': f'{target} 品牌认知仍需加强', 'evidence_refs': ['ev1']}],
                'opportunities': [{'text': f'可利用 {peer_name} 在实施复杂度上的短板切入', 'evidence_refs': ['ev1', 'ev2']}],
                'threats': [{'text': f'{peer_name} 的生态优势可能压缩 {target} 的获客空间', 'evidence_refs': ['ev2']}],
            }
        section_id = payload['section_id']
        return {
            'title': payload['section_title'],
            'paragraphs': [{'text': f'{section_id} 段落结论', 'kind': 'paragraph', 'evidence_refs': ['ev1']}],
            'bullets': [{'text': f'{section_id} 要点', 'kind': 'bullet', 'evidence_refs': ['ev2']}],
        }

    def invoke_json(self, *args, **kwargs):
        return self.invoke_json_with_tools(*args, **kwargs)

    def invoke_text_stream(self, *args, **kwargs):
        if False:
            yield ""


class _MatrixSummaryLLM:
    config = type('Cfg', (), {'agent_llm_retry_count': 0, 'openai_model': 'test-model'})()

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def invoke_text(self, *args, **kwargs):
        payload = kwargs['user_payload']
        self.calls.append((payload['product_name'], payload['field_name']))
        return f"{payload['product_name']} 的 {payload['field_label']}已总结"

    def invoke_text_stream(self, *args, **kwargs):
        if False:
            yield ""


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
        target_product='Target',
        analysis_subjects=[
            {'name': 'Target', 'role': 'target', 'is_target': True},
            {'name': 'A', 'role': 'direct', 'is_target': False},
            {'name': 'B', 'role': 'substitute', 'is_target': False},
        ],
        planner_meta={
            'candidate_groups': {
                'target': {'name': 'Target'},
                'direct': [{'name': 'A'}],
                'substitute': [{'name': 'B'}],
            }
        },
    )
    records = [
        CompetitorAnalysisRecord(product_name='Target', fields=[AnalysisFieldResult(field_name='feature_tree', summary='t', evidence_refs=[], confidence=0.8, normalized_value={})]),
        CompetitorAnalysisRecord(product_name='A', fields=[AnalysisFieldResult(field_name='feature_tree', summary='x', evidence_refs=[], confidence=0.8, normalized_value={})]),
        CompetitorAnalysisRecord(product_name='B', fields=[AnalysisFieldResult(field_name='feature_tree', summary='y', evidence_refs=[], confidence=0.8, normalized_value={})]),
    ]
    matrix = agent._comparison_matrix(state, records)
    assert '目标产品' in matrix[0]['product']
    assert '直接竞品' in matrix[1]['product']
    assert '间接竞品' in matrix[2]['product']


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
        actions = agent._opportunity_bullets(records, state=state)

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
        actions = agent._opportunity_bullets(records, state=state)
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


def test_comparison_matrix_uses_llm_summary_per_cell_when_available() -> None:
    llm = _MatrixSummaryLLM()
    agent = WriterAgent(llm=llm)
    state = RunState(
        industry='meeting_software',
        competitors=['腾讯会议'],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
        ],
    )
    records = [
        CompetitorAnalysisRecord(
            product_name='腾讯会议',
            fields=[
                AnalysisFieldResult(field_name='feature_tree', summary='支持会议、录制与协作', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                AnalysisFieldResult(field_name='pricing_model', summary='提供免费版与企业版', evidence_refs=['ev2'], confidence=0.8, normalized_value={}),
            ],
        )
    ]

    matrix = agent._comparison_matrix(state, records)

    assert matrix[0]['feature_tree'] == '腾讯会议 的 功能体系已总结。'
    assert matrix[0]['pricing_model'] == '腾讯会议 的 定价模式已总结。'
    assert set(llm.calls) == {('腾讯会议', 'feature_tree'), ('腾讯会议', 'pricing_model')}


def test_records_and_report_prioritize_target_product() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state = RunState(
        industry='knowledge_base',
        competitors=['Comp A', 'Comp B'],
        planned_competitors=['Comp A', 'Comp B'],
        target_product='My Product',
        analysis_subjects=[
            {'name': 'My Product', 'role': 'target', 'is_target': True},
            {'name': 'Comp A', 'role': 'direct', 'is_target': False},
            {'name': 'Comp B', 'role': 'substitute', 'is_target': False},
        ],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name='Comp A',
                fields=[
                    AnalysisFieldResult(field_name='strengths', summary='生态完整', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='weaknesses', summary='价格较高', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                ],
            ),
            CompetitorAnalysisRecord(
                product_name='My Product',
                fields=[
                    AnalysisFieldResult(field_name='strengths', summary='AI 问答体验更聚焦', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='weaknesses', summary='品牌认知仍需提升', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                ],
            ),
            CompetitorAnalysisRecord(
                product_name='Comp B',
                fields=[
                    AnalysisFieldResult(field_name='strengths', summary='客户基础较大', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                ],
            ),
        ],
        evidences=[Evidence(source_url='https://example.com/1', snippet='evidence', evidence_id='ev1')],
    )

    records = agent._records(state)
    matrix = agent._comparison_matrix(state, records)
    report = agent.build_streamable_report(state).report

    assert records[0].product_name == 'My Product'
    assert matrix[0]['role'] == 'target'
    assert '目标产品' in matrix[0]['product']
    assert report.comparison_matrix[0]['role'] == 'target'
    assert 'My Product' in report.executive_summary


def test_streamable_report_uses_new_section_order_and_item_level_content() -> None:
    agent = WriterAgent(llm=_DummyLLM())
    state, _ = _build_state('能力完整且价格清晰')

    report = agent.build_streamable_report(state).report

    assert [section.section_id for section in report.sections] == [
        'analysis_background',
        'comparison_overview',
        'capability_comparison',
        'pricing_strategy',
        'user_feedback_analysis',
        'swot_analysis',
        'strategic_insights',
        'conclusion_risks',
    ]
    section_blocks = [block for block in report.blocks if block.block_type in {'section_paragraph', 'section_bullets'}]
    assert section_blocks
    assert [block.block_type for block in report.blocks[:4]] == ['title', 'executive_summary', 'section_paragraph', 'comparison_matrix']
    assert sum(1 for block in report.blocks if block.section_id == 'comparison_overview') == 0
    assert any(isinstance(block.content, list) and block.content for block in section_blocks)
    first_item_block = next(block for block in section_blocks if isinstance(block.content, list) and block.content)
    first_item = first_item_block.content[0]
    assert isinstance(first_item, dict) or hasattr(first_item, 'text')


def test_parallel_writer_swot_uses_peer_strengths_and_weaknesses_for_relative_ot() -> None:
    agent = WriterAgent(llm=_ParallelLLM())
    state = RunState(
        industry='knowledge_base',
        competitors=['Comp A'],
        planned_competitors=['Comp A'],
        target_product='My Product',
        analysis_subjects=[
            {'name': 'My Product', 'role': 'target', 'is_target': True},
            {'name': 'Comp A', 'role': 'direct', 'is_target': False},
        ],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name='My Product',
                fields=[
                    AnalysisFieldResult(field_name='strengths', summary='问答链路更聚焦', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='weaknesses', summary='品牌认知较弱', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                ],
            ),
            CompetitorAnalysisRecord(
                product_name='Comp A',
                fields=[
                    AnalysisFieldResult(field_name='strengths', summary='生态更完整', evidence_refs=['ev2'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='weaknesses', summary='实施复杂度更高', evidence_refs=['ev2'], confidence=0.8, normalized_value={}),
                ],
            ),
        ],
        findings=[
            Finding(statement='Comp A 生态更完整', category='feature', evidence_refs=['ev2'], competitor='Comp A', impact='high', confidence=0.8),
        ],
        evidences=[
            Evidence(source_url='https://example.com/my-product', snippet='my product evidence', evidence_id='ev1'),
            Evidence(source_url='https://example.com/comp-a', snippet='comp a evidence', evidence_id='ev2'),
        ],
    )

    groups = agent.plan_report_write_groups(state, agent._records(state))
    target_group = next(group for group in groups if group.section_id == 'swot_analysis' and group.product_name == 'My Product')

    fragment = agent._run_swot_llm_for_product(state, agent._records(state), target_group)

    texts = [item.text for item in fragment.items]
    assert any('Comp A' in text and '机会：' in text for text in texts)
    assert any('Comp A' in text and '威胁：' in text for text in texts)
    assert any(set(item.evidence_refs) == {'ev1', 'ev2'} for item in fragment.items if '机会：' in item.text)


def test_parallel_writer_aggregates_sections_in_template_order() -> None:
    agent = WriterAgent(llm=_ParallelLLM())
    state = RunState(
        industry='saas',
        competitors=['Comp A'],
        target_product='My Product',
        analysis_subjects=[
            {'name': 'My Product', 'role': 'target', 'is_target': True},
            {'name': 'Comp A', 'role': 'direct', 'is_target': False},
        ],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
            AnalysisSchemaField(field_name='user_feedback', priority=3),
        ],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name='My Product',
                fields=[
                    AnalysisFieldResult(field_name='feature_tree', summary='支持问答', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='pricing_model', summary='按席位订阅', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='user_feedback', summary='反馈集中在易用性', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='strengths', summary='聚焦场景', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='weaknesses', summary='生态仍小', evidence_refs=['ev1'], confidence=0.8, normalized_value={}),
                ],
            ),
            CompetitorAnalysisRecord(
                product_name='Comp A',
                fields=[
                    AnalysisFieldResult(field_name='feature_tree', summary='支持知识管理', evidence_refs=['ev2'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='pricing_model', summary='企业定制报价', evidence_refs=['ev2'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='user_feedback', summary='反馈集中在生态联动', evidence_refs=['ev2'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='strengths', summary='生态完整', evidence_refs=['ev2'], confidence=0.8, normalized_value={}),
                    AnalysisFieldResult(field_name='weaknesses', summary='学习成本高', evidence_refs=['ev2'], confidence=0.8, normalized_value={}),
                ],
            ),
        ],
        evidences=[
            Evidence(source_url='https://example.com/my-product', snippet='my product evidence', evidence_id='ev1'),
            Evidence(source_url='https://example.com/comp-a', snippet='comp a evidence', evidence_id='ev2'),
        ],
    )

    streamed: list[str] = []
    drafted = agent.run_parallel_markdown_stream(state, on_delta=streamed.append)

    assert drafted.report.markdown.startswith('# My Product竞品分析报告')
    assert [section.section_id for section in drafted.report.sections] == [
        'analysis_background',
        'comparison_overview',
        'capability_comparison',
        'pricing_strategy',
        'user_feedback_analysis',
        'swot_analysis',
        'strategic_insights',
        'conclusion_risks',
    ]
    assert '## 参考来源' in drafted.report.markdown
    assert any('### My Product SWOT分析' in chunk for chunk in streamed)
