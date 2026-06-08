from __future__ import annotations

from app.agents.questionnaire_agent import QuestionnaireAgent
from app.core.models import Report, ReportBlock, ReportContentItem


class _DummyLLM:
    config = type('Cfg', (), {'agent_llm_retry_count': 0, 'openai_model': 'test-model'})()


def test_questionnaire_chunks_follow_new_report_blocks_only() -> None:
    agent = QuestionnaireAgent(llm=_DummyLLM())  # type: ignore[arg-type]
    report = Report(
        executive_summary='summary',
        markdown='',
        blocks=[
            ReportBlock(block_id='title', block_type='title', title='竞品分析报告', order=0, content='竞品分析报告'),
            ReportBlock(block_id='executive_summary', block_type='executive_summary', title='执行摘要', order=1, content='核心结论'),
            ReportBlock(block_id='bg', block_type='section_paragraph', section_id='analysis_background', title='一、分析背景与目标', order=2, content='背景内容'),
            ReportBlock(
                block_id='matrix',
                block_type='comparison_matrix',
                title='二、竞品对比总览',
                order=3,
                content=[{'product': 'A', 'feature_tree': '能力完整', 'role': 'direct'}],
            ),
            ReportBlock(
                block_id='cap',
                block_type='section_paragraph',
                section_id='capability_comparison',
                title='三、核心能力与产品形态',
                order=4,
                content=[ReportContentItem(item_id='1', text='能力差异明显', kind='paragraph')],
            ),
            ReportBlock(
                block_id='swot',
                block_type='section_bullets',
                section_id='swot_analysis',
                title='六、标准化SWOT分析',
                order=5,
                content=[ReportContentItem(item_id='2', text='优势：协作链路完整', kind='bullet')],
            ),
            ReportBlock(block_id='refs', block_type='reference_list', title='参考来源', order=6, content=['https://example.com']),
        ],
    )

    chunks = agent._questionnaire_chunks_from_report(report)

    assert [chunk['chunk_title'] for chunk in chunks] == [
        '执行摘要',
        '二、竞品对比总览',
        '三、核心能力与产品形态',
        '六、标准化SWOT分析',
    ]
    assert all(chunk['chunk_title'] != '一、分析背景与目标' for chunk in chunks)
    assert all(chunk['chunk_title'] != '参考来源' for chunk in chunks)
    assert any('| product | feature_tree |' in chunk['content'] for chunk in chunks)
