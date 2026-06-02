from __future__ import annotations

from app.core.config import AppConfig
from harness.tools.bootstrap import build_tool_runtime, register_internal_llm_tool


def test_internal_llm_tool_is_registered_but_not_model_visible() -> None:
    runtime = build_tool_runtime(AppConfig())
    register_internal_llm_tool(runtime, lambda **_: {"ok": True})

    assert runtime.registry.get_spec("llm.invoke_json").visibility == "internal"
    assert {spec.name for spec in runtime.registry.list_specs() if spec.visibility == "model"} == {
        "web.search",
        "web.fetch",
        "web.extract",
    }
