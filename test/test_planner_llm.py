#!/usr/bin/env python3
"""
专门测试 planner_llm.py 的脚本。

使用方式：
1. 只修改顶部 TEST_CASE 这一份配置
2. 直接运行 `python test_planner_llm.py`
3. 脚本会按同一个测试用例依次打印：
   - 搜索 query
   - 竞品发现结果
   - schema 生成结果
"""

import os
import sys

# 添加 backend 到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

from app.core.config import AppConfig
from app.core.planner_llm import PlannerLLMClient


TEST_CASE = {
    "name": "线上会议软件竞品分析",
    "prompt": "帮我做一个线上会议软件的竞品分析",
    "industry": "",
    "hints": [],
    "schema_candidates": [],
    "max_direct": 5,
    "max_substitute": 3,
}


def print_case_summary() -> None:
    print(f"测试用例: {TEST_CASE['name']}")
    print(f"Prompt: {TEST_CASE['prompt']}")
    print(f"Industry: {TEST_CASE['industry']}")
    print(f"Hints: {TEST_CASE['hints']}")
    print(f"Schema Candidates: {TEST_CASE['schema_candidates']}")
    print(f"Max Direct: {TEST_CASE['max_direct']}, Max Substitute: {TEST_CASE['max_substitute']}")


def build_planner() -> PlannerLLMClient | None:
    config = AppConfig()
    planner = PlannerLLMClient(config)
    print(f"\nLLM 启用状态: {planner.enabled()}")
    if not planner.enabled():
        print("LLM 未启用，请检查 .env 文件中的 API Key 配置")
        return None
    return planner


def test_search_query_generation() -> None:
    """测试搜索 query 生成功能"""
    print("\n" + "=" * 80)
    print("测试搜索 Query 生成 (_generate_search_queries)")
    print("=" * 80)
    print_case_summary()

    planner = build_planner()
    if planner is None:
        return

    try:
        queries = planner._generate_search_queries(
            TEST_CASE["prompt"],
            TEST_CASE["hints"],
            industry=TEST_CASE["industry"],
        )
        print(f"\n生成的搜索 Query ({len(queries)} 个):")
        for i, query in enumerate(queries, 1):
            print(f"  {i}. {query}")
    except Exception as exc:
        print(f"错误: {exc}")
        import traceback

        traceback.print_exc()


def test_discover_competitors() -> None:
    """测试竞品发现功能"""
    print("\n" + "=" * 80)
    print("测试竞品发现 (discover_competitors_grouped)")
    print("=" * 80)
    print_case_summary()

    planner = build_planner()
    if planner is None:
        return

    try:
        result = planner.discover_competitors_grouped(
            prompt=TEST_CASE["prompt"],
            industry=TEST_CASE["industry"],
            competitor_hints=TEST_CASE["hints"],
            max_direct=TEST_CASE["max_direct"],
            max_substitute=TEST_CASE["max_substitute"],
        )

        candidate_pool = result.get("candidate_pool", [])
        print(f"\n候选池 ({len(candidate_pool)} 个): {candidate_pool}")

        print(f"  直接竞品 ({len(result['competitors']['direct'])} 个):")
        for item in result["competitors"]["direct"]:
            print(f"    - {item['name']} (confidence: {item.get('confidence', 0)})")

        print(f"  替代竞品 ({len(result['competitors']['substitute'])} 个):")
        for item in result["competitors"]["substitute"]:
            print(f"    - {item['name']} (confidence: {item.get('confidence', 0)})")

        print(f"  搜索结果数量: {len(result['search_results'])}")
        for i, row in enumerate(result["search_results"][:5], 1):
            print(f"    [{i}] {row.get('title', 'N/A')[:80]}...")
    except Exception as exc:
        print(f"错误: {exc}")
        import traceback

        traceback.print_exc()


def test_plan_dynamic_schema() -> None:
    """测试动态 schema 生成功能"""
    print("\n" + "=" * 80)
    print("测试动态 Schema 生成 (plan_dynamic_schema)")
    print("=" * 80)
    print_case_summary()

    planner = build_planner()
    if planner is None:
        return

    try:
        schema = planner.plan_dynamic_schema(
            prompt=TEST_CASE["prompt"],
            industry=TEST_CASE["industry"],
            candidates=TEST_CASE["schema_candidates"],
        )

        print(f"\n生成的 Schema 字段 ({len(schema)} 个):")
        for i, field in enumerate(schema, 1):
            print(f"  {i}. {field['field_name']}")
            print(f"     Query Templates: {field.get('query_templates', [])}")
            print(f"     Sources: {field.get('recommended_sources', [])}")
    except Exception as exc:
        print(f"错误: {exc}")
        import traceback

        traceback.print_exc()


def main() -> None:
    print("Planner LLM 测试脚本")
    print("=" * 80)
    print("只需要修改顶部 TEST_CASE，即可复用到所有测试步骤。")
    print("=" * 80)

    test_search_query_generation()
    test_discover_competitors()
    test_plan_dynamic_schema()

    print("\n" + "=" * 80)
    print("测试完成")
    print("=" * 80)


if __name__ == "__main__":
    main()
