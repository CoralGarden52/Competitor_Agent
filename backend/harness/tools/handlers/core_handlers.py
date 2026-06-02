from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from app.core.collector.extractor import extract_fields, mask_pii
from app.core.collector.fetch import fetch_with_fallback
from app.core.collector.provider_registry import ProviderRegistry
from app.core.collector.readability_local import extract_to_markdown
from app.core.collector.search import search_with_fallback
from harness.tools.types import ToolError, ToolRequest, ToolResult, ToolSpec


class WebSearchHandler:
    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

    def spec(self) -> ToolSpec:
        return ToolSpec(name="web.search", group="web", description="search web by providers")

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
        provider = ""
        for item in local_trace:
            if item.get("status") == "success":
                provider = str(item.get("provider", ""))
                break
        serialized_hits: list[dict[str, Any]] = []
        for hit in hits:
            if is_dataclass(hit):
                serialized_hits.append(asdict(hit))
            elif isinstance(hit, dict):
                serialized_hits.append(hit)
            else:
                serialized_hits.append(
                    {
                        "query": str(getattr(hit, "query", "") or ""),
                        "title": str(getattr(hit, "title", "") or ""),
                        "url": str(getattr(hit, "url", "") or ""),
                        "snippet": str(getattr(hit, "snippet", "") or ""),
                        "source_provider": str(getattr(hit, "source_provider", "") or ""),
                        "status": str(getattr(hit, "status", "ok") or "ok"),
                        "latency_ms": int(getattr(hit, "latency_ms", 0) or 0),
                    }
                )
        return ToolResult(ok=True, provider=provider, output={"hits": serialized_hits, "trace": local_trace})


class WebFetchHandler:
    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

    def spec(self) -> ToolSpec:
        return ToolSpec(name="web.fetch", group="web", description="fetch page content")

    def handle(self, request: ToolRequest) -> ToolResult:
        url = str(request.args.get("url", "")).strip()
        trace: list[dict] = []
        content, provider, local_trace = fetch_with_fallback(url=url, registry=self.registry, fallback_trace=trace)
        if not content:
            raise ToolError(code="network_error", message=f"fetch_failed: {url}", provider=provider)
        return ToolResult(ok=True, provider=provider, output={"content": content, "trace": local_trace})


class WebExtractHandler:
    def spec(self) -> ToolSpec:
        return ToolSpec(name="web.extract", group="web", description="extract and sanitize web content")

    def handle(self, request: ToolRequest) -> ToolResult:
        content = str(request.args.get("content", "") or "")
        title = str(request.args.get("title", "") or "")
        snippet = str(request.args.get("snippet", "") or "")
        markdown = extract_to_markdown(content, title=title or "document", content_type="markdown")
        sanitized = mask_pii(markdown)
        fields = extract_fields(sanitized, snippet)
        return ToolResult(ok=True, output={"sanitized": sanitized, "extract_fields": fields})


class LLMInvokeJsonHandler:
    def __init__(self, call_impl: Any) -> None:
        self.call_impl = call_impl

    def spec(self) -> ToolSpec:
        return ToolSpec(name="llm.invoke_json", group="llm", description="invoke llm for json")

    def handle(self, request: ToolRequest) -> ToolResult:
        try:
            parsed = self.call_impl(
                system_prompt=str(request.args.get("system_prompt", "")),
                user_payload=dict(request.args.get("user_payload", {}) or {}),
                trace_name=str(request.args.get("trace_name", "llm.invoke_json")),
                metadata=dict(request.args.get("metadata", {}) or {}),
                network_retries=request.max_retries,
            )
            return ToolResult(ok=True, output={"parsed": parsed})
        except Exception as exc:
            msg = str(exc).lower()
            code = "unknown_error"
            if "429" in msg:
                code = "http_429"
            elif "5" in msg and "http" in msg:
                code = "http_5xx"
            elif "json" in msg:
                code = "validation_error"
            elif "network" in msg or "timeout" in msg:
                code = "network_error"
            raise ToolError(code=code, message=str(exc)) from exc
