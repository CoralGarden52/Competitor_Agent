from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable


HookPoint = str
HookCallback = Callable[['HookContext'], None]


@dataclass
class HookContext:
    hook_point: HookPoint
    run_id: str = ''
    attempt: int = 0
    stage: str = ''
    agent_name: str = ''
    trace_name: str = ''
    payload: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class HookRegistry:
    def __init__(self) -> None:
        self._callbacks: dict[HookPoint, list[HookCallback]] = {}

    def register(self, hook_point: HookPoint, callback: HookCallback) -> None:
        self._callbacks.setdefault(hook_point, []).append(callback)

    def emit(self, hook_point: HookPoint, context: HookContext) -> None:
        callbacks = self._callbacks.get(hook_point, [])
        for callback in callbacks:
            try:
                callback(context)
            except Exception:
                continue


class AuditHook:
    def __init__(self, event_sink: Callable[[str, dict[str, Any]], None]) -> None:
        self._event_sink = event_sink

    def __call__(self, context: HookContext) -> None:
        payload = {
            'hook_point': context.hook_point,
            'run_id': context.run_id,
            'attempt': context.attempt,
            'stage': context.stage,
            'agent_name': context.agent_name,
            'trace_name': context.trace_name,
            'payload': context.payload,
            'error': context.error,
            'created_at': context.created_at,
        }
        self._event_sink('hook.event', payload)
