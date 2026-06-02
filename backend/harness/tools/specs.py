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
