from __future__ import annotations

from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.models import DraftOutput, Report, RunState
from app.core.prompts.agent_prompts import DRAFT_SYSTEM_PROMPT


class WriterAgent:
    def __init__(self, llm: AgentLLMClient):
        self.llm = llm

    def run_llm(self, state: RunState) -> DraftOutput:
        payload = {
            'industry': state.industry,
            'language': state.language,
            'write_language': 'en' if str(state.language).lower().startswith('en') else 'zh',
            'profiles': [x.model_dump(mode='json') for x in state.profiles],
            'findings': [x.model_dump(mode='json') for x in state.findings],
            'evidences': [x.model_dump(mode='json') for x in state.evidences],
        }
        result = self.llm.invoke_json(
            trace_name='agent.draft.generate_report',
            system_prompt=DRAFT_SYSTEM_PROMPT,
            user_payload=payload,
            metadata={
                'run_id': state.run_id,
                'node_name': 'draft',
                'agent_name': 'WriterAgent',
                'model': self.llm.config.openai_model,
                'industry': state.industry,
                'competitor_count': len(state.planned_competitors or state.competitors),
                'attempt': state.attempt,
            },
        )
        try:
            return DraftOutput.model_validate(result)
        except Exception as exc:
            raise LLMCallError(
                reason='validation_error',
                message=f'DraftOutput validation failed: {exc}',
                attempt_count=self.llm.config.agent_llm_retry_count + 1,
                retry_count_used=self.llm.config.agent_llm_retry_count,
            ) from exc

    def run_fallback(self, state: RunState) -> DraftOutput:
        matrix = []
        for profile in state.profiles:
            field_summaries = profile.domain_extensions.get('field_summaries', {}) if profile.domain_extensions else {}
            matrix.append(
                {
                    'product': profile.product_name,
                    'advantages': profile.advantages,
                    'disadvantages': profile.disadvantages,
                    'pricing_model': profile.pricing_model.model_type,
                    'feedback_negative_top': profile.user_feedback.negative_themes[:2],
                    'field_summaries': field_summaries,
                }
            )

        markdown_lines = ['# Competitor Analysis Report', '', f'Industry: {state.industry}', '', '## Comparison Matrix']
        for row in matrix:
            markdown_lines.append(f"- {row['product']}: pricing={row['pricing_model']}, pros={', '.join(row['advantages'])}")
            if row.get('field_summaries'):
                markdown_lines.append(f"  - field_summaries={list(row['field_summaries'].keys())}")

        report = Report(
            executive_summary='This report compares competitors on features, pricing, and user feedback.',
            comparison_matrix=matrix,
            swot={'strengths': ['Traceable evidence flow'], 'weaknesses': ['Public-web bias'], 'opportunities': ['Industry extension growth'], 'threats': ['Source volatility']},
            opportunities=['Refine collection depth per domain extension field'],
            appendix_sources=[ev.source_url for ev in state.evidences],
            markdown='\n'.join(markdown_lines),
        )
        return DraftOutput(report=report)
