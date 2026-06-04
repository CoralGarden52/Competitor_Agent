from __future__ import annotations

from app.core.models import ActionTarget, ActionType, DecisionContextSnapshot, ManagerDecision, RunState
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService


def test_guard_rewrites_collect_gap_when_qa_collect_not_allowed(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], planned_competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=2,
        planned_competitors=["alpha"],
        schema_fields=["feature_tree"],
        evidence_count=5,
        qa_collect_allowed=False,
        report_ready=False,
    )
    decision = ManagerDecision(
        turn=2,
        action_type=ActionType.collect_gap,
        target_agent="CollectorAgent",
        targets=ActionTarget(competitors=["alpha"], fields=["feature_tree"]),
        reason="llm_selected_collect_gap",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.collect_initial
    assert guarded.target_agent == "CollectorAgent"
    assert guarded.metadata["guard_rewritten"] is True
    assert guarded.metadata["original_action_type"] == "collect_gap"


def test_guard_keeps_finalize_when_delivery_requirements_are_met(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=3,
        analyze_ready=True,
        report_ready=True,
        coverage_summary={"coverage": 0.9},
    )
    decision = ManagerDecision(
        turn=3,
        action_type=ActionType.finalize_run,
        target_agent="Finalizer",
        targets=ActionTarget(),
        reason="llm_selected_finalize",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.finalize_run
    assert "guard_rewritten" not in guarded.metadata
