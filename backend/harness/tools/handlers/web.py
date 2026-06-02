from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from harness.tools.providers.fetch import fetch_with_fallback
from harness.tools.providers.registry import ProviderRegistry
from harness.tools.providers.search import search_with_fallback
from harness.tools.types import ToolError, ToolRequest, ToolResult


class WebSearchHandler:
    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

    def handle(self, request: ToolRequest) -> ToolResult:
        query = str(request.args.get("query", "")).strip()
        allowlist = request.args.get("provider_allowlist")
        trace: list[dict] = []
        hits, local_trace = search_with_fallback(
            query=query,
            registry=self.registry,
            fallback_trace=trace,
            max_results=int(request.args.get("max_results", 8) or 8),
            provider_allowlist=allowlist if isinstance(allowlist, list) else None,
        )
        provider = next((str(item.get("provider", "")) for item in local_trace if item.get("status") == "success"), "")
        serialized_hits: list[dict[str, Any]] = []
        for hit in hits:
            if is_dataclass(hit):
                serialized_hits.append(asdict(hit))
            elif isinstance(hit, dict):
                serialized_hits.append(hit)
        return ToolResult(ok=True, provider=provider, output={"hits": serialized_hits, "trace": local_trace})


class WebFetchHandler:
    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

    def handle(self, request: ToolRequest) -> ToolResult:
        url = str(request.args.get("url", "")).strip()
        trace: list[dict] = []
        content, provider, local_trace = fetch_with_fallback(url=url, registry=self.registry, fallback_trace=trace)
        if not content:
            raise ToolError(code="network_error", message=f"fetch_failed: {url}", provider=provider)
        return ToolResult(ok=True, provider=provider, output={"content": content, "trace": local_trace})


class WebExtractHandler:
    def handle(self, request: ToolRequest) -> ToolResult:
        from app.core.collector.extractor import extract_fields, mask_pii
        from app.core.collector.readability_local import extract_to_markdown

        content = str(request.args.get("content", "") or "")
        title = str(request.args.get("title", "") or "")
        snippet = str(request.args.get("snippet", "") or "")
        markdown = extract_to_markdown(content, title=title or "document", content_type="markdown")
        sanitized = mask_pii(markdown)
        return ToolResult(ok=True, output={"sanitized": sanitized, "extract_fields": extract_fields(sanitized, snippet)})


class CorpusSearchHandler:
    def __init__(self, store: Any) -> None:
        self.store = store

    def handle(self, request: ToolRequest) -> ToolResult:
        documents = self.store.search_comparison_corpus(
            topic_key=str(request.args.get("topic_key", "") or ""),
            industry=str(request.args.get("industry", "") or ""),
            keywords=request.args.get("keywords", []) if isinstance(request.args.get("keywords", []), list) else [],
            limit=int(request.args.get("limit", 8) or 8),
        )
        return ToolResult(ok=True, provider="sqlite", output={"documents": documents})
