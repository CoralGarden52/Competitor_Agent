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
    AnalyzeHandoff,
    AnalyzeOutput,
    CollectHandoff,
    CollectOutput,
    CompetitorProfile,
    DraftOutput,
    FeatureNode,
    Finding,
    PlanHandoff,
    PricingModel,
    QAOutput,
    RawEvidence,
    ReworkIssue,
    ReworkTicket,
    RunRequest,
    RunState,
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


def test_collector_preview_handles_empty_competitor_plan(tmp_path) -> None:
    store = SQLiteStore(tmp_path / 'test.db')
    service = CompetitorWorkflowService(store)
    service.orchestrator.generate_dynamic_plan = lambda **_kwargs: {  # type: ignore[method-assign]
        'planned_competitors': [],
        'candidate_groups': {'direct': [], 'substitute': []},
        'analysis_schema_plan': [],
        'inferred_industry': 'saas',
        'planner_meta': {},
    }
    preview = service.collector_preview(prompt='AI agent competitor analysis')
    assert preview['planned_competitors'] == []
    assert preview['preview'] == []
    assert 'no_competitors_discovered' in preview['errors']


def test_plan_persists_inferred_industry_into_run_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path / 'test.db')
    service = CompetitorWorkflowService(store)
    service.orchestrator.generate_dynamic_plan = lambda **_kwargs: {  # type: ignore[method-assign]
        'planned_competitors': ['alpha'],
        'candidate_groups': {'direct': [{'name': 'alpha'}], 'substitute': []},
        'analysis_schema_plan': [],
        'inferred_industry': 'meeting_software',
        'planner_meta': {},
    }
    state = RunState(industry='', competitors=['alpha'], user_prompt='线上会议软件竞品分析')
    service._plan(state)
    assert state.industry == 'meeting_software'


def test_stage_handoffs_are_persisted_and_replayed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / 'test.db')
    service = CompetitorWorkflowService(store)
    run = service.start_run(RunRequest(industry='saas', competitors=['alpha'])).state

    handoffs = store.list_stage_handoffs(run.run_id)
    handoff_types = [item['handoff_type'] for item in handoffs]

    assert 'PlanHandoff' in handoff_types
    assert 'CollectHandoff' in handoff_types
    assert 'AnalyzeHandoff' in handoff_types

    plan_handoff = store.latest_stage_handoff(run.run_id, stage=StageName.plan)
    collect_handoff = store.latest_stage_handoff(run.run_id, stage=StageName.collect)
    analyze_handoff = store.latest_stage_handoff(run.run_id, stage=StageName.analyze)

    assert isinstance(plan_handoff, PlanHandoff)
    assert isinstance(collect_handoff, CollectHandoff)
    assert isinstance(analyze_handoff, AnalyzeHandoff)

    replay = service.replay_node(run.run_id, 'analyze')
    assert replay['handoffs']
