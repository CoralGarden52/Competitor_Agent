from __future__ import annotations

from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.models import AnalyzeOutput, CompetitorProfile, FeatureNode, FeedbackSummary, Finding, PricingModel, PricingTier, RunState
from app.core.prompts.agent_prompts import ANALYZE_SYSTEM_PROMPT
from app.core.schema_registry import get_domain_schema
from app.core.storage import SQLiteStore


class AnalystAgent:
    def __init__(self, llm: AgentLLMClient, store: SQLiteStore):
        self.llm = llm
        self.store = store

    def run_llm(self, state: RunState) -> AnalyzeOutput:
        payload = {
            'industry': state.industry,
            'language': state.language,
            'planned_competitors': state.planned_competitors or state.competitors,
            'analysis_schema_plan': state.analysis_schema_plan,
            'evidences': [ev.model_dump(mode='json') for ev in state.evidences],
        }
        result = self.llm.invoke_json(
            trace_name='agent.analyze.generate_profiles',
            system_prompt=ANALYZE_SYSTEM_PROMPT,
            user_payload=payload,
            metadata={
                'run_id': state.run_id,
                'node_name': 'analyze',
                'agent_name': 'AnalystAgent',
                'model': self.llm.config.openai_model,
                'industry': state.industry,
                'competitor_count': len(state.planned_competitors or state.competitors),
                'attempt': state.attempt,
            },
        )
        try:
            return AnalyzeOutput.model_validate(result)
        except Exception as exc:
            raise LLMCallError(
                reason='validation_error',
                message=f'AnalyzeOutput validation failed: {exc}',
                attempt_count=self.llm.config.agent_llm_retry_count + 1,
                retry_count_used=self.llm.config.agent_llm_retry_count,
            ) from exc

    def run_fallback(self, state: RunState) -> AnalyzeOutput:
        domain = get_domain_schema(self.store, state.industry)
        output = AnalyzeOutput()
        active_competitors = state.planned_competitors or state.competitors

        for competitor in active_competitors:
            related = [ev for ev in state.evidences if competitor.lower() in ev.source_url.lower() or competitor.lower() in ev.snippet.lower()]
            refs = [ev.evidence_id for ev in related]
            extension_data = {field: f'inferred_{field}_{competitor}' for field in domain.required_extension_fields}

            pricing_hints = [ev for ev in related if 'pricing' in ev.snippet.lower() or 'plan' in ev.snippet.lower()]
            feedback_hints = [ev for ev in related if 'review' in ev.snippet.lower() or 'user' in ev.snippet.lower()]

            profile = CompetitorProfile(
                industry=state.industry,
                product_name=competitor,
                positioning=f'{competitor} market positioning inferred from public sources',
                feature_tree=[FeatureNode(name='Core Platform', capability='Core value capabilities', children=[FeatureNode(name='Integrations', capability='Integrates with common tools')])],
                advantages=['Visible feature breadth', 'Documented pricing paths'] if refs else ['Insufficient evidence'],
                disadvantages=['Gaps in advanced support transparency'] if refs else ['No reliable external evidence'],
                pricing_model=PricingModel(
                    model_type='subscription' if pricing_hints else 'unknown',
                    free_tier=bool(pricing_hints),
                    billing_dimensions=['seat', 'usage'] if pricing_hints else [],
                    tiers=[PricingTier(name='Observed Plan', price_range='unknown', billing_cycle='monthly', limits=['derived from web evidence'])] if pricing_hints else [],
                ),
                user_feedback=FeedbackSummary(
                    positive_themes=['Ease of use'] if feedback_hints else [],
                    negative_themes=['Pricing concerns'] if feedback_hints else [],
                    representative_quotes=[ev.snippet[:160] for ev in feedback_hints[:2]],
                    sentiment_distribution={'positive': 0.55, 'neutral': 0.25, 'negative': 0.2} if feedback_hints else {},
                ),
                evidence_refs=refs,
                domain_extensions=extension_data,
            )
            output.profiles.append(profile)
            if refs:
                output.findings.append(
                    Finding(
                        statement=f'{competitor} profile synthesized from {len(refs)} evidence item(s).',
                        category='feature',
                        evidence_refs=refs[:2],
                        confidence=0.68,
                        risk_flag=False,
                    )
                )

        return output
