from __future__ import annotations

from app.agents.orchestrator_agent import OrchestratorAgent
from app.core.models import QAOutput, RunState


def test_orchestrator_stage_execution_order() -> None:
    orchestrator = OrchestratorAgent(max_rework_iterations=2)
    state = RunState(industry='saas', competitors=['alpha'])
    calls: list[str] = []

    def plan(s):
        calls.append('plan')

    def collect(s):
        calls.append('collect')

    def normalize(s):
        calls.append('normalize')

    def analyze(s):
        calls.append('analyze')

    def draft(s):
        calls.append('draft')

    def qa(s):
        calls.append('qa')
        return QAOutput(passed=True)

    result = orchestrator.execute_attempt(
        state,
        plan_handler=plan,
        collect_handler=collect,
        normalize_handler=normalize,
        analyze_handler=analyze,
        draft_handler=draft,
        qa_handler=qa,
    )

    assert result.passed is True
    assert calls == ['plan', 'collect', 'normalize', 'analyze', 'qa', 'draft']
