from __future__ import annotations

from app.core.tools.registry import ToolRegistry
from app.core.tools.types import ToolRequest, ToolResult, ToolSpec


class _H:
    def __init__(self, name: str, group: str = "web") -> None:
        self._spec = ToolSpec(name=name, group=group, description="x")

    def spec(self) -> ToolSpec:
        return self._spec

    def handle(self, request: ToolRequest) -> ToolResult:
        return ToolResult(ok=True, output={"echo": request.args})


def test_registry_duplicate_rejected() -> None:
    reg = ToolRegistry()
    reg.register(_H("web.search"))
    try:
        reg.register(_H("web.search"))
        assert False
    except ValueError:
        assert True


def test_registry_enable_disable_and_group() -> None:
    reg = ToolRegistry()
    reg.register(_H("web.search", "web"))
    reg.register(_H("llm.invoke_json", "llm"))
    assert reg.names_by_group("web") == ["web.search"]
    reg.disable("web.search")
    assert reg.names_by_group("web") == []
    reg.enable("web.search")
    assert reg.names_by_group("web") == ["web.search"]
