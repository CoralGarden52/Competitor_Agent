from __future__ import annotations

from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.models import QAOutput, RunState
from app.core.prompts.agent_prompts import QA_SYSTEM_PROMPT
from app.core.qa import run_qa_gate
from app.core.storage import SQLiteStore


class QACriticAgent:
    def __init__(self, llm: AgentLLMClient, store: SQLiteStore):
        self.llm = llm
        self.store = store

    def run_llm(self, state: RunState) -> QAOutput:
        payload = {
            'industry': state.industry,
            'language': state.language,
            'analysis_schema_plan': state.analysis_schema_plan,
            'profiles': [x.model_dump(mode='json') for x in state.profiles],
            'findings': [x.model_dump(mode='json') for x in state.findings],
            'report': state.report.model_dump(mode='json') if state.report else None,
            'evidences': [x.model_dump(mode='json') for x in state.evidences],
            'constraints': {
                'require_traceable_evidence': True,
                'default_language': 'zh',
            },
        }
        result = self.llm.invoke_json(
            trace_name='agent.qa.evaluate_report',
            system_prompt=QA_SYSTEM_PROMPT,
            user_payload=payload,
            metadata={
                'run_id': state.run_id,
                'node_name': 'qa',
                'agent_name': 'QACriticAgent',
                'model': self.llm.config.openai_model,
                'industry': state.industry,
                'competitor_count': len(state.planned_competitors or state.competitors),
                'attempt': state.attempt,
            },
        )
        try:
            return QAOutput.model_validate(result)
        except Exception as exc:
            raise LLMCallError(
                reason='validation_error',
                message=f'QAOutput validation failed: {exc}',
                attempt_count=self.llm.config.agent_llm_retry_count + 1,
                retry_count_used=self.llm.config.agent_llm_retry_count,
            ) from exc

    def run_fallback(self, state: RunState) -> QAOutput:
        result = run_qa_gate(state, self.store)
        return QAOutput(
            passed=result.passed,
            issues=result.issues,
            target_agent=result.target_agent,
            ticket=None,
        )
