from __future__ import annotations

from harness.tools.types import ToolSpec


WEB_SEARCH_SPEC = ToolSpec(
    name="web.search",
    group="web",
    description="Search public web sources with the configured providers.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            "provider_allowlist": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["query"],
    },
    output_schema={"type": "object", "properties": {"hits": {"type": "array"}, "trace": {"type": "array"}}},
)

WEB_FETCH_SPEC = ToolSpec(
    name="web.fetch",
    group="web",
    description="Fetch readable content from a public URL.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "provider_order": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["url"],
    },
    output_schema={"type": "object", "properties": {"content": {"type": "string"}, "trace": {"type": "array"}}},
)

WEB_EXTRACT_SPEC = ToolSpec(
    name="web.extract",
    group="web",
    description="Clean input text and extract structured fields.",
    input_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "title": {"type": "string"},
            "snippet": {"type": "string"},
        },
        "required": ["content"],
    },
    output_schema={"type": "object", "properties": {"sanitized": {"type": "string"}, "extract_fields": {"type": "object"}}},
)

CORPUS_SEARCH_SPEC = ToolSpec(
    name="corpus.search",
    group="corpus",
    description="Search persisted cross-competitor comparison corpus by structured tags.",
    input_schema={
        "type": "object",
        "properties": {
            "topic_key": {"type": "string"},
            "industry": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    },
    output_schema={"type": "object", "properties": {"documents": {"type": "array"}}},
    visibility="internal",
)

LLM_INVOKE_JSON_SPEC = ToolSpec(
    name="llm.invoke_json",
    group="llm",
    description="Internal JSON LLM gateway.",
    input_schema={
        "type": "object",
        "properties": {
            "trace_name": {"type": "string"},
            "system_prompt": {"type": "string"},
            "user_payload": {"type": "object"},
            "metadata": {"type": "object"},
        },
        "required": ["trace_name", "system_prompt", "user_payload"],
    },
    output_schema={"type": "object", "properties": {"parsed": {"type": "object"}}},
    visibility="internal",
)

STATE_GET_RUN_SNAPSHOT_SPEC = ToolSpec(
    name="state.get_run_snapshot",
    group="state",
    description="Read the high-level run state for manager decisions.",
    input_schema={"type": "object", "properties": {"run_id": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"run": {"type": "object"}}},
)

STATE_GET_COVERAGE_SUMMARY_SPEC = ToolSpec(
    name="state.get_coverage_summary",
    group="state",
    description="Read analysis coverage summary for the current run.",
    input_schema={"type": "object", "properties": {"run_id": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"coverage": {"type": "object"}}},
)

STATE_GET_GAP_SUMMARY_SPEC = ToolSpec(
    name="state.get_gap_summary",
    group="state",
    description="Read field gap summary for the current run.",
    input_schema={"type": "object", "properties": {"run_id": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"gaps": {"type": "array"}}},
)

STATE_GET_REPORT_STATUS_SPEC = ToolSpec(
    name="state.get_report_status",
    group="state",
    description="Read whether the current report is ready for delivery.",
    input_schema={"type": "object", "properties": {"run_id": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"report": {"type": "object"}}},
)


def _action_spec(name: str, description: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        group="action",
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "competitors": {"type": "array", "items": {"type": "string"}},
                "fields": {"type": "array", "items": {"type": "string"}},
                "sections": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
                "mode": {"type": "string"},
            },
        },
        output_schema={"type": "object", "properties": {"status": {"type": "string"}, "summary": {"type": "string"}}},
    )


ACTION_PLAN_SCOPE_SPEC = _action_spec("action.plan_scope", "Execute scope and schema planning.")
ACTION_COLLECT_INITIAL_SPEC = _action_spec("action.collect_initial", "Execute initial evidence collection.")
ACTION_COLLECT_GAP_SPEC = _action_spec("action.collect_gap", "Collect additional evidence for QA gaps.")
ACTION_NORMALIZE_EVIDENCE_SPEC = _action_spec("action.normalize_evidence", "Normalize and deduplicate evidence.")
ACTION_REANALYZE_TARGETS_SPEC = _action_spec("action.reanalyze_targets", "Analyze or re-analyze target competitors and fields.")
ACTION_DRAFT_REPORT_SPEC = _action_spec("action.draft_report", "Generate the single final report draft.")
ACTION_RUN_QA_SPEC = _action_spec("action.run_qa", "Run the pre-draft quality gate.")
ACTION_FINALIZE_RUN_SPEC = _action_spec("action.finalize_run", "Finalize the completed run.")
