#!/usr/bin/env python
"""Run the current end-to-end competitor workflow via the public service entrypoint."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_config
from app.core.models import RunRequest
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService


DEFAULT_CASE = {
    "prompt": "进行在线会议软件领域的竞品分析",
    "industry": "general",
    "competitors": [],
    "competitor_hints": [],
    "aspect_hints": ["feature_tree", "pricing_model", "user_feedback"],
    "language": "zh-CN",
    "timeframe": "last_12_months",
    "output_dir": "complete_flow_result",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full workflow with the current manager/runtime path.")
    parser.add_argument("--prompt", default=DEFAULT_CASE["prompt"])
    parser.add_argument("--industry", default=DEFAULT_CASE["industry"])
    parser.add_argument("--competitors", nargs="*", default=DEFAULT_CASE["competitors"])
    parser.add_argument("--competitor-hints", nargs="*", default=DEFAULT_CASE["competitor_hints"])
    parser.add_argument("--aspect-hints", nargs="*", default=DEFAULT_CASE["aspect_hints"])
    parser.add_argument("--language", default=DEFAULT_CASE["language"])
    parser.add_argument("--timeframe", default=DEFAULT_CASE["timeframe"])
    parser.add_argument("--output-dir", default=DEFAULT_CASE["output_dir"])
    parser.add_argument("--skip-save", action="store_true")
    return parser.parse_args()


def _summarize_state(state: Any) -> dict[str, Any]:
    report_markdown = state.report.markdown if state.report else ""
    latest_decision = state.latest_decision.model_dump(mode="json") if state.latest_decision else None
    return {
        "run_id": state.run_id,
        "status": state.status,
        "industry": state.industry,
        "turn_count": state.turn_count,
        "attempt": state.attempt,
        "planned_competitors": state.planned_competitors,
        "schema_fields": [item.field_name for item in state.analysis_schema_plan],
        "evidence_count": len(state.evidences),
        "competitor_analysis_count": len(state.competitor_analyses),
        "profile_count": len(state.profiles),
        "finding_count": len(state.findings),
        "ticket_count": len(state.tickets),
        "has_report": bool(report_markdown.strip()),
        "report_length": len(report_markdown),
        "latest_decision": latest_decision,
        "last_action_result": state.last_action_result,
        "transition_reason": state.transition_reason.value if state.transition_reason else None,
        "recovery_state": state.recovery_state.value if state.recovery_state else None,
        "last_error": state.last_error,
    }


def _save_outputs(
    *,
    service: CompetitorWorkflowService,
    run_id: str,
    output_dir: Path,
    summary: dict[str, Any],
    report_markdown: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    summary_path = output_dir / f"full_flow_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    replay = service.replay_run(run_id)
    replay_path = output_dir / f"full_flow_replay_{stamp}.json"
    replay_path.write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")

    exported = service.export_run_logs(run_id)
    logs_path = output_dir / f"full_flow_logs_{stamp}.json"
    logs_path.write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")

    if report_markdown.strip():
        report_path = output_dir / f"full_flow_report_{stamp}.md"
        report_path.write_text(report_markdown, encoding="utf-8")

    print(f"summary saved to: {summary_path}")
    print(f"replay saved to:  {replay_path}")
    print(f"logs saved to:    {logs_path}")


def main() -> None:
    args = _parse_args()
    config = get_config()
    store = SQLiteStore(config.sqlite_path_obj)
    service = CompetitorWorkflowService(store)

    request = RunRequest(
        industry=args.industry,
        competitors=[item.strip() for item in args.competitors if item.strip()],
        user_prompt=args.prompt,
        competitor_hints=[item.strip() for item in args.competitor_hints if item.strip()],
        aspect_hints=[item.strip() for item in args.aspect_hints if item.strip()],
        language=args.language,
        timeframe=args.timeframe,
    )

    print("=" * 80)
    print("Current full workflow smoke")
    print("=" * 80)
    print(f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"sqlite_path: {config.sqlite_path_obj}")
    print(f"prompt: {request.user_prompt}")
    print(f"industry: {request.industry}")
    print(f"competitors: {request.competitors}")
    print(f"competitor_hints: {request.competitor_hints}")
    print(f"aspect_hints: {request.aspect_hints}")

    started_at = time.time()
    response = service.start_run(request)
    elapsed = time.time() - started_at

    state = response.state
    report_markdown = state.report.markdown if state.report else ""
    summary = _summarize_state(state)
    summary["elapsed_seconds"] = round(elapsed, 2)

    print()
    print("Result")
    print(f"status: {summary['status']}")
    print(f"run_id: {summary['run_id']}")
    print(f"elapsed: {elapsed:.2f}s")
    print(f"turn_count: {summary['turn_count']}")
    print(f"attempt: {summary['attempt']}")
    print(f"planned_competitors: {summary['planned_competitors']}")
    print(f"schema_fields: {summary['schema_fields']}")
    print(f"evidence_count: {summary['evidence_count']}")
    print(f"competitor_analysis_count: {summary['competitor_analysis_count']}")
    print(f"finding_count: {summary['finding_count']}")
    print(f"ticket_count: {summary['ticket_count']}")
    print(f"has_report: {summary['has_report']}")
    print(f"transition_reason: {summary['transition_reason']}")
    print(f"recovery_state: {summary['recovery_state']}")
    if summary["last_error"]:
        print(f"last_error: {summary['last_error']}")

    if not args.skip_save:
        output_dir = Path(__file__).parent / "mock_data" / args.output_dir
        _save_outputs(
            service=service,
            run_id=state.run_id,
            output_dir=output_dir,
            summary=summary,
            report_markdown=report_markdown,
        )


if __name__ == "__main__":
    main()
