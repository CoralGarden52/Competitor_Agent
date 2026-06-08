from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    group: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    visibility: Literal["model", "internal"] = "model"
    enabled: bool = True

    @property
    def schema(self) -> dict[str, Any]:
        """Compatibility alias for prompt renderers."""
        return self.input_schema


@dataclass
class ToolRequest:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    timeout_s: float | None = None
    max_retries: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    provider: str = ""
    retry_count: int = 0
    latency_ms: int = 0


class ToolError(RuntimeError):
    def __init__(self, *, code: str, message: str, provider: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider


class ToolHandler(Protocol):
    def handle(self, request: ToolRequest) -> ToolResult: ...
