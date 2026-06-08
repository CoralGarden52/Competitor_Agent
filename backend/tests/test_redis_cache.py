from __future__ import annotations

import asyncio
import hashlib
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.cache import WorkflowCache
from app.core.chat_stream import InMemoryChatStreamBroker, RedisChatStreamBroker
from app.core.config import AppConfig
from app.core.models import EventRecord, Report, RunState, StageName
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService


@dataclass
class _CacheConfig:
    redis_default_ttl_seconds: int = 300
    redis_workspace_ttl_seconds: int = 60
    redis_chat_ttl_seconds: int = 300
    redis_report_chunks_ttl_seconds: int = 1800


class _FakeSubscription:
    def __init__(self, stream_queue: queue.Queue[dict[str, Any]], close_cb: Any) -> None:
        self.stream_queue = stream_queue
        self.close = close_cb


class _FakeRedisRuntime:
    def __init__(self) -> None:
        self.enabled = True
        self.values: dict[str, Any] = {}
        self.channels: dict[str, list[queue.Queue[dict[str, Any]]]] = {}

    def get_json(self, key: str) -> Any | None:
        return self.values.get(key)

    def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> bool:  # noqa: ARG002
        self.values[key] = value
        return True

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.values:
                self.values.pop(key, None)
                deleted += 1
        return deleted

    def delete_prefix(self, prefix: str) -> int:
        keys = [key for key in self.values if key.startswith(prefix)]
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    def publish(self, channel: str, data: dict[str, Any]) -> bool:
        for item in list(self.channels.get(channel, [])):
            item.put_nowait(data)
        return True

    async def subscribe(self, channel: str) -> _FakeSubscription:
        stream_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.channels.setdefault(channel, []).append(stream_queue)

        async def _close() -> None:
            queues = self.channels.get(channel, [])
            if stream_queue in queues:
                queues.remove(stream_queue)
            if not queues and channel in self.channels:
                self.channels.pop(channel, None)

        return _FakeSubscription(stream_queue, _close)


def _build_cache() -> WorkflowCache:
    return WorkflowCache(_FakeRedisRuntime(), _CacheConfig())


def _build_service(tmp_path: Path) -> tuple[CompetitorWorkflowService, WorkflowCache]:
    cache = _build_cache()
    store = SQLiteStore(tmp_path / 'cache_test.db', cache_backend=cache)
    service = CompetitorWorkflowService(store, cache=cache, chat_stream_broker=InMemoryChatStreamBroker())
    return service, cache


def test_storage_populates_run_state_and_runs_list_cache(tmp_path: Path) -> None:
    cache = _build_cache()
    store = SQLiteStore(tmp_path / 'store_cache.db', cache_backend=cache)
    state = RunState(industry='saas', competitors=['alpha'], status='completed')

    store.save_state(state)

    cached_state = cache.get_run_state(state.run_id)
    assert isinstance(cached_state, dict)
    assert cached_state['run_id'] == state.run_id

    runs = store.list_runs(limit=20)
    assert runs
    cached_runs = cache.get_runs_list(20)
    assert isinstance(cached_runs, list)
    assert cached_runs[0]['run_id'] == state.run_id


def test_save_state_preserves_existing_task_summary_when_incoming_state_is_stale(tmp_path: Path) -> None:
    cache = _build_cache()
    store = SQLiteStore(tmp_path / 'store_cache.db', cache_backend=cache)
    state = RunState(industry='saas', competitors=['alpha'], status='running', user_prompt='在线会议软件竞品分析')

    store.save_state(state)
    store.update_run_task_summary(state.run_id, '在线会议软件竞品分析')

    state.status = 'completed'
    store.save_state(state)

    persisted = store.get_state(state.run_id)
    assert persisted is not None
    assert persisted.task_summary == '在线会议软件竞品分析'

    cached_state = cache.get_run_state(state.run_id)
    assert isinstance(cached_state, dict)
    assert cached_state['task_summary'] == '在线会议软件竞品分析'

    cached_summary = cache.get_run_summary(state.run_id)
    assert isinstance(cached_summary, dict)
    assert cached_summary['task_summary'] == '在线会议软件竞品分析'


def test_app_config_exposes_redis_settings() -> None:
    config = AppConfig()
    assert isinstance(config.redis_enabled, bool)
    assert config.redis_host
    assert config.redis_port > 0


def test_workspace_cache_invalidates_when_event_is_appended(tmp_path: Path) -> None:
    service, cache = _build_service(tmp_path)
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='summary', markdown='# Report\n\nBody'),
        status='completed',
    )
    service.store.save_state(state)

    payload = service.workspace_payload(state.run_id)
    assert payload['run']['run_id'] == state.run_id
    assert cache.get_workspace(state.run_id) is not None

    service.store.append_event(
        EventRecord(run_id=state.run_id, stage=StageName.analyze, event_type='tool_event', payload={'ok': True})
    )

    assert cache.get_workspace(state.run_id) is None


def test_chat_payload_and_report_chunks_are_cached(tmp_path: Path) -> None:
    service, cache = _build_service(tmp_path)
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='summary', markdown='# Report\n\n## Pricing\nSeat based pricing.'),
        status='completed',
    )
    service.store.save_state(state)

    payload = service.report_conversation.conversation_payload(state.run_id)
    assert payload['conversation']['run_id'] == state.run_id
    assert cache.get_chat_payload(state.run_id) is not None

    chunks = service.report_conversation._get_report_chunks(run_id=state.run_id, markdown=state.report.markdown)
    assert chunks
    report_hash = hashlib.sha1(state.report.markdown.encode('utf-8')).hexdigest()
    assert cache.get_report_chunks(state.run_id, report_hash) is not None


def test_redis_chat_stream_broker_supports_cross_instance_publish() -> None:
    runtime = _FakeRedisRuntime()
    cache_a = WorkflowCache(runtime, _CacheConfig())
    cache_b = WorkflowCache(runtime, _CacheConfig())
    broker_a = RedisChatStreamBroker(cache_a)
    broker_b = RedisChatStreamBroker(cache_b)

    async def _exercise() -> None:
        stream_queue = await broker_b.subscribe('turn_demo')
        broker_a.publish('turn_demo', 'chat_progress', {'message': 'hello'})
        message = await asyncio.to_thread(stream_queue.get, True, 1)
        assert message['event'] == 'chat_progress'
        assert message['data']['message'] == 'hello'
        await broker_b.unsubscribe('turn_demo', stream_queue)

    asyncio.run(_exercise())


def test_run_stream_events_are_published_for_cross_instance_consumers(tmp_path: Path) -> None:
    runtime = _FakeRedisRuntime()
    cache_writer = WorkflowCache(runtime, _CacheConfig())
    cache_reader = WorkflowCache(runtime, _CacheConfig())
    writer_store = SQLiteStore(tmp_path / 'writer.db', cache_backend=cache_writer)

    async def _exercise() -> None:
        subscription = await cache_reader.subscribe_run_stream('run_demo')
        assert subscription is not None
        writer_store.append_event(
            EventRecord(
                run_id='run_demo',
                stage=StageName.collect,
                event_type='collect.completed',
                payload={'count': 3},
            )
        )
        message = await asyncio.to_thread(subscription.stream_queue.get, True, 1)
        assert message['event_id'] >= 1
        assert message['stage'] == 'collect'
        assert message['event_type'] == 'collect.completed'
        assert message['payload']['count'] == 3
        await subscription.close()

    asyncio.run(_exercise())
