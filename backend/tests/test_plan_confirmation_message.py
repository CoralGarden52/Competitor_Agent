from __future__ import annotations

from app.core.models import AnalysisSchemaField, RunState
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService


def test_plan_confirmation_message_uses_plain_labels_without_markdown(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'confirm.db'))
    state = RunState(
        industry='video meeting saas',
        competitors=['Zoom'],
        planned_competitors=['Zoom'],
        user_prompt='分析腾讯会议竞品',
        target_product='腾讯会议',
        target_product_description='云视频会议 SaaS',
        planner_meta={
            'candidate_groups': {
                'direct': [{'name': 'Zoom'}],
                'substitute': [{'name': 'Google Meet'}],
            }
        },
    )
    state.analysis_schema_plan = [AnalysisSchemaField(field_name='feature_tree')]

    message = service._build_plan_confirmation_message(state)

    assert '核心目的：' in message
    assert '目标行业/场景：' in message
    assert '分析对象：目标产品：腾讯会议；直接竞品：Zoom；替代竞品：Google Meet' in message
    assert '- **核心目的**' not in message


def test_plan_confirmation_message_marks_missing_direct_competitors_explicitly(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'confirm_missing.db'))
    state = RunState(
        industry='video meeting saas',
        competitors=[],
        planned_competitors=[],
        user_prompt='分析腾讯会议竞品',
        target_product='腾讯会议',
        target_product_description='云视频会议 SaaS',
        planner_meta={'candidate_groups': {'direct': [], 'substitute': []}},
    )

    message = service._build_plan_confirmation_message(state)

    assert '直接竞品：当前未通过横向语料确认到有效结果' in message
