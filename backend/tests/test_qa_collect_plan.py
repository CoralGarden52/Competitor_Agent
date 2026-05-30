from __future__ import annotations

import pytest

from app.core.models import (
    AnalysisFieldResult,
    AnalysisSchemaField,
    CollectOutput,
    CompetitorAnalysisRecord,
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
            global_notes="pricing evidence strengthen",
            items=[
                QACollectPlanItem(
                    competitor="alpha",
                    field_name="pricing_model",
                    reason="report contains unknown pricing details",
                    query_list=["alpha pricing official", "alpha pricing plans official"],
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
                    query_list=["alpha pricing official", "alpha enterprise billing rules"],
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
                "query_list": ["alpha pricing official", "alpha pricing plans official"],
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
    assert captured["field_query_overrides"]["alpha::pricing_model"] == ["alpha pricing official", "alpha pricing plans official"]
    assert state.planner_meta["qa_reanalyze_targets"] == {"alpha": ["pricing_model"]}
    assert "qa_collect_plan" not in state.planner_meta


def test_consume_qa_collect_plan_returns_reanalyze_targets(tmp_path) -> None:
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
                "query_list": ["alpha pricing", "alpha pricing plans official"],
                "priority": 1,
            },
            {
                "competitor": "alpha",
                "field_name": "feature_tree",
                "reason": "weak evidence",
                "query_list": ["alpha feature", "alpha product capabilities"],
                "priority": 2,
            },
        ],
    }

    consumed = service._consume_qa_collect_plan(state)
    assert consumed is not None
    assert consumed["target_competitors"] == ["alpha"]
    assert consumed["field_query_overrides"]["alpha::pricing_model"] == ["alpha pricing", "alpha pricing plans official"]
    assert consumed["reanalyze_targets"] == {"alpha": ["pricing_model", "feature_tree"]}


def test_analyze_incremental_only_recomputes_target_fields(tmp_path) -> None:
    from app.core.models import AnalysisFieldResult, AnalysisSchemaField, CompetitorAnalysisRecord

    store = SQLiteStore(tmp_path / "test.db")
    service = CompetitorWorkflowService(store)
    state = RunState(
        industry="saas",
        competitors=["alpha", "beta"],
        planned_competitors=["alpha", "beta"],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name="feature_tree", priority=1),
            AnalysisSchemaField(field_name="pricing_model", priority=2),
        ],
    )
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name="alpha",
            fields=[
                AnalysisFieldResult(field_name="feature_tree", summary="OLD-alpha-feature_tree", evidence_refs=["ev1"], confidence=0.6, normalized_value={}, evidence_gaps=[]),
                AnalysisFieldResult(field_name="pricing_model", summary="OLD-alpha-pricing_model", evidence_refs=["ev2"], confidence=0.6, normalized_value={}, evidence_gaps=[]),
            ],
        ),
        CompetitorAnalysisRecord(
            product_name="beta",
            fields=[
                AnalysisFieldResult(field_name="feature_tree", summary="OLD-beta-feature_tree", evidence_refs=["ev3"], confidence=0.6, normalized_value={}, evidence_gaps=[]),
                AnalysisFieldResult(field_name="pricing_model", summary="OLD-beta-pricing_model", evidence_refs=["ev4"], confidence=0.6, normalized_value={}, evidence_gaps=[]),
            ],
        ),
    ]
    state.planner_meta["qa_reanalyze_targets"] = {"alpha": ["pricing_model"]}

    captured: dict = {}

    def _fake_run_llm(run_state, *, reanalyze_targets=None, previous_records=None):
        captured["reanalyze_targets"] = reanalyze_targets
        captured["previous_records"] = previous_records
        return service.analyst_agent.run_fallback(
            RunState(
                industry=run_state.industry,
                competitors=run_state.competitors,
                planned_competitors=run_state.planned_competitors,
                analysis_schema_plan=run_state.analysis_schema_plan,
                evidences=run_state.evidences,
            )
        )

    service.analyst_agent.run_llm = _fake_run_llm  # type: ignore[method-assign]
    service._analyze(state)

    assert captured["reanalyze_targets"] == {"alpha": {"pricing_model"}}
    assert captured["previous_records"] is not None
    assert "qa_reanalyze_targets" not in state.planner_meta


def test_calc_analyze_coverage_summary_only_rule(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    service = CompetitorWorkflowService(store)
    state = RunState(
        industry="saas",
        competitors=["alpha", "beta"],
        planned_competitors=["alpha", "beta"],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name="feature_tree", priority=1),
            AnalysisSchemaField(field_name="pricing_model", priority=2),
        ],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name="alpha",
                fields=[
                    AnalysisFieldResult(field_name="feature_tree", summary="known", evidence_refs=[], confidence=0.7, normalized_value={}, evidence_gaps=[]),
                    AnalysisFieldResult(field_name="pricing_model", summary="unknown", evidence_refs=[], confidence=0.7, normalized_value={}, evidence_gaps=[]),
                ],
            ),
            CompetitorAnalysisRecord(
                product_name="beta",
                fields=[
                    AnalysisFieldResult(field_name="feature_tree", summary="none", evidence_refs=[], confidence=0.7, normalized_value={}, evidence_gaps=[]),
                    AnalysisFieldResult(field_name="pricing_model", summary="  value ", evidence_refs=[], confidence=0.7, normalized_value={}, evidence_gaps=[]),
                ],
            ),
        ],
    )
    stats = service._calc_analyze_coverage(state)
    assert stats["total_units"] == 4
    assert stats["passed_units"] == 2
    assert abs(float(stats["coverage"]) - 0.5) < 1e-9
