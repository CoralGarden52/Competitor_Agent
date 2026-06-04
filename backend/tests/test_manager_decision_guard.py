from __future__ import annotations

from app.core.models import ActionTarget, ActionType, DecisionContextSnapshot, ManagerDecision, RunState
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService


def test_guard_rewrites_plan_scope_to_reanalyze_when_evidence_exists_without_findings(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], planned_competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=2,
        planned_competitors=["alpha"],
        schema_fields=["feature_tree"],
        evidence_count=5,
        finding_count=0,
        report_ready=False,
    )
    decision = ManagerDecision(
        turn=2,
        action_type=ActionType.plan_scope,
        target_agent="OrchestratorAgent",
        targets=ActionTarget(competitors=["alpha"]),
        reason="llm_selected_plan",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.reanalyze_targets
    assert guarded.target_agent == "AnalystAgent"
    assert guarded.metadata["guard_rewritten"] is True
    assert guarded.metadata["original_action_type"] == "plan_scope"


def test_guard_keeps_plan_scope_when_scope_missing(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(run_id=state.run_id, turn_count=1)
    decision = ManagerDecision(
        turn=1,
        action_type=ActionType.plan_scope,
        target_agent="OrchestratorAgent",
        targets=ActionTarget(),
        reason="llm_selected_plan",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.plan_scope
    assert "guard_rewritten" not in guarded.metadata
