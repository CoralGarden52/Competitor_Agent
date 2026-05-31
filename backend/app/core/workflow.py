from __future__ import annotations

import json
import concurrent.futures
import logging
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
    AnalysisSchemaField,
    AnalyzeHandoff,
    ApprovalPolicy,
    CollectHandoff,
    CompetitorProfile,
    CompetitorEvidenceBundle,
    FieldRiskProfile,
    FieldEvidenceBundle,
    EventEnvelope,
    EventType,
    FeatureNode,
    FeedbackSummary,
    Finding,
    PlanHandoff,
    PolicyAuditRecord,
    PolicyDecision,
    PolicyUpsertRequest,
    PricingModel,
    PricingTier,
    ProposalActivateRequest,
    ProposalReviewRequest,
    ProposalStatus,
    QAOutput,
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
from app.core.schema_registry import CORE_SCHEMA_VERSION, get_domain_schema, registry_snapshot
from app.core.storage import SQLiteStore


logger = logging.getLogger(__name__)


class CompetitorWorkflowService:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.config = get_config()
        self.planner_llm = PlannerLLMClient(self.config, self.store)
        self.agent_llm = AgentLLMClient(self.config, store)
        self.collector = CollectorPipeline(self.config, self.store)
        self.policy_engine = ApprovalPolicyEngine(store)
        self.orchestrator = OrchestratorAgent(max_rework_iterations=self.config.max_rework_iterations, planner=self.planner_llm)
        self.collector_agent = CollectorAgent(self.collector, self.store)
        self.analyst_agent = AnalystAgent(self.agent_llm, self.store)
        self.writer_agent = WriterAgent(self.agent_llm)
        self.qa_critic_agent = QACriticAgent(self.agent_llm, self.store)
        self.runtime = WorkflowLangGraphRuntime(self)
        self._run_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='workflow-run')
        self._background_runs: dict[str, concurrent.futures.Future[None]] = {}

    def start_run(self, request: RunRequest) -> RunResponse:
        state = self._initialize_run_state(request)
        state = self._execute_run(state)
        return RunResponse(summary=self._summary_for(state), state=state)

    def start_run_async(self, request: RunRequest) -> RunResponse:
        state = self._initialize_run_state(request)
        future = self._run_executor.submit(self._execute_run_background, state)
        self._background_runs[state.run_id] = future
        return RunResponse(summary=self._summary_for(state), state=state)

    def _initialize_run_state(self, request: RunRequest) -> RunState:
        state = RunState(
            industry=request.industry.strip().lower(),
            competitors=request.competitors,
            user_prompt=request.user_prompt.strip(),
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
        self._save_and_event(
            state,
            StageName.plan,
            'start',
            {'competitors': request.competitors, 'user_prompt': request.user_prompt.strip()},
        )
        return state

    def _execute_run(self, state: RunState) -> RunState:
        state = self.runtime.execute(state)
        if state.status not in ('completed', 'failed'):
            state.status = 'failed'
            self._save_and_event(state, StageName.qa, 'max_iterations_reached', {'iteration': state.attempt, 'reason': 'runtime_ended_without_terminal_status'})
        self.store.save_state(state)
        self._auto_save_demo_workspace(state.run_id)
        return state

    def _execute_run_background(self, state: RunState) -> None:
        try:
            self._execute_run(state)
        except Exception as exc:
            logger.exception('Background run failed for %s', state.run_id)
            state.status = 'failed'
            self._save_and_event(
                state,
                StageName.finalize,
                'background_failed',
                {'error': str(exc)},
            )
            self.store.save_state(state)
            self._auto_save_demo_workspace(state.run_id)
        finally:
            self._background_runs.pop(state.run_id, None)

    def get_run(self, run_id: str) -> RunResponse | None:
        state = self.store.get_state(run_id)
        if state is None:
            return None
        return RunResponse(summary=self._summary_for(state), state=state)

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        return self.store.list_runs(limit=limit)

    def delete_run(self, run_id: str) -> bool:
        return self.store.delete_run(run_id)

    def list_run_events(self, run_id: str, *, after_id: int = 0, limit: int | None = None) -> list[dict]:
        return self.store.list_events(run_id, after_id=after_id, limit=limit)

    def replay_run(self, run_id: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            return {'run_id': run_id, 'timeline': [], 'status': 'not_found'}
        timeline = self.store.replay_timeline(run_id)
        handoffs = self.store.list_stage_handoffs(run_id)
        llm_calls = self.store.list_llm_calls(run_id)
        return {'run_id': run_id, 'status': run.state.status, 'timeline': timeline, 'handoffs': handoffs, 'llm_calls': llm_calls}

    def replay_node(self, run_id: str, node_name: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            return {'run_id': run_id, 'node_name': node_name, 'io': [], 'status': 'not_found'}
        io = self.store.replay_node_io(run_id, node_name)
        handoffs = self.store.list_stage_handoffs(run_id, stage=node_name)
        llm_calls = self.store.list_llm_calls(run_id, node_name=node_name)
        return {'run_id': run_id, 'node_name': node_name, 'io': io, 'handoffs': handoffs, 'llm_calls': llm_calls}

    def workspace_payload(self, run_id: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            return {'run_id': run_id, 'status': 'not_found'}
        replay = self.replay_run(run_id)
        events = self.list_run_events(run_id)
        manual_interventions = self.store.list_manual_interventions(run_id)
        return self._build_workspace_payload(run=run, replay=replay, events=events, manual_interventions=manual_interventions)

    def export_run_logs(self, run_id: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            return {'run_id': run_id, 'status': 'not_found'}
        replay = self.replay_run(run_id)
        events = self.list_run_events(run_id)
        manual_interventions = self.store.list_manual_interventions(run_id)
        timeline = replay.get('timeline', []) if isinstance(replay.get('timeline', []), list) else []
        handoffs = replay.get('handoffs', []) if isinstance(replay.get('handoffs', []), list) else []
        llm_calls = replay.get('llm_calls', []) if isinstance(replay.get('llm_calls', []), list) else []
        stage_io = {stage: self.store.replay_node_io(run_id, stage) for stage in self._stage_names()}
        stage_logs = self._build_stage_observability(
            state=run.state,
            events=events,
            handoffs=handoffs,
            llm_calls=llm_calls,
            stage_io=stage_io,
        )
        return {
            'run_id': run_id,
            'status': run.state.status,
            'events': events,
            'timeline': timeline,
            'handoffs': handoffs,
            'llm_calls': llm_calls,
            'stage_logs': stage_logs,
            'manual_interventions': manual_interventions,
            'report_markdown': run.state.report.markdown if run.state.report else '',
        }

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
            state.analysis_schema_plan = [AnalysisSchemaField.model_validate(item) for item in patch['analysis_schema_plan']]  # type: ignore[list-item]
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

    def summarize_task(self, *, text: str, language: str = 'zh-CN') -> dict[str, str]:
        cleaned = str(text or '').strip()
        if not cleaned:
            return {'summary_text': ''}

        fallback = self._fallback_task_summary(cleaned)
        if not self.agent_llm.enabled():
            return {'summary_text': fallback}

        try:
            payload = self.agent_llm.invoke_json(
                trace_name='agent.task.summarize',
                system_prompt=(
                    '你是一个竞品分析任务摘要助手。'
                    '请将用户输入的任务压缩成一句简洁明确的中文摘要，突出分析目标和对象。'
                    '输出 JSON：{"summary_text":"..."}。'
                    'summary_text 不超过 40 个汉字。'
                ),
                user_payload={'text': cleaned, 'language': language},
                metadata={'run_id': '', 'attempt': 0, 'node_name': 'summary', 'agent_name': 'TaskSummaryAgent'},
                network_retries=1,
            )
        except Exception:
            return {'summary_text': fallback}

        summary = str(payload.get('summary_text', '')).strip()
        return {'summary_text': summary or fallback}

    @staticmethod
    def _fallback_task_summary(text: str) -> str:
        compact = ' '.join(str(text or '').split())
        if len(compact) <= 40:
            return compact
        return f'{compact[:40]}...'

    def collector_preview(self, *, prompt: str, industry_hint: str = '', competitor_hints: list[str] | None = None) -> dict:
        import concurrent.futures

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

        if not planned_competitors:
            response = {
                'prompt': prompt,
                'industry_hint': industry_hint,
                'inferred_industry': inferred_industry,
                'effective_max_urls': effective_max_urls,
                'max_urls_note': 'server uses COLLECTOR_MAX_URLS from .env',
                'candidate_groups': candidate_groups,
                'candidates': candidate_groups,
                'handoff_targets': {'direct': [], 'substitute': []},
                'plan_phase': {
                    'competitors_generated': planned_competitors,
                    'schema_generated': analysis_schema_plan,
                    'planner_meta': dynamic_plan.get('planner_meta', {}),
                },
                'execution_timeline': execution_timeline,
                'preview': [],
                'errors': ['no_competitors_discovered'],
                'planned_competitors': planned_competitors,
                'analysis_schema_plan': analysis_schema_plan,
                'planner_meta': dynamic_plan.get('planner_meta', {}),
            }
            auto_saved, auto_saved_file, auto_saved_error = self._auto_save_preview_result(response)
            response['auto_saved'] = auto_saved
            response['auto_saved_file'] = auto_saved_file
            if auto_saved_error:
                response['auto_saved_error'] = auto_saved_error
            self._auto_save_demo_preview(response)
            return response

        def _collect_one_competitor(competitor: str) -> tuple[str, dict]:
            """并发采集单个竞品的数据"""
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
            return competitor, {
                'competitor': competitor,
                'evidence_count': len(result.evidences),
                'sample': result.evidences[:3],
                'search_events': search_events,
                'fetch_events': fetch_events,
                'fallback_trace': fallback_trace,
                'field_stats': field_stats,
                'field_summaries': field_summaries,
                'provider_events': result.provider_events,
                'errors': result.errors,
            }

        # 并发执行所有竞品的采集
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(planned_competitors), 4)) as executor:
            futures = {executor.submit(_collect_one_competitor, comp): comp for comp in planned_competitors}
            for future in concurrent.futures.as_completed(futures):
                competitor = futures[future]
                try:
                    competitor, result_data = future.result()
                except Exception as exc:
                    errors.append(f'{competitor}: {exc}')
                    preview.append(
                        {
                            'competitor': competitor,
                            'evidence_count': 0,
                            'sample': [],
                            'search_events': [],
                            'fetch_events': [],
                            'fallback_trace': [],
                            'field_stats': [],
                            'field_summaries': [],
                            'error': str(exc),
                        }
                    )
                    execution_timeline.append(
                        {
                            'seq': seq,
                            'competitor': competitor,
                            'event_type': 'collector.preview.failed',
                            'error': str(exc),
                        }
                    )
                    seq += 1
                    continue
                preview.append({
                    'competitor': result_data['competitor'],
                    'evidence_count': result_data['evidence_count'],
                    'sample': result_data['sample'],
                    'search_events': result_data['search_events'],
                    'fetch_events': result_data['fetch_events'],
                    'fallback_trace': result_data['fallback_trace'],
                    'field_stats': result_data['field_stats'],
                    'field_summaries': result_data['field_summaries'],
                })
                for event in result_data['provider_events']:
                    execution_timeline.append({'seq': seq, 'competitor': competitor, **event})
                    seq += 1
                errors.extend(result_data['errors'])
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
        self._auto_save_demo_preview(response)
        return response

    def _auto_save_preview_result(self, payload: dict) -> tuple[bool, str, str]:
        if not self.config.collector_preview_auto_save_enabled:
            return False, '', ''
        try:
            save_dir = Path(self.config.collector_preview_save_dir)
            if not save_dir.is_absolute():
                project_root = Path(__file__).resolve().parents[3]
                save_dir = project_root / save_dir
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
        self.planner_llm.set_trace_context(run_id=state.run_id, attempt=state.attempt, node_name='plan', agent_name='PlannerLLMClient')
        try:
            dynamic_plan = self.orchestrator.generate_dynamic_plan(
                prompt=state.user_prompt,
                industry=state.industry,
                competitors=state.competitors,
            )
        finally:
            self.planner_llm.clear_trace_context()
        inferred_industry = str(dynamic_plan.get('inferred_industry', '')).strip().lower()
        if inferred_industry:
            state.industry = inferred_industry
        state.planned_competitors = dynamic_plan.get('planned_competitors', state.competitors)
        state.analysis_schema_plan = [
            item if isinstance(item, AnalysisSchemaField) else AnalysisSchemaField.model_validate(item)
            for item in dynamic_plan.get('analysis_schema_plan', [])
        ]
        state.planner_meta = dynamic_plan.get('planner_meta', {})
        candidate_groups = dynamic_plan.get('candidate_groups', {})
        if isinstance(candidate_groups, dict) and candidate_groups:
            state.planner_meta['candidate_groups'] = candidate_groups
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
            {'analysis_schema_plan': [item.model_dump(mode='json') for item in state.analysis_schema_plan]},
        )
        self._save_handoff(state, StageName.plan, self._build_plan_handoff(state))

    def _collect(self, state: RunState) -> None:
        qa_collect_plan = self._consume_qa_collect_plan(state)
        if qa_collect_plan and isinstance(state.planner_meta, dict):
            reanalyze_targets = qa_collect_plan.get('reanalyze_targets', {})
            if isinstance(reanalyze_targets, dict) and reanalyze_targets:
                state.planner_meta['qa_reanalyze_targets'] = reanalyze_targets
        result: CollectOutput = self.collector_agent.run(
            state,
            target_competitors=qa_collect_plan.get('target_competitors') if qa_collect_plan else None,
            field_query_overrides=qa_collect_plan.get('field_query_overrides') if qa_collect_plan else None,
        )
        for pe in result.provider_events:
            self._save_and_event(state, StageName.collect, 'provider_event', pe)
        if qa_collect_plan:
            # Re-collect mode: preserve prior evidence and append new evidence.
            state.evidences = list(state.evidences) + list(result.raw_evidences)
        else:
            state.evidences = list(result.raw_evidences)
        active_competitors = state.planned_competitors or state.competitors
        coverage = min(1.0, len(state.evidences) / max(2, len(active_competitors) * 2))
        quality = 0.35 if result.errors else 0.72
        state.self_eval['collect'] = SelfEval(coverage=coverage, consistency=0.75, evidence_quality=quality, uncertainty=0.35)
        self._save_and_event(
            state,
            StageName.collect,
            EventType.collect_completed.value,
            {
                'evidence_count': len(state.evidences),
                'error_count': len(result.errors),
                'errors': result.errors[:5],
                'qa_plan_used': bool(qa_collect_plan),
            },
        )
        self._save_handoff(
            state,
            StageName.collect,
            self._build_collect_handoff(
                state,
                provider_events=result.provider_events,
                errors=result.errors,
                qa_collect_plan_used=bool(qa_collect_plan),
            ),
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
        raw_targets = state.planner_meta.pop('qa_reanalyze_targets', None) if isinstance(state.planner_meta, dict) else None
        reanalyze_targets: dict[str, set[str]] = {}
        if isinstance(raw_targets, dict):
            for competitor, fields in raw_targets.items():
                if not isinstance(fields, list):
                    continue
                c = str(competitor).strip()
                cleaned = {str(x).strip() for x in fields if str(x).strip()}
                if c and cleaned:
                    reanalyze_targets[c] = cleaned
        incremental_reanalyze = bool(reanalyze_targets)
        target_field_count = sum(len(v) for v in reanalyze_targets.values())
        try:
            analyzed = self.analyst_agent.run_llm(
                state,
                reanalyze_targets=reanalyze_targets or None,
                previous_records=state.competitor_analyses if incremental_reanalyze else None,
            )
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
                    'incremental_reanalyze': incremental_reanalyze,
                    'target_competitor_count': len(reanalyze_targets),
                    'target_field_count': target_field_count,
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
        state.competitor_analyses = analyzed.competitors
        state.profiles = analyzed.profiles
        state.findings = analyzed.findings
        domain = get_domain_schema(self.store, state.industry)
        coverage_stats = self._calc_analyze_coverage(state)
        coverage = float(coverage_stats['coverage'])
        passed_units = int(coverage_stats['passed_units'])
        total_units = int(coverage_stats['total_units'])
        print(f"Analyze coverage: {passed_units}/{total_units} ({coverage:.2%})")
        state.self_eval['analyze'] = SelfEval(coverage=coverage, consistency=0.8, evidence_quality=0.7, uncertainty=0.3)
        self._save_and_event(
            state,
            StageName.analyze,
            EventType.analyze_completed.value,
            {
                'competitor_analysis_count': len(state.competitor_analyses),
                'profile_count': len(state.profiles),
                'finding_count': len(state.findings),
                'domain': domain.industry,
                'domain_version': domain.version,
                'incremental_reanalyze': incremental_reanalyze,
                'target_competitor_count': len(reanalyze_targets),
                'target_field_count': target_field_count,
                'coverage': coverage,
                'coverage_passed_units': passed_units,
                'coverage_total_units': total_units,
            },
        )
        self._save_handoff(state, StageName.analyze, self._build_analyze_handoff(state))

    @staticmethod
    def _is_analysis_unit_passed(summary: object) -> bool:
        text = str(summary or '').strip().lower()
        return text not in {'', 'unknown', 'none', 'null'}

    def _calc_analyze_coverage(self, state: RunState) -> dict[str, float | int]:
        schema_fields = [item.field_name for item in state.analysis_schema_plan if item.field_name]
        competitors = state.planned_competitors or state.competitors
        total_units = len(competitors) * len(schema_fields)
        if total_units <= 0:
            return {'coverage': 0.0, 'passed_units': 0, 'total_units': 0}

        record_map = {record.product_name: record for record in state.competitor_analyses}
        passed_units = 0
        for competitor in competitors:
            record = record_map.get(competitor)
            field_map = {field.field_name: field for field in (record.fields if record else [])}
            for field_name in schema_fields:
                field = field_map.get(field_name)
                if field is None:
                    continue
                if self._is_analysis_unit_passed(field.summary):
                    passed_units += 1
        coverage = passed_units / total_units
        return {'coverage': coverage, 'passed_units': passed_units, 'total_units': total_units}

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
        analyze_handoff = self.store.latest_stage_handoff(run_id=state.run_id, stage=StageName.analyze, attempt=state.attempt)
        handoff_analyses = analyze_handoff.competitor_analyses if isinstance(analyze_handoff, AnalyzeHandoff) else []
        active_analyses = handoff_analyses or state.competitor_analyses
        self._save_and_event(
            state,
            StageName.qa,
            'qa.analysis_review.started',
            {'competitor_count': len(active_analyses), 'handoff_used': bool(handoff_analyses)},
        )
        if not active_analyses:
            result = QAOutput(passed=True, issues=[], target_agent=None, ticket=None, collect_plan=None)
            self._save_and_event(state, StageName.qa, 'qa_checked', {'passed': True, 'issue_count': 0, 'reason': 'no_competitor_analyses'})
            return result

        schema_fields = [item.field_name for item in state.analysis_schema_plan if item.field_name]
        reviews: list[dict] = []
        review_errors: list[dict] = []
        max_workers = min(4, len(active_analyses))

        def _review_one(record) -> dict:
            payload = {
                'competitor': record.product_name,
                'run_id': state.run_id,
                'fields': [field.model_dump(mode='json') for field in record.fields],
            }
            return self.qa_critic_agent.run_competitor_analysis_review_llm(
                analysis_json=payload,
                schema_fields=schema_fields,
                industry_hint=state.industry,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(_review_one, record): record.product_name for record in active_analyses}
            for future in concurrent.futures.as_completed(future_map):
                competitor = future_map[future]
                try:
                    review = future.result()
                    reviews.append({'competitor': competitor, 'review': review})
                    self._save_and_event(state, StageName.qa, 'qa.analysis_review.completed', {'competitor': competitor})
                except Exception as exc:  # noqa: BLE001
                    review_errors.append({'competitor': competitor, 'error': str(exc)})
                    self._save_and_event(state, StageName.qa, 'qa.analysis_review.failed', {'competitor': competitor, 'error': str(exc)})

        collect_items: list[dict] = []
        for review_row in reviews:
            review = review_row.get('review', {})
            if not isinstance(review, dict) or not bool(review.get('needs_recollect', False)):
                continue
            collect_plan = review.get('collect_plan', {})
            if not isinstance(collect_plan, dict):
                continue
            items = collect_plan.get('items', [])
            if not isinstance(items, list):
                continue
            for one in items:
                if isinstance(one, dict):
                    collect_items.append(one)

        normalized_items: list[dict] = []
        for one in collect_items:
            competitor = str(one.get('competitor', '')).strip()
            field_name = str(one.get('field_name', '')).strip()
            reason = str(one.get('reason', '')).strip() or f'evidence_insufficient_for_{field_name}'
            query_list = one.get('query_list', []) if isinstance(one.get('query_list', []), list) else []
            queries = [str(q).strip() for q in query_list if str(q).strip()]
            priority = int(one.get('priority', 1) or 1)
            if not competitor or not field_name or len(queries) < 2:
                continue
            normalized_items.append(
                {
                    'competitor': competitor,
                    'field_name': field_name,
                    'reason': reason,
                    'query_list': queries[:4],
                    'priority': max(1, min(priority, 10)),
                }
            )

        if not normalized_items:
            result = QAOutput(passed=True, issues=[], target_agent=None, ticket=None, collect_plan=None)
            self._save_and_event(state, StageName.qa, 'qa_checked', {'passed': True, 'issue_count': 0, 'review_errors': review_errors})
            return result

        issues = [
            ReworkIssue(
                code=f'insufficient_{item["competitor"]}_{item["field_name"]}',
                message=f'{item["competitor"]}:{item["field_name"]} evidence insufficient',
                stage=StageName.collect,
            )
            for item in normalized_items
        ]
        result = QAOutput.model_validate(
            {
                'passed': False,
                'issues': [x.model_dump(mode='json') for x in issues],
                'target_agent': 'Collect',
                'ticket': None,
                'collect_plan': {'enabled': True, 'items': normalized_items, 'global_notes': 'analysis_stage_parallel_qa'},
            }
        )
        self._save_and_event(
            state,
            StageName.qa,
            'qa_checked',
            {
                'passed': False,
                'issue_count': len(result.issues),
                'target_agent': result.target_agent,
                'collect_item_count': len(normalized_items),
                'review_errors': review_errors,
            },
        )
        return result

    def _apply_rework_ticket(self, state: RunState, result: QAOutput) -> None:
        assert result.target_agent is not None
        qa_collect_plan = {}
        if hasattr(result, 'collect_plan') and getattr(result, 'collect_plan') is not None:
            qa_collect_plan = result.collect_plan.model_dump(mode='json')
        ticket = ReworkTicket(
            target_agent=result.target_agent,
            issues=result.issues,
            evidence_refs=[ref for finding in state.findings for ref in finding.evidence_refs],
            qa_rules=['core_schema_required', 'evidence_traceability', 'self_eval_threshold'],
            severity=Severity.high if len(result.issues) > 2 else Severity.medium,
            deadline=datetime.now(UTC).isoformat(),
            acceptance_criteria=['All required fields present', 'Every finding has valid evidence_refs', 'Self-eval thresholds met'],
            status=TicketStatus.in_progress,
            domain_extensions={'qa_collect_plan': qa_collect_plan} if qa_collect_plan else {},
        )
        state.tickets.append(ticket)
        if qa_collect_plan:
            state.planner_meta['qa_collect_plan'] = qa_collect_plan
        state.parent_attempt = state.attempt
        state.attempt += 1
        state.ticket_id = ticket.ticket_id
        self._save_and_event(state, StageName.qa, EventType.qa_rework_ticket_created.value, ticket.model_dump())

    @staticmethod
    def _consume_qa_collect_plan(state: RunState) -> dict[str, object] | None:
        plan = state.planner_meta.pop('qa_collect_plan', None) if isinstance(state.planner_meta, dict) else None
        if not isinstance(plan, dict):
            return None
        if not bool(plan.get('enabled', False)):
            return None
        items = plan.get('items', [])
        if not isinstance(items, list) or not items:
            return None
        target_competitors: list[str] = []
        field_query_overrides: dict[str, list[str]] = {}
        reanalyze_targets: dict[str, list[str]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            competitor = str(item.get('competitor', '')).strip()
            field_name = str(item.get('field_name', '')).strip()
            query_list = item.get('query_list', [])
            if not competitor or not field_name or not isinstance(query_list, list):
                continue
            if competitor not in target_competitors:
                target_competitors.append(competitor)
            reanalyze_targets.setdefault(competitor, [])
            if field_name not in reanalyze_targets[competitor]:
                reanalyze_targets[competitor].append(field_name)
            sanitized_queries = [str(x).strip() for x in query_list if str(x).strip()]
            if sanitized_queries:
                field_query_overrides[f'{competitor}::{field_name}'] = sanitized_queries[:4]
        if not target_competitors:
            return None
        return {
            'target_competitors': target_competitors,
            'field_query_overrides': field_query_overrides,
            'reanalyze_targets': reanalyze_targets,
        }

    def _save_handoff(
        self,
        state: RunState,
        stage: StageName,
        handoff: PlanHandoff | CollectHandoff | AnalyzeHandoff,
    ) -> None:
        self.store.save_stage_handoff(
            run_id=state.run_id,
            stage=stage,
            attempt=state.attempt,
            handoff=handoff,
        )
        self._save_and_event(
            state,
            stage,
            f'{stage.value}.handoff.saved',
            {
                'handoff_type': handoff.__class__.__name__,
                'attempt': state.attempt,
            },
        )

    @staticmethod
    def _build_plan_handoff(state: RunState) -> PlanHandoff:
        return PlanHandoff(
            run_id=state.run_id,
            attempt=state.attempt,
            inferred_industry=state.industry,
            planned_competitors=state.planned_competitors or state.competitors,
            candidate_groups=state.planner_meta.get('candidate_groups', {}) if isinstance(state.planner_meta, dict) else {},
            analysis_schema_plan=state.analysis_schema_plan,
            split_strategy=state.split_strategy,
            planner_meta=state.planner_meta,
        )

    def _build_collect_handoff(
        self,
        state: RunState,
        *,
        provider_events: list[dict],
        errors: list[str],
        qa_collect_plan_used: bool,
    ) -> CollectHandoff:
        schema_fields = [item.field_name for item in state.analysis_schema_plan]
        competitors = state.planned_competitors or state.competitors
        bundles: list[CompetitorEvidenceBundle] = []
        for competitor in competitors:
            fields: list[FieldEvidenceBundle] = []
            for field_name in schema_fields:
                matches = [
                    ev
                    for ev in state.evidences
                    if self.analyst_agent._evidence_matches_competitor(ev, competitor)
                    and self.analyst_agent._evidence_matches_field(ev, field_name)
                ]
                fields.append(FieldEvidenceBundle(field_name=field_name, evidences=matches))
            bundles.append(CompetitorEvidenceBundle(product_name=competitor, fields=fields))
        return CollectHandoff(
            run_id=state.run_id,
            attempt=state.attempt,
            competitors=competitors,
            schema_fields=schema_fields,
            evidence_bundles=bundles,
            provider_events=provider_events,
            errors=errors,
            total_evidence_count=len(state.evidences),
            qa_collect_plan_used=qa_collect_plan_used,
        )

    @staticmethod
    def _build_analyze_handoff(state: RunState) -> AnalyzeHandoff:
        coverage_summary: list[dict[str, object]] = []
        gap_summary: list[dict[str, object]] = []
        for record in state.competitor_analyses:
            for field in record.fields:
                coverage_summary.append(
                    {
                        'competitor': record.product_name,
                        'field_name': field.field_name,
                        'evidence_count': len(field.evidence_refs),
                        'confidence': field.confidence,
                    }
                )
                if field.evidence_gaps:
                    gap_summary.append(
                        {
                            'competitor': record.product_name,
                            'field_name': field.field_name,
                            'gaps': field.evidence_gaps,
                        }
                    )
        return AnalyzeHandoff(
            run_id=state.run_id,
            attempt=state.attempt,
            competitors=state.planned_competitors or state.competitors,
            competitor_analyses=state.competitor_analyses,
            profiles=state.profiles,
            findings=state.findings,
            coverage_summary=coverage_summary,
            evidence_gap_summary=gap_summary,
        )

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
        if self._should_print_event(stage=stage, event_type=event_type):
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] EVENT: {stage.value} -> {event_type} "
                f"(attempt={state.attempt}, status={state.status}, evidences={len(state.evidences)}, findings={len(state.findings)})"
            )
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
    def _should_print_event(*, stage: StageName, event_type: str) -> bool:
        # `provider_event` is extremely high-frequency during collect and can flood terminal output.
        # Keep persisting it to storage/events, but skip console print for readability.
        if stage == StageName.collect and event_type == 'provider_event':
            return False
        return True

    def _build_workspace_payload(
        self,
        *,
        run: RunResponse,
        replay: dict[str, object],
        events: list[dict],
        manual_interventions: list[dict],
    ) -> dict[str, object]:
        state = run.state
        timeline = replay.get('timeline', []) if isinstance(replay.get('timeline', []), list) else []
        handoffs = replay.get('handoffs', []) if isinstance(replay.get('handoffs', []), list) else []
        llm_calls = replay.get('llm_calls', []) if isinstance(replay.get('llm_calls', []), list) else []
        stage_io = {stage: self.store.replay_node_io(state.run_id, stage) for stage in self._stage_names()}
        agent_workflows = self._build_agent_workflows(
            state=state,
            handoffs=handoffs,
            llm_calls=llm_calls,
            events=events,
        )
        stage_logs = self._build_stage_observability(
            state=state,
            events=events,
            handoffs=handoffs,
            llm_calls=llm_calls,
            stage_io=stage_io,
        )
        qa_ticket = state.tickets[0] if state.tickets else None
        qa_collect_items = []
        if qa_ticket is not None and isinstance(qa_ticket.domain_extensions, dict):
            qa_collect_items = qa_ticket.domain_extensions.get('collect_plan', {}).get('items', [])

        return {
            'summary': run.summary.model_dump(mode='json'),
            'request': {
                'industry': state.industry,
                'user_prompt': state.user_prompt,
                'competitors': state.competitors,
                'language': state.language,
                'timeframe': state.timeframe,
            },
            'run': {
                'run_id': state.run_id,
                'status': state.status,
                'industry': state.industry,
                'planned_competitors': state.planned_competitors,
                'schema_fields': [item.field_name for item in state.analysis_schema_plan],
                'evidence_count': len(state.evidences),
                'finding_count': len(state.findings),
                'competitor_count': len(state.competitors),
            },
            'workflow': {
                'dag': self._build_dag(timeline),
                'timeline': timeline,
                'agent_stages': self._build_agent_stage_cards(state=state, timeline=timeline, handoffs=handoffs),
                'agent_workflows': agent_workflows,
                'agent_handoffs': self._build_agent_handoffs(state=state, handoffs=handoffs, stage_io=stage_io),
                'handoffs': [
                    {
                        'stage': item.get('stage', ''),
                        'attempt': item.get('attempt', 0),
                        'handoff_type': item.get('handoff_type', ''),
                        'created_at': item.get('created_at', ''),
                        'summary': self._summarize_handoff_payload(item.get('payload', {})),
                        'highlights': self._handoff_highlights(item.get('payload', {})),
                        'payload': item.get('payload', {}),
                    }
                    for item in handoffs
                ],
            },
            'qa': {
                'passed': qa_ticket is None,
                'target_agent': qa_ticket.target_agent if qa_ticket else None,
                'issue_count': len(qa_ticket.issues) if qa_ticket else 0,
                'issues': [issue.model_dump(mode='json') for issue in qa_ticket.issues] if qa_ticket else [],
                'collect_items': qa_collect_items,
            },
            'report': {
                'markdown': state.report.markdown if state.report else '',
                'sources': state.report.appendix_sources if state.report else [],
            },
            'artifacts': self._build_workspace_artifacts(state),
            'observability': {
                'llm_calls': llm_calls,
                'stage_logs': stage_logs,
                'agent_traces': self._build_agent_traces(
                    state=state,
                    stage_io=stage_io,
                    handoffs=handoffs,
                    llm_calls=llm_calls,
                    events=events,
                ),
                'events': events,
                'manual_interventions': manual_interventions,
                'log_download_path': f'/runs/{state.run_id}/logs/export',
            },
        }

    @staticmethod
    def _stage_names() -> list[str]:
        return ['plan', 'collect', 'normalize', 'analyze', 'draft', 'qa', 'finalize']

    @staticmethod
    def _build_dag(timeline: list[dict]) -> dict[str, list]:
        known_order = ['plan', 'collect', 'normalize', 'analyze', 'draft', 'qa', 'finalize']
        sequence: list[str] = []
        for item in timeline:
            stage = str(item.get('node_name', '')).strip()
            if stage:
                sequence.append(stage)
        nodes = [stage for stage in known_order if stage in sequence]
        for stage in sequence:
            if stage not in nodes:
                nodes.append(stage)
        edges: list[dict[str, str]] = []
        for index in range(len(sequence) - 1):
            source = sequence[index]
            target = sequence[index + 1]
            if not source or not target or source == target:
                continue
            edge = {'from': source, 'to': target}
            if edge not in edges:
                edges.append(edge)
        return {'nodes': nodes, 'edges': edges}

    def _build_agent_stage_cards(self, *, state: RunState, timeline: list[dict], handoffs: list[dict]) -> list[dict[str, object]]:
        timeline_map = {str(item.get('node_name', '')): item for item in timeline}
        handoff_map = {str(item.get('stage', '')): item for item in handoffs}
        stage_meta = [
            ('plan', 'Planner Agent'),
            ('collect', 'Collector Agent'),
            ('analyze', 'Analyst Agent'),
            ('qa', 'QA Agent'),
            ('draft', 'Report Agent'),
            ('finalize', 'Finalize'),
        ]
        cards: list[dict[str, object]] = []
        for stage, label in stage_meta:
            timeline_row = timeline_map.get(stage, {})
            handoff_row = handoff_map.get(stage, {})
            cards.append(
                {
                    'stage': stage,
                    'agent': label,
                    'status': timeline_row.get('status', 'pending'),
                    'duration_ms': timeline_row.get('duration_ms'),
                    'summary': self._summarize_stage(stage, state),
                    'handoff_type': handoff_row.get('handoff_type', ''),
                    'handoff_summary': self._summarize_handoff_payload(handoff_row.get('payload', {})),
                }
            )
        return cards

    def _build_agent_workflows(
        self,
        *,
        state: RunState,
        handoffs: list[dict],
        llm_calls: list[dict],
        events: list[dict],
    ) -> dict[str, dict[str, object]]:
        handoff_map = {str(item.get('stage', '')): item for item in handoffs}
        event_map: dict[str, list[dict]] = {}
        for item in events:
            stage = str(item.get('stage', '')).strip()
            if not stage:
                continue
            event_map.setdefault(stage, []).append(item)
        llm_map: dict[str, list[dict]] = {}
        for item in llm_calls:
            stage = str(item.get('node_name', '')).strip()
            if not stage:
                continue
            llm_map.setdefault(stage, []).append(item)

        workflows: dict[str, dict[str, object]] = {}
        for stage in self._stage_names():
            nodes: list[str] = ['input']
            if stage == 'plan':
                planner_meta = state.planner_meta if isinstance(state.planner_meta, dict) else {}
                llm_by_step = planner_meta.get('llm_call_status_by_step', {})
                if isinstance(llm_by_step, dict):
                    for step in llm_by_step.keys():
                        nodes.append(step)
                nodes.extend(['candidate_groups', 'schema_plan', 'handoff', 'output'])
            elif stage == 'collect':
                provider_events = []
                payload = handoff_map.get(stage, {}).get('payload', {})
                if isinstance(payload, dict):
                    provider_events = payload.get('provider_events', [])
                collect_nodes = ['receive_plan_handoff', 'dispatch_parallel_collect']
                if isinstance(provider_events, list) and provider_events:
                    event_types = {str(item.get('event_type', '')).strip() for item in provider_events if isinstance(item, dict)}
                    if any('search' in item for item in event_types):
                        collect_nodes.append('search_sources')
                    if any('fetch' in item for item in event_types):
                        collect_nodes.append('fetch_pages')
                    if any('fallback' in item or 'rerank' in item for item in event_types):
                        collect_nodes.append('fallback_and_rerank')
                collect_nodes.extend(['merge_evidence', 'collect_handoff', 'output'])
                nodes.extend(collect_nodes)
            elif stage == 'normalize':
                nodes.extend(['normalize_evidence', 'output'])
            elif stage == 'analyze':
                nodes.extend(['receive_collect_handoff'])
                trace_steps = [str(item.get('trace_name', '')).strip().replace('agent.analyze.', '') for item in llm_map.get(stage, []) if str(item.get('trace_name', '')).strip()]
                nodes.extend(trace_steps or ['field_analysis', 'synthesize_findings'])
                nodes.extend(['analyze_handoff', 'output'])
            elif stage == 'draft':
                nodes.extend(['receive_analyze_handoff'])
                trace_steps = [str(item.get('trace_name', '')).strip().replace('agent.draft.', '') for item in llm_map.get(stage, []) if str(item.get('trace_name', '')).strip()]
                nodes.extend(trace_steps or ['generate_report'])
                nodes.extend(['report_output', 'output'])
            elif stage == 'qa':
                nodes.extend(['receive_analysis_handoff'])
                trace_steps = [str(item.get('trace_name', '')).strip().replace('agent.qa.', '') for item in llm_map.get(stage, []) if str(item.get('trace_name', '')).strip()]
                nodes.extend(trace_steps or ['analysis_review'])
                nodes.extend(['route_decision', 'output'])
            elif stage == 'finalize':
                nodes.extend(['resolve_tickets', 'persist_state', 'output'])
            deduped_nodes: list[str] = []
            for item in nodes:
                cleaned = str(item).strip()
                if cleaned and cleaned not in deduped_nodes:
                    deduped_nodes.append(cleaned)
            edges = [{'from': deduped_nodes[index], 'to': deduped_nodes[index + 1]} for index in range(len(deduped_nodes) - 1)]
            workflows[stage] = {'nodes': deduped_nodes, 'edges': edges}
        return workflows

    def _build_stage_observability(
        self,
        *,
        state: RunState,
        events: list[dict],
        handoffs: list[dict],
        llm_calls: list[dict],
        stage_io: dict[str, list[dict]],
    ) -> dict[str, dict[str, object]]:
        output: dict[str, dict[str, object]] = {}
        for stage in self._stage_names():
            output[stage] = {
                'stage': stage,
                'io': stage_io.get(stage, []),
                'inputs': [item for item in stage_io.get(stage, []) if str(item.get('io_type', '')) == 'input'],
                'outputs': [item for item in stage_io.get(stage, []) if str(item.get('io_type', '')) == 'output'],
                'events': [item for item in events if str(item.get('stage', '')) == stage],
                'handoffs': [item for item in handoffs if str(item.get('stage', '')) == stage],
                'llm_calls': [item for item in llm_calls if str(item.get('node_name', '')) == stage],
            }
        return output

    def _build_workspace_artifacts(self, state: RunState) -> dict[str, object]:
        return {
            'analysis_schema_plan': [item.model_dump(mode='json') for item in state.analysis_schema_plan],
            'evidences': [item.model_dump(mode='json') for item in state.evidences],
            'competitor_analyses': [item.model_dump(mode='json') for item in state.competitor_analyses],
            'profiles': [item.model_dump(mode='json') for item in state.profiles],
            'findings': [item.model_dump(mode='json') for item in state.findings],
            'tickets': [item.model_dump(mode='json') for item in state.tickets],
            'report': state.report.model_dump(mode='json') if state.report else None,
        }

    def _build_agent_handoffs(
        self,
        *,
        state: RunState,
        handoffs: list[dict],
        stage_io: dict[str, list[dict]],
    ) -> list[dict[str, object]]:
        handoffs_by_stage: dict[str, list[dict]] = {}
        for item in handoffs:
            stage = str(item.get('stage', '')).strip()
            if not stage:
                continue
            handoffs_by_stage.setdefault(stage, []).append(item)

        output: list[dict[str, object]] = []
        for stage in self._stage_names():
            stage_inputs = [item for item in stage_io.get(stage, []) if str(item.get('io_type', '')) == 'input']
            stage_outputs = [item for item in stage_io.get(stage, []) if str(item.get('io_type', '')) == 'output']
            latest_handoff = handoffs_by_stage.get(stage, [])[-1] if handoffs_by_stage.get(stage) else None
            output.append(
                {
                    'stage': stage,
                    'agent_name': self._stage_agent_name(stage),
                    'status': self._stage_status_from_io(stage_inputs, stage_outputs, state.status),
                    'input_schema': {
                        'schema_name': self._input_schema_name(stage),
                        'payload': stage_inputs[-1].get('payload', {}) if stage_inputs else {},
                        'created_at': stage_inputs[-1].get('created_at', '') if stage_inputs else '',
                    },
                    'output_schema': {
                        'schema_name': str(latest_handoff.get('handoff_type', '')) if latest_handoff else self._output_schema_name(stage),
                        'payload': latest_handoff.get('payload', {}) if latest_handoff else (stage_outputs[-1].get('payload', {}) if stage_outputs else {}),
                        'created_at': latest_handoff.get('created_at', '') if latest_handoff else (stage_outputs[-1].get('created_at', '') if stage_outputs else ''),
                    },
                    'handoff_summary': self._summarize_handoff_payload(latest_handoff.get('payload', {}) if latest_handoff else {}),
                    'handoff_highlights': self._handoff_highlights(latest_handoff.get('payload', {}) if latest_handoff else {}),
                }
            )
        return output

    def _build_agent_traces(
        self,
        *,
        state: RunState,
        stage_io: dict[str, list[dict]],
        handoffs: list[dict],
        llm_calls: list[dict],
        events: list[dict],
    ) -> list[dict[str, object]]:
        handoffs_by_stage: dict[str, list[dict]] = {}
        llm_by_stage: dict[str, list[dict]] = {}
        events_by_stage: dict[str, list[dict]] = {}
        for item in handoffs:
            stage = str(item.get('stage', '')).strip()
            if stage:
                handoffs_by_stage.setdefault(stage, []).append(item)
        for item in llm_calls:
            stage = str(item.get('node_name', '')).strip()
            if stage:
                llm_by_stage.setdefault(stage, []).append(item)
        for item in events:
            stage = str(item.get('stage', '')).strip()
            if stage:
                events_by_stage.setdefault(stage, []).append(item)

        traces: list[dict[str, object]] = []
        for stage in self._stage_names():
            stage_inputs = [item for item in stage_io.get(stage, []) if str(item.get('io_type', '')) == 'input']
            stage_outputs = [item for item in stage_io.get(stage, []) if str(item.get('io_type', '')) == 'output']
            stage_handoffs = handoffs_by_stage.get(stage, [])
            stage_llm_calls = llm_by_stage.get(stage, [])
            stage_events = events_by_stage.get(stage, [])

            steps: list[dict[str, object]] = []
            for item in stage_inputs:
                steps.append(
                    {
                        'step_type': 'input',
                        'display_name': f'{self._input_schema_name(stage)} Input',
                        'created_at': item.get('created_at', ''),
                        'payload': item.get('payload', {}),
                    }
                )
            for item in stage_events:
                payload = item.get('payload', {})
                steps.append(
                    {
                        'step_type': 'event',
                        'display_name': self._humanize_step_label(str(item.get('event_type', 'event'))),
                        'created_at': item.get('created_at', ''),
                        'event_type': item.get('event_type', ''),
                        'payload': payload,
                        'payload_preview': self._truncate_json_preview(payload),
                    }
                )
            for index, item in enumerate(stage_llm_calls, start=1):
                parsed_response = item.get('parsed_response', {})
                user_payload = item.get('user_payload', {})
                steps.append(
                    {
                        'step_type': 'llm_call',
                        'step_order': index,
                        'display_name': self._humanize_step_label(str(item.get('trace_name', 'llm_call'))),
                        'trace_name': item.get('trace_name', ''),
                        'created_at': item.get('created_at', ''),
                        'status': item.get('status', ''),
                        'model': item.get('model', ''),
                        'system_prompt': item.get('system_prompt', ''),
                        'user_payload': user_payload,
                        'raw_response': item.get('raw_response', {}),
                        'parsed_response': parsed_response,
                        'input_preview': self._truncate_json_preview(user_payload),
                        'output_preview': self._truncate_json_preview(parsed_response),
                        'latency_ms': item.get('latency_ms', 0),
                        'prompt_tokens': item.get('prompt_tokens', 0),
                        'completion_tokens': item.get('completion_tokens', 0),
                        'total_tokens': item.get('total_tokens', 0),
                        'finish_reason': item.get('finish_reason', ''),
                        'error_reason': item.get('error_reason', ''),
                        'error_message': item.get('error_message', ''),
                    }
                )
            for item in stage_handoffs:
                payload = item.get('payload', {})
                steps.append(
                    {
                        'step_type': 'handoff',
                        'display_name': f'{item.get("handoff_type", self._output_schema_name(stage))} Handoff',
                        'created_at': item.get('created_at', ''),
                        'schema_name': item.get('handoff_type', ''),
                        'payload': payload,
                        'payload_preview': self._truncate_json_preview(payload),
                        'summary': self._summarize_handoff_payload(payload),
                    }
                )
            for item in stage_outputs:
                steps.append(
                    {
                        'step_type': 'output',
                        'display_name': f'{self._output_schema_name(stage)} Output',
                        'created_at': item.get('created_at', ''),
                        'payload': item.get('payload', {}),
                    }
                )

            steps.sort(key=lambda item: str(item.get('created_at', '')))
            traces.append(
                {
                    'stage': stage,
                    'agent_name': self._stage_agent_name(stage),
                    'status': self._stage_status_from_io(stage_inputs, stage_outputs, state.status),
                    'summary': {
                        'llm_call_count': len(stage_llm_calls),
                        'total_tokens': sum(int(item.get('total_tokens', 0) or 0) for item in stage_llm_calls),
                        'prompt_tokens': sum(int(item.get('prompt_tokens', 0) or 0) for item in stage_llm_calls),
                        'completion_tokens': sum(int(item.get('completion_tokens', 0) or 0) for item in stage_llm_calls),
                        'event_count': len(stage_events),
                        'handoff_count': len(stage_handoffs),
                        'input_count': len(stage_inputs),
                        'output_count': len(stage_outputs),
                    },
                    'steps': steps,
                }
            )
        return traces

    @staticmethod
    def _truncate_json_preview(payload: object, limit: int = 240) -> str:
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            text = str(payload)
        return text if len(text) <= limit else f'{text[:limit]}...'

    @staticmethod
    def _humanize_step_label(value: str) -> str:
        cleaned = (
            value.replace('agent.', '')
            .replace('planner.', '')
            .replace('collector.', '')
            .replace('_', ' ')
            .replace('.', ' / ')
            .strip()
        )
        if not cleaned:
            return value
        return ' '.join(part.capitalize() if part else '' for part in cleaned.split(' '))

    @staticmethod
    def _stage_agent_name(stage: str) -> str:
        mapping = {
            'plan': 'Planner Agent',
            'collect': 'Collector Agent',
            'normalize': 'Normalizer',
            'analyze': 'Analyst Agent',
            'draft': 'Writer Agent',
            'qa': 'QA Agent',
            'finalize': 'Finalize',
        }
        return mapping.get(stage, stage)

    @staticmethod
    def _input_schema_name(stage: str) -> str:
        mapping = {
            'plan': 'RunRequest',
            'collect': 'PlanHandoff',
            'normalize': 'CollectOutput',
            'analyze': 'CollectHandoff',
            'draft': 'AnalyzeHandoff',
            'qa': 'AnalyzeHandoff',
            'finalize': 'RunState',
        }
        return mapping.get(stage, 'UnknownInput')

    @staticmethod
    def _output_schema_name(stage: str) -> str:
        mapping = {
            'plan': 'PlanHandoff',
            'collect': 'CollectHandoff',
            'normalize': 'CollectOutput',
            'analyze': 'AnalyzeHandoff',
            'draft': 'Report',
            'qa': 'QAOutput',
            'finalize': 'RunState',
        }
        return mapping.get(stage, 'UnknownOutput')

    @staticmethod
    def _stage_status_from_io(stage_inputs: list[dict], stage_outputs: list[dict], run_status: str) -> str:
        if stage_outputs:
            return 'completed'
        if stage_inputs:
            return 'running' if run_status == 'running' else run_status
        return 'pending'

    @staticmethod
    def _summarize_stage(stage: str, state: RunState) -> str:
        if stage == 'plan':
            return f'发现 {len(state.planned_competitors or state.competitors)} 个竞品，并规划 {len(state.analysis_schema_plan)} 个分析字段。'
        if stage == 'collect':
            return f'累计采集 {len(state.evidences)} 条证据，并完成字段级归因。'
        if stage == 'analyze':
            return f'生成 {len(state.findings)} 条结构化结论，产出 {len(state.profiles)} 份竞品画像。'
        if stage == 'qa':
            return '执行完整性、引用与 unknown 检查，并在必要时生成回采计划。'
        if stage == 'draft':
            return '汇总分析结果，生成可编辑 Markdown 报告。'
        if stage == 'finalize':
            return f'运行状态：{state.status}。'
        return ''

    @staticmethod
    def _summarize_handoff_payload(payload: object) -> str:
        if not isinstance(payload, dict) or not payload:
            return '暂无交接摘要。'
        if 'planned_competitors' in payload:
            competitors = payload.get('planned_competitors', [])
            return f'向下游交接 {len(competitors) if isinstance(competitors, list) else 0} 个竞品候选与 schema。'
        if 'total_evidence_count' in payload:
            return f'向分析阶段交接证据集合，总证据数 {payload.get("total_evidence_count", 0)}。'
        if 'findings' in payload:
            findings = payload.get('findings', [])
            return f'向报告与 QA 交接分析结果，findings {len(findings) if isinstance(findings, list) else 0} 条。'
        return f'交接字段：{", ".join(list(payload.keys())[:4])}'

    @staticmethod
    def _handoff_highlights(payload: object) -> list[str]:
        if not isinstance(payload, dict) or not payload:
            return []
        highlights: list[str] = []
        for key, value in list(payload.items())[:4]:
            if isinstance(value, list):
                highlights.append(f'{key}: {len(value)} items')
            else:
                highlights.append(f'{key}: {str(value)[:60]}')
        return highlights

    def _auto_save_demo_preview(self, payload: dict[str, object]) -> None:
        if not self.config.demo_workspace_auto_save_enabled:
            return
        try:
            save_dir = Path(self.config.demo_workspace_save_dir)
            if not save_dir.is_absolute():
                save_dir = Path(__file__).resolve().parents[3] / save_dir
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / 'latest_preview.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        except Exception:
            return

    def _auto_save_demo_workspace(self, run_id: str) -> None:
        if not self.config.demo_workspace_auto_save_enabled:
            return
        try:
            payload = self.workspace_payload(run_id)
            if payload.get('status') == 'not_found':
                return
            save_dir = Path(self.config.demo_workspace_save_dir)
            if not save_dir.is_absolute():
                save_dir = Path(__file__).resolve().parents[3] / save_dir
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / 'latest_workspace.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
            report = payload.get('report', {})
            report_markdown = str(report.get('markdown', '')) if isinstance(report, dict) else ''
            if report_markdown:
                (save_dir / 'latest_report.md').write_text(report_markdown, encoding='utf-8')
            logs_payload = self.export_run_logs(run_id)
            (save_dir / 'latest_logs.json').write_text(json.dumps(logs_payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        except Exception:
            return

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
