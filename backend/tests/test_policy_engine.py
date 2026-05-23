from __future__ import annotations

from app.core.approval_policy_engine import ApprovalPolicyEngine, PolicyContext
from app.core.models import ApprovalPolicy, PolicyDecision, RiskLevel, SchemaEvolutionProposal
from app.core.storage import SQLiteStore


def test_policy_engine_industry_priority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / 'test.db')
    engine = ApprovalPolicyEngine(store)

    store.upsert_policy(
        ApprovalPolicy(
            policy_id='pol_global_reject',
            industry='global',
            priority=50,
            max_fields=10,
            max_qa_failures=10,
            max_allowed_risk=RiskLevel.high,
            decision=PolicyDecision.rejected,
        )
    )
    store.upsert_policy(
        ApprovalPolicy(
            policy_id='pol_saas_approve',
            industry='saas',
            priority=1,
            max_fields=10,
            max_qa_failures=10,
            max_allowed_risk=RiskLevel.high,
            decision=PolicyDecision.approved,
        )
    )

    proposal = SchemaEvolutionProposal(industry='saas', missing_dimension='domain_extensions', rationale='test', suggested_fields=['deployment_model'])
    result = engine.decide(proposal, PolicyContext(industry='saas', qa_failure_count=1))
    assert result.decision == PolicyDecision.approved
    assert result.matched_policy_id == 'pol_saas_approve'


def test_policy_engine_high_risk_rejected(tmp_path) -> None:
    store = SQLiteStore(tmp_path / 'test.db')
    engine = ApprovalPolicyEngine(store)

    store.upsert_policy(
        ApprovalPolicy(
            policy_id='pol_saas_low_only',
            industry='saas',
            priority=1,
            max_fields=10,
            max_qa_failures=10,
            max_allowed_risk=RiskLevel.low,
            decision=PolicyDecision.approved,
        )
    )

    proposal = SchemaEvolutionProposal(industry='saas', missing_dimension='domain_extensions', rationale='test', suggested_fields=['compliance_support'])
    result = engine.decide(proposal, PolicyContext(industry='saas', qa_failure_count=1))
    assert result.decision == PolicyDecision.rejected


def test_policy_engine_default_reject_when_no_match(tmp_path) -> None:
    store = SQLiteStore(tmp_path / 'test.db')
    engine = ApprovalPolicyEngine(store)

    store.upsert_policy(
        ApprovalPolicy(
            policy_id='pol_saas_tight',
            industry='saas',
            priority=1,
            max_fields=1,
            max_qa_failures=0,
            max_allowed_risk=RiskLevel.low,
            decision=PolicyDecision.approved,
        )
    )

    proposal = SchemaEvolutionProposal(industry='saas', missing_dimension='domain_extensions', rationale='test', suggested_fields=['deployment_model', 'seller_ecosystem'])
    result = engine.decide(proposal, PolicyContext(industry='saas', qa_failure_count=99))
    assert result.decision == PolicyDecision.rejected
    assert result.matched_policy_id is None
