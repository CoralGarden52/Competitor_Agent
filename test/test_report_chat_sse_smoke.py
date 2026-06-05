#!/usr/bin/env python3
"""Report chat SSE/token streaming smoke test.

用途：
1. 不依赖真实 LLM，不打外网。
2. 真实走 FastAPI 接口：POST /runs/{run_id}/chat -> GET /runs/{run_id}/chat/{turn_id}/stream
3. 验证 report chat 是否会连续输出 chat_delta，并最终返回 chat_done。

运行：
    cd /home/wyz/Competitor_Agent
    python test/test_report_chat_sse_smoke.py

如果当前根目录 python 没装后端依赖，请改用：
    cd /home/wyz/Competitor_Agent/backend
    python ../test/test_report_chat_sse_smoke.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.models import Report, RunState
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService
from app.main import create_app


def build_service() -> CompetitorWorkflowService:
    db_dir = ROOT / "test" / "mock_data" / "report_chat_sse_smoke"
    db_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(db_dir / "report_chat_sse_smoke.db")
    return CompetitorWorkflowService(store)


def install_fakes(service: CompetitorWorkflowService) -> str:
    streamed_answer = "这是一次 SSE 流式输出测试。"

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, **kwargs):  # noqa: ARG001
        if trace_name == "report_conversation_web_collect_decision":
            return {"needs_web_collect": False, "queries": [], "reason": "report chunk is sufficient"}
        if trace_name == "report_conversation_memory_compact":
            return {
                "mid_summary": "用户询问报告追问流式输出，系统已逐段回答。",
                "next_work_memory": "如果用户继续追问，可继续基于当前报告回答。",
            }
        raise AssertionError(f"unexpected invoke_json trace_name: {trace_name}")

    def _fake_invoke_text_stream(*, trace_name, system_prompt, user_payload, metadata, **kwargs):  # noqa: ARG001
        if trace_name != "report_conversation_turn_stream":
            raise AssertionError(f"unexpected invoke_text_stream trace_name: {trace_name}")
        for char in streamed_answer:
            time.sleep(0.01)
            yield char

    service.agent_llm.invoke_json = _fake_invoke_json  # type: ignore[method-assign]
    service.agent_llm.invoke_text_stream = _fake_invoke_text_stream  # type: ignore[method-assign]
    return streamed_answer


def create_seed_run(service: CompetitorWorkflowService) -> RunState:
    state = RunState(
        industry="saas",
        competitors=["alpha"],
        report=Report(
            executive_summary="alpha summary",
            markdown="# Report\n\n## Pricing\nSeat based pricing.\n\n## Strengths\nEasy onboarding.",
        ),
        status="completed",
    )
    service.store.save_state(state)
    return state


def main() -> int:
    print("Report chat SSE/token streaming smoke test")
    print(f"workspace: {ROOT}")

    service = build_service()
    expected_answer = install_fakes(service)
    state = create_seed_run(service)

    app = create_app()
    from app.core.deps import get_service

    app.dependency_overrides[get_service] = lambda: service
    client = TestClient(app)

    create_resp = client.post(
        f"/runs/{state.run_id}/chat",
        json={
            "message": "请基于当前报告回答一下定价方式",
            "mode": "answer_only",
            "allow_web_collect": False,
            "auto_apply": False,
        },
    )
    if create_resp.status_code != 200:
        print(f"FAIL: create chat turn failed -> {create_resp.status_code} {create_resp.text}")
        return 1

    turn_payload = create_resp.json()
    turn_id = turn_payload["turn_id"]
    print(f"run_id: {state.run_id}")
    print(f"turn_id: {turn_id}")

    seen_events: list[str] = []
    streamed_text = ""
    snapshot_text = ""
    done_payload: dict | None = None

    with client.stream("GET", f"/runs/{state.run_id}/chat/{turn_id}/stream") as response:
        if response.status_code != 200:
            print(f"FAIL: stream open failed -> {response.status_code} {response.text}")
            return 1
        current_event = "message"
        for raw_line in response.iter_lines():
            line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="ignore")
            if not line:
                continue
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
                seen_events.append(current_event)
                continue
            if not line.startswith("data:"):
                continue
            data_text = line.split(":", 1)[1].strip()
            payload = json.loads(data_text)
            if current_event == "chat_delta":
                delta = str(payload.get("delta", "") or "")
                streamed_text += delta
                print(f"delta: {delta}")
            elif current_event == "chat_snapshot":
                snapshot = str(payload.get("assistant_answer", "") or "")
                if snapshot:
                    snapshot_text = snapshot
                    print(f"snapshot: {snapshot}")
            elif current_event == "chat_progress":
                print(f"progress: {payload.get('message', '')}")
            elif current_event == "chat_done":
                done_payload = payload
                print("done event received")
                break
            elif current_event == "chat_error":
                print(f"FAIL: chat_error -> {payload}")
                return 1

    turn_resp = client.get(f"/runs/{state.run_id}/chat/{turn_id}")
    if turn_resp.status_code != 200:
        print(f"FAIL: turn fetch failed -> {turn_resp.status_code} {turn_resp.text}")
        return 1
    result = turn_resp.json()

    failures: list[str] = []
    if "chat_bootstrap" not in seen_events:
        failures.append("missing chat_bootstrap")
    if "chat_delta" not in seen_events:
        failures.append("missing chat_delta")
    if "chat_done" not in seen_events:
        failures.append("missing chat_done")
    if not streamed_text:
        failures.append("streamed_text is empty")
    elif expected_answer.endswith(streamed_text) is False:
        failures.append(f"delta stream is not a suffix of expected answer: expected={expected_answer!r} actual={streamed_text!r}")
    if result.get("status") != "completed":
        failures.append(f"turn status not completed: {result.get('status')!r}")
    persisted_answer = str(result.get("assistant_answer", "") or "").strip()
    if expected_answer not in persisted_answer:
        failures.append("persisted assistant_answer does not contain streamed answer")
    if "本轮操作：" not in persisted_answer:
        failures.append("persisted assistant_answer missing formatted action summary")
    if done_payload is None or not isinstance(done_payload.get("result", None), dict):
        failures.append("chat_done payload missing final result")

    print("=" * 80)
    print("events:", seen_events)
    print("snapshot_text:", snapshot_text)
    print("streamed_answer:", streamed_text)
    print("persisted_answer:", result.get("assistant_answer", ""))
    print("actions_taken:", result.get("actions_taken", []))
    print("memory_snapshot_keys:", sorted((result.get("memory_snapshot") or {}).keys()))

    if failures:
        print("RESULT: FAIL")
        for item in failures:
            print("-", item)
        return 2

    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
