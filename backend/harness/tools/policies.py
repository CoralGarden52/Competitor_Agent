from __future__ import annotations


ROLE_TOOL_ALLOWLISTS: dict[str, frozenset[str]] = {
    "CollectorPipeline": frozenset({"web.search", "web.fetch", "web.extract"}),
    "PlannerLLMClient": frozenset({"llm.invoke_json", "web.search", "web.fetch"}),
    "AnalystAgent": frozenset({"web.search", "web.fetch", "web.extract"}),
    "WriterAgent": frozenset({"web.extract"}),
    "QACriticAgent": frozenset({"web.search", "web.fetch"}),
    "CollectorDeepDiveSubagent": frozenset({"web.search", "web.fetch", "web.extract"}),
}


def allowed_tools_for(role_name: str, requested_tools: list[str] | tuple[str, ...] | None = None) -> list[str]:
    configured = ROLE_TOOL_ALLOWLISTS.get(role_name, frozenset())
    if requested_tools is None:
        return sorted(configured)
    return [name for name in requested_tools if name in configured]
