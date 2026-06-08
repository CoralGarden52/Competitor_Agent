#!/usr/bin/env python3
"""真实 LLM Manager 决策 smoke test。

用途：
1. 不跑主流程，不抓网页。
2. 直接构造几种正常运行状态，调用真实的 ManagerAgent 决策。
3. 检查 action 是否落在预期的正常路径上。

运行：
    cd /home/wyz/Competitor_Agent
    python test/test_manager_agent_live_smoke.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_config
from app.core.models import AnalysisFieldResult, AnalysisSchemaField, CompetitorAnalysisRecord, Finding, RawEvidence, Report, ReportSection, RunState
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService


@dataclass
class LiveCase:
    name: str
    expected_stage_path: str
    state: RunState
    expected_actions: set[str]


def build_service() -> CompetitorWorkflowService:
    config = get_config()
    api_key = str(getattr(config, "openai_api_key", "") or "").strip()
    base_url = str(getattr(config, "openai_base_url", "") or "").strip()
    model = str(getattr(config, "openai_model", "") or "").strip()
    if not api_key or not base_url or not model:
        raise RuntimeError("LLM 未启用，请先配置 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL")
    db_dir = ROOT / "test" / "mock_data" / "manager_live_smoke"
    db_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db_dir / "manager_live_smoke.db")
    return CompetitorWorkflowService(store)


def build_cases() -> list[LiveCase]:
    base_evidences = [RawEvidence(source_url="https://example.com/pricing", snippet="alpha pricing page")]

    plan_case = RunState(
        industry="saas",
        competitors=["alpha"],
        user_prompt="帮我分析 alpha 的竞品情况",
    )

    analyze_case = RunState(
        industry="saas",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="帮我分析 alpha 的竞品情况",
        analysis_schema_plan=[AnalysisSchemaField(field_name="pricing_model")],
        evidences=list(base_evidences),
    )

    draft_case = RunState(
        industry="saas",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="帮我分析 alpha 的竞品情况",
        analysis_schema_plan=[AnalysisSchemaField(field_name="pricing_model")],
        evidences=list(base_evidences),
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name="alpha",
                fields=[AnalysisFieldResult(field_name="pricing_model", summary="tiered pricing", evidence_refs=["evd_1"])],
            )
        ],
        findings=[Finding(statement="alpha uses tiered pricing", category="pricing", evidence_refs=["evd_1"])],
    )

    qa_case = RunState(
        industry="saas",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="帮我分析 alpha 的竞品情况",
        analysis_schema_plan=[AnalysisSchemaField(field_name="pricing_model")],
        evidences=list(base_evidences),
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name="alpha",
                fields=[AnalysisFieldResult(field_name="pricing_model", summary="tiered pricing", evidence_refs=["evd_1"])],
            )
        ],
        findings=[Finding(statement="alpha uses tiered pricing", category="pricing", evidence_refs=["evd_1"])],
        report=Report(
            executive_summary="alpha pricing summary",
            sections=[
                ReportSection(
                    section_id="pricing_strategy",
                    title="Pricing",
                    field_name="pricing_model",
                    content_markdown="alpha uses tiered pricing",
                )
            ],
            markdown="# Report\n\nalpha uses tiered pricing",
            html="<h1>Report</h1><p>alpha uses tiered pricing</p>",
        ),
    )

    finalize_case = RunState(
        industry="saas",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="帮我分析 alpha 的竞品情况",
        attempt=2,
        analysis_schema_plan=[AnalysisSchemaField(field_name="pricing_model")],
        evidences=list(base_evidences),
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name="alpha",
                fields=[AnalysisFieldResult(field_name="pricing_model", summary="tiered pricing", evidence_refs=["evd_1"])],
            )
        ],
        findings=[Finding(statement="alpha uses tiered pricing", category="pricing", evidence_refs=["evd_1"])],
        report=Report(
            executive_summary="alpha pricing summary",
            sections=[
                ReportSection(
                    section_id="pricing_strategy",
                    title="Pricing",
                    field_name="pricing_model",
                    content_markdown="alpha uses tiered pricing",
                )
            ],
            markdown="# Report\n\nalpha uses tiered pricing",
            html="<h1>Report</h1><p>alpha uses tiered pricing</p>",
        ),
    )

    return [
        LiveCase(
            name="fresh_run_should_plan",
            expected_stage_path="plan missing -> plan_scope",
            state=plan_case,
            expected_actions={"plan_scope"},
        ),
        LiveCase(
            name="plan_ready_should_analyze_after_collect",
            expected_stage_path="plan ready + collect artifact exists -> reanalyze_targets",
            state=analyze_case,
            expected_actions={"reanalyze_targets"},
        ),
        LiveCase(
            name="analyze_ready_should_draft",
            expected_stage_path="analyze artifact exists + report missing -> redraft_report",
            state=draft_case,
            expected_actions={"redraft_report"},
        ),
        LiveCase(
            name="report_ready_should_run_qa",
            expected_stage_path="report artifact exists + qa pending -> run_qa",
            state=qa_case,
            expected_actions={"run_qa"},
        ),
        LiveCase(
            name="qa_ready_should_finalize",
            expected_stage_path="report ready + qa ready -> finalize_run",
            state=finalize_case,
            expected_actions={"finalize_run", "run_qa"},
        ),
    ]


def classify_decision_source(decision) -> str:
    metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
    if metadata.get("guard_rewritten", False):
        return "guard_rewritten"
    if metadata.get("fallback", False):
        return "fallback"
    return "llm_direct"


def run_case(service: CompetitorWorkflowService, case: LiveCase) -> bool:
    service.store.save_state(case.state)
    context = service._build_decision_context(case.state)
    decision = service._manager_decide(case.state)
    ok = decision.action_type.value in case.expected_actions
    decision_source = classify_decision_source(decision)
    print("=" * 80)
    print(f"CASE: {case.name}")
    print(f"expected_stage_path: {case.expected_stage_path}")
    print(f"expected_actions: {sorted(case.expected_actions)}")
    print(f"actual_action: {decision.action_type.value}")
    print(f"decision_source: {decision_source}")
    print(f"target_agent: {decision.target_agent}")
    print(f"reason: {decision.reason}")
    print(f"decision_basis: {decision.decision_basis}")
    print(f"rejected_actions: {decision.rejected_actions}")
    print(f"confidence: {decision.confidence}")
    print(
        "context_summary:",
        {
            "plan_ready": context.plan_ready,
            "collect_ready": context.collect_ready,
            "analyze_ready": context.analyze_ready,
            "draft_ready": context.draft_ready,
            "qa_ready": context.qa_ready,
            "report_ready": context.report_ready,
            "evidence_count": context.evidence_count,
            "finding_count": context.finding_count,
        },
    )
    if decision_source != "llm_direct":
        print(f"decision_metadata: {decision.metadata}")
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    print("Manager Agent 真实 LLM 决策 smoke test")
    print(f"workspace: {ROOT}")
    print(f"date: {os.popen('date').read().strip()}")
    try:
        service = build_service()
    except Exception as exc:  # noqa: BLE001
        print(f"无法启动 live smoke test: {exc}")
        return 1

    passed = 0
    cases = build_cases()
    for case in cases:
        try:
            if run_case(service, case):
                passed += 1
        except Exception as exc:  # noqa: BLE001
            print("=" * 80)
            print(f"CASE: {case.name}")
            print(f"RESULT: ERROR - {exc}")

    print("=" * 80)
    print(f"SUMMARY: {passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 2


if __name__ == "__main__":
    raise SystemExit(main())
