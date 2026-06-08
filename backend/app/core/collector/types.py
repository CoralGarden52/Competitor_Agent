from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class CollectorOutput:
    evidences: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    provider_events: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
