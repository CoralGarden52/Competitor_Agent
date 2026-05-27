from __future__ import annotations

import pytest

from app.core.models import (
    CollectOutput,
    QACollectPlan,
    QACollectPlanItem,
    QAOutput,
    ReworkIssue,
    RunState,
    StageName,
)
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService


def test_qa_output_collect_plan_validation_passes() -> None:
    out = QAOutput(
        passed=False,
        issues=[ReworkIssue(code="evidence.low", message="alpha pricing_model evidence low", stage=StageName.collect)],
        target_agent="Collect",
        collect_plan=QACollectPlan(
            enabled=True,
            global_notes="pricing evidence补强",
            items=[
                QACollectPlanItem(
                    competitor="alpha",
                    field_name="pricing_model",
                    reason="report contains unknown pricing details",
                    query_list=["alpha 官网 价格 套餐", "alpha pricing plans official"],
                    priority=1,
                )
            ],
        ),
    )
    assert out.collect_plan is not None
    assert out.collect_plan.enabled is True
    assert len(out.collect_plan.items) == 1


def test_qa_output_collect_target_requires_collect_plan() -> None:
    with pytest.raises(Exception):
        QAOutput(
            passed=False,
            issues=[ReworkIssue(code="evidence.low", message="missing plan", stage=StageName.collect)],
            target_agent="Collect",
            collect_plan=None,
        )


def test_apply_rework_ticket_persists_qa_collect_plan(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    service = CompetitorWorkflowService(store)
    state = RunState(industry="saas", competitors=["alpha"])
    qa = QAOutput(
        passed=False,
        issues=[ReworkIssue(code="unknown.field", message="alpha pricing unknown", stage=StageName.collect)],
        target_agent="Collect",
        collect_plan=QACollectPlan(
            enabled=True,
            global_notes="collect missing pricing evidence",
            items=[
                QACollectPlanItem(
                    competitor="alpha",
                    field_name="pricing_model",
                    reason="unknown in report",
                    query_list=["alpha 官网 价格 套餐", "alpha 企业版 计费 规则"],
                    priority=1,
                )
            ],
        ),
    )
    service._apply_rework_ticket(state, qa)
    assert state.tickets
    assert "qa_collect_plan" in state.tickets[-1].domain_extensions
    assert "qa_collect_plan" in state.planner_meta


def test_collect_uses_targeted_qa_plan(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    service = CompetitorWorkflowService(store)
    state = RunState(industry="saas", competitors=["alpha", "beta"])
    state.planner_meta["qa_collect_plan"] = {
        "enabled": True,
        "items": [
            {
                "competitor": "alpha",
                "field_name": "pricing_model",
                "reason": "unknown",
                "query_list": ["alpha 官网 价格 套餐", "alpha pricing plans official"],
                "priority": 1,
            }
        ],
    }

    captured: dict = {}

    def _fake_run(_state, *, target_competitors=None, field_query_overrides=None):
        captured["target_competitors"] = target_competitors
        captured["field_query_overrides"] = field_query_overrides
        return CollectOutput(raw_evidences=[], provider_events=[], errors=[])

    service.collector_agent.run = _fake_run  # type: ignore[method-assign]
    service._collect(state)

    assert captured["target_competitors"] == ["alpha"]
    assert captured["field_query_overrides"]["alpha::pricing_model"] == ["alpha 官网 价格 套餐", "alpha pricing plans official"]
    assert "qa_collect_plan" not in state.planner_meta
