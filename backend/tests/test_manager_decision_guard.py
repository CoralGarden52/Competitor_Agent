from __future__ import annotations

from app.core.models import (
    ActionTarget,
    ActionType,
    AnalysisFieldResult,
    AnalysisSchemaField,
    CompetitorAnalysisRecord,
    DecisionContextSnapshot,
    Finding,
    ManagerDecision,
    QAOutput,
    Report,
    RunState,
)
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


def test_decision_context_marks_qa_ready_from_last_passed_qa_on_first_attempt(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        attempt=1,
        planner_meta={"last_qa_checked": True, "last_qa_passed": True, "last_qa_issue_count": 0},
    )
    state.report = Report(executive_summary="summary", markdown="# Report\n\nbody")
    state.planned_competitors = ["alpha"]
    state.analysis_schema_plan = [AnalysisSchemaField(field_name="pricing_model")]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name="alpha",
            fields=[AnalysisFieldResult(field_name="pricing_model", summary="tiered", evidence_refs=["evd_1"])],
        )
    ]
    state.findings = [Finding(statement="alpha has tiered pricing", category="pricing", evidence_refs=["evd_1"])]

    context = service._build_decision_context(state)

    assert context.qa_ready is True
    assert context.last_qa_checked is True
    assert context.last_qa_passed is True
    assert context.last_qa_issue_count == 0
    assert context.qa_reviewed is True
    assert context.qa_passed is True


def test_decision_context_keeps_qa_pending_before_qa_runs(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    state.report = Report(executive_summary="summary", markdown="# Report\n\nbody")

    context = service._build_decision_context(state)

    assert context.report_ready is True
    assert context.qa_ready is False
    assert context.last_qa_checked is False
    assert context.qa_reviewed is False


def test_decision_context_allows_pending_qa_collect_plan(tmp_path) -> None:
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
    state.report = Report(executive_summary="summary", markdown="# Report\n\nbody")

    context = service._build_decision_context(state)

    assert context.qa_collect_pending is True
    assert context.qa_collect_allowed is True
    assert context.qa_collect_item_count == 1
    assert context.qa_recommendation == "collect_gap"


def test_decision_context_marks_quality_gate_finalize_eligible(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], planned_competitors=["alpha"], user_prompt="test")
    state.report = Report(executive_summary="summary", markdown="# Report\n\nbody")

    state.analysis_schema_plan = [AnalysisSchemaField(field_name="pricing_model")]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name="alpha",
            fields=[AnalysisFieldResult(field_name="pricing_model", summary="tiered", evidence_refs=["evd_1"])],
        )
    ]
    state.findings = [Finding(statement="alpha has tiered pricing", category="pricing", evidence_refs=["evd_1"])]

    context = service._build_decision_context(state)

    assert context.quality_gate["coverage_ok"] is True
    assert context.quality_gate["critical_gaps_count"] == 0
    assert context.quality_gate["finalize_eligible"] is True


def test_decision_context_does_not_finalize_with_risk_for_failed_qa(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="test",
        planner_meta={
            "last_qa_checked": True,
            "last_qa_passed": False,
            "last_qa_issue_count": 1,
            "qa_collect_plan": {
                "enabled": True,
                "items": [{"competitor": "alpha", "field_name": "pricing_model", "reason": "missing", "query_list": ["a", "b"], "priority": 1}],
            },
            "qa_collect_round_used": True,
        },
    )
    state.report = Report(executive_summary="summary", markdown="# Report\n\nbody")
    state.analysis_schema_plan = [AnalysisSchemaField(field_name="pricing_model")]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name="alpha",
            fields=[AnalysisFieldResult(field_name="pricing_model", summary="tiered", evidence_refs=["evd_1"])],
        )
    ]
    state.findings = [Finding(statement="alpha has tiered pricing", category="pricing", evidence_refs=["evd_1"])]
    state.decision_history = [
        ManagerDecision(
            turn=3,
            action_type=ActionType.draft_report,
            target_agent="WriterAgent",
            targets=ActionTarget(),
            reason="prior_draft_attempt",
        )
    ]

    context = service._build_decision_context(state)

    assert context.qa_failure_kind == "collect_gap"
    assert context.qa_collect_allowed is True
    assert context.finalize_with_risk_eligible is False
    assert context.qa_recommendation == "collect_gap"


def test_decision_context_treats_passed_qa_as_finalize_eligible(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="test",
        planner_meta={"last_qa_checked": True, "last_qa_passed": True, "last_qa_issue_count": 0},
    )
    state.report = Report(executive_summary="summary", markdown="# Report\n\nbody")
    state.analysis_schema_plan = [
        AnalysisSchemaField(field_name="pricing_model"),
        AnalysisSchemaField(field_name="feature_tree"),
    ]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name="alpha",
            fields=[AnalysisFieldResult(field_name="pricing_model", summary="tiered", evidence_refs=["evd_1"])],
        )
    ]
    state.findings = [Finding(statement="alpha has tiered pricing", category="pricing", evidence_refs=["evd_1"])]

    context = service._build_decision_context(state)

    assert context.coverage_summary["coverage"] < 0.8
    assert context.quality_gate["qa_delivery_approved"] is True
    assert context.quality_gate["finalize_eligible"] is True
    assert context.qa_recommendation == "finalize_run"


def test_guard_keeps_finalize_when_delivery_requirements_are_met(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=3,
        analyze_ready=True,
        report_ready=True,
        qa_ready=True,
        qa_reviewed=True,
        qa_passed=True,
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


def test_guard_blocks_finalize_without_report_or_analysis(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=3,
        analyze_ready=False,
        report_ready=False,
    )
    decision = ManagerDecision(
        turn=3,
        action_type=ActionType.finalize_run,
        target_agent="Finalizer",
        targets=ActionTarget(),
        reason="llm_selected_finalize_too_early",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.reanalyze_targets
    assert guarded.target_agent == "AnalystAgent"
    assert guarded.metadata["guard_reason"] == "finalize_requires_analysis_qa_and_report"


def test_guard_blocks_qa_without_analysis(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(run_id=state.run_id, turn_count=3, report_ready=False)
    decision = ManagerDecision(
        turn=3,
        action_type=ActionType.run_qa,
        target_agent="QACriticAgent",
        targets=ActionTarget(),
        reason="llm_selected_qa_too_early",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.reanalyze_targets
    assert guarded.target_agent == "AnalystAgent"
    assert guarded.metadata["guard_reason"] == "qa_requires_analysis_ready"


def test_guard_blocks_manager_finalize_report_ready_run_before_qa(tmp_path) -> None:
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
        analyze_ready=True,
        qa_ready=False,
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
    assert guarded.metadata["guard_reason"] == "finalize_requires_pre_draft_qa"


def test_guard_does_not_force_finalize_after_qa_pass(tmp_path) -> None:
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
        analyze_ready=True,
        qa_ready=True,
        last_qa_checked=True,
        last_qa_passed=True,
        last_qa_issue_count=0,
    )
    decision = ManagerDecision(
        turn=4,
        action_type=ActionType.run_qa,
        target_agent="QACriticAgent",
        targets=ActionTarget(),
        reason="llm_selected_qa_again",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.run_qa
    assert guarded.target_agent == "QACriticAgent"
    assert "guard_rewritten" not in guarded.metadata


def test_guard_blocks_immediate_repeated_qa_after_pass(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=5,
        report_ready=True,
        analyze_ready=True,
        qa_reviewed=True,
        qa_passed=True,
        last_action_type="run_qa",
        last_action_status="completed",
        quality_gate={"finalize_eligible": False},
    )
    decision = ManagerDecision(
        turn=5,
        action_type=ActionType.run_qa,
        target_agent="QACriticAgent",
        targets=ActionTarget(),
        reason="llm_repeated_qa",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.finalize_run
    assert guarded.target_agent == "Finalizer"
    assert guarded.metadata["guard_reason"] == "repeat_qa_blocked_finalize_ready"


def test_guard_blocks_immediate_repeated_failed_qa_without_pending_work(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=5,
        report_ready=True,
        analyze_ready=True,
        qa_reviewed=True,
        qa_passed=False,
        last_action_type="run_qa",
        last_action_status="completed",
        quality_gate={"finalize_eligible": False},
    )
    decision = ManagerDecision(
        turn=5,
        action_type=ActionType.run_qa,
        target_agent="QACriticAgent",
        targets=ActionTarget(),
        reason="llm_repeated_failed_qa",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.collect_gap
    assert guarded.target_agent == "CollectorAgent"
    assert guarded.metadata["guard_reason"] == "repeat_qa_blocked_recollect_required"


def test_guard_blocks_draft_when_qa_failed_collect_gap_is_pending(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "guard.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    context = DecisionContextSnapshot(
        run_id=state.run_id,
        turn_count=6,
        report_ready=True,
        analyze_ready=True,
        qa_reviewed=True,
        qa_passed=False,
        qa_failure_kind="collect_gap",
        qa_collect_allowed=False,
        qa_collect_pending=True,
        finalize_with_risk_eligible=True,
        quality_gate={"coverage_ok": True, "finalize_eligible": False},
    )
    decision = ManagerDecision(
        turn=6,
        action_type=ActionType.draft_report,
        target_agent="WriterAgent",
        targets=ActionTarget(),
        reason="llm_selected_draft",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.collect_gap
    assert guarded.target_agent == "CollectorAgent"
    assert guarded.metadata["guard_reason"] == "draft_blocked_failed_qa_recollect"


def test_guard_allows_manager_finalize_after_failed_qa_when_report_and_analysis_exist(tmp_path) -> None:
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
        analyze_ready=True,
        qa_ready=False,
        last_qa_checked=True,
        last_qa_passed=False,
        last_qa_issue_count=1,
    )
    decision = ManagerDecision(
        turn=5,
        action_type=ActionType.finalize_run,
        target_agent="Finalizer",
        targets=ActionTarget(),
        reason="llm_selected_finalize_after_failed_qa",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.collect_gap
    assert guarded.target_agent == "CollectorAgent"
    assert guarded.metadata["guard_reason"] == "finalize_blocked_failed_qa_recollect"


def test_guard_does_not_force_collect_gap_from_failed_qa_collect_plan(tmp_path) -> None:
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
        analyze_ready=True,
        qa_ready=False,
        last_qa_checked=True,
        last_qa_passed=False,
        last_qa_issue_count=1,
        qa_collect_allowed=True,
        qa_collect_pending=True,
    )
    decision = ManagerDecision(
        turn=6,
        action_type=ActionType.run_qa,
        target_agent="QACriticAgent",
        targets=ActionTarget(),
        reason="llm_selected_qa_again",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.run_qa
    assert guarded.target_agent == "QACriticAgent"
    assert "guard_rewritten" not in guarded.metadata


def test_guard_does_not_force_reanalyze_for_qa_reanalyze_targets(tmp_path) -> None:
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
        analyze_ready=True,
        qa_ready=False,
        last_qa_checked=True,
        last_qa_passed=False,
        last_qa_issue_count=1,
        qa_reanalyze_pending=True,
    )
    decision = ManagerDecision(
        turn=7,
        action_type=ActionType.draft_report,
        target_agent="WriterAgent",
        targets=ActionTarget(),
        reason="llm_selected_draft",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.reanalyze_targets
    assert guarded.target_agent == "AnalystAgent"
    assert guarded.metadata["guard_reason"] == "draft_blocked_failed_qa_reanalyze"


def test_guard_does_not_force_finalize_when_qa_failed_and_collect_budget_exhausted(tmp_path) -> None:
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
        analyze_ready=True,
        qa_ready=False,
        last_qa_checked=True,
        last_qa_passed=False,
        last_qa_issue_count=2,
    )
    decision = ManagerDecision(
        turn=8,
        action_type=ActionType.draft_report,
        target_agent="WriterAgent",
        targets=ActionTarget(),
        reason="llm_selected_draft_after_failed_qa",
    )

    guarded = service._guard_manager_decision(state=state, context=context, decision=decision)

    assert guarded.action_type == ActionType.collect_gap
    assert guarded.target_agent == "CollectorAgent"
    assert guarded.metadata["guard_reason"] == "draft_blocked_failed_qa_recollect"


def test_run_qa_action_persists_qa_status(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "guard.db")
    service = CompetitorWorkflowService(store)
    state = RunState(industry="general", competitors=["alpha"], planned_competitors=["alpha"], user_prompt="test")
    state.analysis_schema_plan = [AnalysisSchemaField(field_name="pricing_model")]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name="alpha",
            fields=[AnalysisFieldResult(field_name="pricing_model", summary="tiered", evidence_refs=["evd_1"])],
        )
    ]
    state.findings = [Finding(statement="alpha has tiered pricing", category="pricing", evidence_refs=["evd_1"])]
    store.save_state(state)
    service._qa = lambda _state: QAOutput(passed=True, issues=[], target_agent=None, ticket=None, collect_plan=None)  # type: ignore[method-assign]

    result = service._run_action_tool("run_qa", {}, {"run_id": state.run_id})
    saved = store.get_state(state.run_id)

    assert result["status"] == "completed"
    assert result["artifacts"]["passed"] is True
    assert result["artifacts"]["issue_count"] == 0
    assert result["artifacts"]["qa_current_coverage"] == 1.0
    assert saved is not None
    assert saved.planner_meta["last_qa_checked"] is True
    assert saved.planner_meta["last_qa_passed"] is True
    assert saved.planner_meta["last_qa_issue_count"] == 0
    assert saved.planner_meta["qa_current_coverage"] == 1.0


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

    assert result["artifacts"]["passed"] is False
    assert result["artifacts"]["issue_count"] == 1
    assert result["artifacts"]["qa_current_coverage"] == 0.0
    assert saved is not None
    assert saved.planner_meta["last_qa_checked"] is True
    assert saved.planner_meta["last_qa_passed"] is False
    assert saved.planner_meta["last_qa_issue_count"] == 1
    assert saved.planner_meta["qa_collect_plan"]["enabled"] is True


def test_run_qa_action_keeps_failed_result_when_coverage_is_high(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "guard.db")
    service = CompetitorWorkflowService(store)
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test")
    state.analysis_schema_plan = [
        AnalysisSchemaField(field_name="pricing_model", display_name="Pricing"),
    ]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name="alpha",
            fields=[
                AnalysisFieldResult(
                    field_name="pricing_model",
                    summary="pricing exists",
                    evidence_refs=["evd_1"],
                )
            ],
        )
    ]
    state.findings = [Finding(statement="alpha pricing exists", category="pricing", evidence_refs=["evd_1"])]
    store.save_state(state)
    service._qa = lambda _state: QAOutput.model_validate(  # type: ignore[method-assign]
        {
            "passed": False,
            "issues": [{"code": "pricing_evidence_thin", "message": "need stronger pricing evidence", "stage": "collect"}],
            "target_agent": "Collect",
            "collect_plan": {
                "enabled": True,
                "items": [{"competitor": "alpha", "field_name": "pricing_model", "reason": "evidence_thin", "query_list": ["a", "b"], "priority": 1}],
            },
        }
    )

    result = service._run_action_tool("run_qa", {}, {"run_id": state.run_id})
    saved = store.get_state(state.run_id)

    assert result["artifacts"]["qa_current_coverage"] == 1.0
    assert result["artifacts"]["passed"] is False
    assert result["artifacts"]["issue_count"] == 1
    assert saved is not None
    assert saved.planner_meta["last_qa_checked"] is True
    assert saved.planner_meta["last_qa_passed"] is False
    assert saved.planner_meta["last_qa_issue_count"] == 1
    assert saved.planner_meta["qa_collect_plan"]["enabled"] is True


def test_run_qa_action_second_round_allows_draft_when_coverage_is_high(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "guard.db")
    service = CompetitorWorkflowService(store)
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={"qa_collect_round_used": True, "qa_last_failed_coverage": 0.4},
    )
    state.analysis_schema_plan = [
        AnalysisSchemaField(field_name="pricing_model", display_name="Pricing"),
    ]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name="alpha",
            fields=[
                AnalysisFieldResult(
                    field_name="pricing_model",
                    summary="pricing exists",
                    evidence_refs=["evd_1"],
                )
            ],
        )
    ]
    state.findings = [Finding(statement="alpha pricing exists", category="pricing", evidence_refs=["evd_1"])]
    store.save_state(state)
    service._qa = lambda _state: QAOutput.model_validate(  # type: ignore[method-assign]
        {
            "passed": False,
            "issues": [{"code": "pricing_evidence_thin", "message": "need stronger pricing evidence", "stage": "collect"}],
            "target_agent": "Collect",
            "collect_plan": {
                "enabled": True,
                "items": [{"competitor": "alpha", "field_name": "pricing_model", "reason": "evidence_thin", "query_list": ["a", "b"], "priority": 1}],
            },
        }
    )

    result = service._run_action_tool("run_qa", {}, {"run_id": state.run_id})
    saved = store.get_state(state.run_id)

    assert result["artifacts"]["qa_current_coverage"] == 1.0
    assert result["artifacts"]["passed"] is True
    assert result["artifacts"]["issue_count"] == 0
    assert saved is not None
    assert saved.planner_meta["last_qa_checked"] is True
    assert saved.planner_meta["last_qa_passed"] is True
    assert saved.planner_meta["last_qa_issue_count"] == 0
    assert "qa_collect_plan" not in saved.planner_meta


def test_run_qa_action_second_round_allows_draft_when_coverage_improves(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "guard.db")
    service = CompetitorWorkflowService(store)
    state = RunState(
        industry="general",
        competitors=["alpha"],
        user_prompt="test",
        planner_meta={"qa_collect_round_used": True, "qa_last_failed_coverage": 0.4},
    )
    state.analysis_schema_plan = [
        AnalysisSchemaField(field_name="pricing_model", display_name="Pricing"),
        AnalysisSchemaField(field_name="feature_tree", display_name="Feature Tree"),
    ]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name="alpha",
            fields=[
                AnalysisFieldResult(
                    field_name="pricing_model",
                    summary="pricing exists",
                    evidence_refs=["evd_1"],
                )
            ],
        )
    ]
    state.findings = [Finding(statement="alpha pricing exists", category="pricing", evidence_refs=["evd_1"])]
    store.save_state(state)
    service._qa = lambda _state: QAOutput.model_validate(  # type: ignore[method-assign]
        {
            "passed": False,
            "issues": [{"code": "feature_evidence_thin", "message": "need stronger feature evidence", "stage": "collect"}],
            "target_agent": "Collect",
            "collect_plan": {
                "enabled": True,
                "items": [{"competitor": "alpha", "field_name": "feature_tree", "reason": "evidence_thin", "query_list": ["a", "b"], "priority": 1}],
            },
        }
    )

    result = service._run_action_tool("run_qa", {}, {"run_id": state.run_id})
    saved = store.get_state(state.run_id)

    assert result["artifacts"]["qa_current_coverage"] == 0.5
    assert result["artifacts"]["qa_previous_coverage"] == 0.4
    assert result["artifacts"]["passed"] is True
    assert result["artifacts"]["issue_count"] == 0
    assert saved is not None
    assert saved.planner_meta["last_qa_passed"] is True
    assert "qa_collect_plan" not in saved.planner_meta


def test_draft_action_preserves_previous_qa_status(tmp_path) -> None:
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
    assert state.planner_meta["last_qa_checked"] is True
    assert state.planner_meta["last_qa_passed"] is True
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
