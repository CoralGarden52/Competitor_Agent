from __future__ import annotations

from pathlib import Path

ALLOWLIST = {
    'app/core/agent_llm.py',
    'app/core/planner_llm.py',
    'app/core/collector/providers.py',
}

PATTERNS = [
    'import urllib.request',
    'urllib.request.urlopen(',
    'from openai import OpenAI',
    'import requests',
    'import httpx',
]


def test_no_scattered_network_or_llm_calls_outside_allowlist() -> None:
    root = Path(__file__).resolve().parents[1]
    app_root = root / 'app'
    violations: list[str] = []

    for py_file in app_root.rglob('*.py'):
        rel = py_file.relative_to(root).as_posix()
        text = py_file.read_text(encoding='utf-8', errors='ignore')
        if not any(p in text for p in PATTERNS):
            continue
        if rel in ALLOWLIST:
            continue
        matched = [p for p in PATTERNS if p in text]
        violations.append(f"{rel}: {matched}")

    assert not violations, 'Scattered network/LLM direct calls found outside allowlist:\n' + '\n'.join(violations)
