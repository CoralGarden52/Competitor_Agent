from __future__ import annotations

from typing import Any

from harness.tools.types import ToolError, ToolRequest, ToolResult


class LLMInvokeJsonHandler:
    def __init__(self, call_impl: Any) -> None:
        self.call_impl = call_impl

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
            elif "network" in msg or "timeout" in msg or "closed connection" in msg:
                code = "network_error"
            raise ToolError(code=code, message=str(exc)) from exc
