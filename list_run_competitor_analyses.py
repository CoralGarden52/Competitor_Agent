#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).parent
DEFAULT_ANALYST_DIR = BASE_DIR / "mock_data" / "complete_flow_result" / "analyst_output"
DEFAULT_META = BASE_DIR / "mock_data" / "complete_flow_result" / "complete_flow_result.json"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="List competitor analysis json files for a specific run_id")
    p.add_argument("--run-id", type=str, required=True, help="target run_id, e.g. run_0e196845b6c6")
    p.add_argument("--analyst-dir", type=str, default=str(DEFAULT_ANALYST_DIR), help="analyst_output directory")
    p.add_argument(
        "--meta-json",
        type=str,
        default=str(DEFAULT_META),
        help="optional complete_flow_result.json, used to narrow to this run's competitors",
    )
    p.add_argument(
        "--strict-competitors",
        action="store_true",
        help="if enabled, only keep files whose competitor is in meta_json competitors",
    )
    return p.parse_args()


def _safe_load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> None:
    args = _parse_args()
    run_id = args.run_id.strip()
    analyst_dir = Path(args.analyst_dir).resolve()
    meta_path = Path(args.meta_json).resolve()

    if not analyst_dir.exists():
        raise FileNotFoundError(f"analyst_dir not found: {analyst_dir}")

    allowed_competitors: set[str] = set()
    if args.strict_competitors and meta_path.exists():
        meta = _safe_load_json(meta_path) or {}
        if str(meta.get("run_id", "")).strip() == run_id:
            allowed_competitors = {str(x).strip() for x in meta.get("competitors", []) if str(x).strip()}

    matched: list[tuple[str, Path]] = []
    for path in sorted(analyst_dir.glob("*_analysis.json")):
        data = _safe_load_json(path)
        if not isinstance(data, dict):
            continue
        if str(data.get("run_id", "")).strip() != run_id:
            continue
        competitor = str(data.get("competitor", "")).strip()
        if args.strict_competitors and allowed_competitors and competitor not in allowed_competitors:
            continue
        matched.append((competitor, path))

    print(f"run_id: {run_id}")
    print(f"analyst_dir: {analyst_dir}")
    if args.strict_competitors:
        print(f"strict_competitors: true (allowed={len(allowed_competitors)})")
    print(f"matched_files: {len(matched)}")
    print("-" * 80)

    for idx, (competitor, path) in enumerate(matched, start=1):
        print(f"{idx}. competitor={competitor} | path={path}")


if __name__ == "__main__":
    main()
