from __future__ import annotations

import hashlib
from typing import Any


class WorkflowCache:
    def __init__(self, runtime: Any, config: Any):
        self.runtime = runtime
        self.config = config
        self._local_values: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return True

    def _get(self, key: str) -> Any | None:
        if getattr(self.runtime, 'enabled', False):
            return self.runtime.get_json(key)
        return self._local_values.get(key)

    def _set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> bool:
        effective_ttl = ttl_seconds if ttl_seconds is not None else int(self.config.redis_default_ttl_seconds)
        if getattr(self.runtime, 'enabled', False):
            return bool(self.runtime.set_json(key, value, ttl_seconds=effective_ttl))
        self._local_values[key] = value
        return True

    def _delete(self, *keys: str) -> int:
        if getattr(self.runtime, 'enabled', False):
            return int(self.runtime.delete(*keys))
        deleted = 0
        for key in keys:
            if key in self._local_values:
                self._local_values.pop(key, None)
                deleted += 1
        return deleted

    def _delete_prefix(self, prefix: str) -> int:
        if getattr(self.runtime, 'enabled', False):
            return int(self.runtime.delete_prefix(prefix))
        keys = [key for key in self._local_values if key.startswith(prefix)]
        for key in keys:
            self._local_values.pop(key, None)
        return len(keys)

    def run_state_key(self, run_id: str) -> str:
        return f'ca:run:{run_id}:state'

    def run_summary_key(self, run_id: str) -> str:
        return f'ca:run:{run_id}:summary'

    def runs_list_key(self, limit: int) -> str:
        return f'ca:runs:list:{limit}'

    def workspace_key(self, run_id: str) -> str:
        return f'ca:run:{run_id}:workspace'

    def chat_payload_key(self, run_id: str) -> str:
        return f'ca:run:{run_id}:chat_payload'

    def conversation_key(self, run_id: str) -> str:
        return f'ca:run:{run_id}:conversation'

    def turn_result_key(self, turn_id: str) -> str:
        return f'ca:chat:turn:{turn_id}:result'

    def report_chunks_key(self, run_id: str, report_hash: str) -> str:
        return f'ca:report:{run_id}:{report_hash}:chunks'

    def corpus_key(self, industry: str, keywords: list[str], limit: int) -> str:
        digest = hashlib.sha1('|'.join(sorted(keywords)).encode('utf-8')).hexdigest()[:16]
        return f'ca:corpus:{industry}:{digest}:{limit}'

    def webpage_key(self, url: str) -> str:
        digest = hashlib.sha1(url.encode('utf-8')).hexdigest()
        return f'ca:webpage:{digest}'

    def run_stream_channel(self, run_id: str) -> str:
        return f'ca:run:stream:{run_id}'

    def set_run_state(self, run_id: str, payload: dict[str, Any]) -> bool:
        return self._set(self.run_state_key(run_id), payload)

    def get_run_state(self, run_id: str) -> dict[str, Any] | None:
        value = self._get(self.run_state_key(run_id))
        return value if isinstance(value, dict) else None

    def set_run_summary(self, run_id: str, payload: dict[str, Any]) -> bool:
        return self._set(self.run_summary_key(run_id), payload)

    def get_run_summary(self, run_id: str) -> dict[str, Any] | None:
        value = self._get(self.run_summary_key(run_id))
        return value if isinstance(value, dict) else None

    def set_runs_list(self, limit: int, payload: list[dict[str, Any]]) -> bool:
        return self._set(self.runs_list_key(limit), payload)

    def get_runs_list(self, limit: int) -> list[dict[str, Any]] | None:
        value = self._get(self.runs_list_key(limit))
        return value if isinstance(value, list) else None

    def invalidate_runs_lists(self) -> int:
        return self._delete_prefix('ca:runs:list:')

    def set_workspace(self, run_id: str, payload: dict[str, Any]) -> bool:
        return self._set(self.workspace_key(run_id), payload, ttl_seconds=int(self.config.redis_workspace_ttl_seconds))

    def get_workspace(self, run_id: str) -> dict[str, Any] | None:
        value = self._get(self.workspace_key(run_id))
        return value if isinstance(value, dict) else None

    def invalidate_workspace(self, run_id: str) -> int:
        return self._delete(self.workspace_key(run_id))

    def set_chat_payload(self, run_id: str, payload: dict[str, Any]) -> bool:
        return self._set(self.chat_payload_key(run_id), payload, ttl_seconds=int(self.config.redis_chat_ttl_seconds))

    def get_chat_payload(self, run_id: str) -> dict[str, Any] | None:
        value = self._get(self.chat_payload_key(run_id))
        return value if isinstance(value, dict) else None

    def invalidate_chat_payload(self, run_id: str) -> int:
        return self._delete(self.chat_payload_key(run_id))

    def set_conversation(self, run_id: str, payload: dict[str, Any]) -> bool:
        return self._set(self.conversation_key(run_id), payload, ttl_seconds=int(self.config.redis_chat_ttl_seconds))

    def get_conversation(self, run_id: str) -> dict[str, Any] | None:
        value = self._get(self.conversation_key(run_id))
        return value if isinstance(value, dict) else None

    def set_turn_result(self, turn_id: str, payload: dict[str, Any]) -> bool:
        return self._set(self.turn_result_key(turn_id), payload, ttl_seconds=int(self.config.redis_chat_ttl_seconds))

    def get_turn_result(self, turn_id: str) -> dict[str, Any] | None:
        value = self._get(self.turn_result_key(turn_id))
        return value if isinstance(value, dict) else None

    def set_report_chunks(self, run_id: str, report_hash: str, payload: list[dict[str, Any]]) -> bool:
        return self._set(self.report_chunks_key(run_id, report_hash), payload, ttl_seconds=int(self.config.redis_report_chunks_ttl_seconds))

    def get_report_chunks(self, run_id: str, report_hash: str) -> list[dict[str, Any]] | None:
        value = self._get(self.report_chunks_key(run_id, report_hash))
        return value if isinstance(value, list) else None

    def set_corpus_search(self, *, industry: str, keywords: list[str], limit: int, payload: list[dict[str, Any]]) -> bool:
        return self._set(self.corpus_key(industry, keywords, limit), payload)

    def get_corpus_search(self, *, industry: str, keywords: list[str], limit: int) -> list[dict[str, Any]] | None:
        value = self._get(self.corpus_key(industry, keywords, limit))
        return value if isinstance(value, list) else None

    def invalidate_corpus_industry(self, industry: str) -> int:
        return self._delete_prefix(f'ca:corpus:{industry}:')

    def set_webpage(self, url: str, payload: dict[str, Any]) -> bool:
        return self._set(self.webpage_key(url), payload)

    def get_webpage(self, url: str) -> dict[str, Any] | None:
        value = self._get(self.webpage_key(url))
        return value if isinstance(value, dict) else None

    def delete_run(self, run_id: str) -> None:
        self._delete(
            self.run_state_key(run_id),
            self.run_summary_key(run_id),
            self.workspace_key(run_id),
            self.chat_payload_key(run_id),
            self.conversation_key(run_id),
        )
        self._delete_prefix(f'ca:report:{run_id}:')
        self.invalidate_runs_lists()

    def publish(self, channel: str, data: dict[str, Any]) -> bool:
        if not getattr(self.runtime, 'enabled', False):
            return False
        return bool(self.runtime.publish(channel, data))

    async def subscribe(self, channel: str) -> Any | None:
        if not getattr(self.runtime, 'enabled', False):
            return None
        return await self.runtime.subscribe(channel)

    def publish_run_event(self, run_id: str, payload: dict[str, Any]) -> bool:
        return self.publish(self.run_stream_channel(run_id), payload)

    async def subscribe_run_stream(self, run_id: str) -> Any | None:
        return await self.subscribe(self.run_stream_channel(run_id))
