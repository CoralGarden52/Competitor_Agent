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

from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.collector.pipeline import CollectorPipeline
from app.core.config import get_config
from app.core.models import AnalysisSchemaField
from app.core.storage import SQLiteStore

COMPETITORS = ["飞书", "WPS Office", "云之家", "石墨文档"]


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


def _build_llm_payload(competitor: str, crawled_items: list[dict[str, Any]], root: Path) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for item in crawled_items:
        txt_path = str(item.get("saved_txt_path", "")).strip()
        if not txt_path:
            continue
        abs_path = root / txt_path
        if not abs_path.exists():
            continue
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
        pages.append(
            {
                "source_url": item.get("source_url", ""),
                "retrieval_status": item.get("retrieval_status", ""),
                "content_excerpt": text[:8000],
            }
        )
    return {
        "competitor": competitor,
        "schema_field": "pricing_model",
        "pages": pages,
        "output_schema": {
            "normalized_value": {
                "model_type": "unknown|subscription|freemium|per_seat|hybrid|one_time|usage_based",
                "free_tier": "boolean",
                "billing_dimensions": ["string"],
                "tiers": [
                    {
                        "name": "string",
                        "price_range": "string",
                        "billing_cycle": "string",
                        "limits": ["string"],
                    }
                ],
            }
        },
    }


def _extract_pricing_with_llm(
    *,
    llm: AgentLLMClient,
    competitor: str,
    crawled_items: list[dict[str, Any]],
    root: Path,
    run_id: str,
) -> dict[str, Any]:
    payload = _build_llm_payload(competitor, crawled_items, root)
    if not payload["pages"]:
        return {
            "enabled": llm.enabled(),
            "status": "skipped_no_pages",
            "reason": "no_crawled_pages",
            "normalized_value": _default_normalized_value(),
        }
    if not llm.enabled():
        return {
            "enabled": False,
            "status": "skipped_llm_not_configured",
            "reason": "missing OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL",
            "normalized_value": _default_normalized_value(),
        }

    try:
        result = llm.invoke_json(
            trace_name="script.pricing_model.extract_normalized_value",
            system_prompt=(
                "你是企业软件定价分析助手。"
                "请根据网页内容提炼 pricing_model 的 normalized_value。"
                "不得编造，无法确认的信息填 unknown、false 或空数组。"
                "仅返回 JSON，格式："
                "{\"normalized_value\":{\"model_type\":\"...\",\"free_tier\":false,"
                "\"billing_dimensions\":[],\"tiers\":[{\"name\":\"...\",\"price_range\":\"...\","
                "\"billing_cycle\":\"...\",\"limits\":[]}]}}"
            ),
            user_payload=payload,
            metadata={
                "run_id": run_id,
                "attempt": 1,
                "node_name": "pricing_model_schema_test",
                "agent_name": "PricingModelExtractor",
                "competitor": competitor,
                "page_count": len(payload["pages"]),
            },
        )
        normalized = result.get("normalized_value", {})
        if not isinstance(normalized, dict):
            normalized = _default_normalized_value()
        normalized.setdefault("model_type", "unknown")
        normalized.setdefault("free_tier", False)
        normalized.setdefault("billing_dimensions", [])
        normalized.setdefault("tiers", [])
        return {
            "enabled": True,
            "status": "ok",
            "reason": "",
            "normalized_value": normalized,
        }
    except LLMCallError as exc:
        return {
            "enabled": True,
            "status": "failed",
            "reason": str(exc),
            "normalized_value": _default_normalized_value(),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": True,
            "status": "failed",
            "reason": str(exc),
            "normalized_value": _default_normalized_value(),
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

    schema_plan = [
        AnalysisSchemaField(
            field_name="pricing_model",
            query_templates=[
                "{product} 官网 价格 套餐",
                "{product} pricing plans official",
                "{product} 收费 版本 对比",
            ],
            recommended_sources=["official", "review"],
            priority=1,
        )
    ]

    run_id = f"pricing_model_probe_{ts}"
    report: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": ts,
        "schema_field": "pricing_model",
        "competitors": COMPETITORS,
        "provider_health": pipeline.provider_health(),
        "results": [],
    }

    for competitor in COMPETITORS:
        output = pipeline.collect(
            run_id=run_id,
            industry="collaboration",
            competitor=competitor,
            schema_plan=schema_plan,
            per_field_limit=3,
        )

        search_urls = _extract_search_urls(output.provider_events)
        crawled_items: list[dict[str, Any]] = []

        for idx, ev in enumerate(output.evidences, start=1):
            raw_path = root / str(ev.get("raw_content_path", ""))
            txt_name = f"{_safe_name(competitor)}_{idx:02d}_{ev.get('content_hash', '')[:10]}.txt"
            target_txt = txt_dir / txt_name
            txt_saved = False

            if raw_path.exists() and raw_path.is_file():
                shutil.copyfile(raw_path, target_txt)
                txt_saved = True

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
        llm_pricing = _extract_pricing_with_llm(
            llm=llm,
            competitor=competitor,
            crawled_items=crawled_items,
            root=root,
            run_id=run_id,
        )

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

    print(f"JSON report: {json_path}")
    print(f"TXT folder: {txt_dir}")


if __name__ == "__main__":
    main()
