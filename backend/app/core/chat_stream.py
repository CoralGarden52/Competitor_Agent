from __future__ import annotations

import asyncio
from collections import defaultdict
import queue
from typing import Any


class BaseChatStreamBroker:
    async def subscribe(self, turn_id: str) -> queue.Queue[dict[str, Any]]:
        raise NotImplementedError

    async def unsubscribe(self, turn_id: str, stream_queue: queue.Queue[dict[str, Any]]) -> None:
        raise NotImplementedError

    def publish(self, turn_id: str, event_type: str, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self, turn_id: str) -> None:
        raise NotImplementedError


class InMemoryChatStreamBroker(BaseChatStreamBroker):
    def __init__(self) -> None:
        self._queues: dict[str, list[queue.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, turn_id: str) -> queue.Queue[dict[str, Any]]:
        stream_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        async with self._lock:
            self._queues[turn_id].append(stream_queue)
        return stream_queue

    async def unsubscribe(self, turn_id: str, stream_queue: queue.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            queues = self._queues.get(turn_id, [])
            if stream_queue in queues:
                queues.remove(stream_queue)
            if not queues and turn_id in self._queues:
                self._queues.pop(turn_id, None)

    def publish(self, turn_id: str, event_type: str, data: dict[str, Any]) -> None:
        event = {'event': event_type, 'data': data}
        for stream_queue in list(self._queues.get(turn_id, [])):
            try:
                stream_queue.put_nowait(event)
            except Exception:
                continue

    def close(self, turn_id: str) -> None:
        self.publish(turn_id, 'chat_close', {'turn_id': turn_id})


class RedisChatStreamBroker(BaseChatStreamBroker):
    def __init__(self, cache: Any) -> None:
        self.cache = cache
        self._subscriptions: dict[int, Any] = {}

    def _channel(self, turn_id: str) -> str:
        return f'ca:chat:stream:{turn_id}'

    async def subscribe(self, turn_id: str) -> queue.Queue[dict[str, Any]]:
        subscription = await self.cache.subscribe(self._channel(turn_id)) if self.cache is not None else None
        if subscription is None:
            return queue.Queue()
        stream_queue = subscription.stream_queue
        self._subscriptions[id(stream_queue)] = subscription
        return stream_queue

    async def unsubscribe(self, turn_id: str, stream_queue: queue.Queue[dict[str, Any]]) -> None:  # noqa: ARG002
        subscription = self._subscriptions.pop(id(stream_queue), None)
        if subscription is None:
            return
        await subscription.close()

    def publish(self, turn_id: str, event_type: str, data: dict[str, Any]) -> None:
        if self.cache is None:
            return
        self.cache.publish(self._channel(turn_id), {'event': event_type, 'data': data})

    def close(self, turn_id: str) -> None:
        self.publish(turn_id, 'chat_close', {'turn_id': turn_id})


ChatStreamBroker = InMemoryChatStreamBroker
