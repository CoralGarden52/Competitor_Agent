from __future__ import annotations

from pathlib import Path

from harness.subagents.registry import collector_deep_dive_role
from harness.tools.specs import WEB_EXTRACT_SPEC, WEB_FETCH_SPEC, WEB_SEARCH_SPEC


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROMPT_SOURCE_FILES = [
    BACKEND_ROOT / "app" / "agents" / "analyst_agent.py",
    BACKEND_ROOT / "app" / "core" / "agent_llm.py",
    BACKEND_ROOT / "app" / "core" / "collector" / "deep_dive.py",
    BACKEND_ROOT / "harness" / "subagents" / "executor.py",
    BACKEND_ROOT / "harness" / "subagents" / "registry.py",
    BACKEND_ROOT / "harness" / "tools" / "loop.py",
]
FORBIDDEN_ENGLISH_PROMPT_FRAGMENTS = [
    "You are ",
    "Return strict JSON",
    "Return only one valid JSON",
    "Do not include markdown",
    "Use tools when needed",
    "Available tools:",
    "Analyze exactly one schema field",
    "Extract all pricing-relevant facts",
    "Merge chunk results",
    "Find independent public sources",
]


def test_runtime_prompt_sources_do_not_contain_english_directives() -> None:
    for path in PROMPT_SOURCE_FILES:
        text = path.read_text(encoding="utf-8")
        for fragment in FORBIDDEN_ENGLISH_PROMPT_FRAGMENTS:
            assert fragment not in text, f"{path.name} still contains English prompt fragment: {fragment}"


def test_model_visible_tool_descriptions_are_chinese() -> None:
    descriptions = [
        WEB_SEARCH_SPEC.description,
        WEB_FETCH_SPEC.description,
        WEB_EXTRACT_SPEC.description,
        collector_deep_dive_role().system_prompt,
    ]
    assert all(any("\u4e00" <= char <= "\u9fff" for char in description) for description in descriptions)
