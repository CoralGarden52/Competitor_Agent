from __future__ import annotations

import os
from dataclasses import dataclass


_TRUTHY_VALUES = {'1', 'true', 'yes', 'on'}


def _env_flag_preferred(*names: str) -> bool:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip().lower() in _TRUTHY_VALUES
    return False


def _first_env_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


@dataclass
class LangSmithTracingConfig:
    enabled: bool
    api_key: str | None
    project: str
    endpoint: str

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    @property
    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if self.enabled and not self.api_key:
            missing.append('LANGSMITH_API_KEY (or LANGCHAIN_API_KEY)')
        return missing


@dataclass
class TracingConfig:
    mode: str
    langsmith: LangSmithTracingConfig

    @property
    def normalized_mode(self) -> str:
        mode = (self.mode or '').strip().lower()
        if mode not in {'strict', 'relaxed'}:
            return 'relaxed'
        return mode


def get_tracing_config(default_mode: str = 'relaxed') -> TracingConfig:
    mode = _first_env_value('TRACING_MODE') or default_mode or 'relaxed'
    langsmith = LangSmithTracingConfig(
        enabled=_env_flag_preferred('LANGSMITH_TRACING', 'LANGCHAIN_TRACING_V2', 'LANGCHAIN_TRACING'),
        api_key=_first_env_value('LANGSMITH_API_KEY', 'LANGCHAIN_API_KEY'),
        project=_first_env_value('LANGSMITH_PROJECT', 'LANGCHAIN_PROJECT') or 'competitor-analysis',
        endpoint=_first_env_value('LANGSMITH_ENDPOINT', 'LANGCHAIN_ENDPOINT') or 'https://api.smith.langchain.com',
    )
    return TracingConfig(mode=mode, langsmith=langsmith)
