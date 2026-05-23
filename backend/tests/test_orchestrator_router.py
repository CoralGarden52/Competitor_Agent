from __future__ import annotations

from app.agents.router import route_after_qa
from app.core.models import QAOutput


def test_router_finalize_on_qa_pass() -> None:
    result = route_after_qa(qa_result=QAOutput(passed=True), iteration=1, max_rework_iterations=2)
    assert result.action == 'finalize'


def test_router_retry_on_qa_fail_before_max() -> None:
    qa = QAOutput(passed=False, target_agent='Analyze')
    result = route_after_qa(qa_result=qa, iteration=1, max_rework_iterations=2)
    assert result.action == 'retry'
    assert result.route_back_stage is not None


def test_router_fail_after_max() -> None:
    qa = QAOutput(passed=False, target_agent='Draft')
    result = route_after_qa(qa_result=qa, iteration=3, max_rework_iterations=2)
    assert result.action == 'fail'
