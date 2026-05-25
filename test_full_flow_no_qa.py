#!/usr/bin/env python
"""运行完整流程到报告生成（跳过QA阶段）"""
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'backend'))

from app.core.config import get_config
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService
from app.core.models import RunState

TEST_CASE = {
    "name": "协作办公软件完整流程",
    "prompt": "帮我做一个协作办公软件的竞品分析",
    "industry": "",
    "hints": [],
    "language": "zh-CN",
    "timeframe": "last_12_months",
    "max_direct": 5,
    "max_substitute": 3,
    "output_dir": "complete_flow_result",
}


def print_case_summary() -> None:
    print(f"测试用例: {TEST_CASE['name']}")
    print(f"Prompt: {TEST_CASE['prompt']}")
    print(f"Industry: {TEST_CASE['industry']}")
    print(f"Hints: {TEST_CASE['hints']}")
    print(f"Language: {TEST_CASE['language']}")
    print(f"Timeframe: {TEST_CASE['timeframe']}")
    print(f"Max Direct: {TEST_CASE['max_direct']}")
    print(f"Max Substitute: {TEST_CASE['max_substitute']}")


def main():
    config = get_config()
    store = SQLiteStore(config.sqlite_path_obj)
    service = CompetitorWorkflowService(store)

    print("=" * 80)
    print("完整竞品分析流程（跳过QA）")
    print("=" * 80)
    print_case_summary()
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    total_start = time.time()

    # 1. 竞品发现
    print("[1/4] 竞品发现 (generate_dynamic_plan)...")
    start = time.time()

    plan = service.orchestrator.generate_dynamic_plan(
        prompt=TEST_CASE["prompt"],
        industry=TEST_CASE["industry"],
        competitor_hints=TEST_CASE["hints"],
        max_direct=TEST_CASE["max_direct"],
        max_substitute=TEST_CASE["max_substitute"],
    )

    step1_time = time.time() - start
    competitors = plan.get('planned_competitors', [])
    schema_plan = plan.get('analysis_schema_plan', [])
    inferred_industry = str(plan.get('inferred_industry', '')).strip().lower()

    print(f"      ✓ 完成，耗时: {step1_time:.2f}s")
    print(f"      推断行业: {inferred_industry or 'general'}")
    print(f"      发现竞品: {competitors}")
    print(f"      Schema字段: {[s.get('field_name') for s in schema_plan]}")

    # 2. 创建 RunState
    print()
    print("[2/4] 初始化 RunState...")
    start = time.time()

    state = RunState(
        industry=TEST_CASE["industry"] or inferred_industry or 'general',
        competitors=competitors,
        user_prompt=TEST_CASE["prompt"],
        language=TEST_CASE["language"],
        timeframe=TEST_CASE["timeframe"],
        planned_competitors=competitors,
        analysis_schema_plan=schema_plan,
    )

    step2_time = time.time() - start
    print(f"      ✓ 完成，耗时: {step2_time:.2f}s")
    print(f"      Run ID: {state.run_id}")

    # 3. 信息采集（collect）
    print()
    print("[3/4] 信息采集 (collect)...")
    print(f"      竞品数量: {len(competitors)}")
    start = time.time()

    service._collect(state)

    step3_time = time.time() - start
    print(f"      ✓ 完成，耗时: {step3_time:.2f}s")
    print(f"      证据数量: {len(state.evidences or [])}")

    # 4. 规范化（normalize）
    print()
    print("[4/5] 规范化 (normalize)...")
    start = time.time()

    service._normalize(state)

    step4_time = time.time() - start
    print(f"      ✓ 完成，耗时: {step4_time:.2f}s")
    print(f"      规范化后证据数量: {len(state.evidences or [])}")

    # 5. 分析总结
    print()
    print("[5/5] 分析总结 + 报告生成...")
    start = time.time()

    service._analyze(state)
    analyze_time = time.time() - start
    print(f"      ✓ 分析完成，耗时: {analyze_time:.2f}s")
    print(f"      分析记录数: {len(state.competitor_analyses or [])}")
    print(f"      概要数: {len(state.profiles or [])}")
    print(f"      发现数: {len(state.findings or [])}")

    # 5. 报告生成
    start2 = time.time()
    service._draft(state)
    draft_time = time.time() - start2
    print(f"      ✓ 报告生成完成，耗时: {draft_time:.2f}s")

    step5_time = time.time() - start

    # 汇总时间
    total_time = time.time() - total_start

    print()
    print("=" * 80)
    print("时间汇总")
    print("=" * 80)
    print(f"  1. 竞品发现: {step1_time:.2f}s ({step1_time/total_time*100:.1f}%)")
    print(f"  2. 初始化:    {step2_time:.2f}s ({step2_time/total_time*100:.1f}%)")
    print(f"  3. 信息采集: {step3_time:.2f}s ({step3_time/total_time*100:.1f}%)")
    print(f"  4. 规范化:   {step4_time:.2f}s ({step4_time/total_time*100:.1f}%)")
    print(f"  5. 分析+报告: {step5_time:.2f}s ({step5_time/total_time*100:.1f}%)")
    print(f"     - 分析:    {analyze_time:.2f}s")
    print(f"     - 报告:    {draft_time:.2f}s")
    print(f"  ----------------------------------------")
    print(f"  总计: {total_time:.2f}s")
    print()

    # 获取报告内容
    report_content = state.report.markdown if state.report else ""

    # 保存结果
    output_dir = Path(__file__).parent / 'mock_data' / TEST_CASE["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    # 保存报告
    if report_content:
        report_path = output_dir / 'final_report.md'
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_content)
        print(f"✓ 报告已保存到: {report_path}")

    # 保存详细结果
    result = {
        'timing': {
            'plan': step1_time,
            'init': step2_time,
            'collect': step3_time,
            'normalize': step4_time,
            'analyze': analyze_time,
            'draft': draft_time,
            'analyze_draft_total': step5_time,
            'total': total_time
        },
        'run_id': state.run_id,
        'industry': state.industry,
        'competitors': competitors,
        'schema_fields': [s.get('field_name') for s in schema_plan],
        'evidence_count': len(state.evidences or []),
        'analyses_count': len(state.competitor_analyses or []),
        'profiles_count': len(state.profiles or []),
        'findings_count': len(state.findings or []),
        'report_exists': bool(report_content),
        'report_length': len(report_content)
    }

    with open(output_dir / 'complete_flow_result.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"✓ 结果已保存到: {output_dir / 'complete_flow_result.json'}")

    # 保存分析 agent 的详细输出
    analyst_output_dir = output_dir / 'analyst_output'
    analyst_output_dir.mkdir(parents=True, exist_ok=True)

    # 保存每个竞品的分析详情
    for i, analysis in enumerate(state.competitor_analyses or []):
        competitor_name = analysis.product_name if hasattr(analysis, 'product_name') else competitors[i] if i < len(competitors) else f'competitor_{i}'

        analysis_data = {
            'competitor': competitor_name,
            'run_id': state.run_id,
            'fields': [
                {
                    'field_name': field.field_name,
                    'summary': field.summary,
                    'confidence': field.confidence,
                    'evidence_refs': field.evidence_refs,
                    'normalized_value': field.normalized_value,
                    'evidence_gaps': field.evidence_gaps,
                }
                for field in analysis.fields
            ] if hasattr(analysis, 'fields') else [],
        }

        output_file = analyst_output_dir / f'{competitor_name}_analysis.json'
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(analysis_data, f, ensure_ascii=False, indent=2)
        print(f"✓ 分析详情已保存到: {output_file}")

    # 保存所有发现
    if state.findings:
        findings_data = [
            {
                'statement': f.statement if hasattr(f, 'statement') else str(f),
                'category': f.category if hasattr(f, 'category') else 'unknown',
                'evidence_refs': f.evidence_refs if hasattr(f, 'evidence_refs') else [],
                'competitor': f.competitor if hasattr(f, 'competitor') else 'unknown',
                'impact': f.impact if hasattr(f, 'impact') else 'medium',
                'confidence': f.confidence if hasattr(f, 'confidence') else 0.5,
            }
            for f in state.findings
        ]

        findings_file = analyst_output_dir / 'all_findings.json'
        with open(findings_file, 'w', encoding='utf-8') as f:
            json.dump(findings_data, f, ensure_ascii=False, indent=2)
        print(f"✓ 发现已保存到: {findings_file}")

    # 保存概要
    if state.profiles:
        profiles_data = []
        for p in state.profiles:
            try:
                # 安全地序列化每个字段
                profile = {
                    'product_name': p.product_name if hasattr(p, 'product_name') else 'unknown',
                    'feature_tree': [],
                    'advantages': [],
                    'disadvantages': [],
                    'pricing_model': {},
                    'user_feedback': {},
                }
                
                # 序列化 feature_tree
                if hasattr(p, 'feature_tree') and p.feature_tree:
                    for node in p.feature_tree:
                        if hasattr(node, 'model_dump'):
                            profile['feature_tree'].append(node.model_dump(mode='json'))
                        else:
                            profile['feature_tree'].append(str(node))
                
                # 序列化 advantages
                if hasattr(p, 'advantages') and p.advantages:
                    profile['advantages'] = [str(a) for a in p.advantages]
                
                # 序列化 disadvantages
                if hasattr(p, 'disadvantages') and p.disadvantages:
                    profile['disadvantages'] = [str(d) for d in p.disadvantages]
                
                # 序列化 pricing_model
                if hasattr(p, 'pricing_model') and p.pricing_model:
                    if hasattr(p.pricing_model, 'model_dump'):
                        profile['pricing_model'] = p.pricing_model.model_dump(mode='json')
                    else:
                        profile['pricing_model'] = str(p.pricing_model)
                
                # 序列化 user_feedback
                if hasattr(p, 'user_feedback') and p.user_feedback:
                    if hasattr(p.user_feedback, 'model_dump'):
                        profile['user_feedback'] = p.user_feedback.model_dump(mode='json')
                    else:
                        profile['user_feedback'] = str(p.user_feedback)
                
                profiles_data.append(profile)
            except Exception as e:
                print(f"    ⚠ 序列化 profile 失败: {e}")
        
        if profiles_data:
            profiles_file = analyst_output_dir / 'all_profiles.json'
            with open(profiles_file, 'w', encoding='utf-8') as f:
                json.dump(profiles_data, f, ensure_ascii=False, indent=2)
            print(f"✓ 概要已保存到: {profiles_file}")

    # 打印报告预览
    print()
    print("=" * 80)
    print("报告预览（前5000字）")
    print("=" * 80)
    if report_content:
        preview = report_content[:5000]
        print(preview)
        if len(report_content) > 5000:
            print(f"\n... (报告共 {len(report_content)} 字，继续查看请打开文件)")
    else:
        print("✗ 报告生成失败")

    print()
    print("=" * 80)
    print("流程完成！")
    print("=" * 80)


if __name__ == '__main__':
    main()
