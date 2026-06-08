from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.collector import router as collector_router
from app.api.runs import router as runs_router
from app.api.schema import router as schema_router
from app.core.logging_setup import configure_logging
from app.core.tracing_factory import get_tracing_runtime

configure_logging()

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title='Competitor Analysis Backend', version='0.1.0')
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )
    app.include_router(runs_router)
    app.include_router(schema_router)
    app.include_router(collector_router)

    @app.get('/healthz')
    def healthz() -> dict[str, str]:
        return {'status': 'ok'}

    @app.on_event('startup')
    def startup_trace_status() -> None:
        runtime = get_tracing_runtime()
        logger.info(
            'Startup tracing status: mode=%s langsmith_enabled=%s project=%s endpoint=%s',
            runtime.mode,
            runtime.langsmith_enabled,
            runtime.project,
            runtime.endpoint,
        )

    return app


app = create_app()
