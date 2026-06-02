from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallTurn:
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_output: dict[str, Any] | None = None


def parse_tool_call_turn(payload: dict[str, Any]) -> ToolCallTurn:
    raw_calls = payload.get('tool_calls', []) if isinstance(payload, dict) else []
    calls: list[ToolCall] = []
    if isinstance(raw_calls, list):
        for item in raw_calls:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name', '')).strip()
            if not name:
                continue
            args = item.get('arguments', {})
            calls.append(ToolCall(name=name, arguments=args if isinstance(args, dict) else {}))
    final_output = payload.get('final_output') if isinstance(payload, dict) else None
    if not isinstance(final_output, dict):
        final_output = None
    return ToolCallTurn(tool_calls=calls, final_output=final_output)


def tool_specs_for_prompt(specs: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for spec in specs:
        lines.append(
            f"- {spec.get('name','')}：分组={spec.get('group','')}；说明={spec.get('description','')}；参数结构={spec.get('input_schema', spec.get('schema', {}))}"
        )
    return '\n'.join(lines)
