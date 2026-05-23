from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.config import AppConfig, get_config
from app.core.deps import get_service
from app.core.workflow import CompetitorWorkflowService

router = APIRouter(prefix='/schema', tags=['schema'])


@router.get('/registry')
def get_registry(industry: str | None = Query(default=None), service: CompetitorWorkflowService = Depends(get_service)) -> dict[str, object]:
    return service.schema_registry(industry=industry)


@router.get('/runtime-config')
def get_runtime_config(config: AppConfig = Depends(get_config)) -> dict[str, object]:
    return config.masked_runtime_config()
