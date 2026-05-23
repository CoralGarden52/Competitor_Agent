from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from typing import Any


@dataclass
class SearchHit:
    query: str
    title: str
    url: str
    snippet: str
    source_provider: str
    status: str = 'ok'
    latency_ms: int = 0


@dataclass
class FetchResult:
    url: str
    content: str
    status: str
    provider: str


@dataclass
class ProviderHealth:
    provider: str
    capabilities: list[str]
    available: bool
    auth_ready: bool
    rate_limited: bool = False
    note: str = ''


class SearchProvider(Protocol):
    def name(self) -> str: ...

    def health(self) -> ProviderHealth: ...

    def search(self, query: str, max_results: int) -> tuple[list[SearchHit], list[str]]: ...


class FetchProvider(Protocol):
    def name(self) -> str: ...

    def health(self) -> ProviderHealth: ...

    def fetch(self, url: str) -> tuple[str, list[str]]: ...


@dataclass
class CollectorOutput:
    evidences: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    provider_events: list[dict[str, Any]] = field(default_factory=list)
