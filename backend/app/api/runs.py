from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.deps import get_service
from app.core.models import RunRequest, RunResponse, RunSummary
from app.core.workflow import CompetitorWorkflowService

router = APIRouter(prefix='/runs', tags=['runs'])


@router.post('', response_model=RunResponse)
def create_run(payload: RunRequest, service: CompetitorWorkflowService = Depends(get_service)) -> RunResponse:
    return service.start_run_async(payload)


@router.get('', response_model=list[RunSummary])
def list_runs(limit: int = Query(default=20, ge=1, le=100), service: CompetitorWorkflowService = Depends(get_service)) -> list[RunSummary]:
    return service.list_runs(limit)


@router.get('/{run_id}', response_model=RunResponse)
def get_run(run_id: str, service: CompetitorWorkflowService = Depends(get_service)) -> RunResponse:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='run not found')
    return run


@router.get('/{run_id}/events')
def get_run_events(
    run_id: str,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    service: CompetitorWorkflowService = Depends(get_service),
) -> dict:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='run not found')
    items = service.list_run_events(run_id, after_id=after_id, limit=limit)
    last_event_id = after_id
    if items:
        last_event_id = max(int(item.get('event_id', 0) or 0) for item in items)
    return {
        'run_id': run_id,
        'items': items,
        'next_after_id': last_event_id,
        'has_more': len(items) >= limit,
    }


@router.get('/{run_id}/replay')
def replay_run(run_id: str, service: CompetitorWorkflowService = Depends(get_service)) -> dict:
    data = service.replay_run(run_id)
    if data.get('status') == 'not_found':
        raise HTTPException(status_code=404, detail='run not found')
    return data


@router.get('/{run_id}/workspace')
def workspace_run(run_id: str, service: CompetitorWorkflowService = Depends(get_service)) -> dict:
    data = service.workspace_payload(run_id)
    if data.get('status') == 'not_found':
        raise HTTPException(status_code=404, detail='run not found')
    return data


@router.get('/{run_id}/stream')
async def stream_run(run_id: str, service: CompetitorWorkflowService = Depends(get_service)) -> StreamingResponse:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='run not found')

    async def event_generator():
        last_event_id = 0
        workspace_signature: tuple[str, int, int] | None = None

        initial_workspace = service.workspace_payload(run_id)
        if initial_workspace.get('status') != 'not_found':
            initial_run = initial_workspace.get('run', {})
            payload = {
                'run_id': run_id,
                'status': initial_run.get('status', 'running') if isinstance(initial_run, dict) else 'running',
                'workspace': initial_workspace,
            }
            yield f"event: workspace\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            event_list = initial_workspace.get('observability', {}).get('events', []) if isinstance(initial_workspace.get('observability', {}), dict) else []
            if isinstance(event_list, list) and event_list:
                last_event_id = max(int(item.get('event_id', 0) or 0) for item in event_list)
            workspace_signature = (
                str(payload['status']),
                int(initial_run.get('evidence_count', 0) or 0) if isinstance(initial_run, dict) else 0,
                int(initial_run.get('finding_count', 0) or 0) if isinstance(initial_run, dict) else 0,
            )

        while True:
            current_run = service.get_run(run_id)
            if current_run is None:
                payload = {'run_id': run_id, 'status': 'not_found'}
                yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                break

            new_events = service.list_run_events(run_id, after_id=last_event_id, limit=200)
            for item in new_events:
                last_event_id = max(last_event_id, int(item.get('event_id', 0) or 0))
                yield f"event: run_event\ndata: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"

            should_refresh_workspace = bool(new_events) or current_run.state.status in ('completed', 'failed')
            if should_refresh_workspace:
                workspace = service.workspace_payload(run_id)
                run_block = workspace.get('run', {})
                signature = (
                    str(run_block.get('status', 'running')) if isinstance(run_block, dict) else 'running',
                    int(run_block.get('evidence_count', 0) or 0) if isinstance(run_block, dict) else 0,
                    int(run_block.get('finding_count', 0) or 0) if isinstance(run_block, dict) else 0,
                )
                if signature != workspace_signature:
                    payload = {
                        'run_id': run_id,
                        'status': signature[0],
                        'workspace': workspace,
                    }
                    yield f"event: workspace\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    workspace_signature = signature

            if current_run.state.status in ('completed', 'failed'):
                payload = {'run_id': run_id, 'status': current_run.state.status, 'last_event_id': last_event_id}
                yield f"event: run_done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                break

            yield "event: heartbeat\ndata: {\"ok\": true}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


@router.get('/{run_id}/logs/export')
def export_run_logs(run_id: str, service: CompetitorWorkflowService = Depends(get_service)) -> dict:
    data = service.export_run_logs(run_id)
    if data.get('status') == 'not_found':
        raise HTTPException(status_code=404, detail='run not found')
    return data


@router.get('/{run_id}/nodes/{node_name}')
def replay_node(run_id: str, node_name: str, service: CompetitorWorkflowService = Depends(get_service)) -> dict:
    data = service.replay_node(run_id, node_name)
    if data.get('status') == 'not_found':
        raise HTTPException(status_code=404, detail='run not found')
    return data


@router.post('/{run_id}/ops/resume')
def resume_run(run_id: str, service: CompetitorWorkflowService = Depends(get_service)) -> RunResponse:
    result = service.resume_from_checkpoint(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail='checkpoint not found')
    return result


@router.post('/{run_id}/ops/intervene', response_model=RunResponse)
def intervene_run(
    run_id: str,
    payload: dict = Body(..., example={'node_name': 'plan', 'action': 'edit_schema', 'actor': 'judge', 'reason': 'manual approve', 'patch': {'analysis_schema_plan': []}}),
    service: CompetitorWorkflowService = Depends(get_service),
) -> RunResponse:
    result = service.manual_intervene(
        run_id=run_id,
        node_name=str(payload.get('node_name', 'manual')),
        action=str(payload.get('action', 'manual_update')),
        actor=str(payload.get('actor', 'operator')),
        reason=str(payload.get('reason', 'manual intervention')),
        patch=payload.get('patch', {}) if isinstance(payload.get('patch', {}), dict) else {},
    )
    if result is None:
        raise HTTPException(status_code=404, detail='run not found')
    return result
