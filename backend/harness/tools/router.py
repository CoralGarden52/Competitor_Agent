from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any, Callable

from harness.tools.registry import ToolRegistry
from harness.tools.policies import ROLE_TOOL_ALLOWLISTS
from harness.tools.types import ToolError, ToolRequest, ToolResult


EventSink = Callable[[dict[str, Any]], None]


class ToolRouter:
    def __init__(self, registry: ToolRegistry, *, event_sink: EventSink | None = None) -> None:
        self.registry = registry
        self.event_sink = event_sink
        self.hook_emitter: Callable[[str, dict[str, Any]], None] | None = None

    def invoke(self, request: ToolRequest) -> ToolResult:
        self._emit_hook('before_tool', request=request, payload={'args': request.args})
        allowed_tools = request.metadata.get("allowed_tools")
        role_name = str(request.metadata.get("agent_name", "") or "")
        role_allowed = ROLE_TOOL_ALLOWLISTS.get(role_name)
        if role_allowed is not None and request.name not in role_allowed:
            result = ToolResult(ok=False, error_code="forbidden_tool", error_message=f"tool_not_allowed_for_role: {request.name}")
            self._emit("tool.failed", request=request, result=result, retry_count=0, forbidden=True)
            self._emit_hook('on_error', request=request, payload={}, error={'error_code': 'forbidden_tool', 'error_message': result.error_message})
            return result
        if isinstance(allowed_tools, list) and allowed_tools:
            allowed_set = {str(item).strip() for item in allowed_tools if str(item).strip()}
            if request.name not in allowed_set:
                result = ToolResult(
                    ok=False,
                    error_code="forbidden_tool",
                    error_message=f"tool_not_allowed_for_role: {request.name}",
                )
                self._emit("tool.failed", request=request, result=result, retry_count=0, forbidden=True)
                self._emit_hook('on_error', request=request, payload={}, error={'error_code': 'forbidden_tool', 'error_message': result.error_message})
                return result
        try:
            spec = self.registry.get_spec(request.name)
            handler = self.registry.get(request.name)
        except KeyError as exc:
            code = "tool_disabled" if "tool_disabled" in str(exc) else "tool_not_found"
            result = ToolResult(ok=False, error_code=code, error_message=str(exc))
            self._emit("tool.failed", request=request, result=result, retry_count=0)
            return result
        validation_error = self._validate_args(spec.input_schema, request.args)
        if validation_error:
            result = ToolResult(ok=False, error_code="invalid_arguments", error_message=validation_error)
            self._emit("tool.failed", request=request, result=result, retry_count=0)
            return result
        retries = max(0, int(request.max_retries or 0))
        start_all = time.time()
        for attempt in range(retries + 1):
            started = time.time()
            self._emit("tool.called", request=request, retry_count=attempt)
            try:
                result = handler.handle(request)
                result.output = self._json_safe(result.output)
                result.retry_count = attempt
                result.latency_ms = int((time.time() - started) * 1000)
                self._emit("tool.succeeded", request=request, result=result, retry_count=attempt)
                self._emit_hook('after_tool', request=request, payload={'ok': True, 'output': result.output, 'retry_count': attempt})
                return result
            except ToolError as exc:
                if attempt < retries and self._retryable(exc.code):
                    continue
                result = ToolResult(
                    ok=False,
                    error_code=exc.code,
                    error_message=str(exc),
                    provider=exc.provider,
                    retry_count=attempt,
                    latency_ms=int((time.time() - start_all) * 1000),
                )
                self._emit("tool.failed", request=request, result=result, retry_count=attempt)
                self._emit_hook('on_error', request=request, payload={}, error={'error_code': exc.code, 'error_message': str(exc)})
                return result
            except Exception as exc:
                if attempt < retries:
                    continue
                result = ToolResult(
                    ok=False,
                    error_code="unknown_error",
                    error_message=str(exc),
                    retry_count=attempt,
                    latency_ms=int((time.time() - start_all) * 1000),
                )
                self._emit("tool.failed", request=request, result=result, retry_count=attempt)
                self._emit_hook('on_error', request=request, payload={}, error={'error_code': 'unknown_error', 'error_message': str(exc)})
                return result
        return ToolResult(ok=False, error_code="unknown_error", error_message="tool_invoke_failed")

    def _emit_hook(self, hook_point: str, *, request: ToolRequest, payload: dict[str, Any], error: dict[str, Any] | None = None) -> None:
        if self.hook_emitter is None:
            return
        self.hook_emitter(
            hook_point,
            {
                'metadata': request.metadata,
                'run_id': str(request.metadata.get('run_id', '') or ''),
                'attempt': int(request.metadata.get('attempt', 0) or 0),
                'stage': str(request.metadata.get('node_name', '') or ''),
                'agent_name': str(request.metadata.get('agent_name', '') or ''),
                'trace_name': str(request.metadata.get('trace_name', '') or ''),
                'payload': payload,
                'error': error,
            },
        )

    def _emit(
        self,
        event_type: str,
        *,
        request: ToolRequest,
        retry_count: int,
        result: ToolResult | None = None,
        forbidden: bool = False,
    ) -> None:
        if self.event_sink is None:
            return
        digest = hashlib.sha256(json.dumps(self._json_safe(request.args), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        payload: dict[str, Any] = {
            "event_type": event_type,
            "tool_name": request.name,
            "group": request.metadata.get("group", ""),
            "provider": (result.provider if result else "") or request.metadata.get("provider", ""),
            "latency_ms": result.latency_ms if result else 0,
            "retry_count": retry_count,
            "error_code": result.error_code if result else "",
            "args_digest": digest,
            "agent_name": str(request.metadata.get("agent_name", "") or ""),
            "trace_name": str(request.metadata.get("trace_name", "") or ""),
            "tool_round": int(request.metadata.get("tool_round", 0) or 0),
            "forbidden": bool(forbidden),
            "metadata": request.metadata,
        }
        self.event_sink(payload)

    @staticmethod
    def _retryable(code: str) -> bool:
        return code in {"http_429", "http_5xx", "network_error", "network_timeout"}

    @staticmethod
    def _validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str:
        required = schema.get("required", []) if isinstance(schema, dict) else []
        for key in required if isinstance(required, list) else []:
            if key not in args or args.get(key) in (None, ""):
                return f"missing_required_argument: {key}"
        return ""

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if is_dataclass(value):
            return cls._json_safe(asdict(value))
        if hasattr(value, "model_dump"):
            return cls._json_safe(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe(item) for item in value]
        return str(value)
