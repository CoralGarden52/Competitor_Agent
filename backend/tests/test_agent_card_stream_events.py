from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import psycopg  # type: ignore # noqa: F401
except ModuleNotFoundError:
    psycopg_stub = types.ModuleType("psycopg")
    psycopg_rows_stub = types.ModuleType("psycopg.rows")
    psycopg_conninfo_stub = types.SimpleNamespace(
        conninfo_to_dict=lambda conninfo: {"dbname": "test"},
        make_conninfo=lambda **kwargs: "postgresql://stub",
    )

    def _missing_connect(*args, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("psycopg is not installed; postgres paths are unavailable in this test")

    psycopg_stub.connect = _missing_connect  # type: ignore[attr-defined]
    psycopg_stub.conninfo = psycopg_conninfo_stub  # type: ignore[attr-defined]
    psycopg_rows_stub.dict_row = object()
    sys.modules["psycopg"] = psycopg_stub
    sys.modules["psycopg.rows"] = psycopg_rows_stub

from app.core.models import (
    AnalysisFieldResult,
    AnalysisSchemaField,
    CollectOutput,
    CompetitorAnalysisRecord,
    CompetitorProfile,
    FeatureNode,
    FeedbackSummary,
    Finding,
    PricingModel,
    RawEvidence,
    RunState,
    TaskResult,
)
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService


def _event_payload(events: list[dict], event_type: str) -> dict:
    event = next(item for item in events if item["event_type"] == event_type)
    payload = event.get("payload", {})
    envelope = payload.get("envelope", {}) if isinstance(payload, dict) else {}
    if isinstance(envelope, dict) and isinstance(envelope.get("payload"), dict):
        return envelope["payload"]
    snapshot = payload.get("snapshot", {}) if isinstance(payload, dict) else {}
    return snapshot.get("output_payload", {}) if isinstance(snapshot, dict) else {}


def test_plan_emits_card_stream_events(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "plan_card.db"))
    state = RunState(industry="general", competitors=["alpha"], user_prompt="test", target_product="target")

    service.orchestrator.generate_dynamic_plan = lambda **_kwargs: {  # type: ignore[method-assign]
        "inferred_industry": "general",
        "target_product": "target",
        "planned_competitors": ["alpha", "beta"],
        "analysis_schema_plan": [
            {"field_name": "feature_tree", "query_templates": [], "recommended_sources": ["official"], "priority": 1},
            {"field_name": "user_feedback", "query_templates": [], "recommended_sources": ["community"], "priority": 2},
        ],
        "planner_meta": {},
        "candidate_groups": {},
        "comparison_search_plan": {},
        "comparison_corpus": [],
        "comparison_decision_evidence_refs": [],
    }

    service._plan(state)
    events = service.list_run_events(state.run_id)

    competitors_payload = _event_payload(events, "plan.card.competitors_stream")
    schema_payload = _event_payload(events, "plan.card.schema_stream")

    assert competitors_payload["planned_competitors"] == ["alpha", "beta"]
    assert competitors_payload["analysis_subjects"][0]["name"] == "target"
    assert "alpha" in competitors_payload["display_text"]
    assert "target" in competitors_payload["display_text"]
    assert schema_payload["schema_field_labels"]["feature_tree"] == "功能树"
    assert schema_payload["schema_field_labels"]["user_feedback"] == "用户反馈"


def test_collect_emits_source_found_card_events(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "collect_card.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="test",
        analysis_schema_plan=[AnalysisSchemaField(field_name="feature_tree")],
    )

    def _fake_consume_task(_task, _state):
        return (
            TaskResult(task_id="t1", run_id=state.run_id, owner_agent="CollectorAgent", status="completed"),
            CollectOutput(
                provider_events=[
                    {
                        "event_type": "collector.fetch.scheduled",
                        "field_name": "feature_tree",
                        "title": "Alpha feature overview",
                        "url": "https://example.com/alpha/features",
                        "source_provider": "tavily",
                    },
                    {
                        "event_type": "collector.fetch.scheduled",
                        "field_name": "feature_tree",
                        "title": "Alpha feature overview",
                        "url": "https://example.com/alpha/features",
                        "source_provider": "tavily",
                    },
                ],
                raw_evidences=[
                    RawEvidence(
                        source_url="https://example.com/alpha/features",
                        title="Alpha feature overview",
                        snippet="feature summary",
                        domain_extensions={"competitor": "alpha", "schema_field": "feature_tree"},
                    )
                ],
            ),
        )

    service.collector_agent.consume_task = _fake_consume_task  # type: ignore[method-assign]
    service._collect(state)
    events = service.list_run_events(state.run_id)
    source_events = [item for item in events if item["event_type"] == "collect.card.source_found"]

    assert len(source_events) == 1
    payload = _event_payload(events, "collect.card.source_found")
    assert payload["field_label"] == "功能树"
    assert payload["source_url"] == "https://example.com/alpha/features"
    assert payload["total_found"] == 1


def test_analyze_emits_field_summary_card_events(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "analyze_card.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="test",
        analysis_schema_plan=[AnalysisSchemaField(field_name="strengths")],
    )

    def _fake_consume_task(_task, _state, *, progress_callback=None):
        result = AnalysisFieldResult(
            field_name="strengths",
            summary="Alpha 在易用性和稳定性上表现突出",
            evidence_refs=["ev1"],
            confidence=0.88,
        )
        if progress_callback is not None:
            progress_callback("alpha", result)
        record = CompetitorAnalysisRecord(product_name="alpha", fields=[result])
        profile = CompetitorProfile(
            industry="general",
            product_name="alpha",
            positioning="meeting",
            feature_tree=[FeatureNode(name="core", capability="video")],
            advantages=["easy"],
            disadvantages=["pricing"],
            pricing_model=PricingModel(model_type="subscription", free_tier=True),
            user_feedback=FeedbackSummary(),
            evidence_refs=["ev1"],
        )
        finding = Finding(statement="alpha strengths: stable", category="feature", evidence_refs=["ev1"])
        return (
            TaskResult(task_id="t2", run_id=state.run_id, owner_agent="AnalystAgent", status="completed"),
            type("AnalyzeOut", (), {"competitors": [record], "profiles": [profile], "findings": [finding]})(),
        )

    service.analyst_agent.consume_task = _fake_consume_task  # type: ignore[method-assign]
    service._analyze(state)
    events = service.list_run_events(state.run_id)

    field_payload = _event_payload(events, "analyze.card.field_summary")
    competitor_payload = _event_payload(events, "analyze.card.competitor_summary")

    assert field_payload["field_label"] == "优势"
    assert "易用性" in field_payload["summary"]
    assert competitor_payload["competitor"] == "alpha"
    assert competitor_payload["summary_lines"]


def test_analyze_card_summary_is_summarized_not_point_prefix(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "analyze_card_compact.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="test",
        analysis_schema_plan=[AnalysisSchemaField(field_name="weaknesses")],
    )

    def _fake_consume_task(_task, _state, *, progress_callback=None):
        result = AnalysisFieldResult(
            field_name="weaknesses",
            summary="1. 免费版限制严格；2. 国内访问与合规适配不足；3. 更新频繁且兼容性波动明显。",
            evidence_refs=["ev1"],
            confidence=0.88,
        )
        if progress_callback is not None:
            progress_callback("alpha", result)
        record = CompetitorAnalysisRecord(product_name="alpha", fields=[result])
        return (
            TaskResult(task_id="t2", run_id=state.run_id, owner_agent="AnalystAgent", status="completed"),
            type("AnalyzeOut", (), {"competitors": [record], "profiles": [], "findings": []})(),
        )

    service.analyst_agent.consume_task = _fake_consume_task  # type: ignore[method-assign]
    service._analyze(state)
    events = service.list_run_events(state.run_id)

    field_payload = _event_payload(events, "analyze.card.field_summary")
    competitor_payload = _event_payload(events, "analyze.card.competitor_summary")

    assert not field_payload["summary"].startswith("3点：")
    assert "免费版限制严格" in field_payload["summary"]
    assert "合规适配不足" in field_payload["summary"]
    assert competitor_payload["summary_lines"] == [f'劣势：{field_payload["summary"]}']


def test_analyze_card_summary_strips_evidence_preface(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "analyze_card_clean.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="test",
        analysis_schema_plan=[AnalysisSchemaField(field_name="feature_tree")],
    )

    def _fake_consume_task(_task, _state, *, progress_callback=None):
        result = AnalysisFieldResult(
            field_name="feature_tree",
            summary="基于当前有限公开证据，金山文档依托 WPS 产品体系，支持协作编辑、云端同步和权限管控。",
            evidence_refs=["ev1"],
            confidence=0.88,
        )
        if progress_callback is not None:
            progress_callback("alpha", result)
        record = CompetitorAnalysisRecord(product_name="alpha", fields=[result])
        return (
            TaskResult(task_id="t2", run_id=state.run_id, owner_agent="AnalystAgent", status="completed"),
            type("AnalyzeOut", (), {"competitors": [record], "profiles": [], "findings": []})(),
        )

    service.analyst_agent.consume_task = _fake_consume_task  # type: ignore[method-assign]
    service._analyze(state)
    events = service.list_run_events(state.run_id)

    field_payload = _event_payload(events, "analyze.card.field_summary")

    assert "基于当前有限公开证据" not in field_payload["summary"]
    assert "WPS" in field_payload["summary"]


def test_qa_emits_review_summary_card_events(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / "qa_card.db"))
    state = RunState(
        industry="general",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="test",
        analysis_schema_plan=[AnalysisSchemaField(field_name="weaknesses")],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name="alpha",
                fields=[AnalysisFieldResult(field_name="weaknesses", summary="unknown", evidence_refs=[], confidence=0.2)],
            )
        ],
    )

    service.qa_critic_agent.run_competitor_analysis_review_llm = lambda **_kwargs: {  # type: ignore[method-assign]
        "needs_recollect": True,
        "insufficient_fields": [{"field_name": "weaknesses", "reason": "empty", "priority": 1}],
        "collect_plan": {
            "items": [
                {
                    "competitor": "alpha",
                    "field_name": "weaknesses",
                    "reason": "empty",
                    "query_list": ["alpha weaknesses review", "alpha weaknesses feedback"],
                    "priority": 1,
                }
            ]
        },
    }

    result = service._qa(state)
    events = service.list_run_events(state.run_id)

    review_payload = _event_payload(events, "qa.card.review_summary")
    final_payload = _event_payload(events, "qa.card.final_summary")

    assert result.passed is False
    assert review_payload["competitor"] == "alpha"
    assert review_payload["needs_recollect"] is True
    assert review_payload["field_reviews"][0]["field_name"] == "weaknesses"
    assert review_payload["field_reviews"][0]["before_summary"] == "unknown"
    assert "劣势" in review_payload["summary_text"]
    assert final_payload["passed"] is False
    assert final_payload["collect_items"][0]["field_name"] == "weaknesses"
