from __future__ import annotations

from harness.tools.providers.registry import ProviderRegistry
from harness.tools.providers.search import search_with_fallback
from harness.tools.providers.types import ProviderHealth, SearchHit
from harness.tools.providers import providers
from app.core.config import AppConfig


class _FakeSearchProvider:
    def __init__(self, provider_name: str, *, hits: list[SearchHit] | None = None, errors: list[str] | None = None):
        self._name = provider_name
        self._hits = hits or []
        self._errors = errors or []
        self.calls = 0

    def name(self) -> str:
        return self._name

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self._name, capabilities=["web_search"], available=True, auth_ready=True)

    def search(self, query: str, max_results: int) -> tuple[list[SearchHit], list[str]]:
        self.calls += 1
        return self._hits[:max_results], self._errors


def _make_hit(*, query: str, provider: str, url: str) -> SearchHit:
    return SearchHit(
        query=query,
        title=f"{provider} title",
        url=url,
        snippet=f"{provider} snippet",
        source_provider=provider,
    )


def test_allowlist_pricing_only_calls_tavily() -> None:
    query = "notion 官网 价格 套餐"
    tavily = _FakeSearchProvider("tavily", hits=[_make_hit(query=query, provider="tavily", url="https://example.com/pricing")])
    zhihu = _FakeSearchProvider("zhihu_official", hits=[_make_hit(query=query, provider="zhihu_official", url="https://zhihu.com/q/1")])
    registry = ProviderRegistry(
        search_catalog={"zhihu_official": zhihu, "tavily": tavily},
        fetch_catalog={},
        search_order=["zhihu_official", "tavily"],
        fetch_order=[],
    )

    fallback_trace: list[dict] = []
    hits, trace = search_with_fallback(
        query=query,
        registry=registry,
        fallback_trace=fallback_trace,
        provider_allowlist=["tavily"],
    )

    assert len(hits) == 1
    assert hits[0].source_provider == "tavily"
    assert tavily.calls == 1
    assert zhihu.calls == 0
    assert any(item.get("provider") == "tavily" and item.get("status") == "success" for item in trace)


def test_allowlist_pricing_tavily_failure_no_fallback() -> None:
    query = "notion 官网 企业版 计费"
    tavily = _FakeSearchProvider("tavily", hits=[], errors=["tavily timeout"])
    qianfan = _FakeSearchProvider("qianfan", hits=[_make_hit(query=query, provider="qianfan", url="https://example.com/alt-pricing")])
    zhihu = _FakeSearchProvider("zhihu_official", hits=[_make_hit(query=query, provider="zhihu_official", url="https://zhihu.com/q/2")])
    registry = ProviderRegistry(
        search_catalog={"tavily": tavily, "qianfan": qianfan, "zhihu_official": zhihu},
        fetch_catalog={},
        search_order=["tavily", "qianfan", "zhihu_official"],
        fetch_order=[],
    )

    fallback_trace: list[dict] = []
    hits, trace = search_with_fallback(
        query=query,
        registry=registry,
        fallback_trace=fallback_trace,
        provider_allowlist=["tavily"],
    )

    assert hits == []
    assert tavily.calls == 1
    assert qianfan.calls == 0
    assert zhihu.calls == 0
    assert any(item.get("provider") == "tavily" and item.get("status") == "empty" for item in trace)


def test_default_fallback_behavior_unchanged_for_other_fields() -> None:
    query = "notion 核心功能"
    tavily = _FakeSearchProvider("tavily", hits=[], errors=["empty"])
    qianfan = _FakeSearchProvider("qianfan", hits=[_make_hit(query=query, provider="qianfan", url="https://example.com/features")])
    registry = ProviderRegistry(
        search_catalog={"tavily": tavily, "qianfan": qianfan},
        fetch_catalog={},
        search_order=["tavily", "qianfan"],
        fetch_order=[],
    )

    fallback_trace: list[dict] = []
    hits, trace = search_with_fallback(query=query, registry=registry, fallback_trace=fallback_trace)

    assert len(hits) == 1
    assert hits[0].source_provider == "qianfan"
    assert tavily.calls == 1
    assert qianfan.calls == 1
    assert any(item.get("provider") == "tavily" and item.get("status") == "empty" for item in trace)
    assert any(item.get("provider") == "qianfan" and item.get("status") == "success" for item in trace)


def test_zhihu_empty_payload_returns_no_hits(monkeypatch) -> None:
    monkeypatch.setattr(providers, "_http_get_json", lambda *_args, **_kwargs: {"Data": None})
    provider = providers.ZhihuOfficialProvider(AppConfig(zhihu_search_access_secret="secret"))

    hits, errors = provider.search("Zoom user feedback", 3)

    assert hits == []
    assert errors == []
