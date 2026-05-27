#!/usr/bin/env python
"""Analysis-stage QA recollect script (run_id-driven, no report patch)."""
from __future__ import annotations

import concurrent.futures
import json
import time
from pathlib import Path
from typing import Any
import sys

sys.path.insert(0, str(Path(__file__).parent / "backend"))

from app.core.collector.verifier import dedup_by_url_and_hash, verify_cross_source
from app.core.config import get_config
from app.core.models import AnalysisSchemaField, RawEvidence, RunState
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService

RUN_ID = "run_0e196845b6c6"
INDUSTRY_HINT = "general"
ANALYST_OUTPUT_DIR = Path(__file__).parent / "mock_data" / "complete_flow_result" / "analyst_output"
OUTPUT_DIR = Path(__file__).parent / "mock_data" / "complete_flow_result"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sanitize_query_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out = [str(x).strip() for x in values if str(x).strip()]
    return out[:2]


def _scan_analysis_files(run_id: str) -> list[tuple[Path, dict[str, Any]]]:
    if not ANALYST_OUTPUT_DIR.exists():
        raise FileNotFoundError(f"analyst_output directory not found: {ANALYST_OUTPUT_DIR}")
    matched: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(ANALYST_OUTPUT_DIR.glob("*_analysis.json")):
        try:
            data = _load_json(path)
        except Exception:
            continue
        if str(data.get("run_id", "")).strip() == run_id:
            matched.append((path, data))
    return matched


def _schema_fields_from_analyses(items: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for _path, data in items:
        fields = data.get("fields", [])
        if not isinstance(fields, list):
            continue
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = str(field.get("field_name", "")).strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _competitor_schema_plan_from_analysis_json(analysis_json: dict[str, Any]) -> list[dict[str, Any]]:
    fields = analysis_json.get("fields", [])
    if not isinstance(fields, list):
        return []
    plan: list[dict[str, Any]] = []
    seen: set[str] = set()
    priority = 1
    for item in fields:
        if not isinstance(item, dict):
            continue
        field_name = str(item.get("field_name", "")).strip()
        if not field_name or field_name in seen:
            continue
        seen.add(field_name)
        plan.append(
            {
                "field_name": field_name,
                "query_templates": [f"{{product}} {field_name}"],
                "recommended_sources": ["public_web"],
                "priority": priority,
            }
        )
        priority += 1
    return plan


def _build_schema_plan(collect_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    field_queries: dict[str, list[str]] = {}
    for item in collect_items:
        field_name = str(item.get("field_name", "")).strip()
        query_list = _sanitize_query_list(item.get("query_list", []))
        if not field_name or len(query_list) < 1:
            continue
        if field_name not in field_queries:
            field_queries[field_name] = query_list
    plan: list[dict[str, Any]] = []
    for idx, (field_name, queries) in enumerate(field_queries.items(), start=1):
        plan.append(
            {
                "field_name": field_name,
                "query_templates": queries,
                "recommended_sources": ["public_web"],
                "priority": idx,
            }
        )
    return plan


def _build_field_query_overrides(collect_items: list[dict[str, Any]]) -> dict[str, list[str]]:
    overrides: dict[str, list[str]] = {}
    for item in collect_items:
        competitor = str(item.get("competitor", "")).strip()
        field_name = str(item.get("field_name", "")).strip()
        query_list = _sanitize_query_list(item.get("query_list", []))
        if not competitor or not field_name or len(query_list) < 1:
            continue
        overrides[f"{competitor}::{field_name}"] = query_list
    return overrides


def _collect_rows_to_raw_evidences(competitor: str, rows: list[dict[str, Any]]) -> list[RawEvidence]:
    output: list[RawEvidence] = []
    for item in rows:
        ev = RawEvidence(
            query=str(item.get("query", "")),
            source_url=str(item.get("source_url", "")),
            title=str(item.get("title", "")),
            snippet=str(item.get("snippet", "")),
            claim_tags=["feature", "pricing", "feedback"],
            credibility_score=float(item.get("confidence", 0.7) or 0.7),
            confidence=float(item.get("confidence", 0.7) or 0.7),
            recency_score=float(item.get("recency_score", 0.5) or 0.5),
            raw_content_path=str(item.get("raw_content_path", "")),
            extract_fields=item.get("extract_fields", {}) if isinstance(item.get("extract_fields"), dict) else {},
            license_or_tos_note=str(item.get("license_or_tos_note", "")),
            source_type=str(item.get("source_type", "report") or "report"),
            retrieval_method=str(item.get("retrieval_method", "collector_pipeline")),
            retrieval_status=str(item.get("retrieval_status", "partial")),
            domain_extensions={
                "competitor": competitor,
                "source_provider": item.get("source_provider", ""),
                "content_excerpt": item.get("content_excerpt", ""),
                "schema_field": item.get("schema_field", ""),
                "query_template": item.get("query_template", ""),
                "recommended_source_type": item.get("recommended_source_type", ""),
            },
        )
        output.append(ev)
    return output


def _backup_json(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}.backup_{stamp}{path.suffix}")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup


def _merge_fields_in_place(
    *,
    original_fields: list[dict[str, Any]],
    updated_fields: list[dict[str, Any]],
    target_field_names: set[str],
) -> list[dict[str, Any]]:
    update_map: dict[str, dict[str, Any]] = {}
    for item in updated_fields:
        if not isinstance(item, dict):
            continue
        field_name = str(item.get("field_name", "")).strip()
        if field_name:
            update_map[field_name] = item

    merged: list[dict[str, Any]] = []
    existed: set[str] = set()
    for item in original_fields:
        if not isinstance(item, dict):
            merged.append(item)
            continue
        field_name = str(item.get("field_name", "")).strip()
        if field_name and field_name in target_field_names and field_name in update_map:
            merged.append(update_map[field_name])
            existed.add(field_name)
        else:
            merged.append(item)

    for field_name in target_field_names:
        if field_name in existed:
            continue
        candidate = update_map.get(field_name)
        if candidate:
            merged.append(candidate)
    return merged


def main() -> None:
    print("=" * 80)
    print("Analysis-stage QA Recollect Script")
    print("=" * 80)
    print(f"fixed run_id: {RUN_ID}")
    print(f"analyst_output: {ANALYST_OUTPUT_DIR}")
    print()

    start = time.time()
    cfg = get_config()
    store = SQLiteStore(cfg.sqlite_path_obj)
    service = CompetitorWorkflowService(store)
    print(f"collector_search_order={cfg.collector_search_order}")
    print(f"collector_search_order_list={cfg.collector_search_order_list}")
    print(f"zhihu_search_access_secret_exists={bool(cfg.zhihu_search_access_secret)}")

    print("[1/5] locate analysis json files by run_id")
    step_start = time.time()
    analysis_files = _scan_analysis_files(RUN_ID)
    if not analysis_files:
        raise RuntimeError(f"no analysis json matched run_id={RUN_ID} under {ANALYST_OUTPUT_DIR}")
    print(f"      matched competitors: {len(analysis_files)}")
    print(f"      done in {time.time() - step_start:.2f}s")

    schema_fields = _schema_fields_from_analyses(analysis_files)

    print("[2/5] parallel qa review (one llm per competitor)")
    step_start = time.time()
    qa_results: list[dict[str, Any]] = []
    qa_errors: list[dict[str, Any]] = []

    def _review_one(item: tuple[Path, dict[str, Any]]) -> dict[str, Any]:
        path, data = item
        review = service.qa_critic_agent.run_competitor_analysis_review_llm(
            analysis_json=data,
            schema_fields=schema_fields,
            industry_hint=INDUSTRY_HINT,
        )
        return {"path": str(path), "competitor": data.get("competitor", ""), "review": review}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(analysis_files), 4)) as executor:
        futures = [executor.submit(_review_one, item) for item in analysis_files]
        for future in concurrent.futures.as_completed(futures):
            try:
                qa_results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                qa_errors.append({"error": f"{type(exc).__name__}: {exc}"})

    collect_items: list[dict[str, Any]] = []
    for item in qa_results:
        review = item.get("review", {}) if isinstance(item.get("review"), dict) else {}
        if not bool(review.get("needs_recollect", False)):
            continue
        collect_plan = review.get("collect_plan", {}) if isinstance(review.get("collect_plan"), dict) else {}
        items = collect_plan.get("items", []) if isinstance(collect_plan.get("items"), list) else []
        for one in items:
            if isinstance(one, dict):
                collect_items.append(one)

    print(f"      qa success: {len(qa_results)}")
    print(f"      qa errors: {len(qa_errors)}")
    print(f"      recollect field items: {len(collect_items)}")
    print(f"      done in {time.time() - step_start:.2f}s")

    if not collect_items:
        print("[3/5] no recollect needed, write audit summary and exit")
        step_start = time.time()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        summary_path = OUTPUT_DIR / f"qa_rework_result_{stamp}.json"
        summary = {
            "run_id": RUN_ID,
            "needs_recollect": False,
            "qa_review_count": len(qa_results),
            "qa_errors": qa_errors,
            "collect_items": [],
            "updated_analysis_files": [],
            "recollected_evidence_count": 0,
            "elapsed_seconds": round(time.time() - start, 2),
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"      audit summary: {summary_path}")
        print(f"      done in {time.time() - step_start:.2f}s")
        print()
        print("=" * 80)
        print(f"finished in {time.time() - start:.2f}s")
        print("=" * 80)
        return

    print("[3/5] targeted recollect for insufficient competitor+field")
    step_start = time.time()
    collect_items_clean: list[dict[str, Any]] = []
    for item in collect_items:
        competitor = str(item.get("competitor", "")).strip()
        field_name = str(item.get("field_name", "")).strip()
        query_list = _sanitize_query_list(item.get("query_list", []))
        if competitor and field_name and len(query_list) >= 1:
            collect_items_clean.append(
                {
                    "competitor": competitor,
                    "field_name": field_name,
                    "query_list": query_list,
                    "reason": str(item.get("reason", "")),
                    "priority": int(item.get("priority", 1) or 1),
                }
            )

    schema_plan = _build_schema_plan(collect_items_clean)
    field_query_overrides = _build_field_query_overrides(collect_items_clean)
    target_competitors = sorted({str(x.get("competitor", "")).strip() for x in collect_items_clean if str(x.get("competitor", "")).strip()})
    target_fields_by_competitor: dict[str, set[str]] = {}
    for item in collect_items_clean:
        competitor = str(item.get("competitor", "")).strip()
        field_name = str(item.get("field_name", "")).strip()
        if not competitor or not field_name:
            continue
        target_fields_by_competitor.setdefault(competitor, set()).add(field_name)

    recollected_by_competitor: dict[str, list[dict[str, Any]]] = {}
    recollect_errors: list[str] = []
    recollect_provider_logs: list[dict[str, Any]] = []
    for competitor in target_competitors:
        log_state = RunState(
            run_id=RUN_ID,
            industry=INDUSTRY_HINT,
            competitors=[competitor],
            planned_competitors=[competitor],
            user_prompt="analysis_stage_qa_recollect_log",
        )
        try:
            result = service.collector.collect(
                run_id="analysis_stage_qa",
                industry=INDUSTRY_HINT,
                competitor=competitor,
                schema_plan=schema_plan,
                per_field_limit=cfg.collector_per_field_limit,
                field_query_overrides=field_query_overrides,
            )
            rows = verify_cross_source(dedup_by_url_and_hash(result.evidences))
            recollected_by_competitor[competitor] = rows
            provider_events = result.provider_events if isinstance(result.provider_events, list) else []
            search_hits = [x for x in provider_events if str(x.get("event_type", "")).startswith("collector.search.")]
            fetch_hits = [x for x in provider_events if str(x.get("event_type", "")).startswith("collector.fetch.")]
            fallback_trace = []
            for event in provider_events:
                if str(event.get("event_type", "")) == "collector.fallback.trace":
                    fallback_trace = event.get("fallback_trace", [])
                    break
            log_payload = {
                "competitor": competitor,
                "target_field_count": len(target_fields_by_competitor.get(competitor, set())),
                "recollected_evidence_count": len(rows),
                "collector_errors": result.errors,
                "search_event_count": len(search_hits),
                "fetch_event_count": len(fetch_hits),
                "fallback_trace": fallback_trace,
                "search_events_sample": search_hits[:20],
                "fetch_events_sample": fetch_hits[:20],
            }
            recollect_provider_logs.append(log_payload)
            service.qa_critic_agent._append_qa_log(
                event_type="qa_recollect_provider_trace",
                run_state=log_state,
                payload=log_payload,
            )
        except Exception as exc:  # noqa: BLE001
            recollected_by_competitor[competitor] = []
            recollect_errors.append(f"{competitor}: {type(exc).__name__}: {exc}")
            err_payload = {"competitor": competitor, "error": f"{type(exc).__name__}: {exc}"}
            recollect_provider_logs.append(err_payload)
            service.qa_critic_agent._append_qa_log(
                event_type="qa_recollect_provider_trace",
                run_state=log_state,
                payload=err_payload,
            )

    recollected_total = sum(len(v) for v in recollected_by_competitor.values())
    print(f"      target competitors: {len(target_competitors)}")
    print(f"      recollected evidences: {recollected_total}")
    print(f"      done in {time.time() - step_start:.2f}s")

    print("[4/5] rerun analyst for targeted competitors and overwrite analysis json")
    step_start = time.time()
    updated_files: list[str] = []
    backup_files: list[str] = []
    rewrite_errors: list[str] = []
    reanalyze_stats: list[dict[str, Any]] = []

    analysis_map = {str(data.get("competitor", "")).strip(): path for path, data in analysis_files}
    analysis_json_map = {str(data.get("competitor", "")).strip(): data for path, data in analysis_files}

    for competitor in target_competitors:
        output_path = analysis_map.get(competitor)
        if output_path is None:
            rewrite_errors.append(f"missing analysis file for competitor={competitor}")
            continue

        rows = recollected_by_competitor.get(competitor, [])
        if not rows:
            rewrite_errors.append(f"no recollected evidences for competitor={competitor}")
            continue

        raw_evidences = _collect_rows_to_raw_evidences(competitor, rows)
        competitor_analysis_json = analysis_json_map.get(competitor, {})
        competitor_schema_plan = _competitor_schema_plan_from_analysis_json(competitor_analysis_json)
        reanalyze_field_names = [str(item.get("field_name", "")).strip() for item in competitor_schema_plan if str(item.get("field_name", "")).strip()]
        state = RunState(
            run_id=RUN_ID,
            industry=INDUSTRY_HINT,
            competitors=[competitor],
            planned_competitors=[competitor],
            user_prompt="analysis_stage_qa_reanalyze",
            analysis_schema_plan=[AnalysisSchemaField.model_validate(item) for item in competitor_schema_plan],
            evidences=raw_evidences,
        )

        try:
            analyze_out = service.analyst_agent.run_llm(state)
            if not analyze_out.competitors:
                rewrite_errors.append(f"reanalyze returned empty competitors for {competitor}")
                continue
            record = analyze_out.competitors[0]
            backup = _backup_json(output_path)
            backup_files.append(str(backup))
            original_payload = _load_json(output_path)
            original_fields = original_payload.get("fields", []) if isinstance(original_payload.get("fields"), list) else []
            updated_fields = [x.model_dump(mode="json") for x in record.fields]
            target_field_names = target_fields_by_competitor.get(competitor, set())
            merged_fields = _merge_fields_in_place(
                original_fields=original_fields,
                updated_fields=updated_fields,
                target_field_names=target_field_names,
            )
            payload = {
                "competitor": competitor,
                "run_id": RUN_ID,
                "fields": merged_fields,
            }
            output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            updated_files.append(str(output_path))
            reanalyze_stats.append(
                {
                    "competitor": competitor,
                    "reanalyze_field_count": len(reanalyze_field_names),
                    "reanalyze_field_names": reanalyze_field_names,
                }
            )
        except Exception as exc:  # noqa: BLE001
            rewrite_errors.append(f"{competitor}: {type(exc).__name__}: {exc}")
            reanalyze_stats.append(
                {
                    "competitor": competitor,
                    "reanalyze_field_count": len(reanalyze_field_names),
                    "reanalyze_field_names": reanalyze_field_names,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    print(f"      updated analysis files: {len(updated_files)}")
    print(f"      done in {time.time() - step_start:.2f}s")

    print("[5/5] write audit summary")
    step_start = time.time()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    summary_path = OUTPUT_DIR / f"qa_rework_result_{stamp}.json"
    summary = {
        "run_id": RUN_ID,
        "needs_recollect": True,
        "qa_review_count": len(qa_results),
        "qa_errors": qa_errors,
        "collect_items": collect_items_clean,
        "target_competitors": target_competitors,
        "recollected_evidence_count": recollected_total,
        "recollect_errors": recollect_errors,
        "recollect_provider_logs": recollect_provider_logs,
        "reanalyze_stats": reanalyze_stats,
        "updated_analysis_files": updated_files,
        "backup_files": backup_files,
        "rewrite_errors": rewrite_errors,
        "qa_log_file": str((Path(__file__).parent / "QA_log" / "qa_agent.jsonl").resolve()),
        "elapsed_seconds": round(time.time() - start, 2),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"      audit summary: {summary_path}")
    print(f"      done in {time.time() - step_start:.2f}s")
    print()
    print("=" * 80)
    print(f"finished in {time.time() - start:.2f}s")
    print("=" * 80)


if __name__ == "__main__":
    main()
