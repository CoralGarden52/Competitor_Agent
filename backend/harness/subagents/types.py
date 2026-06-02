from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4


class SubagentBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class SubagentBudget:
    max_rounds: int = 3
    max_tool_calls: int = 6
    max_tokens: int = 4000
    timeout_s: float = 90.0


@dataclass
class SubagentUsage:
    rounds: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0


@dataclass
class SubagentRequest:
    parent_run_id: str
    attempt: int
    industry: str
    competitor: str
    field_name: str
    objective: str
    seed_queries: list[str] = field(default_factory=list)
    existing_evidences: list[dict[str, Any]] = field(default_factory=list)
    subagent_id: str = field(default_factory=lambda: f'sub_{uuid4().hex[:12]}')


@dataclass
class SubagentResult:
    subagent_id: str
    status: str
    competitor: str
    field_name: str
    usage: SubagentUsage = field(default_factory=SubagentUsage)
    new_evidences: list[dict[str, Any]] = field(default_factory=list)
    verification_claims: list[str] = field(default_factory=list)
    verification_conflicts: list[str] = field(default_factory=list)
    verification_gaps: list[str] = field(default_factory=list)
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    error: str = ''

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SubagentTokenTracker:
    def __init__(self, max_tokens: int) -> None:
        self.max_tokens = max(1, int(max_tokens))
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

    def before_request(self, messages: list[dict[str, Any]]) -> int:
        estimated_prompt = self._estimate(messages)
        remaining = self.max_tokens - self.total_tokens - estimated_prompt
        if remaining <= 0:
            raise SubagentBudgetExceeded('subagent token budget exhausted before LLM request')
        return remaining

    def after_response(self, usage: Any, messages: list[dict[str, Any]], content: Any) -> None:
        if isinstance(usage, dict) and int(usage.get('total_tokens', 0) or 0) > 0:
            prompt_tokens = int(usage.get('prompt_tokens', 0) or 0)
            completion_tokens = int(usage.get('completion_tokens', 0) or 0)
            total_tokens = int(usage.get('total_tokens', prompt_tokens + completion_tokens) or 0)
        else:
            prompt_tokens = self._estimate(messages)
            completion_tokens = self._estimate(content)
            total_tokens = prompt_tokens + completion_tokens
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += total_tokens
        if self.total_tokens > self.max_tokens:
            raise SubagentBudgetExceeded('subagent token budget exhausted after LLM response')

    @staticmethod
    def _estimate(payload: Any) -> int:
        text = json.dumps(payload, ensure_ascii=False, default=str) if not isinstance(payload, str) else payload
        return max(1, len(text) // 4)
