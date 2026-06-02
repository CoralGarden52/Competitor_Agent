from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from harness.tools.handlers import CorpusSearchHandler, LLMInvokeJsonHandler, WebExtractHandler, WebFetchHandler, WebSearchHandler
from harness.tools.providers import ProviderRegistry, build_fetch_provider_catalog, build_search_provider_catalog
from harness.tools.registry import ToolRegistry
from harness.tools.router import EventSink, ToolRouter
from harness.tools.specs import CORPUS_SEARCH_SPEC, LLM_INVOKE_JSON_SPEC, WEB_EXTRACT_SPEC, WEB_FETCH_SPEC, WEB_SEARCH_SPEC


@dataclass
class ToolRuntime:
    registry: ToolRegistry
    router: ToolRouter
    provider_registry: ProviderRegistry


def build_tool_runtime(config: Any, *, event_sink: EventSink | None = None, store: Any | None = None) -> ToolRuntime:
    providers = ProviderRegistry(
        search_catalog=build_search_provider_catalog(config),
        fetch_catalog=build_fetch_provider_catalog(config),
        search_order=config.collector_search_order_list,
        fetch_order=config.collector_fetch_order_list,
        strict_search_order=config.collector_search_order_strict,
    )
    registry = ToolRegistry()
    registry.register(spec=WEB_SEARCH_SPEC, handler=WebSearchHandler(providers))
    registry.register(spec=WEB_FETCH_SPEC, handler=WebFetchHandler(providers))
    registry.register(spec=WEB_EXTRACT_SPEC, handler=WebExtractHandler())
    if store is not None:
        registry.register(spec=CORPUS_SEARCH_SPEC, handler=CorpusSearchHandler(store))
    return ToolRuntime(registry=registry, router=ToolRouter(registry, event_sink=event_sink), provider_registry=providers)


def register_internal_llm_tool(runtime: ToolRuntime, call_impl: Callable[..., dict[str, Any]]) -> None:
    runtime.registry.register(spec=LLM_INVOKE_JSON_SPEC, handler=LLMInvokeJsonHandler(call_impl))
