from __future__ import annotations

import json

import pytest

from app.agents.qa_critic_agent import QACriticAgent
from app.core.agent_llm import LLMCallError
from app.core.models import RunState
from app.core.storage import SQLiteStore


class _FakeLLM:
    class _Cfg:
        openai_model = "test-model"
        agent_llm_retry_count = 2

    config = _Cfg()

    def __init__(self, result=None, exc: Exception | None = None):
        self._result = result or {"passed": True, "issues": [], "target_agent": None, "ticket": None, "collect_plan": None}
        self._exc = exc

    def invoke_json(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._result


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_qa_logging_success_writes_input_and_output(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    agent = QACriticAgent(_FakeLLM(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(QACriticAgent, "_qa_log_dir", classmethod(lambda cls: tmp_path / "QA_log"))

    state = RunState(industry="saas", competitors=["alpha"], user_prompt="hello")
    _ = agent.run_llm(state)

    log_file = tmp_path / "QA_log" / "qa_agent.jsonl"
    assert log_file.exists()
    rows = _read_jsonl(log_file)
    assert len(rows) == 2
    assert rows[0]["event_type"] == "qa_input"
    assert rows[1]["event_type"] == "qa_output"
    assert rows[0]["run_id"] == state.run_id
    assert rows[1]["attempt"] == state.attempt


def test_qa_logging_error_and_fallback(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    exc = LLMCallError(reason="http_429", message="rate limit", attempt_count=3, retry_count_used=2)
    agent = QACriticAgent(_FakeLLM(exc=exc), store)  # type: ignore[arg-type]
    monkeypatch.setattr(QACriticAgent, "_qa_log_dir", classmethod(lambda cls: tmp_path / "QA_log"))
    state = RunState(industry="saas", competitors=["alpha"])

    with pytest.raises(LLMCallError):
        agent.run_llm(state)
    _ = agent.run_fallback(state)

    rows = _read_jsonl(tmp_path / "QA_log" / "qa_agent.jsonl")
    event_types = [x["event_type"] for x in rows]
    assert "qa_input" in event_types
    assert "qa_error" in event_types
    assert "qa_fallback_output" in event_types


def test_qa_logging_masks_sensitive_values(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    fake = _FakeLLM()
    agent = QACriticAgent(fake, store)  # type: ignore[arg-type]
    monkeypatch.setattr(QACriticAgent, "_qa_log_dir", classmethod(lambda cls: tmp_path / "QA_log"))
    state = RunState(industry="saas", competitors=["alpha"], user_prompt="Bearer abcdefghijklmnopqrstuvwxyz123456")

    _ = agent.run_llm(state)
    rows = _read_jsonl(tmp_path / "QA_log" / "qa_agent.jsonl")
    input_payload = rows[0]["payload"]["user_payload"]
    # prompt in payload should be masked for bearer/token-like fragments
    assert "abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(input_payload, ensure_ascii=False)


def test_qa_analysis_review_llm_propagates_run_trace_metadata(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    agent = QACriticAgent(_FakeLLM(), store)  # type: ignore[arg-type]
    monkeypatch.setattr(QACriticAgent, "_qa_log_dir", classmethod(lambda cls: tmp_path / "QA_log"))

    captured: dict[str, object] = {}

    def fake_invoke_llm_json(**kwargs):
        captured["metadata"] = kwargs["metadata"]
        return {"needs_recollect": False, "insufficient_fields": [], "collect_plan": {"items": []}}

    monkeypatch.setattr(agent, "_invoke_llm_json", fake_invoke_llm_json)

    _ = agent.run_competitor_analysis_review_llm(
        analysis_json={"competitor": "alpha", "fields": []},
        schema_fields=["feature_tree"],
        industry_hint="saas",
        run_id="run_trace_case",
        attempt=3,
    )

    metadata = captured["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["run_id"] == "run_trace_case"
    assert metadata["attempt"] == 3
    assert metadata["node_name"] == "qa"
