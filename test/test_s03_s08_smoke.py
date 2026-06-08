#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_config
from app.core.models import RunRequest, StageName
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService


def main() -> None:
    config = get_config()
    store = SQLiteStore(config.sqlite_path_obj)
    service = CompetitorWorkflowService(store)

    # Build a minimal run state without triggering real network pipeline.
    state = service._initialize_run_state(  # noqa: SLF001
        request=RunRequest(
            industry="general",
            competitors=["A"],
            user_prompt="smoke",
            competitor_hints=[],
            aspect_hints=[],
            language="zh-CN",
            timeframe="last_12_months",
        )
    )
    service.mark_todo_stage_started(state, StageName.plan, "OrchestratorAgent")
    service.mark_todo_stage_completed(state, StageName.plan, "OrchestratorAgent")
    service._emit_hook(  # noqa: SLF001
        "after_stage",
        {
            "run_id": state.run_id,
            "attempt": state.attempt,
            "stage": "plan",
            "agent_name": "OrchestratorAgent",
            "payload": {"smoke": True},
        },
    )

    replay = service.replay_run(state.run_id)
    workspace = service.workspace_payload(state.run_id)
    exported = service.export_run_logs(state.run_id)

    assert "todo_plan" in replay
    assert "todo_events" in replay
    assert "hook_events" in replay
    assert "todo_plan" in workspace
    assert "todo_events" in (workspace.get("observability", {}) or {})
    assert "hook_events" in (workspace.get("observability", {}) or {})
    assert "todo_events" in exported
    assert "hook_events" in exported

    print("s03+s08 smoke passed")


if __name__ == "__main__":
    main()
