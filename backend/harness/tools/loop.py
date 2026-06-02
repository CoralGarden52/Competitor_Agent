from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from harness.tools.policies import allowed_tools_for
from harness.tools.protocol import parse_tool_call_turn, tool_specs_for_prompt
from harness.tools.router import ToolRouter
from harness.tools.types import ToolRequest, ToolResult


class ToolLoopError(RuntimeError):
    def __init__(self, code: str, message: str, *, rounds: int = 0, tool_calls: int = 0, history: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.rounds = rounds
        self.tool_calls = tool_calls
        self.history = history or []


@dataclass
class ToolLoopResult:
    final_output: dict[str, Any]
    history: list[dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    tool_calls: int = 0


class ToolLoopExecutor:
    def __init__(self, router: ToolRouter) -> None:
        self.router = router

    def run(
        self,
        *,
        invoke_model: Callable[..., dict[str, Any]],
        trace_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        metadata: dict[str, Any],
        tool_names: list[str],
        max_tool_rounds: int = 4,
        max_tool_calls: int | None = None,
        token_tracker: Any | None = None,
        fallback_to_plain_json: bool = True,
        after_tool: Callable[[str, dict[str, Any], ToolResult], None] | None = None,
    ) -> ToolLoopResult:
        agent_name = str(metadata.get("agent_name", "") or "")
        allowed_names = allowed_tools_for(agent_name, tool_names) if agent_name else list(tool_names)
        specs = []
        for name in allowed_names:
            try:
                spec = self.router.registry.get_spec(name)
            except KeyError:
                continue
            if spec.visibility != "model":
                continue
            specs.append(
                {
                    "name": spec.name,
                    "group": spec.group,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                }
            )
        if not specs:
            return ToolLoopResult(final_output=self._invoke_model(
                invoke_model, trace_name=trace_name, system_prompt=system_prompt, user_payload=user_payload,
                metadata={**metadata, "_via_tool": True}, token_tracker=token_tracker,
            ))

        protocol_prompt = (
            f"{system_prompt}\n\n"
            "你可以在生成最终答案之前调用工具。只返回严格 JSON："
            '{"tool_calls":[{"name":"tool.name","arguments":{}}],"final_output":null} '
            '或 {"tool_calls":[],"final_output":{}}。\n'
            f"可用工具：\n{tool_specs_for_prompt(specs)}"
        )
        history: list[dict[str, Any]] = []
        tool_call_count = 0
        for round_index in range(1, max(1, max_tool_rounds) + 1):
            result = self._invoke_model(
                invoke_model, trace_name=f"{trace_name}.tool_round", system_prompt=protocol_prompt,
                user_payload={"task": user_payload, "tool_history": copy.deepcopy(history), "round": round_index, "max_rounds": max_tool_rounds},
                metadata={**metadata, "_via_tool": True, "tool_round": round_index}, token_tracker=token_tracker,
            )
            turn = parse_tool_call_turn(result)
            if turn.final_output is not None and not turn.tool_calls:
                return ToolLoopResult(final_output=turn.final_output, history=history, rounds=round_index, tool_calls=tool_call_count)
            if not turn.tool_calls:
                if fallback_to_plain_json:
                    return ToolLoopResult(final_output=result, history=history, rounds=round_index, tool_calls=tool_call_count)
                raise ToolLoopError("tool_protocol_error", "empty tool_calls without final_output", rounds=round_index, tool_calls=tool_call_count, history=history)
            round_calls: list[dict[str, Any]] = []
            for call in turn.tool_calls:
                if max_tool_calls is not None and tool_call_count >= max_tool_calls:
                    raise ToolLoopError("tool_budget_exhausted", "tool call budget exhausted", rounds=round_index, tool_calls=tool_call_count, history=history)
                tool_call_count += 1
                tool_result = self.router.invoke(
                    ToolRequest(
                        name=call.name,
                        args=call.arguments,
                        metadata={
                            **metadata,
                            "group": "tool_call_protocol",
                            "allowed_tools": allowed_names,
                            "trace_name": trace_name,
                            "tool_round": round_index,
                        },
                    )
                )
                if after_tool is not None:
                    after_tool(call.name, call.arguments, tool_result)
                round_calls.append(
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "ok": tool_result.ok,
                        "output": tool_result.output,
                        "error_code": tool_result.error_code,
                        "error_message": tool_result.error_message,
                    }
                )
            history.append({"round": round_index, "tool_calls": round_calls})
        raise ToolLoopError("tool_round_exhausted", f"tool call rounds exhausted: {max_tool_rounds}", rounds=max_tool_rounds, tool_calls=tool_call_count, history=history)

    @staticmethod
    def _invoke_model(invoke_model: Callable[..., dict[str, Any]], *, token_tracker: Any | None, **kwargs: Any) -> dict[str, Any]:
        if token_tracker is not None:
            kwargs["token_tracker"] = token_tracker
        return invoke_model(**kwargs)
