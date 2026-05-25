from __future__ import annotations

from dataclasses import dataclass

from app.core.collector.types import FetchProvider, SearchProvider


@dataclass
class ProviderRegistry:
    search_catalog: dict[str, SearchProvider]
    fetch_catalog: dict[str, FetchProvider]
    search_order: list[str]
    fetch_order: list[str]

    def ordered_search_providers(self) -> list[SearchProvider]:
        ordered = [self.search_catalog[name] for name in self.search_order if name in self.search_catalog]
        seen = {provider.name() for provider in ordered}
        ordered.extend(provider for name, provider in self.search_catalog.items() if name not in seen)
        return ordered

    def ordered_fetch_providers(self) -> list[FetchProvider]:
        ordered = [self.fetch_catalog[name] for name in self.fetch_order if name in self.fetch_catalog]
        seen = {provider.name() for provider in ordered}
        ordered.extend(provider for name, provider in self.fetch_catalog.items() if name not in seen)
        return ordered

    def search_provider_names(self) -> list[str]:
        return [p.name() for p in self.ordered_search_providers()]

    def fetch_provider_names(self) -> list[str]:
        return [p.name() for p in self.ordered_fetch_providers()]
