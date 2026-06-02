from __future__ import annotations

from app.core.tools.registry import ToolRegistry
from app.core.tools.router import ToolRouter
from app.core.tools.types import ToolError, ToolRequest, ToolResult, ToolSpec


class _Ok:
    def spec(self) -> ToolSpec:
        return ToolSpec(name="web.search", group="web", description="ok")

    def handle(self, request: ToolRequest) -> ToolResult:
        return ToolResult(ok=True, provider="mock", output={"k": 1})


class _FailRetry:
    def __init__(self) -> None:
        self.calls = 0

    def spec(self) -> ToolSpec:
        return ToolSpec(name="llm.invoke_json", group="llm", description="retry")

    def handle(self, request: ToolRequest) -> ToolResult:
        self.calls += 1
        if self.calls < 2:
            raise ToolError(code="http_429", message="rate limited")
        return ToolResult(ok=True, output={"parsed": {"ok": True}})


def test_router_success_with_events() -> None:
    events = []
    reg = ToolRegistry()
    reg.register(_Ok())
    router = ToolRouter(reg, event_sink=events.append)
    result = router.invoke(ToolRequest(name="web.search", args={"q": "x"}, metadata={"group": "web"}))
    assert result.ok is True
    assert events[0]["event_type"] == "tool.called"
    assert events[1]["event_type"] == "tool.succeeded"


def test_router_retryable_error_retries() -> None:
    h = _FailRetry()
    reg = ToolRegistry()
    reg.register(h)
    router = ToolRouter(reg)
    result = router.invoke(ToolRequest(name="llm.invoke_json", max_retries=1))
    assert result.ok is True
    assert h.calls == 2


def test_router_forbidden_tool_rejected() -> None:
    events = []
    reg = ToolRegistry()
    reg.register(_Ok())
    router = ToolRouter(reg, event_sink=events.append)
    result = router.invoke(
        ToolRequest(
            name="web.search",
            args={"q": "x"},
            metadata={"allowed_tools": ["web.fetch"], "agent_name": "WriterAgent", "trace_name": "t1", "tool_round": 1},
        )
    )
    assert result.ok is False
    assert result.error_code == "forbidden_tool"
    assert events[-1]["forbidden"] is True
