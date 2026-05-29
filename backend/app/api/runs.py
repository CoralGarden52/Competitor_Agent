from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.core.deps import get_service
from app.core.models import RunRequest, RunResponse, RunSummary
from app.core.workflow import CompetitorWorkflowService

router = APIRouter(prefix='/runs', tags=['runs'])


@router.post('', response_model=RunResponse)
def create_run(payload: RunRequest, service: CompetitorWorkflowService = Depends(get_service)) -> RunResponse:
    return service.start_run(payload)


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
def get_run_events(run_id: str, service: CompetitorWorkflowService = Depends(get_service)) -> list[dict]:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='run not found')
    return service.list_run_events(run_id)


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
