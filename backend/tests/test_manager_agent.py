from __future__ import annotations

from app.core.models import AnalysisFieldResult, AnalysisSchemaField, CompetitorAnalysisRecord, DecisionContextSnapshot, Finding, RawEvidence, Report, ReportSection, RunState
from app.core.storage import SQLiteStore
from app.core.workflow import CompetitorWorkflowService
from app.agents.manager_agent import ManagerAgent


def test_manager_infers_new_action_types_from_tools() -> None:
    assert ManagerAgent._infer_action_type_from_tool('action.collect_initial') == 'collect_initial'
    assert ManagerAgent._infer_action_type_from_tool('action.collect_gap') == 'collect_gap'
    assert ManagerAgent._infer_action_type_from_tool('action.reanalyze_targets') == 'reanalyze_targets'
    assert ManagerAgent._infer_action_type_from_tool('action.draft_report') == 'draft_report'


def test_manager_decide_picks_plan_scope_for_fresh_run(monkeypatch, tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_plan.db'))
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        user_prompt='analyze alpha',
    )
    service.store.save_state(state)

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, token_tracker=None, network_retries=None):  # noqa: ARG001
        return {
            'tool_calls': [],
            'final_output': {
                'action_type': 'plan_scope',
                'target_agent': 'OrchestratorAgent',
                'targets': {'competitors': ['alpha']},
                'reason': 'scope_and_schema_missing',
                'expected_outcome': 'produce planned competitors and schema',
                'success_criteria': ['planned_competitors populated', 'analysis_schema_plan populated'],
                'priority': 1,
                'decision_basis': ['plan_missing'],
                'rejected_actions': [{'action': 'collect_initial', 'reason': 'cannot collect before planning'}],
                'confidence': 0.95,
                'metadata': {},
            },
        }

    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)

    decision = service._manager_decide(state)

    assert decision.action_type.value == 'plan_scope'
    assert decision.target_agent == 'OrchestratorAgent'
    assert decision.decision_basis == ['plan_missing']


def test_manager_decide_uses_state_tools_and_picks_reanalyze_when_evidence_ready(monkeypatch, tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_reanalyze.db'))
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        planned_competitors=['alpha'],
        user_prompt='analyze alpha',
    )
    state.analysis_schema_plan = [AnalysisSchemaField(field_name='pricing_model')]
    state.evidences = [RawEvidence(source_url='https://example.com/pricing', snippet='price info')]
    service.store.save_state(state)

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, token_tracker=None, network_retries=None):  # noqa: ARG001
        history = user_payload.get('tool_history', [])
        if not history:
            return {
                'tool_calls': [{'name': 'state.get_run_snapshot', 'arguments': {'run_id': state.run_id}}],
                'final_output': None,
            }
        run_snapshot = history[0]['tool_calls'][0]['output']['run']
        assert run_snapshot['evidence_count'] == 1
        return {
            'tool_calls': [],
            'final_output': {
                'action_type': 'reanalyze_targets',
                'target_agent': 'AnalystAgent',
                'targets': {'competitors': ['alpha'], 'fields': ['pricing_model']},
                'reason': 'evidence_ready_analysis_missing',
                'expected_outcome': 'produce_competitor_analysis',
                'success_criteria': ['analysis generated for pricing_model'],
                'priority': 1,
                'decision_basis': ['evidence_ready', 'analysis_missing'],
                'rejected_actions': [{'action': 'collect_initial', 'reason': 'existing evidence is available'}],
                'confidence': 0.88,
                'metadata': {},
            },
        }

    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)

    decision = service._manager_decide(state)

    assert decision.action_type.value == 'reanalyze_targets'
    assert decision.target_agent == 'AnalystAgent'
    assert decision.decision_basis == ['evidence_ready', 'analysis_missing']
    assert decision.targets.fields == ['pricing_model']
    assert decision.confidence == 0.88
    assert decision.metadata['tool_calls'] == 1


def test_manager_does_not_treat_plan_comparison_corpus_as_collect_ready(monkeypatch, tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_plan_corpus.db'))
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        planned_competitors=['alpha'],
        user_prompt='analyze alpha',
    )
    state.analysis_schema_plan = [AnalysisSchemaField(field_name='pricing_model')]
    state.evidences = [
        RawEvidence(
            source_url='https://example.com/comparison',
            snippet='cross competitor comparison summary',
            domain_extensions={'origin': 'plan_comparison_corpus'},
        )
    ]
    service.store.save_state(state)

    context = service._build_decision_context(state)
    assert context.plan_ready is True
    assert context.collect_ready is False

    decision = service.manager_agent.fallback_decide(context=context)

    assert decision.action_type.value == 'collect_initial'
    assert decision.target_agent == 'CollectorAgent'


def test_manager_decide_picks_run_qa_when_findings_ready_but_report_missing(monkeypatch, tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_draft_guard.db'))
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        planned_competitors=['alpha'],
        user_prompt='draft alpha',
    )
    state.analysis_schema_plan = [AnalysisSchemaField(field_name='pricing_model')]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name='alpha',
            fields=[AnalysisFieldResult(field_name='pricing_model', summary='tiered', evidence_refs=['evd_1'])],
        )
    ]
    state.findings = [Finding(statement='alpha has tiered pricing', category='pricing', evidence_refs=['evd_1'])]
    service.store.save_state(state)

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, token_tracker=None, network_retries=None):  # noqa: ARG001
        return {
            'tool_calls': [],
            'final_output': {
                'action_type': 'draft_report',
                'target_agent': 'WriterAgent',
                'targets': {'competitors': ['alpha'], 'sections': ['pricing_strategy']},
                'reason': 'findings_ready_report_missing',
                'expected_outcome': 'generate draft report',
                'success_criteria': ['report markdown is generated'],
                'priority': 1,
                'decision_basis': ['findings_ready', 'report_missing'],
                'rejected_actions': [{'action': 'reanalyze_targets', 'reason': 'analysis already exists'}],
                'confidence': 0.91,
                'metadata': {},
            },
        }

    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)

    decision = service._manager_decide(state)

    assert decision.action_type.value == 'run_qa'
    assert decision.target_agent == 'QACriticAgent'
    assert decision.metadata['guard_rewritten'] is True
    assert decision.metadata['guard_reason'] == 'draft_requires_pre_draft_qa'


def test_manager_decide_picks_run_qa_when_report_ready(monkeypatch, tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_qa.db'))
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        planned_competitors=['alpha'],
        user_prompt='qa alpha',
    )
    state.analysis_schema_plan = [AnalysisSchemaField(field_name='pricing_model')]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name='alpha',
            fields=[AnalysisFieldResult(field_name='pricing_model', summary='tiered', evidence_refs=['evd_1'])],
        )
    ]
    state.findings = [Finding(statement='alpha has tiered pricing', category='pricing', evidence_refs=['evd_1'])]
    state.report = Report(
        executive_summary='done',
        sections=[ReportSection(section_id='pricing_strategy', title='Pricing', field_name='pricing_model', content_markdown='ok')],
        markdown='# report',
        html='<h1>report</h1>',
    )
    service.store.save_state(state)

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, token_tracker=None, network_retries=None):  # noqa: ARG001
        return {
            'tool_calls': [],
            'final_output': {
                'action_type': 'run_qa',
                'target_agent': 'QACriticAgent',
                'targets': {'competitors': ['alpha']},
                'reason': 'report_ready_for_validation',
                'expected_outcome': 'qa validates report',
                'success_criteria': ['qa pass or emit rework ticket'],
                'priority': 1,
                'decision_basis': ['report_ready', 'analysis_ready'],
                'rejected_actions': [{'action': 'finalize_run', 'reason': 'qa must run before finalize'}],
                'confidence': 0.94,
                'metadata': {},
            },
        }

    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)

    decision = service._manager_decide(state)

    assert decision.action_type.value == 'run_qa'
    assert decision.target_agent == 'QACriticAgent'
    assert decision.rejected_actions[0]['action'] == 'finalize_run'


def test_manager_decide_repairs_tool_protocol_before_business_fallback(monkeypatch, tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_protocol_repair.db'))
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        planned_competitors=['alpha'],
        user_prompt='qa alpha',
    )
    state.analysis_schema_plan = [AnalysisSchemaField(field_name='pricing_model')]
    state.evidences = [RawEvidence(source_url='https://example.com/pricing', snippet='price info')]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name='alpha',
            fields=[AnalysisFieldResult(field_name='pricing_model', summary='tiered', evidence_refs=['evd_1'])],
        )
    ]
    state.findings = [Finding(statement='alpha has tiered pricing', category='pricing', evidence_refs=['evd_1'])]
    state.report = Report(
        executive_summary='done',
        sections=[ReportSection(section_id='pricing_strategy', title='Pricing', field_name='pricing_model', content_markdown='ok')],
        markdown='# report',
        html='<h1>report</h1>',
    )
    service.store.save_state(state)

    calls = {'count': 0}

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, token_tracker=None, network_retries=None):  # noqa: ARG001
        calls['count'] += 1
        if calls['count'] == 1:
            return {}
        return {
            'action_type': 'run_qa',
            'target_agent': 'QACriticAgent',
            'targets': {'competitors': ['alpha']},
            'reason': 'repaired_protocol_then_run_qa',
            'expected_outcome': 'qa validates report',
            'success_criteria': ['qa pass or emit rework ticket'],
            'priority': 1,
            'decision_basis': ['report_ready', 'protocol_repaired'],
            'rejected_actions': [{'action': 'finalize_run', 'reason': 'qa should run first'}],
            'confidence': 0.87,
            'metadata': {},
        }

    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)

    decision = service._manager_decide(state)

    assert calls['count'] == 2
    assert decision.action_type.value == 'run_qa'
    assert decision.metadata['protocol_repaired'] is True
    assert decision.metadata.get('fallback') is None


def test_manager_decide_allows_finalize_after_qa_ready(monkeypatch, tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_finalize.db'))
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        planned_competitors=['alpha'],
        user_prompt='finalize alpha',
        planner_meta={'last_qa_checked': True, 'last_qa_passed': True, 'last_qa_issue_count': 0},
    )
    state.analysis_schema_plan = [AnalysisSchemaField(field_name='pricing_model')]
    state.competitor_analyses = [
        CompetitorAnalysisRecord(
            product_name='alpha',
            fields=[AnalysisFieldResult(field_name='pricing_model', summary='tiered', evidence_refs=['evd_1'])],
        )
    ]
    state.findings = [Finding(statement='alpha has tiered pricing', category='pricing', evidence_refs=['evd_1'])]
    state.report = Report(
        executive_summary='done',
        sections=[ReportSection(section_id='pricing_strategy', title='Pricing', field_name='pricing_model', content_markdown='ok')],
        markdown='# report',
        html='<h1>report</h1>',
    )
    service.store.save_state(state)

    def _fake_invoke_json(*, trace_name, system_prompt, user_payload, metadata, token_tracker=None, network_retries=None):  # noqa: ARG001
        return {
            'tool_calls': [],
            'final_output': {
                'action_type': 'finalize_run',
                'target_agent': 'Finalizer',
                'targets': {'competitors': ['alpha']},
                'reason': 'report_and_qa_ready_for_delivery',
                'expected_outcome': 'mark run completed',
                'success_criteria': ['run status becomes completed'],
                'priority': 1,
                'decision_basis': ['report_ready', 'analysis_ready', 'qa_ready'],
                'rejected_actions': [{'action': 'run_qa', 'reason': 'qa already completed in prior attempt'}],
                'confidence': 0.89,
                'metadata': {},
            },
        }

    monkeypatch.setattr(service.agent_llm, 'invoke_json', _fake_invoke_json)

    decision = service._manager_decide(state)

    assert decision.action_type.value == 'finalize_run'
    assert decision.target_agent == 'Finalizer'
    assert decision.decision_basis == ['report_ready', 'analysis_ready', 'qa_ready']


def test_manager_fallback_runs_qa_when_quality_gate_allows_but_qa_missing(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_fallback_finalize.db'))
    context = DecisionContextSnapshot(
        run_id='run_test',
        turn_count=5,
        plan_ready=True,
        collect_ready=True,
        analyze_ready=True,
        report_ready=True,
        quality_gate={'finalize_eligible': True},
    )

    decision = service.manager_agent.fallback_decide(context=context)

    assert decision.action_type.value == 'run_qa'
    assert decision.target_agent == 'QACriticAgent'
    assert decision.reason == 'analysis_ready_requires_pre_draft_qa'


def test_manager_fallback_finalizes_when_qa_passed_even_if_quality_gate_static_false(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_fallback_qa_passed.db'))
    context = DecisionContextSnapshot(
        run_id='run_test',
        turn_count=5,
        plan_ready=True,
        collect_ready=True,
        analyze_ready=True,
        report_ready=True,
        qa_reviewed=True,
        qa_passed=True,
        quality_gate={'finalize_eligible': False},
    )

    decision = service.manager_agent.fallback_decide(context=context)

    assert decision.action_type.value == 'finalize_run'
    assert decision.target_agent == 'Finalizer'
    assert decision.reason == 'qa_approved_for_delivery'


def test_manager_fallback_drafts_when_qa_passed_before_report(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_fallback_qa_passed_draft.db'))
    context = DecisionContextSnapshot(
        run_id='run_test',
        turn_count=5,
        plan_ready=True,
        collect_ready=True,
        analyze_ready=True,
        report_ready=False,
        qa_reviewed=True,
        qa_passed=True,
        quality_gate={'finalize_eligible': False},
    )

    decision = service.manager_agent.fallback_decide(context=context)

    assert decision.action_type.value == 'draft_report'
    assert decision.target_agent == 'WriterAgent'
    assert decision.reason == 'qa_approved_for_draft'


def test_manager_fallback_uses_pending_qa_collect_plan(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_fallback_collect.db'))
    context = DecisionContextSnapshot(
        run_id='run_test',
        turn_count=5,
        plan_ready=True,
        collect_ready=True,
        analyze_ready=True,
        report_ready=True,
        qa_reviewed=True,
        qa_passed=False,
        qa_collect_pending=True,
        qa_collect_allowed=True,
        quality_gate={'finalize_eligible': False},
    )

    decision = service.manager_agent.fallback_decide(context=context)

    assert decision.action_type.value == 'collect_gap'
    assert decision.target_agent == 'CollectorAgent'
    assert decision.reason == 'qa_failed_collect_plan_pending'


def test_manager_fallback_does_not_auto_finalize_after_qa_collect_budget_used(tmp_path) -> None:
    service = CompetitorWorkflowService(SQLiteStore(tmp_path / 'manager_fallback_no_auto_finalize.db'))
    context = DecisionContextSnapshot(
        run_id='run_test',
        turn_count=5,
        plan_ready=True,
        collect_ready=True,
        analyze_ready=True,
        report_ready=True,
        qa_reviewed=True,
        qa_passed=False,
        qa_collect_pending=False,
        qa_collect_allowed=False,
        quality_gate={'finalize_eligible': False},
    )

    decision = service.manager_agent.fallback_decide(context=context)

    assert decision.action_type.value == 'collect_gap'
    assert decision.target_agent == 'CollectorAgent'
    assert decision.reason == 'qa_failed_requires_recollect'
