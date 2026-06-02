from __future__ import annotations

from harness.tools.registry import ToolRegistry
from harness.tools.types import ToolRequest, ToolResult, ToolSpec


class _H:
    def __init__(self, name: str, group: str = "web") -> None:
        self._spec = ToolSpec(name=name, group=group, description="x")

    def spec(self) -> ToolSpec:
        return self._spec

    def handle(self, request: ToolRequest) -> ToolResult:
        return ToolResult(ok=True, output={"echo": request.args})


def test_registry_duplicate_rejected() -> None:
    reg = ToolRegistry()
    handler = _H("web.search")
    reg.register(spec=handler.spec(), handler=handler)
    try:
        handler = _H("web.search")
        reg.register(spec=handler.spec(), handler=handler)
        assert False
    except ValueError:
        assert True


def test_registry_enable_disable_and_group() -> None:
    reg = ToolRegistry()
    search = _H("web.search", "web")
    llm = _H("llm.invoke_json", "llm")
    reg.register(spec=search.spec(), handler=search)
    reg.register(spec=llm.spec(), handler=llm)
    assert reg.names_by_group("web") == ["web.search"]
    reg.disable("web.search")
    assert reg.names_by_group("web") == []
    reg.enable("web.search")
    assert reg.names_by_group("web") == ["web.search"]


def test_registry_keeps_spec_separate_from_handler() -> None:
    class _HandlerOnly:
        def handle(self, request: ToolRequest) -> ToolResult:
            return ToolResult(ok=True, output={"ok": True})

    reg = ToolRegistry()
    spec = ToolSpec(name="web.fetch", group="web", description="fetch")
    reg.register(spec=spec, handler=_HandlerOnly())

    assert reg.get_spec("web.fetch") is spec
