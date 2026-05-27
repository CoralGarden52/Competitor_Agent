#!/usr/bin/env python
"""Full flow: plan/collect/normalize/analyze -> QA -> recollect(once) -> analyze -> draft."""
from __future__ import annotations

import json
import time
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent / 'backend'))

from app.core.config import get_config
from app.core.models import AnalysisSchemaField, RunState
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService

TEST_CASE = {
    "name": "协作办公软件全链路(Analyze后QA)",
    "prompt": "帮我做一个协作办公软件的竞品分析",
    "industry": "",
    "hints": [],
    "language": "zh-CN",
    "timeframe": "last_12_months",
    "max_direct": 3,
    "max_substitute": 1,
    "output_dir": "complete_flow_result",
}


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


def _backup_file(path: Path) -> Path:
    stamp = time.strftime('%Y%m%d_%H%M%S')
    backup = path.with_name(f'{path.stem}.backup_{stamp}{path.suffix}')
    backup.write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
    return backup


def _merge_fields(original_fields: list[dict[str, Any]], new_fields: list[dict[str, Any]], target_fields: set[str]) -> list[dict[str, Any]]:
    update_map = {str(x.get('field_name', '')).strip(): x for x in new_fields if isinstance(x, dict) and str(x.get('field_name', '')).strip()}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in original_fields:
        if not isinstance(item, dict):
            merged.append(item)
            continue
        field_name = str(item.get('field_name', '')).strip()
        if field_name in target_fields and field_name in update_map:
            merged.append(update_map[field_name])
            seen.add(field_name)
        else:
            merged.append(item)
    for field_name in sorted(target_fields):
        if field_name not in seen and field_name in update_map:
            merged.append(update_map[field_name])
    return merged


def _competitor_schema_plan_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fields = payload.get("fields", [])
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


def main() -> None:
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
    print(f"      competitors={competitors}")
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
    output_dir = Path(__file__).parent / 'mock_data' / TEST_CASE['output_dir']
    analyst_output_dir = output_dir / 'analyst_output'
    first_files = _save_analysis_files(state=state, analyst_output_dir=analyst_output_dir)
    print(f"      analysis_files={len(first_files)}")
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
        'updated_files': [],
        'backup_files': [],
        'collect_items': [],
    }

    if (not qa.passed) and qa.target_agent == 'Collect' and qa.collect_plan and qa.collect_plan.items:
        print('\n[6/7] QA打回: Collect + Normalize + Analyze (仅1轮)')
        t = time.time()
        qa_rework_summary['triggered'] = True
        qa_rework_summary['collect_items'] = [x.model_dump(mode='json') for x in qa.collect_plan.items]

        target_fields_by_competitor: dict[str, set[str]] = {}
        for item in qa.collect_plan.items:
            target_fields_by_competitor.setdefault(item.competitor, set()).add(item.field_name)

        first_round_analysis_map = {rec.product_name: rec for rec in state.competitor_analyses}
        service._apply_rework_ticket(state, qa)
        service._collect(state)
        service._normalize(state)

        rerun_analysis_map: dict[str, Any] = {}
        for competitor, target_fields in target_fields_by_competitor.items():
            path = analyst_output_dir / f'{competitor}_analysis.json'
            if not path.exists():
                continue

            payload_before = json.loads(path.read_text(encoding='utf-8'))
            competitor_schema_plan = _competitor_schema_plan_from_payload(payload_before)
            if not competitor_schema_plan:
                continue

            competitor_evidences = []
            for ev in state.evidences or []:
                ev_competitor = ""
                ext = getattr(ev, "domain_extensions", {}) or {}
                if isinstance(ext, dict):
                    ev_competitor = str(ext.get("competitor", "")).strip()
                if ev_competitor == competitor:
                    competitor_evidences.append(ev)

            if not competitor_evidences:
                continue

            rerun_state = RunState(
                run_id=state.run_id,
                industry=state.industry,
                competitors=[competitor],
                planned_competitors=[competitor],
                user_prompt=state.user_prompt,
                language=state.language,
                timeframe=state.timeframe,
                analysis_schema_plan=[AnalysisSchemaField.model_validate(item) for item in competitor_schema_plan],
                evidences=competitor_evidences,
            )
            analyze_out = service.analyst_agent.run_llm(rerun_state)
            if not analyze_out.competitors:
                continue
            rerun_analysis_map[competitor] = analyze_out.competitors[0]

            backup = _backup_file(path)
            qa_rework_summary['backup_files'].append(str(backup))
            old_payload = payload_before
            old_fields = old_payload.get('fields', []) if isinstance(old_payload.get('fields', []), list) else []
            new_fields = [x.model_dump(mode='json') for x in rerun_analysis_map[competitor].fields]
            merged = _merge_fields(old_fields, new_fields, target_fields)
            new_payload = {'competitor': competitor, 'run_id': state.run_id, 'fields': merged}
            path.write_text(json.dumps(new_payload, ensure_ascii=False, indent=2), encoding='utf-8')
            qa_rework_summary['updated_files'].append(str(path))

        if rerun_analysis_map:
            merged_analysis_map = dict(first_round_analysis_map)
            merged_analysis_map.update(rerun_analysis_map)
            state.competitor_analyses = [merged_analysis_map[c] for c in state.competitors if c in merged_analysis_map]

        print(f"      updated_files={len(qa_rework_summary['updated_files'])}")
        print(f"      done in {time.time() - t:.2f}s")
    else:
        print('\n[6/7] QA未触发回采，跳过')

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
