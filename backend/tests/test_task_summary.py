from __future__ import annotations

from app.core.workflow import CompetitorWorkflowService


def test_normalize_task_summary_is_not_hard_limited_to_twelve_chars() -> None:
    text = '腾讯会议与主流会议软件竞品分析摘要'

    normalized = CompetitorWorkflowService._normalize_task_summary(text)

    assert normalized == text
    assert len(normalized) > 12
