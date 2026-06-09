from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path

from app.core.cache import WorkflowCache
from app.core.chat_stream import InMemoryChatStreamBroker, RedisChatStreamBroker
from app.core.config import get_config
from app.core.redis_runtime import RedisRuntime
from app.core.storage import PostgresStore
from app.core.tracing_factory import get_tracing_runtime
from app.core.workflow import CompetitorWorkflowService


def _load_mock_site_demo_service():
    config = get_config()
    root = Path(config.mock_site_demo_dir)
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[3] / root
    service_path = root / 'runtime' / 'service.py'
    spec = importlib.util.spec_from_file_location('mock_site_demo.runtime.service', service_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load mock site demo runtime from {service_path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    service_cls = getattr(module, 'MockSiteDemoService', None)
    if service_cls is None:
        raise RuntimeError(f'MockSiteDemoService not found in {service_path}')
    return service_cls(root, config.mock_site_demo_fixture, config)


@lru_cache(maxsize=1)
def get_service() -> CompetitorWorkflowService:
    config = get_config()
    if config.mock_site_demo_enabled:
        return _load_mock_site_demo_service()
    _ = get_tracing_runtime()
    runtime = RedisRuntime(
        enabled=config.redis_enabled,
        host=config.redis_host,
        port=config.redis_port,
        password=config.redis_password,
        db=config.redis_db,
        max_connections=config.redis_max_connections,
    )
    cache = WorkflowCache(runtime, config)
    store = PostgresStore(config.postgres_dsn, cache_backend=cache)
    broker = RedisChatStreamBroker(cache) if runtime.enabled else InMemoryChatStreamBroker()
    return CompetitorWorkflowService(store, cache=cache, chat_stream_broker=broker)
