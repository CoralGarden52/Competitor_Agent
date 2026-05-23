from __future__ import annotations

from functools import lru_cache

from app.core.config import get_config
from app.core.storage import SQLiteStore
from app.core.tracing_factory import get_tracing_runtime
from app.core.workflow import CompetitorWorkflowService


@lru_cache(maxsize=1)
def get_service() -> CompetitorWorkflowService:
    config = get_config()
    _ = get_tracing_runtime()
    store = SQLiteStore(config.sqlite_path_obj)
    return CompetitorWorkflowService(store)
