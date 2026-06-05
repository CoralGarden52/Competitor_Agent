from __future__ import annotations

import asyncio
from collections import defaultdict
import queue
from typing import Any


class ChatStreamBroker:
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
