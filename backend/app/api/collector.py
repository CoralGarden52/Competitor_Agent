from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.collector_models import CollectorPreviewRequest
from app.core.deps import get_service
from app.core.workflow import CompetitorWorkflowService

router = APIRouter(prefix='/collector', tags=['collector'])


@router.post('/preview')
def collector_preview(payload: CollectorPreviewRequest, service: CompetitorWorkflowService = Depends(get_service)) -> dict:
    return service.collector_preview(
        prompt=payload.prompt,
        industry_hint=payload.industry_hint,
        competitor_hints=payload.competitor_hints,
        deep_dive=payload.deep_dive,
    )


@router.get('/providers/health')
def collector_health(service: CompetitorWorkflowService = Depends(get_service)) -> dict:
    return service.collector_provider_health()


@router.get('/llm/health')
def collector_llm_health(service: CompetitorWorkflowService = Depends(get_service)) -> dict:
    return service.collector_llm_health()
