from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time
import types

import pytest

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

from app.core.models import (  # noqa: E402
    AnalysisFieldResult,
    AnalysisSchemaField,
    CompetitorAnalysisRecord,
    Evidence,
    LLMCallTrace,
    RunState,
    RunSummary,
    StageName,
)
from app.core.workflow import CompetitorWorkflowService  # noqa: E402


class _MemoryStore:
    def __init__(self) -> None:
        self.cache = None
        self._states: dict[str, RunState] = {}
        self._events: dict[str, list[dict]] = {}
        self._handoffs: dict[str, list[dict]] = {}
        self._llm_calls: dict[str, list[dict]] = {}
        self._timeline: dict[str, list[dict]] = {}
        self._node_inputs: dict[tuple[str, str], list[dict]] = {}
        self._next_event_id = 1
        self._next_trace_id = 1

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
        return [
            RunSummary(
                run_id=state.run_id,
                industry=state.industry,
                status=state.status,
                competitor_count=len(state.competitors),
                user_prompt=state.user_prompt,
                task_summary=state.task_summary,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            for state in states
        ]

    def delete_run(self, run_id: str) -> bool:
        existed = run_id in self._states
        self._states.pop(run_id, None)
        self._events.pop(run_id, None)
        return existed

    def replay_timeline(self, run_id: str) -> list[dict]:
        return list(self._timeline.get(run_id, []))

    def list_stage_handoffs(self, run_id: str, stage: str | None = None) -> list[dict]:
        items = list(self._handoffs.get(run_id, []))
        if stage:
            items = [item for item in items if str(item.get("stage", "")) == stage]
        return items

    def list_llm_calls(self, run_id: str, node_name: str | None = None) -> list[dict]:
        items = list(self._llm_calls.get(run_id, []))
        if node_name:
            items = [item for item in items if str(item.get("node_name", "")) == node_name]
        return items

    def replay_node_io(self, run_id: str, stage: str) -> list[dict]:
        return list(self._node_inputs.get((run_id, stage), []))

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

    def list_manual_interventions(self, run_id: str) -> list[dict]:  # noqa: ARG002
        return []

    def get_or_create_conversation(self, run_id: str) -> dict[str, object]:
        return {
            "conversation_id": f"conv_{run_id}",
            "run_id": run_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def list_conversation_messages(self, *, run_id: str, conversation_id: str) -> list[dict]:  # noqa: ARG002
        return []

    def list_conversation_turns(self, *, run_id: str, conversation_id: str) -> list[dict]:  # noqa: ARG002
        return []

    def get_conversation_memory(self, conversation_id: str) -> dict[str, object]:  # noqa: ARG002
        return {}

    def list_report_revisions(self, *, run_id: str, conversation_id: str) -> list[dict]:  # noqa: ARG002
        return []

    def trace_node_started(self, *, run_id: str, node_name: str, attempt: int) -> int:
        trace_id = self._next_trace_id
        self._next_trace_id += 1
        self._timeline.setdefault(run_id, []).append(
            {
                "trace_id": trace_id,
                "node_name": node_name,
                "attempt": attempt,
                "status": "running",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "ended_at": None,
                "duration_ms": None,
            }
        )
        return trace_id

    def trace_node_completed(self, *, trace_id: int, run_id: str, node_name: str, output_payload: dict[str, object]) -> None:
        for item in self._timeline.get(run_id, []):
            if int(item.get("trace_id", 0) or 0) == trace_id:
                item["status"] = "completed"
                item["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                item["duration_ms"] = int(item.get("duration_ms") or 320)
                break
        self._node_inputs.setdefault((run_id, node_name), []).append(
            {
                "node_name": node_name,
                "io_type": "output",
                "payload": output_payload,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )

    def save_llm_call(self, trace: LLMCallTrace) -> None:
        self._llm_calls.setdefault(trace.run_id, []).append(
            {
                "trace_id": trace.trace_id,
                "run_id": trace.run_id,
                "attempt": trace.attempt,
                "node_name": trace.node_name,
                "agent_name": trace.agent_name,
                "trace_name": trace.trace_name,
                "model": trace.model,
                "status": trace.status,
                "system_prompt": trace.system_prompt,
                "user_payload": json.loads(json.dumps(trace.user_payload, ensure_ascii=False)),
                "raw_response": json.loads(json.dumps(trace.raw_response, ensure_ascii=False)),
                "parsed_response": json.loads(json.dumps(trace.parsed_response, ensure_ascii=False)),
                "error_reason": trace.error_reason,
                "error_message": trace.error_message,
                "finish_reason": trace.finish_reason,
                "latency_ms": trace.latency_ms,
                "prompt_tokens": trace.prompt_tokens,
                "completion_tokens": trace.completion_tokens,
                "total_tokens": trace.total_tokens,
                "created_at": trace.created_at.isoformat(),
            }
        )


def _build_state() -> RunState:
    captured_at = datetime.now(UTC)
    return RunState(
        industry="saas",
        competitors=["alpha"],
        planned_competitors=["alpha"],
        user_prompt="请生成 alpha 的竞品分析报告",
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
                source_url="https://example.com/alpha",
                title="alpha evidence",
                snippet="evidence snippet",
                captured_at=captured_at,
            )
        ],
    )


def test_draft_stream_emits_structured_report_events_and_persists_blocks(tmp_path) -> None:
    _ = tmp_path
    service = CompetitorWorkflowService(_MemoryStore())
    state = _build_state()
    service.store.save_state(state)

    service._draft(state)
    service.store.save_state(state)

    events = service.list_run_events(state.run_id)
    event_types = [str(item.get("event_type", "")) for item in events]
    started_event = next(item for item in events if item.get("event_type") == "draft_report.started")
    completed_event = next(item for item in events if item.get("event_type") == "draft_report.completed")
    first_block_event = next(item for item in events if item.get("event_type") == "draft_report.block_completed")
    draft_completed_event = next(item for item in events if item.get("event_type") == "draft.completed")

    assert "draft_report.started" in event_types
    assert "draft_report.block_delta" in event_types
    assert "draft_report.block_completed" in event_types
    assert "draft_markdown.delta" in event_types
    assert started_event["payload"]["status"] == "running"
    assert int(completed_event["payload"]["block_count"]) >= 4
    assert first_block_event["payload"]["block"]["block_id"]
    assert draft_completed_event["payload"]["block_count"] >= 4
    assert state.report is not None
    assert state.report.blocks
    assert state.report.citations
    assert state.report.markdown.strip()
    assert state.report.html.strip()
    assert state.status == "running"


def test_draft_stream_build_failure_marks_run_failed(tmp_path) -> None:
    _ = tmp_path
    service = CompetitorWorkflowService(_MemoryStore())
    state = _build_state()

    def _boom(_state: RunState):  # noqa: ANN001, ARG001
        raise RuntimeError("structured report build failed")

    service.writer_agent.build_streamable_report = _boom  # type: ignore[method-assign]
    service.store.save_state(state)

    with pytest.raises(Exception):
        service._draft(state)

    events = service.list_run_events(state.run_id)
    failed_event = next(item for item in events if item.get("event_type") == "draft_report.failed")
    assert failed_event["payload"]["terminal"] is True
    assert state.status == "failed"


def test_streamable_report_uses_block_citations_as_single_provenance_source(tmp_path) -> None:
    _ = tmp_path
    service = CompetitorWorkflowService(_MemoryStore())
    state = _build_state()

    report = service.writer_agent.build_streamable_report(state).report
    assert report.blocks

    section_blocks = [block for block in report.blocks if block.block_type in {"section_paragraph", "section_bullets"}]
    assert section_blocks
    for block in section_blocks:
        if isinstance(block.content, list):
            assert all("溯源：" not in str(item) and "来源：" not in str(item) for item in block.content)
        else:
            assert "溯源：" not in str(block.content)
            assert "来源：" not in str(block.content)

    markdown = report.markdown
    blocks_with_citations = [block for block in report.blocks if block.block_type != "reference_list" and block.citations]
    assert markdown.count("溯源：") == len(blocks_with_citations)
    assert "- 溯源：" not in markdown
    assert "  - 溯源：" not in markdown


def test_workspace_payload_exposes_report_blocks_and_prefers_draft_llm_latency(tmp_path) -> None:
    _ = tmp_path
    service = CompetitorWorkflowService(_MemoryStore())
    state = _build_state()
    service.store.save_state(state)
    trace_id = service.store.trace_node_started(run_id=state.run_id, node_name="draft", attempt=state.attempt)
    service.store.trace_node_completed(trace_id=trace_id, run_id=state.run_id, node_name="draft", output_payload={})
    service.store.save_llm_call(
        LLMCallTrace(
            run_id=state.run_id,
            attempt=state.attempt,
            node_name="draft",
            agent_name="WriterAgent",
            trace_name="agent.draft.generate_markdown_stream",
            system_prompt="",
            user_payload={},
            raw_response={},
            parsed_response={"text": "# report"},
            status="completed",
            model="test-model",
            latency_ms=789000,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )
    )

    workspace = service.workspace_payload(state.run_id)
    draft_stage = next(item for item in workspace["workflow"]["agent_stages"] if item["stage"] == "draft")

    assert draft_stage["duration_ms"] == 789000
    assert workspace["report"]["blocks"] == []
