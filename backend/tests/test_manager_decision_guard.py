from __future__ import annotations

from app.core.models import ActionTarget, ActionType, DecisionContextSnapshot, ManagerDecision, QAOutput, Report, RunState
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


def test_guard_sends_report_ready_run_to_qa_before_finalize(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=3,
        planned_competitors=["alpha"],
        schema_fields=["feature_tree"],
        evidence_count=5,
        finding_count=3,
        report_ready=True,
    )
    decision = ManagerDecision(
        turn=3,
        action_type=ActionType.finalize_run,
        target_agent="Finalizer",
        targets=ActionTarget(),
        reason="llm_selected_finalize",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.run_qa
    assert guarded.target_agent == "QACriticAgent"
    assert guarded.metadata["guard_reason"] == "content_first_finalize_blocked_until_qa"


def test_guard_finalizes_report_ready_run_after_qa_pass(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={"last_qa_checked": True, "last_qa_passed": True, "last_qa_issue_count": 0},
    )
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=4,
        planned_competitors=["alpha"],
        schema_fields=["feature_tree"],
        evidence_count=5,
        finding_count=3,
        report_ready=True,
    )
    decision = ManagerDecision(
        turn=4,
        action_type=ActionType.run_qa,
        target_agent="QACriticAgent",
        targets=ActionTarget(),
        reason="llm_selected_qa_again",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.finalize_run
    assert guarded.target_agent == "Finalizer"
    assert guarded.metadata["guard_reason"] == "content_first_report_qa_passed_to_finalize"


def test_guard_does_not_finalize_when_last_qa_failed(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={"last_qa_checked": True, "last_qa_passed": False, "last_qa_issue_count": 1},
    )
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=5,
        planned_competitors=["alpha"],
        schema_fields=["feature_tree"],
        evidence_count=5,
        finding_count=3,
        report_ready=True,
    )
    decision = ManagerDecision(
        turn=5,
        action_type=ActionType.finalize_run,
        target_agent="Finalizer",
        targets=ActionTarget(),
        reason="llm_selected_finalize_after_failed_qa",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.run_qa
    assert guarded.target_agent == "QACriticAgent"


def test_guard_routes_failed_qa_collect_plan_to_collect_gap(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={
            "last_qa_checked": True,
            "last_qa_passed": False,
            "last_qa_issue_count": 1,
            "qa_collect_plan": {
                "enabled": True,
                "items": [{"competitor": "alpha", "field_name": "pricing_model", "reason": "missing", "query_list": ["a", "b"], "priority": 1}],
            },
        },
    )
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=6,
        planned_competitors=["alpha"],
        schema_fields=["feature_tree"],
        evidence_count=5,
        finding_count=3,
        report_ready=True,
    )
    decision = ManagerDecision(
        turn=6,
        action_type=ActionType.run_qa,
        target_agent="QACriticAgent",
        targets=ActionTarget(),
        reason="llm_selected_qa_again",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.collect_gap
    assert guarded.target_agent == "CollectorAgent"
    assert guarded.metadata["guard_reason"] == "content_first_qa_failed_to_collect_gap"


def test_guard_prioritizes_qa_reanalyze_targets_over_redraft(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={
            "last_qa_checked": True,
            "last_qa_passed": False,
            "last_qa_issue_count": 1,
            "qa_collect_round_used": True,
            "qa_reanalyze_targets": {"alpha": ["pricing_model"]},
        },
    )
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=7,
        planned_competitors=["alpha"],
        schema_fields=["pricing_model"],
        evidence_count=5,
        competitor_analysis_count=1,
        finding_count=3,
        report_ready=True,
    )
    decision = ManagerDecision(
        turn=7,
        action_type=ActionType.redraft_report,
        target_agent="WriterAgent",
        targets=ActionTarget(),
        reason="llm_selected_redraft",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.reanalyze_targets
    assert guarded.target_agent == "AnalystAgent"
    assert guarded.metadata["guard_reason"] == "content_first_qa_collect_to_reanalyze"


def test_guard_finalizes_with_risk_when_qa_failed_and_collect_budget_exhausted(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={
            "last_qa_checked": True,
            "last_qa_passed": False,
            "last_qa_issue_count": 2,
            "qa_collect_round_used": True,
        },
    )
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=8,
        planned_competitors=["alpha"],
        schema_fields=["pricing_model"],
        evidence_count=5,
        competitor_analysis_count=1,
        finding_count=3,
        report_ready=True,
    )
    decision = ManagerDecision(
        turn=8,
        action_type=ActionType.redraft_report,
        target_agent="WriterAgent",
        targets=ActionTarget(),
        reason="llm_selected_redraft_after_exhausted_qa",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.finalize_run
    assert guarded.target_agent == "Finalizer"
    assert guarded.metadata["guard_reason"] == "qa_failed_collect_budget_exhausted_finalize_with_risk"


def test_run_qa_action_persists_qa_status(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "guard.db")
    service = CompetitorWorkflowService(store)
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    store.save_state(state)
    service._qa = lambda _state: QAOutput(passed=True, issues=[], target_agent=None, ticket=None, collect_plan=None)  # type: ignore[method-assign]

    result = service._run_action_tool("run_qa", {}, {"run_id": state.run_id})
    saved = store.get_state(state.run_id)

    assert result["status"] == "completed"
    assert result["artifacts"] == {"passed": True, "issue_count": 0}
    assert saved is not None
    assert saved.planner_meta["last_qa_checked"] is True
    assert saved.planner_meta["last_qa_passed"] is True
    assert saved.planner_meta["last_qa_issue_count"] == 0


def test_run_qa_action_persists_failed_qa_collect_plan(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "guard.db")
    service = CompetitorWorkflowService(store)
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    store.save_state(state)
    service._qa = lambda _state: QAOutput.model_validate(  # type: ignore[method-assign]
        {
            "passed": False,
            "issues": [{"code": "missing_pricing", "message": "need pricing", "stage": "collect"}],
            "target_agent": "Collect",
            "collect_plan": {
                "enabled": True,
                "items": [{"competitor": "alpha", "field_name": "pricing_model", "reason": "missing", "query_list": ["a", "b"], "priority": 1}],
            },
        }
    )

    result = service._run_action_tool("run_qa", {}, {"run_id": state.run_id})
    saved = store.get_state(state.run_id)

    assert result["artifacts"] == {"passed": False, "issue_count": 1}
    assert saved is not None
    assert saved.planner_meta["last_qa_checked"] is True
    assert saved.planner_meta["last_qa_passed"] is False
    assert saved.planner_meta["last_qa_issue_count"] == 1
    assert saved.planner_meta["qa_collect_plan"]["enabled"] is True


def test_draft_action_clears_previous_qa_status(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={"last_qa_checked": True, "last_qa_passed": True, "last_qa_issue_count": 0},
    )

    def fake_draft(target_state: RunState) -> None:
        target_state.report = Report(executive_summary="summary", markdown="# Report\n\nbody")

    service._draft = fake_draft  # type: ignore[method-assign]

    result = service._execute_draft_action(state, sections=[])

    assert result["artifacts"]["report_ready_after"] is True
    assert state.planner_meta["last_qa_checked"] is False
    assert state.planner_meta["last_qa_passed"] is False
    assert state.planner_meta["last_qa_issue_count"] == 0


def test_analyze_action_clears_qa_reanalyze_targets(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={"qa_reanalyze_targets": {"alpha": ["pricing_model"]}},
    )

    service._analyze = lambda _state: None  # type: ignore[method-assign]

    result = service._execute_analyze_action(state, competitors=["alpha"], fields=["pricing_model"])

    assert result["status"] == "completed"
    assert "qa_reanalyze_targets" not in state.planner_meta
    assert state.planner_meta["qa_reanalyzed_after_collect"] is True


def test_finalize_action_exposes_qa_risk_artifacts(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "guard.db")
    service = CompetitorWorkflowService(store)
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={"last_qa_checked": True, "last_qa_passed": False, "last_qa_issue_count": 2},
    )
    store.save_state(state)

    result = service._run_action_tool("finalize_run", {}, {"run_id": state.run_id})
    saved = store.get_state(state.run_id)

    assert result["status"] == "completed"
    assert result["artifacts"]["qa_passed"] is False
    assert result["artifacts"]["qa_issue_count"] == 2
    assert result["artifacts"]["qa_finalize_with_risk"] is True
    assert saved is not None
    assert saved.planner_meta["qa_exhausted"] is True
    assert saved.planner_meta["qa_finalize_with_risk"] is True
