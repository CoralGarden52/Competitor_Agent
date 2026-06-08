from __future__ import annotations

from functools import lru_cache

from app.core.cache import WorkflowCache
from app.core.chat_stream import InMemoryChatStreamBroker, RedisChatStreamBroker
from app.core.config import get_config
from app.core.redis_runtime import RedisRuntime
from app.core.storage import PostgresStore
from app.core.tracing_factory import get_tracing_runtime
from app.core.workflow import CompetitorWorkflowService


@lru_cache(maxsize=1)
def get_service() -> CompetitorWorkflowService:
    config = get_config()
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
