from __future__ import annotations

from collections import defaultdict

from harness.tools.types import ToolHandler, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._enabled: set[str] = set()
        self._groups: dict[str, set[str]] = defaultdict(set)

    def register(self, handler: ToolHandler) -> None:
        spec = handler.spec()
        if spec.name in self._handlers:
            raise ValueError(f"tool_already_registered: {spec.name}")
        self._handlers[spec.name] = handler
        self._groups[spec.group].add(spec.name)
        if spec.enabled:
            self._enabled.add(spec.name)

    def get(self, name: str) -> ToolHandler:
        if name not in self._handlers:
            raise KeyError(f"tool_not_found: {name}")
        if name not in self._enabled:
            raise KeyError(f"tool_disabled: {name}")
        return self._handlers[name]

    def get_spec(self, name: str) -> ToolSpec:
        handler = self.get(name)
        return handler.spec()

    def list_specs(self, *, group: str | None = None) -> list[ToolSpec]:
        names = sorted(self._enabled)
        output: list[ToolSpec] = []
        for name in names:
            spec = self._handlers[name].spec()
            if group is not None and spec.group != group:
                continue
            output.append(spec)
        return output

    def names_by_group(self, group: str) -> list[str]:
        return sorted([x for x in self._groups.get(group, set()) if x in self._enabled])

    def disable(self, name: str) -> None:
        self._enabled.discard(name)

    def enable(self, name: str) -> None:
        if name in self._handlers:
            self._enabled.add(name)
