from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'backend'))

from app.core.config import AppConfig, get_config
from app.core.planner_llm import PlannerLLMClient


MEETING_PROMPT = '进行在线会议软件领域的竞品分析'


def _print_json(label: str, payload: object) -> None:
    print(f"[planner-test] {label}={json.dumps(payload, ensure_ascii=False)}")


def run_infer_product_profile_case() -> None:
    cfg = AppConfig(openai_api_key='', openai_base_url='', openai_model='')
    planner = PlannerLLMClient(cfg)

    profile = planner.infer_product_profile(
        prompt=MEETING_PROMPT,
        industry='meeting_software',
        competitor_hints=['Zoom'],
    )

    print(f"[planner-test] prompt={MEETING_PROMPT}")
    _print_json('inferred_profile', profile)

    assert profile['product_category']
    assert '在线会议' in profile['core_capabilities']
    assert '企业团队' in profile['target_users']
    assert '远程会议' in profile['primary_use_cases']
    assert profile['seed_products'] == ['Zoom']


def run_generate_queries_case() -> None:
    cfg = get_config()
    planner = PlannerLLMClient(cfg)
    profile = planner.infer_product_profile(
        prompt=MEETING_PROMPT,
        industry='meeting_software',
        competitor_hints=['Zoom'],
    )

    queries = planner._generate_search_queries(
        MEETING_PROMPT,
        ['Zoom'],
        industry='meeting_software',
        product_profile=profile,
    )

    _print_json('meeting_profile', profile)
    _print_json('generated_queries', queries)

    assert isinstance(queries, list)
    assert 1 <= len(queries) <= 4
    assert all(str(item).strip() for item in queries)


def run_grouped_competitor_case() -> None:
    cfg = get_config()
    planner = PlannerLLMClient(cfg)

    result = planner.discover_competitors_grouped(
        prompt=MEETING_PROMPT,
        industry='meeting_software',
        competitor_hints=['Zoom'],
        max_direct=3,
        max_substitute=1,
    )

    _print_json('grouped_profile', result['product_profile'])
    _print_json('candidate_pool', result['candidate_pool'])
    _print_json('direct', result['competitors']['direct'])
    _print_json('substitute', result['competitors']['substitute'])
    _print_json('search_results', result['search_results'][:6])

    assert isinstance(result.get('product_profile', {}), dict)
    assert isinstance(result.get('candidate_pool', []), list)
    assert isinstance(result.get('competitors', {}).get('direct', []), list)
    assert isinstance(result.get('competitors', {}).get('substitute', []), list)


def main() -> None:
    print('[planner-test] case=在线会议软件竞品分析')
    runtime_cfg = get_config()
    print(
        f"[planner-test] llm_enabled={bool(runtime_cfg.openai_api_key and runtime_cfg.openai_base_url and runtime_cfg.openai_model)} "
        f"search_order={runtime_cfg.collector_search_order_list}"
    )
    run_infer_product_profile_case()
    run_generate_queries_case()
    run_grouped_competitor_case()
    print('[planner-test] status=ok')


if __name__ == '__main__':
    main()
