from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agents import AnalystAgent, CollectorAgent, OrchestratorAgent, QACriticAgent, WriterAgent
from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.approval_policy_engine import ApprovalPolicyEngine, PolicyContext
from app.core.collector import CollectorPipeline
from app.core.config import get_config
from app.core.langgraph_runtime import WorkflowLangGraphRuntime
from app.core.planner_llm import PlannerLLMClient
from app.core.graph_state import WorkflowGraphState, init_graph_state_from_run_request, make_stage_snapshot
from app.core.models import (
    ApprovalPolicy,
    CompetitorProfile,
    FieldRiskProfile,
    EventEnvelope,
    EventType,
    FeatureNode,
    FeedbackSummary,
    Finding,
    PolicyAuditRecord,
    PolicyDecision,
    PolicyUpsertRequest,
    PricingModel,
    PricingTier,
    ProposalActivateRequest,
    ProposalReviewRequest,
    ProposalStatus,
    QAOutput,
    QAResult,
    ReworkIssue,
    ReworkTicket,
    Report,
    RunRequest,
    RunResponse,
    RunState,
    RunSummary,
    SchemaEvolutionProposal,
    SelfEval,
    Severity,
    StageName,
    TicketStatus,
)
from app.core.qa import run_qa_gate
from app.core.schema_registry import CORE_SCHEMA_VERSION, get_domain_schema, registry_snapshot
from app.core.storage import SQLiteStore


class CompetitorWorkflowService:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.config = get_config()
        self.planner_llm = PlannerLLMClient(self.config)
        self.agent_llm = AgentLLMClient(self.config)
        self.collector = CollectorPipeline(self.config, self.store)
        self.policy_engine = ApprovalPolicyEngine(store)
        self.orchestrator = OrchestratorAgent(max_rework_iterations=self.config.max_rework_iterations, planner=self.planner_llm)
        self.collector_agent = CollectorAgent(self.collector, self.store)
        self.analyst_agent = AnalystAgent(self.agent_llm, self.store)
        self.writer_agent = WriterAgent(self.agent_llm)
        self.qa_critic_agent = QACriticAgent(self.agent_llm, self.store)
        self.runtime = WorkflowLangGraphRuntime(self)

    def start_run(self, request: RunRequest) -> RunResponse:
        state = RunState(
            industry=request.industry.strip().lower(),
            competitors=request.competitors,
            language=request.language,
            timeframe=request.timeframe,
            core_schema_version=CORE_SCHEMA_VERSION,
            domain_schema_version=self.store.get_active_domain_schema(request.industry).get('version', 'v1'),
        )
        _ = init_graph_state_from_run_request(
            request=request,
            run_id=state.run_id,
            core_schema_version=state.core_schema_version,
            domain_schema_version=state.domain_schema_version,
        )
        self._save_and_event(state, StageName.plan, 'start', {'competitors': request.competitors})
        state = self.runtime.execute(state)
        if state.status not in ('completed', 'failed'):
            state.status = 'failed'
            self._save_and_event(state, StageName.qa, 'max_iterations_reached', {'iteration': state.attempt, 'reason': 'runtime_ended_without_terminal_status'})
        self.store.save_state(state)
        return RunResponse(summary=self._summary_for(state), state=state)

    def get_run(self, run_id: str) -> RunResponse | None:
        state = self.store.get_state(run_id)
        if state is None:
            return None
        return RunResponse(summary=self._summary_for(state), state=state)

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        return self.store.list_runs(limit=limit)

    def list_run_events(self, run_id: str) -> list[dict]:
        return self.store.list_events(run_id)

    def replay_run(self, run_id: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            return {'run_id': run_id, 'timeline': [], 'status': 'not_found'}
        timeline = self.store.replay_timeline(run_id)
        return {'run_id': run_id, 'status': run.state.status, 'timeline': timeline}

    def replay_node(self, run_id: str, node_name: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            return {'run_id': run_id, 'node_name': node_name, 'io': [], 'status': 'not_found'}
        io = self.store.replay_node_io(run_id, node_name)
        return {'run_id': run_id, 'node_name': node_name, 'io': io}

    def resume_from_checkpoint(self, run_id: str) -> RunResponse | None:
        state = self.store.latest_checkpoint(run_id)
        if state is None:
            return None
        resumed = self.runtime.execute(state)
        self.store.save_state(resumed)
        return RunResponse(summary=self._summary_for(resumed), state=resumed)

    def manual_intervene(
        self,
        *,
        run_id: str,
        node_name: str,
        action: str,
        actor: str,
        reason: str,
        patch: dict[str, object],
    ) -> RunResponse | None:
        state = self.store.get_state(run_id)
        if state is None:
            return None
        before = state.model_dump()
        if 'analysis_schema_plan' in patch and isinstance(patch['analysis_schema_plan'], list):
            state.analysis_schema_plan = patch['analysis_schema_plan']  # type: ignore[assignment]
        if 'planned_competitors' in patch and isinstance(patch['planned_competitors'], list):
            state.planned_competitors = [str(x) for x in patch['planned_competitors']]  # type: ignore[index]
        if 'status' in patch and isinstance(patch['status'], str):
            state.status = patch['status']  # type: ignore[assignment]
        after = state.model_dump()
        self.store.audit_manual_intervention(
            run_id=run_id,
            node_name=node_name,
            action=action,
            before=before,
            after=after,
            reason=reason,
            actor=actor,
        )
        self.store.save_state(state)
        return RunResponse(summary=self._summary_for(state), state=state)

    def collector_preview(self, *, prompt: str, industry_hint: str = '', competitor_hints: list[str] | None = None) -> dict:
        dynamic_plan = self.orchestrator.generate_dynamic_plan(
            prompt=prompt,
            industry_hint=industry_hint,
            competitor_hints=competitor_hints or [],
        )
        inferred_industry = str(dynamic_plan.get('inferred_industry', (industry_hint or 'general'))).strip().lower() or 'general'
        planned_competitors = dynamic_plan.get('planned_competitors', competitor_hints or [])
        analysis_schema_plan = dynamic_plan.get('analysis_schema_plan', [])
        candidate_groups = dynamic_plan.get('candidate_groups', {'direct': [], 'substitute': []})
        effective_max_urls = self.config.collector_max_urls
        preview = []
        errors = []
        execution_timeline: list[dict] = []
        seq = 1
        execution_timeline.append(
            {
                'seq': seq,
                'event_type': 'plan.competitors_generated',
                'payload': {'planned_competitors': planned_competitors, 'candidate_groups': candidate_groups},
            }
        )
        seq += 1
        execution_timeline.append(
            {
                'seq': seq,
                'event_type': 'plan.schema_generated',
                'payload': {'analysis_schema_plan': analysis_schema_plan},
            }
        )
        seq += 1
        for competitor in planned_competitors:
            result = self.collector.collect(
                run_id='preview',
                industry=inferred_industry,
                competitor=competitor,
                max_urls=effective_max_urls,
                schema_plan=analysis_schema_plan,
                per_field_limit=self.config.collector_per_field_limit,
            )
            search_events = [e for e in result.provider_events if str(e.get('event_type', '')).startswith('collector.search.')]
            fetch_events = [e for e in result.provider_events if str(e.get('event_type', '')).startswith('collector.fetch.')]
            fallback_trace = []
            for event in result.provider_events:
                if event.get('event_type') == 'collector.fallback.trace':
                    fallback_trace = event.get('fallback_trace', [])
                    break
            field_stats = self._build_field_stats(result.provider_events)
            field_summaries = self._build_field_summaries(result.evidences)
            preview.append(
                {
                    'competitor': competitor,
                    'evidence_count': len(result.evidences),
                    'sample': result.evidences[:3],
                    'search_events': search_events,
                    'fetch_events': fetch_events,
                    'fallback_trace': fallback_trace,
                    'field_stats': field_stats,
                    'field_summaries': field_summaries,
                }
            )
            for event in result.provider_events:
                execution_timeline.append({'seq': seq, 'competitor': competitor, **event})
                seq += 1
            errors.extend(result.errors)
        response = {
            'prompt': prompt,
            'industry_hint': industry_hint,
            'inferred_industry': inferred_industry,
            'effective_max_urls': effective_max_urls,
            'max_urls_note': 'server uses COLLECTOR_MAX_URLS from .env',
            'candidate_groups': candidate_groups,
            'candidates': candidate_groups,
            'handoff_targets': {
                'direct': [item.get('name', '') for item in candidate_groups.get('direct', []) if str(item.get('name', '')).strip()],
                'substitute': [item.get('name', '') for item in candidate_groups.get('substitute', []) if str(item.get('name', '')).strip()],
            },
            'plan_phase': {
                'competitors_generated': planned_competitors,
                'schema_generated': analysis_schema_plan,
                'planner_meta': dynamic_plan.get('planner_meta', {}),
            },
            'execution_timeline': execution_timeline,
            'preview': preview,
            'errors': errors,
            'planned_competitors': planned_competitors,
            'analysis_schema_plan': analysis_schema_plan,
            'planner_meta': dynamic_plan.get('planner_meta', {}),
        }
        auto_saved, auto_saved_file, auto_saved_error = self._auto_save_preview_result(response)
        response['auto_saved'] = auto_saved
        response['auto_saved_file'] = auto_saved_file
        if auto_saved_error:
            response['auto_saved_error'] = auto_saved_error
        return response

    def _auto_save_preview_result(self, payload: dict) -> tuple[bool, str, str]:
        if not self.config.collector_preview_auto_save_enabled:
            return False, '', ''
        try:
            save_dir = Path(self.config.collector_preview_save_dir)
            if not save_dir.is_absolute():
                save_dir = Path.cwd() / save_dir
            save_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            suffix = uuid4().hex[:6]
            target = save_dir / f'collector_preview_result_{stamp}_{suffix}.json'
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
            return True, str(target), ''
        except Exception as exc:
            return False, '', str(exc)

    def _build_field_stats(self, provider_events: list[dict]) -> list[dict]:
        started: dict[str, int] = {}
        completed: dict[str, int] = {}
        for event in provider_events:
            et = str(event.get('event_type', ''))
            field_name = str(event.get('field_name', '')).strip()
            if not field_name:
                continue
            if et == 'collector.field_query.started':
                started[field_name] = int(event.get('query_count', 0))
            elif et == 'collector.field_query.completed':
                completed[field_name] = int(event.get('evidence_count', 0))
        fields = sorted(set(started) | set(completed))
        return [
            {
                'field_name': field_name,
                'evidence_count': completed.get(field_name, 0),
                'quota_limit': self.config.collector_per_field_limit,
                'queries_used': started.get(field_name, 0),
            }
            for field_name in fields
        ]

    @staticmethod
    def _build_field_summaries(evidences: list[dict]) -> dict[str, dict]:
        grouped: dict[str, list[dict]] = {}
        for item in evidences:
            field_name = str(item.get('schema_field', '')).strip()
            if not field_name:
                continue
            grouped.setdefault(field_name, []).append(item)
        summaries: dict[str, dict] = {}
        for field_name, items in grouped.items():
            top = items[:3]
            snippets = [str(x.get('snippet', ''))[:120] for x in top if str(x.get('snippet', '')).strip()]
            urls = [str(x.get('source_url', '')) for x in top if str(x.get('source_url', '')).strip()]
            confidence_values = [float(x.get('confidence', 0.0)) for x in top]
            avg_conf = round(sum(confidence_values) / max(1, len(confidence_values)), 2)
            summaries[field_name] = {
                'summary': '；'.join(snippets) if snippets else '暂无有效摘要',
                'evidence_urls': urls,
                'confidence': avg_conf,
            }
        return summaries

    def collector_provider_health(self) -> dict:
        return self.collector.provider_health()

    def collector_llm_health(self) -> dict:
        return self.planner_llm.check_health()

    def schema_registry(self, industry: str | None = None) -> dict[str, object]:
        return registry_snapshot(self.store, industry=industry)

    def list_policies(self, industry: str | None = None) -> list[ApprovalPolicy]:
        return self.store.list_policies(industry)

    def upsert_policy(self, payload: PolicyUpsertRequest) -> ApprovalPolicy:
        policy = ApprovalPolicy(
            policy_id=payload.policy_id or ApprovalPolicy().policy_id,
            industry=payload.industry.strip().lower(),
            enabled=payload.enabled,
            priority=payload.priority,
            max_fields=payload.max_fields,
            max_qa_failures=payload.max_qa_failures,
            max_allowed_risk=payload.max_allowed_risk,
            denied_scopes=payload.denied_scopes,
            decision=payload.decision,
            version=payload.version,
            notes=payload.notes,
        )
        return self.store.upsert_policy(policy)

    def list_field_risks(self, industry: str | None = None) -> list[FieldRiskProfile]:
        return self.store.list_field_risks(industry)

    def upsert_field_risks(self, items: list[FieldRiskProfile]) -> list[FieldRiskProfile]:
        return [self.store.upsert_field_risk(item) for item in items]

    def list_policy_audits(self, proposal_id: str | None = None) -> list[PolicyAuditRecord]:
        return self.store.list_policy_audits(proposal_id)

    def list_proposals(self, status: str | None = None) -> list[SchemaEvolutionProposal]:
        parsed_status = ProposalStatus(status) if status else None
        return self.store.list_proposals(parsed_status)

    def get_proposal(self, proposal_id: str) -> dict | None:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            return None
        return {'proposal': proposal, 'audit': self.store.get_proposal_audit(proposal_id)}

    def review_proposal(self, proposal_id: str, request: ProposalReviewRequest) -> SchemaEvolutionProposal | None:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            return None
        proposal.status = ProposalStatus.reviewed if request.decision == 'reviewed' else ProposalStatus.rejected
        proposal.reviewed_by = request.reviewer
        proposal.review_notes = request.notes
        proposal.updated_at = datetime.now(UTC)
        self.store.review_proposal(proposal, reviewer=request.reviewer, decision=proposal.status, notes=request.notes)
        return proposal

    def activate_proposal(self, proposal_id: str, request: ProposalActivateRequest) -> SchemaEvolutionProposal | None:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            return None
        if proposal.status not in (ProposalStatus.reviewed, ProposalStatus.activated) and not request.force:
            return None
        proposal.status = ProposalStatus.activated
        proposal.updated_at = datetime.now(UTC)
        self.store.activate_proposal(proposal, activated_by=request.activated_by)
        return proposal

    def _plan(self, state: RunState) -> None:
        dynamic_plan = self.orchestrator.generate_dynamic_plan(industry=state.industry, competitors=state.competitors)
        state.planned_competitors = dynamic_plan.get('planned_competitors', state.competitors)
        state.analysis_schema_plan = dynamic_plan.get('analysis_schema_plan', [])
        state.planner_meta = dynamic_plan.get('planner_meta', {})
        split_strategy = 'by_competitor' if len(state.competitors) <= 4 else 'by_topic'
        state.split_strategy = split_strategy
        state.self_eval['plan'] = SelfEval(coverage=1.0, consistency=0.9, evidence_quality=0.8, uncertainty=0.2)
        self._save_and_event(
            state,
            StageName.plan,
            EventType.plan_started.value,
            {
                'split_strategy': split_strategy,
                'core_schema_version': state.core_schema_version,
                'domain_schema_version': state.domain_schema_version,
                'planned_competitors': state.planned_competitors,
                'schema_field_count': len(state.analysis_schema_plan),
            },
        )
        self._save_and_event(
            state,
            StageName.plan,
            'plan.competitors_generated',
            {'planned_competitors': state.planned_competitors, 'planner_meta': state.planner_meta},
        )
        self._save_and_event(
            state,
            StageName.plan,
            'plan.schema_generated',
            {'analysis_schema_plan': state.analysis_schema_plan},
        )

    def _collect(self, state: RunState) -> None:
        result: CollectOutput = self.collector_agent.run(state)
        for pe in result.provider_events:
            self._save_and_event(state, StageName.collect, 'provider_event', pe)
        state.evidences = list(result.raw_evidences)
        active_competitors = state.planned_competitors or state.competitors
        coverage = min(1.0, len(state.evidences) / max(2, len(active_competitors) * 2))
        quality = 0.35 if result.errors else 0.72
        state.self_eval['collect'] = SelfEval(coverage=coverage, consistency=0.75, evidence_quality=quality, uncertainty=0.35)
        self._save_and_event(
            state,
            StageName.collect,
            EventType.collect_completed.value,
            {'evidence_count': len(state.evidences), 'error_count': len(result.errors), 'errors': result.errors[:5]},
        )

    def _normalize(self, state: RunState) -> None:
        seen: set[str] = set()
        normalized = []
        for item in state.evidences:
            key = f'{item.source_url}|{item.snippet}'
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)
        state.evidences = normalized
        state.self_eval['normalize'] = SelfEval(coverage=0.9, consistency=0.9, evidence_quality=0.8, uncertainty=0.2)
        self._save_and_event(state, StageName.normalize, 'normalized', {'evidence_count': len(state.evidences)})

    def _analyze(self, state: RunState) -> None:
        self._save_and_event(state, StageName.analyze, 'agent.llm.started', {'agent': 'AnalystAgent', 'trace_name': 'agent.analyze.generate_profiles'})
        try:
            analyzed = self.analyst_agent.run_llm(state)
            self._save_and_event(
                state,
                StageName.analyze,
                'agent.llm.completed',
                {
                    'agent': 'AnalystAgent',
                    'trace_name': 'agent.analyze.generate_profiles',
                    'attempt_count': 1 + self.config.agent_llm_retry_count,
                    'retry_count_used': self.config.agent_llm_retry_count,
                    'fallback_used': False,
                },
            )
        except LLMCallError as exc:
            fail_payload = {
                'agent': 'AnalystAgent',
                'trace_name': 'agent.analyze.generate_profiles',
                'error': str(exc),
                'failure_reason': exc.reason,
                'attempt_count': exc.attempt_count,
                'retry_count_used': exc.retry_count_used,
                'fallback_used': False,
            }
            self._save_and_event(state, StageName.analyze, 'agent.llm.failed', fail_payload)
            allow_fallback = self.config.agent_llm_fallback_enabled and (
                exc.reason != 'validation_error' or self.config.agent_llm_fallback_on_validation_error
            )
            if not allow_fallback:
                state.status = 'failed'
                raise
            self._save_and_event(
                state,
                StageName.analyze,
                'agent.llm.fallback.started',
                {'agent': 'AnalystAgent', 'fallback_reason': exc.reason, 'fallback_used': True},
            )
            try:
                analyzed = self.analyst_agent.run_fallback(state)
            except Exception as fb_exc:
                state.status = 'failed'
                self._save_and_event(
                    state,
                    StageName.analyze,
                    'agent.llm.fallback.failed',
                    {'agent': 'AnalystAgent', 'error': str(fb_exc), 'fallback_reason': exc.reason, 'fallback_used': True},
                )
                raise
            self._save_and_event(
                state,
                StageName.analyze,
                'agent.llm.fallback.completed',
                {'agent': 'AnalystAgent', 'fallback_reason': exc.reason, 'fallback_used': True},
            )
        state.profiles = analyzed.profiles
        state.findings = analyzed.findings
        domain = get_domain_schema(self.store, state.industry)
        coverage = min(1.0, len([f for f in state.findings if f.evidence_refs]) / max(1, len(state.findings)))
        state.self_eval['analyze'] = SelfEval(coverage=coverage, consistency=0.8, evidence_quality=0.7, uncertainty=0.3)
        self._save_and_event(
            state,
            StageName.analyze,
            EventType.analyze_completed.value,
            {'profile_count': len(state.profiles), 'finding_count': len(state.findings), 'domain': domain.industry, 'domain_version': domain.version},
        )

    def _draft(self, state: RunState) -> None:
        self._save_and_event(state, StageName.draft, 'agent.llm.started', {'agent': 'WriterAgent', 'trace_name': 'agent.draft.generate_report'})
        try:
            drafted: DraftOutput = self.writer_agent.run_llm(state)
            self._save_and_event(
                state,
                StageName.draft,
                'agent.llm.completed',
                {
                    'agent': 'WriterAgent',
                    'trace_name': 'agent.draft.generate_report',
                    'attempt_count': 1 + self.config.agent_llm_retry_count,
                    'retry_count_used': self.config.agent_llm_retry_count,
                    'fallback_used': False,
                },
            )
        except LLMCallError as exc:
            fail_payload = {
                'agent': 'WriterAgent',
                'trace_name': 'agent.draft.generate_report',
                'error': str(exc),
                'failure_reason': exc.reason,
                'attempt_count': exc.attempt_count,
                'retry_count_used': exc.retry_count_used,
                'fallback_used': False,
            }
            self._save_and_event(state, StageName.draft, 'agent.llm.failed', fail_payload)
            allow_fallback = self.config.agent_llm_fallback_enabled and (
                exc.reason != 'validation_error' or self.config.agent_llm_fallback_on_validation_error
            )
            if not allow_fallback:
                state.status = 'failed'
                raise
            self._save_and_event(
                state,
                StageName.draft,
                'agent.llm.fallback.started',
                {'agent': 'WriterAgent', 'fallback_reason': exc.reason, 'fallback_used': True},
            )
            try:
                drafted = self.writer_agent.run_fallback(state)
            except Exception as fb_exc:
                state.status = 'failed'
                self._save_and_event(
                    state,
                    StageName.draft,
                    'agent.llm.fallback.failed',
                    {'agent': 'WriterAgent', 'error': str(fb_exc), 'fallback_reason': exc.reason, 'fallback_used': True},
                )
                raise
            self._save_and_event(
                state,
                StageName.draft,
                'agent.llm.fallback.completed',
                {'agent': 'WriterAgent', 'fallback_reason': exc.reason, 'fallback_used': True},
            )
        state.report = drafted.report
        state.self_eval['draft'] = SelfEval(coverage=0.85, consistency=0.88, evidence_quality=0.76, uncertainty=0.2)
        self._save_and_event(state, StageName.draft, EventType.draft_completed.value, {'has_report': state.report is not None})

    def _qa(self, state: RunState) -> QAOutput:
        self._save_and_event(state, StageName.qa, 'agent.llm.started', {'agent': 'QACriticAgent', 'trace_name': 'agent.qa.evaluate_report'})
        try:
            result = self.qa_critic_agent.run_llm(state)
            self._save_and_event(
                state,
                StageName.qa,
                'agent.llm.completed',
                {
                    'agent': 'QACriticAgent',
                    'trace_name': 'agent.qa.evaluate_report',
                    'attempt_count': 1 + self.config.agent_llm_retry_count,
                    'retry_count_used': self.config.agent_llm_retry_count,
                    'fallback_used': False,
                },
            )
        except LLMCallError as exc:
            fail_payload = {
                'agent': 'QACriticAgent',
                'trace_name': 'agent.qa.evaluate_report',
                'error': str(exc),
                'failure_reason': exc.reason,
                'attempt_count': exc.attempt_count,
                'retry_count_used': exc.retry_count_used,
                'fallback_used': False,
            }
            self._save_and_event(state, StageName.qa, 'agent.llm.failed', fail_payload)
            allow_fallback = self.config.agent_llm_fallback_enabled and (
                exc.reason != 'validation_error' or self.config.agent_llm_fallback_on_validation_error
            )
            if not allow_fallback:
                state.status = 'failed'
                raise
            self._save_and_event(
                state,
                StageName.qa,
                'agent.llm.fallback.started',
                {'agent': 'QACriticAgent', 'fallback_reason': exc.reason, 'fallback_used': True},
            )
            try:
                result = self.qa_critic_agent.run_fallback(state)
            except Exception as fb_exc:
                state.status = 'failed'
                self._save_and_event(
                    state,
                    StageName.qa,
                    'agent.llm.fallback.failed',
                    {'agent': 'QACriticAgent', 'error': str(fb_exc), 'fallback_reason': exc.reason, 'fallback_used': True},
                )
                raise
            self._save_and_event(
                state,
                StageName.qa,
                'agent.llm.fallback.completed',
                {'agent': 'QACriticAgent', 'fallback_reason': exc.reason, 'fallback_used': True},
            )
        payload = {'passed': result.passed, 'issue_count': len(result.issues)}
        if not result.passed:
            payload['target_agent'] = result.target_agent
            payload['issues'] = [issue.model_dump() for issue in result.issues]
        self._save_and_event(state, StageName.qa, 'qa_checked', payload)
        return result

    def _apply_rework_ticket(self, state: RunState, result: QAResult) -> None:
        assert result.target_agent is not None
        ticket = ReworkTicket(
            target_agent=result.target_agent,
            issues=result.issues,
            evidence_refs=[ref for finding in state.findings for ref in finding.evidence_refs],
            qa_rules=['core_schema_required', 'evidence_traceability', 'self_eval_threshold'],
            severity=Severity.high if len(result.issues) > 2 else Severity.medium,
            deadline=datetime.now(UTC).isoformat(),
            acceptance_criteria=['All required fields present', 'Every finding has valid evidence_refs', 'Self-eval thresholds met'],
            status=TicketStatus.in_progress,
        )
        state.tickets.append(ticket)
        state.parent_attempt = state.attempt
        state.attempt += 1
        state.ticket_id = ticket.ticket_id
        self._save_and_event(state, StageName.qa, EventType.qa_rework_ticket_created.value, ticket.model_dump())

    def _finalize(self, state: RunState) -> None:
        for ticket in state.tickets:
            if ticket.status == TicketStatus.in_progress:
                ticket.status = TicketStatus.resolved
        self._save_and_event(state, StageName.finalize, 'finalized', {'ticket_count': len(state.tickets)})

    def _summary_for(self, state: RunState) -> RunSummary:
        now = datetime.now(UTC)
        return RunSummary(
            run_id=state.run_id,
            industry=state.industry,
            status=state.status,
            competitor_count=len(state.competitors),
            created_at=now,
            updated_at=now,
        )

    def _save_and_event(self, state: RunState, stage: StageName, event_type: str, payload: dict) -> None:
        envelope = EventEnvelope(
            event_type=event_type,
            stage=stage,
            run_id=state.run_id,
            attempt=state.attempt,
            payload=payload,
        )
        snapshot = make_stage_snapshot(
            run_id=state.run_id,
            stage=stage,
            input_payload={'attempt': state.attempt},
            output_payload=payload,
        )
        self.store.append_stage_event(
            state.run_id,
            stage,
            event_type,
            {'envelope': envelope.model_dump(mode='json'), 'snapshot': snapshot.model_dump(mode='json')},
        )
        self.store.save_state(state)

    @staticmethod
    def run_state_to_graph_state(state: RunState) -> WorkflowGraphState:
        return {
            'run_id': state.run_id,
            'attempt': state.attempt,
            'parent_attempt': state.parent_attempt,
            'status': state.status,
            'current_stage': 'finalize' if state.status == 'completed' else 'running',
            'industry': state.industry,
            'competitors': state.competitors,
            'language': state.language,
            'timeframe': state.timeframe,
            'raw_evidences': [item.model_dump() for item in state.evidences],
            'profiles': [item.model_dump() for item in state.profiles],
            'findings': [item.model_dump() for item in state.findings],
            'report': state.report.model_dump() if state.report else None,
            'tickets': [item.model_dump() for item in state.tickets],
            'core_schema_version': state.core_schema_version,
            'domain_schema_version': state.domain_schema_version,
            'self_eval': {k: v.model_dump() for k, v in state.self_eval.items()},
            'policy_decisions': [],
            'stage_events': [],
            'errors': [],
            'ticket_id': state.ticket_id,
        }

    @staticmethod
    def graph_state_to_run_state(graph: WorkflowGraphState) -> RunState:
        return RunState(
            run_id=graph['run_id'],
            attempt=graph['attempt'],
            parent_attempt=graph['parent_attempt'],
            ticket_id=graph.get('ticket_id'),
            industry=graph['industry'],
            competitors=graph['competitors'],
            language=graph['language'],
            timeframe=graph['timeframe'],
            core_schema_version=graph['core_schema_version'],
            domain_schema_version=graph['domain_schema_version'],
            status=graph['status'],  # type: ignore[arg-type]
        )
