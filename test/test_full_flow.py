#!/usr/bin/env python
"""Full flow: plan/collect/normalize/analyze -> QA -> recollect(once) -> analyze -> draft."""
from __future__ import annotations

import builtins
import json
import time
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_config
from app.core.models import RunState
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService

TEST_CASE = {
    "name": "在线会议软件竞品分析",
    "prompt": "进行在线会议软件领域的竞品分析",
    "industry": "",
    "hints": [],
    "language": "zh-CN",
    "timeframe": "last_12_months",
    "max_direct": 3,
    "max_substitute": 1,
    "output_dir": "complete_flow_result",
}


def _install_event_log_filter() -> None:
    """Suppress noisy workflow event lines while keeping script-level output."""
    original_print = builtins.print

    def filtered_print(*args, **kwargs):
        text = " ".join(str(x) for x in args)
        if " EVENT: " in text:
            return
        return original_print(*args, **kwargs)

    builtins.print = filtered_print


def _save_analysis_files(*, state: RunState, analyst_output_dir: Path) -> list[str]:
    analyst_output_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for i, analysis in enumerate(state.competitor_analyses or []):
        competitor_name = analysis.product_name if hasattr(analysis, 'product_name') else state.competitors[i]
        payload = {
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
            ],
        }
        path = analyst_output_dir / f'{competitor_name}_analysis.json'
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        files.append(str(path))
    return files


def main() -> None:
    _install_event_log_filter()
    config = get_config()
    store = SQLiteStore(config.sqlite_path_obj)
    service = CompetitorWorkflowService(store)

    print('=' * 80)
    print('全链路流程(Analyze后QA, 回采1轮)')
    print('=' * 80)
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"collector_search_order={config.collector_search_order}")
    print(f"collector_search_order_list={config.collector_search_order_list}")
    print(f"zhihu_search_access_secret_exists={bool(config.zhihu_search_access_secret)}")

    total_start = time.time()

    print('\n[1/7] 动态规划')
    t = time.time()
    plan = service.orchestrator.generate_dynamic_plan(
        prompt=TEST_CASE['prompt'],
        industry=TEST_CASE['industry'],
        competitor_hints=TEST_CASE['hints'],
        max_direct=TEST_CASE['max_direct'],
        max_substitute=TEST_CASE['max_substitute'],
    )
    competitors = plan.get('planned_competitors', [])
    schema_plan = plan.get('analysis_schema_plan', [])
    inferred_industry = str(plan.get('inferred_industry', '')).strip().lower() or 'general'
    planner_meta = plan.get('planner_meta', {}) if isinstance(plan.get('planner_meta', {}), dict) else {}
    candidate_groups = plan.get('candidate_groups', {}) if isinstance(plan.get('candidate_groups', {}), dict) else {}
    candidate_pool = plan.get('candidate_pool', []) if isinstance(plan.get('candidate_pool', []), list) else []
    schema_fields = [
        str(item.get('field_name', '')).strip()
        for item in schema_plan
        if isinstance(item, dict) and str(item.get('field_name', '')).strip()
    ]
    print(f"      competitors={competitors}")
    print(f"      inferred_industry={inferred_industry}")
    print(f"      candidate_groups.direct={candidate_groups.get('direct', [])}")
    print(f"      candidate_groups.substitute={candidate_groups.get('substitute', [])}")
    print(f"      candidate_pool={candidate_pool}")
    print(f"      schema_fields({len(schema_fields)})={schema_fields}")
    if planner_meta:
        print(f"      planner_llm_status={planner_meta.get('llm_call_status', {})}")
        print(f"      planner_llm_status_by_step={planner_meta.get('llm_call_status_by_step', {})}")
    print(f"      done in {time.time() - t:.2f}s")

    print('\n[2/7] 初始化RunState')
    t = time.time()
    state = RunState(
        industry=TEST_CASE['industry'] or inferred_industry,
        competitors=competitors,
        user_prompt=TEST_CASE['prompt'],
        language=TEST_CASE['language'],
        timeframe=TEST_CASE['timeframe'],
        planned_competitors=competitors,
        analysis_schema_plan=schema_plan,
    )
    print(f"      run_id={state.run_id}")
    print(f"      done in {time.time() - t:.2f}s")

    print('\n[3/7] Collect + Normalize')
    t = time.time()
    service._collect(state)
    service._normalize(state)
    print(f"      evidence_count={len(state.evidences or [])}")
    print(f"      done in {time.time() - t:.2f}s")

    print('\n[4/7] Analyze(首轮) + 落盘分析JSON')
    t = time.time()
    service._analyze(state)
    coverage_before_qa = float(state.self_eval.get('analyze').coverage if state.self_eval.get('analyze') else 0.0)
    output_dir = Path(__file__).parent / 'mock_data' / TEST_CASE['output_dir']
    analyst_output_dir = output_dir / 'analyst_output'
    first_files = _save_analysis_files(state=state, analyst_output_dir=analyst_output_dir)
    print(f"      analysis_files={len(first_files)}")
    print(f"      coverage_before_qa={coverage_before_qa:.4f}")
    print(f"      done in {time.time() - t:.2f}s")

    print('\n[5/7] QA(并行审查分析JSON)')
    t = time.time()
    qa = service._qa(state)
    qa_summary = {
        'passed': qa.passed,
        'issue_count': len(qa.issues),
        'target_agent': qa.target_agent,
        'collect_items': len(qa.collect_plan.items) if qa.collect_plan else 0,
    }
    print(f"      qa={qa_summary}")
    print(f"      done in {time.time() - t:.2f}s")

    qa_rework_summary: dict[str, Any] = {
        'triggered': False,
        'incremental_verified': False,
        'reanalyzed_pairs': [],
        'non_target_pairs_checked': 0,
        'collect_items': [],
        'coverage_before_qa': coverage_before_qa,
        'coverage_after_qa': coverage_before_qa,
        'coverage_delta': 0.0,
    }

    if (not qa.passed) and qa.target_agent == 'Collect' and qa.collect_plan and qa.collect_plan.items:
        print('\n[6/7] QA打回: Collect + Normalize + Analyze (仅1轮)')
        t = time.time()
        qa_rework_summary['triggered'] = True
        qa_rework_summary['collect_items'] = [x.model_dump(mode='json') for x in qa.collect_plan.items]

        target_fields_by_competitor: dict[str, set[str]] = {}
        for item in qa.collect_plan.items:
            target_fields_by_competitor.setdefault(item.competitor, set()).add(item.field_name)

        # Snapshot before incremental analyze to verify unchanged non-target fields.
        first_round_summary_map: dict[tuple[str, str], str] = {}
        for record in state.competitor_analyses:
            for field in record.fields:
                first_round_summary_map[(record.product_name, field.field_name)] = field.summary

        service._apply_rework_ticket(state, qa)
        service._collect(state)
        service._normalize(state)
        service._analyze(state)
        coverage_after_qa = float(state.self_eval.get('analyze').coverage if state.self_eval.get('analyze') else 0.0)

        # Strict verification: non-target fields must keep previous summaries.
        non_target_checked = 0
        for record in state.competitor_analyses:
            competitor = record.product_name
            target_fields = target_fields_by_competitor.get(competitor, set())
            for field in record.fields:
                key = (competitor, field.field_name)
                if field.field_name in target_fields:
                    continue
                if key not in first_round_summary_map:
                    continue
                assert (
                    field.summary == first_round_summary_map[key]
                ), f'non-target field changed unexpectedly: {competitor}.{field.field_name}'
                non_target_checked += 1

        reanalyzed_pairs = [
            f'{competitor}.{field_name}'
            for competitor, fields in target_fields_by_competitor.items()
            for field_name in sorted(fields)
        ]
        qa_rework_summary['incremental_verified'] = True
        qa_rework_summary['non_target_pairs_checked'] = non_target_checked
        qa_rework_summary['reanalyzed_pairs'] = sorted(reanalyzed_pairs)
        qa_rework_summary['coverage_after_qa'] = coverage_after_qa
        qa_rework_summary['coverage_delta'] = coverage_after_qa - coverage_before_qa

        print(f"      incremental_verified=True")
        print(f"      reanalyzed_pairs={qa_rework_summary['reanalyzed_pairs']}")
        print(f"      non_target_pairs_checked={non_target_checked}")
        print(f"      coverage_after_qa={coverage_after_qa:.4f}")
        print(f"      coverage_delta={qa_rework_summary['coverage_delta']:+.4f}")
        print(f"      done in {time.time() - t:.2f}s")
    else:
        print('\n[6/7] QA未触发回采，跳过')
        print(f"      coverage_after_qa={coverage_before_qa:.4f} (no recollect)")
        print(f"      coverage_delta={0.0:+.4f}")

    print('\n[7/7] Draft(最终报告) + 结果落盘')
    t = time.time()
    service._draft(state)
    report_content = state.report.markdown if state.report else ''

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')
    report_path = output_dir / f'final_report_{ts}.md'
    if report_content:
        report_path.write_text(report_content, encoding='utf-8')

    qa_rework_file = output_dir / f'qa_rework_result_{ts}.json'
    qa_rework_file.write_text(json.dumps({
        'run_id': state.run_id,
        'qa_summary': qa_summary,
        'rework': qa_rework_summary,
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    result = {
        'run_id': state.run_id,
        'industry': state.industry,
        'competitors': competitors,
        'schema_fields': [s.get('field_name') for s in schema_plan],
        'evidence_count': len(state.evidences or []),
        'analyses_count': len(state.competitor_analyses or []),
        'profiles_count': len(state.profiles or []),
        'findings_count': len(state.findings or []),
        'qa_summary': qa_summary,
        'qa_rework': qa_rework_summary,
        'coverage_before_qa': qa_rework_summary['coverage_before_qa'],
        'coverage_after_qa': qa_rework_summary['coverage_after_qa'],
        'coverage_delta': qa_rework_summary['coverage_delta'],
        'report_exists': bool(report_content),
        'report_length': len(report_content),
        'report_path': str(report_path),
        'qa_rework_result_path': str(qa_rework_file),
        'elapsed_seconds': round(time.time() - total_start, 2),
    }
    result_file = output_dir / 'complete_flow_result.json'
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f"      report={report_path}")
    print(f"      result={result_file}")
    print(f"      done in {time.time() - t:.2f}s")

    print('\n' + '=' * 80)
    print(f"finished in {time.time() - total_start:.2f}s")
    print('=' * 80)


if __name__ == '__main__':
    main()
