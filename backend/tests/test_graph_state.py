from __future__ import annotations

import pytest

from app.core.graph_state import (
    init_graph_state_from_run_request,
    merge_analyze_output,
    merge_collect_output,
    merge_draft_output,
    merge_qa_output,
)
from app.core.models import (
    AnalyzeOutput,
    CollectOutput,
    CompetitorProfile,
    DraftOutput,
    FeatureNode,
    Finding,
    PricingModel,
    QAOutput,
    RawEvidence,
    ReworkIssue,
    ReworkTicket,
    RunRequest,
    StageName,
)
from app.core.workflow import CompetitorWorkflowService
from app.core.storage import SQLiteStore


def _profile() -> CompetitorProfile:
    return CompetitorProfile(
        industry='saas',
        product_name='alpha',
        positioning='p',
        feature_tree=[FeatureNode(name='core', capability='cap')],
        advantages=['a'],
        disadvantages=['d'],
        pricing_model=PricingModel(model_type='subscription', free_tier=True),
        user_feedback={'positive_themes': [], 'negative_themes': [], 'representative_quotes': [], 'sentiment_distribution': {}},
    )


def test_init_graph_state_from_request() -> None:
    req = RunRequest(industry='saas', competitors=['alpha'])
    state = init_graph_state_from_run_request(request=req, run_id='run_x', core_schema_version='core_v1', domain_schema_version='v1')
    assert state['run_id'] == 'run_x'
    assert state['industry'] == 'saas'
    assert state['raw_evidences'] == []


def test_merge_outputs_are_scoped() -> None:
    req = RunRequest(industry='saas', competitors=['alpha'])
    state = init_graph_state_from_run_request(request=req, run_id='run_x', core_schema_version='core_v1', domain_schema_version='v1')

    state = merge_collect_output(state, CollectOutput(raw_evidences=[RawEvidence(source_url='https://x', snippet='s')], errors=[]))
    assert len(state['raw_evidences']) == 1

    state = merge_analyze_output(state, AnalyzeOutput(profiles=[_profile()], findings=[Finding(statement='f', category='feature', evidence_refs=['evd_1'])]))
    assert len(state['profiles']) == 1
    assert len(state['findings']) == 1

    state = merge_draft_output(state, DraftOutput(report={'executive_summary': 'x', 'comparison_matrix': [], 'swot': {'strengths': [], 'weaknesses': [], 'opportunities': [], 'threats': []}, 'opportunities': [], 'appendix_sources': [], 'markdown': ''}))
    assert state['report'] is not None

    qa = QAOutput(passed=False, issues=[ReworkIssue(code='x', message='m', stage=StageName.qa)], target_agent='Draft', ticket=ReworkTicket(target_agent='Draft', issues=[ReworkIssue(code='x', message='m', stage=StageName.qa)]))
    state = merge_qa_output(state, qa)
    assert len(state['tickets']) == 1


def test_finding_requires_evidence_refs() -> None:
    with pytest.raises(Exception):
        Finding(statement='x', category='feature', evidence_refs=[])


def test_run_state_graph_state_roundtrip(tmp_path) -> None:
    store = SQLiteStore(tmp_path / 'test.db')
    service = CompetitorWorkflowService(store)
    req = RunRequest(industry='saas', competitors=['alpha'])
    run = service.start_run(req).state
    graph = service.run_state_to_graph_state(run)
    restored = service.graph_state_to_run_state(graph)
    assert restored.run_id == run.run_id
    assert restored.industry == run.industry
