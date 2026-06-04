from __future__ import annotations

from harness.tools.types import ToolSpec


WEB_SEARCH_SPEC = ToolSpec(
    name="web.search",
    group="web",
    description="使用已配置的服务提供方搜索公开网络来源。",
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
    description="获取公开 URL 的可读内容。",
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
    description="清理输入文本并提取结构化字段。",
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
    description="按结构化标签检索已持久化的横向竞品对比语料。",
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
    description="内部 JSON 格式大模型调用网关。",
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
    description="读取当前运行的高层摘要状态，供管理智能体决策。",
    input_schema={"type": "object", "properties": {"run_id": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"run": {"type": "object"}}},
)

STATE_GET_COVERAGE_SUMMARY_SPEC = ToolSpec(
    name="state.get_coverage_summary",
    group="state",
    description="读取当前运行的分析覆盖率摘要。",
    input_schema={"type": "object", "properties": {"run_id": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"coverage": {"type": "object"}}},
)

STATE_GET_GAP_SUMMARY_SPEC = ToolSpec(
    name="state.get_gap_summary",
    group="state",
    description="读取当前运行的字段缺口摘要。",
    input_schema={"type": "object", "properties": {"run_id": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"gaps": {"type": "array"}}},
)

STATE_GET_REPORT_STATUS_SPEC = ToolSpec(
    name="state.get_report_status",
    group="state",
    description="读取当前报告是否已具备交付条件。",
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


ACTION_PLAN_SCOPE_SPEC = _action_spec("action.plan_scope", "执行范围规划与 schema 规划动作。")
ACTION_COLLECT_INITIAL_SPEC = _action_spec("action.collect_initial", "对计划范围执行初始证据采集动作。")
ACTION_COLLECT_GAP_SPEC = _action_spec("action.collect_gap", "对目标竞品/字段缺口执行补充采集动作。")
ACTION_NORMALIZE_EVIDENCE_SPEC = _action_spec("action.normalize_evidence", "执行证据标准化与去重动作。")
ACTION_REANALYZE_TARGETS_SPEC = _action_spec("action.reanalyze_targets", "对目标竞品/字段执行增量重分析动作。")
ACTION_REDRAFT_REPORT_SPEC = _action_spec("action.redraft_report", "执行报告重写或局部重写动作。")
ACTION_RUN_QA_SPEC = _action_spec("action.run_qa", "执行质量审查动作。")
ACTION_FINALIZE_RUN_SPEC = _action_spec("action.finalize_run", "执行最终收尾与完成动作。")
