from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.core.config import get_config
from app.core.tracing_config import TracingConfig, get_tracing_config

logger = logging.getLogger(__name__)


@dataclass
class TracingRuntime:
    mode: str
    langsmith_enabled: bool
    project: str
    endpoint: str
    client: Any | None


def _build_langsmith_client(cfg: TracingConfig) -> Any | None:
    if not cfg.langsmith.enabled or not cfg.langsmith.api_key:
        return None
    from langsmith import Client

    return Client(api_key=cfg.langsmith.api_key, api_url=cfg.langsmith.endpoint)


@lru_cache(maxsize=1)
def get_tracing_runtime() -> TracingRuntime:
    app_cfg = get_config()
    cfg = get_tracing_config(default_mode=app_cfg.tracing_mode)
    mode = cfg.normalized_mode
    missing = cfg.langsmith.missing_fields

    if cfg.langsmith.enabled and missing and mode == 'strict':
        raise RuntimeError(
            f"Tracing mode is strict but LangSmith config is incomplete: missing {', '.join(missing)}"
        )

    if cfg.langsmith.enabled and missing and mode == 'relaxed':
        logger.warning(
            'LangSmith tracing requested but incomplete config detected (%s). Falling back to local SQLite tracing only.',
            ', '.join(missing),
        )
        logger.info('Tracing runtime: mode=%s providers=none', mode)
        return TracingRuntime(mode=mode, langsmith_enabled=False, project=cfg.langsmith.project, endpoint=cfg.langsmith.endpoint, client=None)

    client = _build_langsmith_client(cfg)
    enabled = client is not None
    logger.info(
        'Tracing runtime: mode=%s providers=%s project=%s',
        mode,
        'langsmith' if enabled else 'none',
        cfg.langsmith.project,
    )
    return TracingRuntime(
        mode=mode,
        langsmith_enabled=enabled,
        project=cfg.langsmith.project,
        endpoint=cfg.langsmith.endpoint,
        client=client,
    )
