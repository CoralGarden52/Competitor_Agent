#!/usr/bin/env python3
"""Draft markdown run-stream smoke test.

用途：
1. 不依赖真实 LLM，不打外网。
2. 用 mock data 直接触发 draft 阶段。
3. 验证 run 级 draft_markdown.* 事件是否连续产生，
   并且最终 workspace.report.markdown 会被正式报告覆盖。

运行：
    cd /home/wyz/Competitor_Agent
    python test/test_draft_markdown_stream_smoke.py

如果当前根目录 python 没装后端依赖，请改用：
    cd /home/wyz/Competitor_Agent/backend
    python ../test/test_draft_markdown_stream_smoke.py
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import types
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    import psycopg  # type: ignore # noqa: F401
except ModuleNotFoundError:
    psycopg_stub = types.ModuleType("psycopg")
    psycopg_rows_stub = types.ModuleType("psycopg.rows")

    def _missing_connect(*args, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("psycopg is not installed; this smoke test only supports SQLiteStore paths")

    psycopg_stub.connect = _missing_connect  # type: ignore[attr-defined]
    psycopg_rows_stub.dict_row = object()
    sys.modules["psycopg"] = psycopg_stub
    sys.modules["psycopg.rows"] = psycopg_rows_stub

from app.core.models import (  # noqa: E402
    AnalysisFieldResult,
    AnalysisSchemaField,
    CompetitorAnalysisRecord,
    DraftOutput,
    Evidence,
    Report,
    ReportSection,
    RunState,
    RunSummary,
    StageName,
    TaskResult,
)
from app.core.workflow import CompetitorWorkflowService  # noqa: E402


class _MemoryStore:
    def __init__(self) -> None:
        self.cache = None
        self._states: dict[str, RunState] = {}
        self._events: dict[str, list[dict]] = {}
        self._handoffs: dict[str, list[dict]] = {}
        self._next_event_id = 1

    def set_cache_backend(self, cache_backend) -> None:  # noqa: ANN001
        self.cache = cache_backend

    def save_state(self, state: RunState) -> None:
        self._states[state.run_id] = state.model_copy(deep=True)

    def get_state(self, run_id: str) -> RunState | None:
        state = self._states.get(run_id)
        return state.model_copy(deep=True) if state is not None else None

    def append_stage_event(self, run_id: str, stage: StageName, event_type: str, payload: dict) -> None:
        event = {
            "event_id": self._next_event_id,
            "run_id": run_id,
            "stage": stage.value,
            "event_type": event_type,
            "payload": payload.get("envelope", {}).get("payload", payload) if isinstance(payload, dict) else {},
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._next_event_id += 1
        self._events.setdefault(run_id, []).append(event)

    def append_event(self, event_record) -> None:  # noqa: ANN001
        stage = getattr(event_record, "stage", StageName.plan)
        payload = getattr(event_record, "payload", {}) or {}
        run_id = str(getattr(event_record, "run_id", "") or "")
        event_type = str(getattr(event_record, "event_type", "") or "")
        self.append_stage_event(run_id, stage, event_type, payload)

    def list_events(self, run_id: str, *, after_id: int = 0, limit: int | None = None) -> list[dict]:
        items = [item for item in self._events.get(run_id, []) if int(item.get("event_id", 0) or 0) > after_id]
        return items[:limit] if isinstance(limit, int) and limit > 0 else items

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        states = list(self._states.values())[:limit]
        output: list[RunSummary] = []
        for state in states:
            output.append(
                RunSummary(
                    run_id=state.run_id,
                    industry=state.industry,
                    status=state.status,
                    competitor_count=len(state.competitors),
                    user_prompt=state.user_prompt,
                    task_summary=state.task_summary,
                    created_at=state.evidences[0].captured_at if state.evidences else datetime.now(UTC),
                    updated_at=state.evidences[0].captured_at if state.evidences else datetime.now(UTC),
                )
            )
        return output

    def delete_run(self, run_id: str) -> bool:
        existed = run_id in self._states
        self._states.pop(run_id, None)
        self._events.pop(run_id, None)
        return existed

    def replay_timeline(self, run_id: str) -> list[dict]:
        return []

    def list_stage_handoffs(self, run_id: str) -> list[dict]:
        return list(self._handoffs.get(run_id, []))

    def list_llm_calls(self, run_id: str) -> list[dict]:
        return []

    def replay_node_io(self, run_id: str, stage: str) -> list[dict]:  # noqa: ARG002
        return []

    def save_stage_handoff(self, *, run_id: str, stage: StageName, attempt: int, handoff) -> None:  # noqa: ANN001
        self._handoffs.setdefault(run_id, []).append(
            {
                "run_id": run_id,
                "stage": stage.value,
                "attempt": attempt,
                "handoff_type": handoff.__class__.__name__,
                "payload": handoff.model_dump(mode="json"),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )


def build_service() -> CompetitorWorkflowService:
    service = CompetitorWorkflowService(_MemoryStore())

    def _workspace_payload(run_id: str) -> dict[str, object]:
        run = service.get_run(run_id)
        if run is None:
            return {"status": "not_found"}
        state = run.state
        return {
            "run": {
                "run_id": state.run_id,
                "status": state.status,
                "task_summary": state.task_summary,
                "current_stage": state.current_stage.value,
                "evidence_count": len(state.evidences),
                "finding_count": len(state.findings),
            },
            "workflow": {"agent_stages": []},
            "qa": {"issue_count": 0, "collect_items": []},
            "report": {"markdown": state.report.markdown if state.report else "", "sources": []},
            "observability": {"events": service.list_run_events(run_id)},
        }

    service.workspace_payload = _workspace_payload  # type: ignore[method-assign]
    return service


def install_fakes(service: CompetitorWorkflowService) -> tuple[str, str]:
    preview_markdown = (
        "# 竞品分析报告\n\n"
        "## 一、研究范围与目标\n"
        "这是流式预览版本。\n\n"
        "## 二、核心结论\n"
        "产品能力较完整。"
    )
    final_markdown = (
        "# 竞品分析报告\n\n"
        "## 一、研究范围与目标\n"
        "这是正式落库版本。\n\n"
        "## 二、核心结论\n"
        "产品能力较完整，建议继续补强定价证据。"
    )

    def _fake_run_markdown_stream(state: RunState, *, on_delta) -> str:  # noqa: ARG001
        for char in preview_markdown:
            time.sleep(0.005)
            on_delta(char)
        return preview_markdown

    def _fake_consume_task(task, state: RunState):  # noqa: ANN001
        report = Report(
            executive_summary="alpha summary",
            markdown=final_markdown,
            sections=[
                ReportSection(
                    section_id="background_goal",
                    title="一、研究范围与目标",
                    content_markdown="这是正式落库版本。",
                ),
                ReportSection(
                    section_id="conclusion_advice",
                    title="二、核心结论",
                    content_markdown="产品能力较完整，建议继续补强定价证据。",
                ),
            ],
            appendix_sources=["https://example.com/evidence"],
        )
        drafted = DraftOutput(report=report)
        task_result = TaskResult(
            task_id=task.task_id,
            run_id=task.run_id,
            owner_agent="WriterAgent",
            status="completed",
            summary="mock drafted report",
            output_payload={"section_count": 2, "report_ready": True},
            changed_fields=[],
            next_recommendations=["finalize_run"],
        )
        return task_result, drafted

    service.writer_agent.run_markdown_stream = _fake_run_markdown_stream  # type: ignore[method-assign]
    service.writer_agent.consume_task = _fake_consume_task  # type: ignore[method-assign]
    return preview_markdown, final_markdown


def create_seed_run(service: CompetitorWorkflowService) -> RunState:
    state = RunState(
        industry="saas",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="请生成一份 alpha 的竞品分析报告",
        task_summary="alpha 竞品分析",
        current_stage=StageName.draft,
        next_stage=StageName.draft,
        analysis_schema_plan=[
            AnalysisSchemaField(field_name="feature_tree", priority=1),
            AnalysisSchemaField(field_name="pricing_model", priority=2),
        ],
        competitor_analyses=[
            CompetitorAnalysisRecord(
                product_name="alpha",
                fields=[
                    AnalysisFieldResult(
                        field_name="feature_tree",
                        summary="支持会议、文档、IM 与协同流程。",
                        evidence_refs=["ev1"],
                        confidence=0.9,
                        normalized_value={},
                    ),
                    AnalysisFieldResult(
                        field_name="pricing_model",
                        summary="包含免费版与企业版。",
                        evidence_refs=["ev1"],
                        confidence=0.8,
                        normalized_value={},
                    ),
                ],
            )
        ],
        evidences=[
            Evidence(
                evidence_id="ev1",
                source_url="https://example.com/evidence",
                snippet="alpha 提供会议、文档与定价方案。",
            )
        ],
        status="running",
    )
    service.store.save_state(state)
    return state


def main() -> int:
    print("Draft markdown run-stream smoke test")
    print(f"workspace: {ROOT}")

    service = build_service()
    expected_preview, expected_final = install_fakes(service)
    state = create_seed_run(service)

    print(f"run_id: {state.run_id}")

    seen_event_types: list[str] = []
    streamed_markdown = ""
    failures: list[str] = []
    live_run = service.get_run(state.run_id)
    if live_run is None:
        print("FAIL: run disappeared before draft execution")
        return 1

    service._draft(live_run.state)

    events = service.list_run_events(state.run_id)
    for item in events:
        event_type = str(item.get("event_type", "") or "")
        seen_event_types.append(event_type)
        if event_type == "draft_markdown.delta":
            delta = str((item.get("payload") or {}).get("delta", "") or "")
            streamed_markdown += delta
            print(f"delta: {delta}")

    workspace_payload = service.workspace_payload(state.run_id)
    persisted_markdown = str((((workspace_payload.get("report") or {}) if isinstance(workspace_payload, dict) else {}).get("markdown", "")) or "")

    if "draft_markdown.started" not in seen_event_types:
        failures.append("missing draft_markdown.started")
    if "draft_markdown.delta" not in seen_event_types:
        failures.append("missing draft_markdown.delta")
    if "draft_markdown.completed" not in seen_event_types:
        failures.append("missing draft_markdown.completed")
    if streamed_markdown != expected_preview:
        failures.append(
            f"streamed markdown mismatch: expected={expected_preview!r} actual={streamed_markdown!r}"
        )
    if persisted_markdown != expected_final:
        failures.append(
            f"persisted workspace markdown mismatch: expected={expected_final!r} actual={persisted_markdown!r}"
        )
    if expected_preview == expected_final:
        failures.append("preview and final markdown should differ in this smoke test")

    print("=" * 80)
    print("event_types:", seen_event_types)
    print("streamed_markdown:", streamed_markdown)
    print("persisted_markdown:", persisted_markdown)

    if failures:
        print("RESULT: FAIL")
        for item in failures:
            print("-", item)
        return 2

    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
