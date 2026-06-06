from __future__ import annotations

import asyncio
import json
import logging
import queue
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

try:
    from redis import Redis
    from redis.asyncio import Redis as AsyncRedis
except Exception:  # noqa: BLE001
    Redis = None
    AsyncRedis = None


@dataclass
class RedisSubscription:
    stream_queue: queue.Queue[dict[str, Any]]
    close: Any


class RedisRuntime:
    def __init__(
        self,
        *,
        enabled: bool,
        host: str,
        port: int,
        password: str,
        db: int,
        max_connections: int,
    ) -> None:
        self.enabled = bool(enabled and Redis is not None and AsyncRedis is not None)
        self._host = host
        self._port = port
        self._password = password
        self._db = db
        self._max_connections = max_connections
        self._client: Redis | None = None
        self._async_client: AsyncRedis | None = None

    def _sync_client(self) -> Redis | None:
        if not self.enabled:
            return None
        if self._client is None:
            try:
                self._client = Redis(
                    host=self._host,
                    port=self._port,
                    password=self._password or None,
                    db=self._db,
                    max_connections=self._max_connections,
                    decode_responses=True,
                )
                self._client.ping()
            except Exception as exc:  # noqa: BLE001
                logger.warning('Redis sync client unavailable, falling back to in-process cache only: %s', exc)
                self.enabled = False
                self._client = None
        return self._client

    def _get_async_client(self) -> AsyncRedis | None:
        if not self.enabled:
            return None
        if self._async_client is None:
            try:
                self._async_client = AsyncRedis(
                    host=self._host,
                    port=self._port,
                    password=self._password or None,
                    db=self._db,
                    max_connections=self._max_connections,
                    decode_responses=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning('Redis async client unavailable, falling back to in-process cache only: %s', exc)
                self.enabled = False
                self._async_client = None
        return self._async_client

    def get_json(self, key: str) -> Any | None:
        client = self._sync_client()
        if client is None:
            return None
        try:
            raw = client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug('Redis get_json failed for %s: %s', key, exc)
            return None

    def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> bool:
        client = self._sync_client()
        if client is None:
            return False
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
            if ttl_seconds and ttl_seconds > 0:
                client.set(key, payload, ex=ttl_seconds)
            else:
                client.set(key, payload)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug('Redis set_json failed for %s: %s', key, exc)
            return False

    def delete(self, *keys: str) -> int:
        client = self._sync_client()
        if client is None or not keys:
            return 0
        try:
            return int(client.delete(*keys))
        except Exception as exc:  # noqa: BLE001
            logger.debug('Redis delete failed for %s: %s', keys, exc)
            return 0

    def delete_prefix(self, prefix: str) -> int:
        client = self._sync_client()
        if client is None or not prefix:
            return 0
        try:
            keys = list(client.scan_iter(match=f'{prefix}*'))
            if not keys:
                return 0
            return int(client.delete(*keys))
        except Exception as exc:  # noqa: BLE001
            logger.debug('Redis delete_prefix failed for %s: %s', prefix, exc)
            return 0

    def publish(self, channel: str, data: dict[str, Any]) -> bool:
        client = self._sync_client()
        if client is None:
            return False
        try:
            client.publish(channel, json.dumps(data, ensure_ascii=False, default=str))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug('Redis publish failed for %s: %s', channel, exc)
            return False

    async def subscribe(self, channel: str) -> RedisSubscription | None:
        client = self._get_async_client()
        if client is None:
            return None
        stream_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        try:
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(channel)
        except Exception as exc:  # noqa: BLE001
            logger.debug('Redis subscribe failed for %s: %s', channel, exc)
            return None

        stop_event = asyncio.Event()

        async def _reader() -> None:
            try:
                while not stop_event.is_set():
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not message:
                        await asyncio.sleep(0.05)
                        continue
                    raw_data = message.get('data')
                    if raw_data is None:
                        continue
                    try:
                        payload = json.loads(str(raw_data))
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(payload, dict):
                        try:
                            stream_queue.put_nowait(payload)
                        except Exception:  # noqa: BLE001
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug('Redis subscription reader failed for %s: %s', channel, exc)
            finally:
                try:
                    await pubsub.unsubscribe(channel)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await pubsub.close()
                except Exception:  # noqa: BLE001
                    pass

        task = asyncio.create_task(_reader())

        async def _close() -> None:
            stop_event.set()
            task.cancel()
            try:
                await task
            except Exception:  # noqa: BLE001
                pass

        return RedisSubscription(stream_queue=stream_queue, close=_close)


class DisabledRedisRuntime(RedisRuntime):
    def __init__(self) -> None:
        super().__init__(enabled=False, host='', port=0, password='', db=0, max_connections=0)
