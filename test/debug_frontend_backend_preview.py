#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


TEST_CASES = [
    {
        'name': 'frontend_live_prompt',
        'prompt': '请分析在线会议软件领域的竞品',
        'industry_hint': '',
        'competitor_hints': [],
        'max_direct': 3,
        'max_substitute': 1,
    },
    {
        'name': 'test_full_flow_prompt',
        'prompt': '进行在线会议软件领域的竞品分析',
        'industry_hint': '',
        'competitor_hints': [],
        'max_direct': 3,
        'max_substitute': 1,
    },
]


def _print_json(label: str, payload: object) -> None:
    print(f'{label}={json.dumps(payload, ensure_ascii=False, indent=2)}')


def _print_header(title: str) -> None:
    print('\n' + '=' * 96)
    print(title)
    print('=' * 96)


def _brief_search_results(rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows[:limit]:
        output.append(
            {
                'title': str(row.get('title', ''))[:120],
                'url': row.get('url', ''),
                'provider': row.get('source_provider', ''),
                'summary': str(row.get('summary', ''))[:180],
            }
        )
    return output


def run_case(case: dict[str, Any]) -> None:
    from app.core.config import get_config
    from app.core.storage import SQLiteStore
    from app.core.workflow import CompetitorWorkflowService

    config = get_config()
    store = SQLiteStore(config.sqlite_path_obj)
    service = CompetitorWorkflowService(store)
    planner = service.planner_llm

    _print_header(f"CASE: {case['name']}")
    print(f"prompt={case['prompt']}")
    print(f'openai_config_ready={config.has_openai_config()}')
    print(f'collector_search_order={config.collector_search_order_list}')
    print(f'collector_fetch_order={config.collector_fetch_order_list}')
    print(f'planner_enabled={planner.enabled()}')
    print(f'provider_health={json.dumps(service.collector_provider_health(), ensure_ascii=False)}')

    inferred_industry = planner.infer_industry_from_prompt(
        prompt=case['prompt'],
        industry_hint=case['industry_hint'],
    )
    print(f'inferred_industry={inferred_industry}')

    product_profile = planner.infer_product_profile(
        prompt=case['prompt'],
        industry=inferred_industry,
        competitor_hints=case['competitor_hints'],
    )
    _print_json('product_profile', product_profile)

    queries = planner._generate_search_queries(
        case['prompt'],
        case['competitor_hints'],
        industry=inferred_industry,
        product_profile=product_profile,
    )
    _print_json('search_queries', queries)

    search_results = planner._search_and_summarize(queries)
    print(f'search_results_count={len(search_results)}')
    _print_json('search_results_top5', _brief_search_results(search_results))

    candidate_pool = planner._build_candidate_pool(
        prompt=case['prompt'],
        industry=inferred_industry,
        competitor_hints=case['competitor_hints'],
        search_results=search_results,
        product_profile=product_profile,
    )
    _print_json('candidate_pool', candidate_pool)

    if not candidate_pool:
        fallback_pool = planner._fallback_candidates_from_search_results(
            prompt=case['prompt'],
            industry=inferred_industry,
            competitor_hints=case['competitor_hints'],
            search_results=search_results,
            product_profile=product_profile,
        )
        _print_json('fallback_candidate_pool', fallback_pool)

    grouped = planner.discover_competitors_grouped(
        prompt=case['prompt'],
        industry=inferred_industry,
        competitor_hints=case['competitor_hints'],
        max_direct=case['max_direct'],
        max_substitute=case['max_substitute'],
    )
    _print_json('grouped_candidate_pool', grouped.get('candidate_pool', []))
    _print_json('grouped_competitors', grouped.get('competitors', {}))
    _print_json('grouped_search_results_top5', _brief_search_results(grouped.get('search_results', [])))

    dynamic_plan = service.orchestrator.generate_dynamic_plan(
        prompt=case['prompt'],
        industry=case['industry_hint'],
        competitor_hints=case['competitor_hints'],
        max_direct=case['max_direct'],
        max_substitute=case['max_substitute'],
    )
    _print_json(
        'dynamic_plan',
        {
            'inferred_industry': dynamic_plan.get('inferred_industry'),
            'planned_competitors': dynamic_plan.get('planned_competitors', []),
            'candidate_groups': dynamic_plan.get('candidate_groups', {}),
            'planner_meta': dynamic_plan.get('planner_meta', {}),
        },
    )

    preview = service.collector_preview(
        prompt=case['prompt'],
        industry_hint=case['industry_hint'],
        competitor_hints=case['competitor_hints'],
    )
    _print_json(
        'collector_preview',
        {
            'inferred_industry': preview.get('inferred_industry'),
            'planned_competitors': preview.get('planned_competitors', []),
            'candidate_groups': preview.get('candidate_groups', {}),
            'errors': preview.get('errors', []),
            'preview_count': len(preview.get('preview', [])),
            'planner_meta': preview.get('planner_meta', {}),
        },
    )


def run_http_smoke(case: dict[str, Any]) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    _print_header(f"HTTP SMOKE: {case['name']}")
    client = TestClient(create_app())

    preview_resp = client.post(
        '/collector/preview',
        json={
            'prompt': case['prompt'],
            'industry_hint': case['industry_hint'],
            'competitor_hints': case['competitor_hints'],
        },
    )
    print(f'preview_status={preview_resp.status_code}')
    _print_json('preview_http_body', preview_resp.json())

    run_resp = client.post(
        '/runs',
        json={
            'industry': case['industry_hint'],
            'competitors': preview_resp.json().get('planned_competitors', []),
            'user_prompt': case['prompt'],
            'language': 'zh-CN',
            'timeframe': 'last_12_months',
        },
    )
    print(f'run_status={run_resp.status_code}')
    _print_json('run_http_body', run_resp.json())


def main() -> None:
    for case in TEST_CASES:
        run_case(case)
        try:
            run_http_smoke(case)
        except Exception as exc:
            print(f'http_smoke_skipped={exc}')


if __name__ == '__main__':
    main()
