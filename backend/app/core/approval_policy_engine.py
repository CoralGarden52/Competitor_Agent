from __future__ import annotations

from dataclasses import dataclass

from app.core.models import ApprovalPolicy, PolicyDecision, PolicyDecisionResult, RiskLevel, SchemaEvolutionProposal
from app.core.storage import PostgresStore


_RISK_SCORE = {
    RiskLevel.low: 1,
    RiskLevel.medium: 2,
    RiskLevel.high: 3,
}


@dataclass
class PolicyContext:
    industry: str
    qa_failure_count: int


class ApprovalPolicyEngine:
    def __init__(self, store: PostgresStore):
        self.store = store

    def decide(self, proposal: SchemaEvolutionProposal, context: PolicyContext) -> PolicyDecisionResult:
        policies = self.store.list_policies(context.industry)
        risks = self.store.list_field_risks(context.industry)
        risk_map = {(item.industry, item.field_name): item.risk_level for item in risks}

        risk_summary: dict[str, str] = {}
        max_found_risk = RiskLevel.low
        for field_name in proposal.suggested_fields:
            risk = risk_map.get((context.industry, field_name)) or risk_map.get(('global', field_name)) or RiskLevel.medium
            risk_summary[field_name] = risk.value
            if _RISK_SCORE[risk] > _RISK_SCORE[max_found_risk]:
                max_found_risk = risk

        matched = self._match_policy(policies, proposal, context, max_found_risk)
        if matched is None:
            return PolicyDecisionResult(
                decision=PolicyDecision.rejected,
                matched_policy_id=None,
                reason='No policy matched; default rejected for manual review.',
                risk_summary=risk_summary,
            )

        if matched.decision == PolicyDecision.approved:
            return PolicyDecisionResult(
                decision=PolicyDecision.approved,
                matched_policy_id=matched.policy_id,
                reason=f'Matched policy {matched.policy_id} and approved automatically.',
                risk_summary=risk_summary,
            )

        if matched.decision == PolicyDecision.review_required:
            return PolicyDecisionResult(
                decision=PolicyDecision.review_required,
                matched_policy_id=matched.policy_id,
                reason=f'Matched policy {matched.policy_id}; manual review required.',
                risk_summary=risk_summary,
            )

        return PolicyDecisionResult(
            decision=PolicyDecision.rejected,
            matched_policy_id=matched.policy_id,
            reason=f'Matched policy {matched.policy_id}; auto rejected.',
            risk_summary=risk_summary,
        )

    def _match_policy(
        self,
        policies: list[ApprovalPolicy],
        proposal: SchemaEvolutionProposal,
        context: PolicyContext,
        max_found_risk: RiskLevel,
    ) -> ApprovalPolicy | None:
        candidates = [p for p in policies if p.enabled and p.industry in (context.industry, 'global')]
        candidates.sort(key=lambda x: x.priority)

        for policy in candidates:
            if len(proposal.suggested_fields) > policy.max_fields:
                continue
            if context.qa_failure_count > policy.max_qa_failures:
                continue
            if _RISK_SCORE[max_found_risk] > _RISK_SCORE[policy.max_allowed_risk]:
                continue
            if any(scope in set(policy.denied_scopes) for scope in proposal.impact_scope):
                continue
            return policy
        return None
