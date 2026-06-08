from __future__ import annotations

from app.agents.orchestrator_agent import OrchestratorAgent
from app.core.config import AppConfig
from app.core.models import QAOutput, RunState
from app.core.planner_llm import PlannerLLMClient


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


def test_generate_dynamic_plan_does_not_merge_competitor_hints_into_planned_competitors() -> None:
    planner = PlannerLLMClient(AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m'))
    orchestrator = OrchestratorAgent(max_rework_iterations=2, planner=planner)

    planner.infer_industry_from_prompt = lambda **_kwargs: 'video_meeting_saas'  # type: ignore[method-assign]
    planner.infer_product_profile = lambda **_kwargs: {  # type: ignore[method-assign]
        'target_product': '腾讯会议',
        'target_product_description': '云视频会议 SaaS',
        'intent_summary': '分析腾讯会议竞品',
        'product_category': '云视频会议 SaaS',
    }
    planner.discover_competitors_grouped = lambda **_kwargs: {  # type: ignore[method-assign]
        'competitors': {
            'direct': [{'name': 'Zoom'}],
            'substitute': [{'name': 'Google Meet'}],
        },
        'comparison_schema_fields': [],
        'comparison_search_plan': {},
        'comparison_corpus': [],
        'comparison_decision_evidence_refs': [],
        'product_profile': {},
        'fallback_reason': '',
    }
    planner.plan_schema = lambda **_kwargs: planner._core_schema_plan_only()  # type: ignore[method-assign]
    planner.last_call_status = lambda: {}  # type: ignore[method-assign]
    planner.step_call_status = lambda: {}  # type: ignore[method-assign]

    plan = orchestrator.generate_dynamic_plan(
        prompt='分析腾讯会议竞品',
        competitor_hints=['并与 Google 日历、Google Meet：为 Workspace'],
    )

    assert plan['planned_competitors'] == ['Zoom']


def test_generate_dynamic_plan_skips_plan_schema_when_no_competitors_confirmed() -> None:
    planner = PlannerLLMClient(AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m'))
    orchestrator = OrchestratorAgent(max_rework_iterations=2, planner=planner)

    planner.infer_industry_from_prompt = lambda **_kwargs: 'video_meeting_saas'  # type: ignore[method-assign]
    planner.infer_product_profile = lambda **_kwargs: {  # type: ignore[method-assign]
        'target_product': '腾讯会议',
        'target_product_description': '云视频会议 SaaS',
        'intent_summary': '分析腾讯会议竞品',
        'product_category': '云视频会议 SaaS',
    }
    planner.discover_competitors_grouped = lambda **_kwargs: {  # type: ignore[method-assign]
        'competitors': {'direct': [], 'substitute': []},
        'comparison_schema_fields': [],
        'comparison_search_plan': {},
        'comparison_corpus': [],
        'comparison_decision_evidence_refs': [],
        'product_profile': {},
        'fallback_reason': '',
    }
    planner.last_call_status = lambda: {}  # type: ignore[method-assign]
    planner.step_call_status = lambda: {}  # type: ignore[method-assign]

    def _unexpected_plan_schema(**_kwargs):
        raise AssertionError('plan_schema should not be called when no competitors are confirmed')

    planner.plan_schema = _unexpected_plan_schema  # type: ignore[method-assign]

    plan = orchestrator.generate_dynamic_plan(prompt='分析腾讯会议竞品')

    assert plan['planned_competitors'] == []
    assert [item['field_name'] for item in plan['analysis_schema_plan'][:5]] == ['feature_tree', 'strengths', 'weaknesses', 'pricing_model', 'user_feedback']


def test_generate_dynamic_plan_prefers_comparison_corpus_synthesis_result() -> None:
    planner = PlannerLLMClient(AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m'))
    orchestrator = OrchestratorAgent(max_rework_iterations=2, planner=planner)

    planner.infer_industry_from_prompt = lambda **_kwargs: 'video_meeting_saas'  # type: ignore[method-assign]
    planner.infer_product_profile = lambda **_kwargs: {  # type: ignore[method-assign]
        'target_product': '腾讯会议',
        'target_product_description': '视频会议 SaaS',
        'intent_summary': '分析腾讯会议竞品',
        'product_category': '视频会议 SaaS',
    }
    planner.discover_competitors_grouped = lambda **_kwargs: {  # type: ignore[method-assign]
        'competitors': {
            'direct': [{'name': '未来'}],
            'substitute': [{'name': '总之'}],
        },
        'comparison_decision': {
            'direct': [
                {'name': '钉钉会议'},
                {'name': 'Microsoft Teams'},
                {'name': 'Zoom'},
                {'name': '华为云会议'},
            ],
            'substitute': [
                {'name': '喧喧私有化部署方案'},
                {'name': '全时云会议'},
                {'name': 'Webex（思科）'},
            ],
        },
        'comparison_schema_fields': [],
        'comparison_search_plan': {},
        'comparison_corpus': [],
        'comparison_decision_evidence_refs': [],
        'product_profile': {},
        'fallback_reason': '',
    }
    planner.plan_schema = lambda **_kwargs: planner._core_schema_plan_only()  # type: ignore[method-assign]
    planner.last_call_status = lambda: {}  # type: ignore[method-assign]
    planner.step_call_status = lambda: {}  # type: ignore[method-assign]

    plan = orchestrator.generate_dynamic_plan(prompt='分析腾讯会议竞品')

    assert plan['planned_competitors'] == [
        '钉钉会议',
        'Microsoft Teams',
    ]
    assert [item['name'] for item in plan['candidate_groups']['direct']] == ['钉钉会议', 'Microsoft Teams']
    assert [item['name'] for item in plan['candidate_groups']['substitute']] == ['喧喧私有化部署方案', '全时云会议', 'Webex（思科）']
    assert plan['planner_meta']['candidate_policy'] == 'direct_only_analysis'

    assert len(plan['planner_meta']['comparison_decision_full']['direct']) == 4
    assert len(plan['planner_meta']['comparison_decision_full']['substitute']) == 3

    state = RunState(
        industry='video_meeting_saas',
        competitors=[],
        planned_competitors=plan['planned_competitors'],
        planner_meta={
            'candidate_groups': plan['candidate_groups'],
            'candidate_policy': plan['planner_meta']['candidate_policy'],
        },
        target_product='腾讯会议',
    )
    assert state.effective_analysis_subject_names() == ['腾讯会议', '钉钉会议', 'Microsoft Teams']
