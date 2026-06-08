#!/usr/bin/env python
"""专项测试 pricing_model schema 字段的采集与爬取能力。"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent / 'backend'))

from app.agents import AnalystAgent
from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.collector.pipeline import CollectorPipeline
from app.core.config import get_config
from app.core.models import AnalysisSchemaField, RawEvidence
from app.core.planner_llm import build_default_schema_plan
from app.core.storage import SQLiteStore

COMPETITORS = ["WPS Office", "云之家", "石墨文档"]
# COMPETITORS = ["飞书"]


def _safe_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", text).strip("_") or "unknown"


def _extract_search_urls(provider_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for event in provider_events:
        if event.get("event_type") != "collector.search.hit":
            continue
        items.append(
            {
                "query": event.get("query", ""),
                "url": event.get("url", ""),
                "title": event.get("title", ""),
                "source_provider": event.get("source_provider", ""),
                "strategy": event.get("strategy", ""),
            }
        )
    return items


def _default_normalized_value() -> dict[str, Any]:
    return {
        "model_type": "unknown",
        "free_tier": False,
        "billing_dimensions": [],
        "tiers": [],
    }


def _to_raw_evidences(items: list[dict[str, Any]]) -> list[RawEvidence]:
    evidences: list[RawEvidence] = []
    for item in items:
        competitor_name = str(item.get("competitor", "") or "")
        evidences.append(
            RawEvidence(
                query=str(item.get("query", "") or ""),
                source_url=str(item.get("source_url", "") or ""),
                title=str(item.get("title", "") or ""),
                snippet=str(item.get("snippet", "") or ""),
                raw_content_path=str(item.get("raw_content_path", "") or ""),
                source_type=str(item.get("source_type", "official") or "official"),
                retrieval_method=str(item.get("retrieval_method", "") or ""),
                retrieval_status=str(item.get("retrieval_status", "partial") or "partial"),
                confidence=float(item.get("confidence", 0.7) or 0.7),
                recency_score=float(item.get("recency_score", 0.5) or 0.5),
                domain_extensions={
                    "competitor": competitor_name,
                    "schema_field": str(item.get("schema_field", "pricing_model") or "pricing_model"),
                    "content_excerpt": str(item.get("content_excerpt", "") or ""),
                    "query_template": str(item.get("query_template", "") or ""),
                    "recommended_source_type": str(item.get("recommended_source_type", "") or ""),
                },
            )
        )
    return evidences


def _extract_pricing_with_analyst(
    *,
    analyst: AnalystAgent,
    competitor: str,
    evidences: list[RawEvidence],
    schema_item: AnalysisSchemaField,
) -> dict[str, Any]:
    if not evidences:
        return {
            "enabled": analyst.llm.enabled(),
            "status": "skipped_no_pages",
            "reason": "no_crawled_pages",
            "analysis_mode": "none",
            "summary": "unknown",
            "normalized_value": _default_normalized_value(),
            "evidence_gaps": ["no_evidence_for_pricing_model"],
            "confidence": 0.0,
        }
    if not analyst.llm.enabled():
        return {
            "enabled": False,
            "status": "skipped_llm_not_configured",
            "reason": "missing OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL",
            "analysis_mode": "none",
            "summary": "unknown",
            "normalized_value": _default_normalized_value(),
            "evidence_gaps": [],
            "confidence": 0.0,
        }
    try:
        field_result = analyst._analyze_single_field(
            competitor=competitor,
            field_name="pricing_model",
            evidences=evidences,
            industry="collaboration",
            schema_item=schema_item,
        )
        normalized = field_result.normalized_value if isinstance(field_result.normalized_value, dict) else _default_normalized_value()
        normalized.setdefault("model_type", "unknown")
        normalized.setdefault("free_tier", False)
        normalized.setdefault("billing_dimensions", [])
        normalized.setdefault("tiers", [])
        analysis_mode = "vision_or_text"
        if normalized.get("tiers") == [{'name': 'Observed Plan', 'price_range': 'unknown', 'billing_cycle': 'monthly'}]:
            analysis_mode = "fallback_like"
        return {
            "enabled": True,
            "status": "ok",
            "reason": "",
            "analysis_mode": analysis_mode,
            "summary": field_result.summary,
            "normalized_value": normalized,
            "evidence_gaps": field_result.evidence_gaps,
            "confidence": field_result.confidence,
            "evidence_refs": field_result.evidence_refs,
        }
    except LLMCallError as exc:
        return {
            "enabled": True,
            "status": "failed",
            "reason": str(exc),
            "analysis_mode": "failed",
            "summary": "unknown",
            "normalized_value": _default_normalized_value(),
            "evidence_gaps": [],
            "confidence": 0.0,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": True,
            "status": "failed",
            "reason": str(exc),
            "analysis_mode": "failed",
            "summary": "unknown",
            "normalized_value": _default_normalized_value(),
            "evidence_gaps": [],
            "confidence": 0.0,
        }


def main() -> None:
    root = Path(__file__).parent
    ts = time.strftime("%Y%m%d_%H%M%S")
    output_dir = root / "mock_data" / "pricing_model_schema_test" / f"run_{ts}"
    txt_dir = output_dir / "crawled_pages_txt"
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_dir.mkdir(parents=True, exist_ok=True)

    config = get_config()
    store = SQLiteStore(config.sqlite_path_obj)
    pipeline = CollectorPipeline(config=config, store=store)
    llm = AgentLLMClient(config=config, store=store)
    analyst = AnalystAgent(llm=llm, store=store)

    default_pricing = next(item for item in build_default_schema_plan() if item.get("field_name") == "pricing_model")
    schema_plan = [
        AnalysisSchemaField(
            field_name=str(default_pricing.get("field_name", "pricing_model")),
            query_templates=[str(q) for q in default_pricing.get("query_templates", [])],
            recommended_sources=[str(s) for s in default_pricing.get("recommended_sources", [])],
            priority=int(default_pricing.get("priority", 1)),
        )
    ]
    pricing_schema_item = schema_plan[0]

    run_id = f"pricing_model_probe_{ts}"
    report: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": ts,
        "schema_field": "pricing_model",
        "competitors": COMPETITORS,
        "provider_health": pipeline.provider_health(),
        "results": [],
    }

    print(f"[pricing-test] run_id={run_id}")
    print(f"[pricing-test] output_dir={output_dir}")
    print(f"[pricing-test] competitors={COMPETITORS}")
    print(f"[pricing-test] llm_enabled={llm.enabled()}")
    print(f"[pricing-test] provider_health={json.dumps(report['provider_health'], ensure_ascii=False)}")

    for competitor in COMPETITORS:
        print(f"\n[pricing-test] ===== competitor={competitor} =====")
        print(f"[pricing-test] schema_queries={schema_plan[0].query_templates}")
        output = pipeline.collect(
            run_id=run_id,
            industry="collaboration",
            competitor=competitor,
            schema_plan=schema_plan,
            per_field_limit=3,
        )
        print(
            f"[pricing-test] collect_done competitor={competitor} "
            f"evidence_count={len(output.evidences)} provider_event_count={len(output.provider_events)}"
        )

        search_urls = _extract_search_urls(output.provider_events)
        print(f"[pricing-test] search_hits={len(search_urls)}")
        for idx, hit in enumerate(search_urls[:5], start=1):
            print(
                f"[pricing-test] search_hit_{idx} "
                f"provider={hit.get('source_provider', '')} "
                f"title={hit.get('title', '')[:80]} "
                f"url={hit.get('url', '')}"
            )
        crawled_items: list[dict[str, Any]] = []

        for idx, ev in enumerate(output.evidences, start=1):
            raw_path = root / str(ev.get("raw_content_path", ""))
            txt_name = f"{_safe_name(competitor)}_{idx:02d}_{ev.get('content_hash', '')[:10]}.txt"
            target_txt = txt_dir / txt_name
            txt_saved = False

            if raw_path.exists() and raw_path.is_file():
                shutil.copyfile(raw_path, target_txt)
                txt_saved = True

            print(
                f"[pricing-test] crawled_{idx} competitor={competitor} "
                f"status={ev.get('retrieval_status', '')} "
                f"source_type={ev.get('source_type', '')} "
                f"url={ev.get('source_url', '')} "
                f"raw_saved={txt_saved}"
            )

            crawled_items.append(
                {
                    "query": ev.get("query", ""),
                    "source_url": ev.get("source_url", ""),
                    "search_provider": ev.get("source_provider", ""),
                    "retrieval_method": ev.get("retrieval_method", ""),
                    "retrieval_status": ev.get("retrieval_status", ""),
                    "content_hash": ev.get("content_hash", ""),
                    "raw_content_path": ev.get("raw_content_path", ""),
                    "saved_txt_path": str(target_txt.relative_to(root)) if txt_saved else "",
                    "saved_txt": txt_saved,
                }
            )
        raw_evidences = _to_raw_evidences([
            {**ev, "competitor": competitor, "schema_field": "pricing_model"} for ev in output.evidences
        ])
        llm_pricing = _extract_pricing_with_analyst(
            analyst=analyst,
            competitor=competitor,
            evidences=raw_evidences,
            schema_item=pricing_schema_item,
        )
        print(
            f"[pricing-test] pricing_analysis competitor={competitor} "
            f"status={llm_pricing.get('status', '')} "
            f"mode={llm_pricing.get('analysis_mode', '')} "
            f"confidence={llm_pricing.get('confidence', 0.0)}"
        )
        print(f"[pricing-test] pricing_summary={llm_pricing.get('summary', '')}")
        print(
            "[pricing-test] pricing_normalized="
            f"{json.dumps(llm_pricing.get('normalized_value', {}), ensure_ascii=False)}"
        )
        if llm_pricing.get("evidence_gaps"):
            print(f"[pricing-test] pricing_gaps={llm_pricing.get('evidence_gaps')}")
        if llm_pricing.get("reason"):
            print(f"[pricing-test] pricing_reason={llm_pricing.get('reason')}")

        report["results"].append(
            {
                "competitor": competitor,
                "search_hit_count": len(search_urls),
                "crawl_count": len(crawled_items),
                "search_urls": search_urls,
                "crawled_urls": crawled_items,
                "pricing_extraction": llm_pricing,
                "provider_events_tail": output.provider_events[-12:],
            }
        )

    json_path = output_dir / "pricing_model_collection_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[pricing-test] report_written={json_path}")
    print(f"JSON report: {json_path}")
    print(f"TXT folder: {txt_dir}")


if __name__ == "__main__":
    main()
