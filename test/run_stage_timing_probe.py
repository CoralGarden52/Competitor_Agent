from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROMPT = '对在线会议软件进行竞品分析。'
DEFAULT_BASE_URL = 'http://127.0.0.1:8010'
EXPORT_DIR = Path(__file__).resolve().parents[1] / 'collector_exports'
TERMINAL_STATUSES = {'completed', 'failed'}
PROMPT = '\u5bf9\u5728\u7ebf\u4f1a\u8bae\u8f6f\u4ef6\u8fdb\u884c\u7ade\u54c1\u5206\u6790\u3002'
STAGE_LABELS = {
    'plan': 'plan / \u89c4\u5212\u667a\u80fd\u4f53',
    'collect': 'collect / \u91c7\u96c6\u667a\u80fd\u4f53',
    'normalize': 'normalize / \u5f52\u4e00\u5316',
    'analyze': 'analyze / \u5206\u6790\u667a\u80fd\u4f53',
    'draft': 'draft / \u5199\u4f5c\u667a\u80fd\u4f53',
    'qa': 'qa / \u8d28\u68c0\u667a\u80fd\u4f53',
    'finalize': 'finalize / \u6536\u5c3e',
}
STAGE_LABELS = {
    'plan': 'plan / 规划智能体',
    'collect': 'collect / 采集智能体',
    'normalize': 'normalize / 归一化',
    'analyze': 'analyze / 分析智能体',
    'draft': 'draft / 写作智能体',
    'qa': 'qa / 质检智能体',
    'finalize': 'finalize / 收尾',
}
PROMPT = '\u5bf9\u5728\u7ebf\u4f1a\u8bae\u8f6f\u4ef6\u8fdb\u884c\u7ade\u54c1\u5206\u6790\u3002'
STAGE_LABELS = {
    'plan': 'plan / \u89c4\u5212\u667a\u80fd\u4f53',
    'collect': 'collect / \u91c7\u96c6\u667a\u80fd\u4f53',
    'normalize': 'normalize / \u5f52\u4e00\u5316',
    'analyze': 'analyze / \u5206\u6790\u667a\u80fd\u4f53',
    'draft': 'draft / \u5199\u4f5c\u667a\u80fd\u4f53',
    'qa': 'qa / \u8d28\u68c0\u667a\u80fd\u4f53',
    'finalize': 'finalize / \u6536\u5c3e',
}


def _json_default(value: Any) -> str:
    return str(value)


def _now_stamp() -> str:
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace('Z', '+00:00')
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _elapsed_seconds(start: datetime | None, end: datetime | None) -> float:
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _http_json(method: str, url: str, *, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any] | list[Any]:
    data = None
    headers = {'Accept': 'application/json'}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8')
    except HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'{method} {url} failed: HTTP {exc.code} {detail}') from exc
    except URLError as exc:
        raise RuntimeError(
            f'{method} {url} failed: {exc}\n'
            'Backend is not reachable. Start it first, for example:\n'
            '  cd Competitor_Analysis/backend\n'
            '  uv run uvicorn app.main:app --reload --port 8010\n'
            'Or pass the actual backend address with --base-url.'
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(f'{method} {url} timed out after {timeout:.1f}s') from exc
    except socket.timeout as exc:
        raise RuntimeError(f'{method} {url} timed out after {timeout:.1f}s') from exc
    return json.loads(body) if body.strip() else {}


def _safe_http_json(method: str, url: str, *, timeout: float = 30.0, retries: int = 2, retry_sleep: float = 1.0) -> dict[str, Any] | list[Any] | None:
    last_error = ''
    for attempt in range(1, retries + 1):
        try:
            return _http_json(method, url, timeout=timeout)
        except RuntimeError as exc:
            last_error = str(exc)
            print(f'warning: {method} {url} failed on attempt {attempt}/{retries}: {exc}')
            if attempt < retries:
                time.sleep(retry_sleep)
    print(f'warning: giving up this poll request: {last_error}')
    return None


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding='utf-8')


def _append_jsonl(path: Path, item: Any) -> None:
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(item, ensure_ascii=False, default=_json_default) + '\n')


def _event_brief(event: dict[str, Any]) -> str:
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    parts: list[str] = []
    for key in ('agent_name', 'tool', 'decision', 'reason', 'query', 'result_count', 'count', 'status', 'summary'):
        value = payload.get(key)
        if value not in (None, '', [], {}):
            parts.append(f'{key}={value}')
    if not parts and payload:
        keys = ','.join(list(payload.keys())[:6])
        parts.append(f'payload_keys={keys}')
    return '; '.join(parts)


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    envelope = payload.get('envelope') if isinstance(payload.get('envelope'), dict) else {}
    inner = envelope.get('payload') if isinstance(envelope.get('payload'), dict) else None
    return inner or payload


def _stage_stats(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(event.get('stage') or 'unknown')].append(event)
    stats: dict[str, dict[str, Any]] = {}
    for stage, items in grouped.items():
        starts = [_parse_time(str(item.get('created_at') or '')) for item in items]
        starts = [item for item in starts if item is not None]
        first = min(starts) if starts else None
        last = max(starts) if starts else None
        stats[stage] = {
            'stage': stage,
            'label': STAGE_LABELS.get(stage, stage),
            'event_count': len(items),
            'started_at': first.isoformat() if first else '',
            'ended_at': last.isoformat() if last else '',
            'elapsed_seconds': _elapsed_seconds(first, last),
            'event_types': [str(item.get('event_type') or '') for item in items],
        }
    return stats


def _stage_turn_episodes(events: list[dict[str, Any]], final_run_payload: dict[str, Any]) -> list[dict[str, Any]]:
    ordered_events = sorted(
        events,
        key=lambda item: (
            int(item.get('event_id') or 0),
            str(item.get('created_at') or ''),
        ),
    )
    open_turns: dict[int, dict[str, Any]] = {}
    episodes: list[dict[str, Any]] = []
    last_event_at: datetime | None = None

    for event in ordered_events:
        event_type = str(event.get('event_type') or '')
        event_at = _parse_time(str(event.get('created_at') or ''))
        if event_at is not None:
            last_event_at = event_at
        payload = _event_payload(event)
        turn = int(payload.get('turn') or 0)
        if turn <= 0:
            continue

        if event_type == 'runtime.turn.started':
            stage = str(payload.get('from_stage') or event.get('stage') or 'unknown')
            open_turns[turn] = {
                'turn': turn,
                'stage': stage,
                'started_at_dt': event_at,
                'started_at': event_at.isoformat() if event_at else str(event.get('created_at') or ''),
                'start_event_id': event.get('event_id'),
            }
            continue

        if event_type != 'runtime.turn.transitioned':
            continue

        stage = str(payload.get('from_stage') or event.get('stage') or 'unknown')
        start = open_turns.pop(turn, {})
        start_dt = start.get('started_at_dt') if isinstance(start.get('started_at_dt'), datetime) else None
        episodes.append(
            {
                'turn': turn,
                'stage': stage,
                'label': STAGE_LABELS.get(stage, stage),
                'started_at': start.get('started_at', ''),
                'ended_at': event_at.isoformat() if event_at else str(event.get('created_at') or ''),
                'elapsed_seconds': _elapsed_seconds(start_dt, event_at),
                'status': 'completed',
                'start_event_id': start.get('start_event_id'),
                'end_event_id': event.get('event_id'),
                'to_stage': payload.get('to_stage', ''),
                'decision': payload.get('decision', ''),
                'reason': payload.get('reason', ''),
            }
        )

    state = final_run_payload.get('state') if isinstance(final_run_payload.get('state'), dict) else {}
    fallback_stage = str(state.get('current_stage') or state.get('next_stage') or 'unknown')
    for turn, start in sorted(open_turns.items()):
        start_dt = start.get('started_at_dt') if isinstance(start.get('started_at_dt'), datetime) else None
        stage = str(start.get('stage') or fallback_stage)
        end_dt = last_event_at
        episodes.append(
            {
                'turn': turn,
                'stage': stage,
                'label': STAGE_LABELS.get(stage, stage),
                'started_at': start.get('started_at', ''),
                'ended_at': end_dt.isoformat() if end_dt else '',
                'elapsed_seconds': _elapsed_seconds(start_dt, end_dt),
                'status': 'running',
                'start_event_id': start.get('start_event_id'),
                'end_event_id': '',
                'to_stage': '',
                'decision': '',
                'reason': 'open turn at probe stop; ended_at is last observed event time',
            }
        )

    return sorted(episodes, key=lambda item: (int(item.get('turn') or 0), str(item.get('started_at') or '')))


def _stage_duration_stats_from_episodes(episodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        grouped[str(episode.get('stage') or 'unknown')].append(episode)

    stats: dict[str, dict[str, Any]] = {}
    for stage, items in grouped.items():
        starts = [_parse_time(str(item.get('started_at') or '')) for item in items]
        ends = [_parse_time(str(item.get('ended_at') or '')) for item in items]
        starts = [item for item in starts if item is not None]
        ends = [item for item in ends if item is not None]
        stats[stage] = {
            'stage': stage,
            'label': STAGE_LABELS.get(stage, stage),
            'run_count': len(items),
            'event_count': len(items),
            'started_at': min(starts).isoformat() if starts else '',
            'ended_at': max(ends).isoformat() if ends else '',
            'elapsed_seconds': sum(float(item.get('elapsed_seconds') or 0) for item in items),
            'completed_runs': sum(1 for item in items if item.get('status') == 'completed'),
            'running_runs': sum(1 for item in items if item.get('status') == 'running'),
            'turns': [int(item.get('turn') or 0) for item in items],
        }
    return stats


def _stage_timeline_stats(timeline: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for item in timeline:
        stage = str(item.get('node_name') or 'unknown')
        stats[stage] = {
            'stage': stage,
            'label': STAGE_LABELS.get(stage, stage),
            'event_count': 1,
            'status': item.get('status', ''),
            'started_at': item.get('started_at', ''),
            'ended_at': item.get('ended_at', ''),
            'duration_ms': int(item.get('duration_ms') or 0),
            'duration_seconds': round(int(item.get('duration_ms') or 0) / 1000, 3),
            'elapsed_seconds': round(int(item.get('duration_ms') or 0) / 1000, 3),
            'attempt': item.get('attempt', 0),
            'trace_id': item.get('trace_id', ''),
            'error_text': item.get('error_text', ''),
        }
    return stats


def _summarize_llm_calls(llm_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for call in llm_calls:
        parsed = call.get('parsed_response') if isinstance(call.get('parsed_response'), dict) else {}
        metadata = parsed.get('metadata') if isinstance(parsed.get('metadata'), dict) else {}
        output.append(
            {
                'trace_id': call.get('trace_id', ''),
                'node_name': call.get('node_name', ''),
                'agent_name': call.get('agent_name', ''),
                'trace_name': call.get('trace_name', ''),
                'status': call.get('status', ''),
                'model': call.get('model', ''),
                'latency_ms': int(call.get('latency_ms') or 0),
                'prompt_tokens': int(call.get('prompt_tokens') or 0),
                'completion_tokens': int(call.get('completion_tokens') or 0),
                'total_tokens': int(call.get('total_tokens') or 0),
                'finish_reason': call.get('finish_reason', ''),
                'tool_rounds': metadata.get('tool_rounds'),
                'tool_calls': metadata.get('tool_calls'),
                'created_at': call.get('created_at', ''),
                'user_payload_keys': list(call.get('user_payload', {}).keys()) if isinstance(call.get('user_payload'), dict) else [],
            }
        )
    return output


def _plan_diagnostics(
    *,
    events: list[dict[str, Any]],
    replay: dict[str, Any],
    workspace: dict[str, Any],
    event_stage_stats: dict[str, dict[str, Any]],
    turn_stage_stats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    plan_events = [item for item in events if str(item.get('stage') or '') == 'plan']
    llm_calls = replay.get('llm_calls', []) if isinstance(replay.get('llm_calls'), list) else []
    plan_llm_calls = [item for item in llm_calls if str(item.get('node_name') or '') == 'plan']
    handoffs = replay.get('handoffs', []) if isinstance(replay.get('handoffs'), list) else []
    plan_handoffs = [item for item in handoffs if str(item.get('stage') or '') == 'plan']
    decision_history = replay.get('decision_history', []) if isinstance(replay.get('decision_history'), list) else []
    plan_decisions = [
        item for item in decision_history
        if str(item.get('action_type') or '') in {'plan_scope'} or str(item.get('target_agent') or '') == 'PlannerAgent'
    ]
    observability = workspace.get('observability', {}) if isinstance(workspace.get('observability'), dict) else {}
    agent_traces = observability.get('agent_traces', []) if isinstance(observability.get('agent_traces'), list) else []
    plan_trace = next((item for item in agent_traces if str(item.get('stage') or '') == 'plan'), {})
    plan_steps = plan_trace.get('steps', []) if isinstance(plan_trace.get('steps'), list) else []

    llm_summary = _summarize_llm_calls(plan_llm_calls)
    total_llm_ms = sum(int(item.get('latency_ms') or 0) for item in llm_summary)
    total_tokens = sum(int(item.get('total_tokens') or 0) for item in llm_summary)
    tool_rounds = sum(int(item.get('tool_rounds') or 0) for item in llm_summary if item.get('tool_rounds') is not None)
    tool_calls = sum(int(item.get('tool_calls') or 0) for item in llm_summary if item.get('tool_calls') is not None)

    possible_savings: list[str] = []
    if len(plan_llm_calls) > 1:
        possible_savings.append('plan 阶段出现多次 LLM 调用，可检查是否存在 protocol repair、重复 manager decision 或可合并的规划调用。')
    if tool_rounds > 1 or tool_calls > 1:
        possible_savings.append('manager tool loop 使用了多轮/多次 action 工具调用，可检查是否能把 plan_scope 决策收敛为单轮。')
    if total_tokens > 12000:
        possible_savings.append('plan 阶段 token 较高，可检查 decision context 是否携带了过多历史、证据或完整 payload。')
    context_events = [item for item in plan_events if str(item.get('event_type') or '') == 'manager.context.prepared']
    if context_events:
        possible_savings.append('已记录 manager.context.prepared，可重点比较其中 plan_ready、competitor_count、schema_fields 与实际规划输出，裁剪低价值上下文字段。')
    if not possible_savings:
        possible_savings.append('plan 阶段未发现明显重复调用；优先查看 llm_calls 的 latency_ms 和 user_payload_keys，判断瓶颈是模型延迟还是上下文体积。')

    return {
        'plan_event_timing': event_stage_stats.get('plan', {}),
        'plan_turn_timing': turn_stage_stats.get('plan', {}),
        'plan_event_count': len(plan_events),
        'plan_events': plan_events,
        'plan_llm_call_count': len(plan_llm_calls),
        'plan_llm_total_latency_ms': total_llm_ms,
        'plan_llm_total_tokens': total_tokens,
        'plan_llm_calls': llm_summary,
        'plan_tool_rounds_from_llm_metadata': tool_rounds,
        'plan_tool_calls_from_llm_metadata': tool_calls,
        'plan_handoffs': plan_handoffs,
        'plan_decisions': plan_decisions,
        'plan_trace_steps': plan_steps,
        'possible_time_savings': possible_savings,
    }


def _print_stage_table(stats: dict[str, dict[str, Any]]) -> None:
    ordered = ['plan', 'collect', 'normalize', 'analyze', 'draft', 'qa', 'finalize']
    seen = set()
    print('\n=== Stage Timing ===')
    print(f'{"stage":<34} {"events":>6} {"elapsed":>10}')
    print('-' * 54)
    for stage in ordered + sorted(stats):
        if stage in seen or stage not in stats:
            continue
        seen.add(stage)
        item = stats[stage]
        print(f'{item["label"]:<34} {item["event_count"]:>6} {item["elapsed_seconds"]:>8.2f}s')


def _print_stage_duration_summary(stats: dict[str, dict[str, Any]], *, title: str) -> None:
    ordered = ['plan', 'collect', 'normalize', 'analyze', 'draft', 'qa', 'finalize']
    seen = set()
    print(f'\n=== {title} ===')
    for stage in ordered + sorted(stats):
        if stage in seen or stage not in stats:
            continue
        seen.add(stage)
        item = stats[stage]
        seconds = float(item.get('elapsed_seconds') or item.get('duration_seconds') or 0)
        run_count = item.get('run_count')
        run_suffix = f' ({run_count} runs)' if run_count not in (None, '') else ''
        print(f'{STAGE_LABELS.get(stage, stage)}: {seconds:.2f}s{run_suffix}')


def _print_stage_runs(episodes: list[dict[str, Any]]) -> None:
    print('\n=== Stage Runs ===')
    if not episodes:
        print('No runtime turn events were captured.')
        return
    print(f'{"turn":>4}  {"stage":<28} {"started_at":<32} {"ended_at":<32} {"duration":>10}  status')
    print('-' * 126)
    for item in episodes:
        status = str(item.get('status') or '')
        ended_at = str(item.get('ended_at') or '')
        if status == 'running':
            ended_at = f'last seen {ended_at}' if ended_at else 'running'
        print(
            f'{int(item.get("turn") or 0):>4}  '
            f'{str(item.get("label") or item.get("stage") or ""):<28} '
            f'{str(item.get("started_at") or ""):<32} '
            f'{ended_at:<32} '
            f'{float(item.get("elapsed_seconds") or 0):>8.2f}s  '
            f'{status}'
        )


def _print_new_events(events: list[dict[str, Any]]) -> None:
    for event in events:
        stage = str(event.get('stage') or 'unknown')
        event_type = str(event.get('event_type') or '')
        created_at = str(event.get('created_at') or '')
        brief = _event_brief(event)
        suffix = f' | {brief}' if brief else ''
        print(f'[{created_at}] {STAGE_LABELS.get(stage, stage)} -> {event_type}{suffix}')


def _state_summary(run_payload: dict[str, Any]) -> dict[str, Any]:
    state = run_payload.get('state') if isinstance(run_payload.get('state'), dict) else {}
    report = state.get('report') if isinstance(state.get('report'), dict) else {}
    return {
        'run_id': state.get('run_id'),
        'status': state.get('status'),
        'current_stage': state.get('current_stage'),
        'next_stage': state.get('next_stage'),
        'turn_count': state.get('turn_count'),
        'industry': state.get('industry'),
        'competitors': state.get('competitors', []),
        'planned_competitors': state.get('planned_competitors', []),
        'evidence_count': len(state.get('evidences', []) or []),
        'finding_count': len(state.get('findings', []) or []),
        'profile_count': len(state.get('profiles', []) or []),
        'has_report': bool(str(report.get('markdown') or '').strip()),
        'report_chars': len(str(report.get('markdown') or '')),
        'last_error': state.get('last_error', {}),
    }


def run_probe(base_url: str, *, poll_interval: float, timeout_seconds: float, verbose_events: bool = False) -> int:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    started_stamp = _now_stamp()
    prefix = EXPORT_DIR / f'run_stage_timing_{started_stamp}'
    event_log_path = prefix.with_suffix('.events.jsonl')
    summary_path = prefix.with_suffix('.summary.json')
    workspace_path = prefix.with_suffix('.workspace.json')
    state_path = prefix.with_suffix('.state.json')
    replay_path = prefix.with_suffix('.replay.json')
    plan_diagnostics_path = prefix.with_suffix('.plan_diagnostics.json')

    request_payload = {
        'industry': '在线会议软件',
        'competitors': [],
        'user_prompt': PROMPT,
        'language': 'zh-CN',
        'timeframe': 'last_12_months',
    }
    request_payload['industry'] = '\u5728\u7ebf\u4f1a\u8bae\u8f6f\u4ef6'
    print(f'Submitting run: {PROMPT}')
    print(f'Backend: {base_url}')
    _http_json('GET', f'{base_url.rstrip("/")}/healthz', timeout=10)
    run_payload = _http_json('POST', f'{base_url.rstrip("/")}/runs', payload=request_payload, timeout=60)
    run_id = str(run_payload.get('summary', {}).get('run_id') or run_payload.get('state', {}).get('run_id') or '')
    if not run_id:
        raise RuntimeError(f'run_id missing in response: {run_payload}')
    print(f'run_id={run_id}')
    _write_json(state_path, run_payload)

    all_events: list[dict[str, Any]] = []
    after_id = 0
    started_at = time.monotonic()
    final_status = 'running'
    final_run_payload = run_payload
    consecutive_poll_failures = 0

    while True:
        events_payload = _safe_http_json('GET', f'{base_url.rstrip("/")}/runs/{run_id}/events?after_id={after_id}&limit=1000', timeout=15, retries=2)
        new_events = events_payload.get('items', []) if isinstance(events_payload, dict) else []
        if new_events:
            all_events.extend(new_events)
            for event in new_events:
                _append_jsonl(event_log_path, event)
            after_id = int(events_payload.get('next_after_id') or after_id)
            if verbose_events:
                _print_new_events(new_events)

        run_poll_payload = _safe_http_json('GET', f'{base_url.rstrip("/")}/runs/{run_id}', timeout=15, retries=2)
        if isinstance(run_poll_payload, dict):
            final_run_payload = run_poll_payload
            final_status = str(final_run_payload.get('state', {}).get('status') or final_run_payload.get('summary', {}).get('status') or 'running')
            consecutive_poll_failures = 0
        else:
            consecutive_poll_failures += 1
            if consecutive_poll_failures >= 5:
                print('warning: run status polling failed 5 times in a row; stopping probe with partial logs.')
                break
        elapsed = time.monotonic() - started_at
        if final_status in TERMINAL_STATUSES:
            break
        if elapsed > timeout_seconds:
            print(f'timeout after {elapsed:.1f}s, run still {final_status}')
            break
        time.sleep(poll_interval)

    replay_payload: dict[str, Any] = {}
    replay_response = _safe_http_json('GET', f'{base_url.rstrip("/")}/runs/{run_id}/replay', timeout=20, retries=3)
    if isinstance(replay_response, dict):
        replay_payload = replay_response
        _write_json(replay_path, replay_payload)
    else:
        replay_payload = {'replay_fetch_error': 'replay request failed after retries'}
        _write_json(replay_path, replay_payload)

    workspace_payload: dict[str, Any] = {}
    workspace_response = _safe_http_json('GET', f'{base_url.rstrip("/")}/runs/{run_id}/workspace', timeout=20, retries=3)
    if isinstance(workspace_response, dict):
        workspace_payload = workspace_response
        _write_json(workspace_path, workspace_payload)
    else:
        workspace_payload = {'workspace_fetch_error': 'workspace request failed after retries'}
        _write_json(workspace_path, workspace_payload)

    event_stage_stats = _stage_stats(all_events)
    stage_turn_episodes = _stage_turn_episodes(all_events, final_run_payload)
    stage_duration_stats = _stage_duration_stats_from_episodes(stage_turn_episodes)
    runtime_trace_stage_stats = _stage_timeline_stats(replay_payload.get('timeline', []) if isinstance(replay_payload.get('timeline'), list) else [])
    plan_diagnostics = _plan_diagnostics(
        events=all_events,
        replay=replay_payload,
        workspace=workspace_payload,
        event_stage_stats=event_stage_stats,
        turn_stage_stats=stage_duration_stats,
    )
    _write_json(plan_diagnostics_path, plan_diagnostics)
    _print_stage_runs(stage_turn_episodes)
    _print_stage_duration_summary(stage_duration_stats, title='Stage Total Durations')
    print('\n=== Plan Diagnostics ===')
    print(f"plan_event_elapsed_seconds: {plan_diagnostics.get('plan_event_timing', {}).get('elapsed_seconds', 0)}")
    print(f"plan_turn_elapsed_seconds: {plan_diagnostics.get('plan_turn_timing', {}).get('elapsed_seconds', 0)}")
    print(f"plan_llm_call_count: {plan_diagnostics.get('plan_llm_call_count', 0)}")
    print(f"plan_llm_total_latency_ms: {plan_diagnostics.get('plan_llm_total_latency_ms', 0)}")
    print(f"plan_llm_total_tokens: {plan_diagnostics.get('plan_llm_total_tokens', 0)}")
    print(f"plan_tool_rounds: {plan_diagnostics.get('plan_tool_rounds_from_llm_metadata', 0)}")
    print(f"plan_tool_calls: {plan_diagnostics.get('plan_tool_calls_from_llm_metadata', 0)}")
    for item in plan_diagnostics.get('possible_time_savings', []):
        print(f'- {item}')
    total_elapsed = time.monotonic() - started_at
    print('\n=== Final Summary ===')
    final_summary = _state_summary(final_run_payload)
    for key, value in final_summary.items():
        print(f'{key}: {value}')
    print(f'total_wall_seconds: {total_elapsed:.2f}')
    print(f'events_log: {event_log_path}')
    print(f'summary_log: {summary_path}')
    print(f'workspace_log: {workspace_path}')
    print(f'replay_log: {replay_path}')
    print(f'plan_diagnostics_log: {plan_diagnostics_path}')

    summary = {
        'prompt': PROMPT,
        'request_payload': request_payload,
        'base_url': base_url,
        'run_id': run_id,
        'final_status': final_status,
        'total_wall_seconds': total_elapsed,
        'stage_stats': stage_duration_stats,
        'stage_turn_episodes': stage_turn_episodes,
        'event_stage_stats': event_stage_stats,
        'runtime_trace_stage_stats': runtime_trace_stage_stats,
        'plan_diagnostics': plan_diagnostics,
        'state_summary': final_summary,
        'workspace_agent_stages': workspace_payload.get('workflow', {}).get('agent_stages', []) if isinstance(workspace_payload, dict) else [],
        'log_files': {
            'events_jsonl': str(event_log_path),
            'summary_json': str(summary_path),
            'workspace_json': str(workspace_path),
            'state_json': str(state_path),
            'replay_json': str(replay_path),
            'plan_diagnostics_json': str(plan_diagnostics_path),
        },
    }
    _write_json(summary_path, summary)
    _write_json(state_path, final_run_payload)
    return 0 if final_status == 'completed' else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='Run a competitor-analysis timing probe and export per-stage logs.')
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL, help='Backend base URL, default: http://127.0.0.1:8010')
    parser.add_argument('--poll-interval', type=float, default=2.0, help='Polling interval in seconds.')
    parser.add_argument('--timeout-seconds', type=float, default=1800.0, help='Maximum wait time for the run.')
    parser.add_argument('--verbose-events', action='store_true', help='Print every backend event while polling. Events are always written to JSONL.')
    args = parser.parse_args()
    return run_probe(args.base_url, poll_interval=args.poll_interval, timeout_seconds=args.timeout_seconds, verbose_events=args.verbose_events)


if __name__ == '__main__':
    sys.exit(main())
