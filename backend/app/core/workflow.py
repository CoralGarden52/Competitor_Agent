from __future__ import annotations

import json
import concurrent.futures
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agents import AnalystAgent, CollectorAgent, ManagerAgent, OrchestratorAgent, QACriticAgent, QuestionnaireAgent, WriterAgent
from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.approval_policy_engine import ApprovalPolicyEngine, PolicyContext
from app.core.collector import CollectorPipeline
from app.core.collector.deep_dive import CollectorDeepDiveCoordinator
from app.core.chat_stream import ChatStreamBroker
from app.core.config import get_config
from app.core.langgraph_runtime import WorkflowLangGraphRuntime
from app.core.planner_llm import PlannerLLMClient
from app.core.graph_state import WorkflowGraphState, init_graph_state_from_run_request, make_stage_snapshot
from app.core.hooks import AuditHook, HookContext, HookRegistry
from app.core.models import (
    ActionExecutionResult,
    ActionTarget,
    ActionType,
    AnalysisSchemaField,
    AnalyzeHandoff,
    ApprovalPolicy,
    ChatTurnRequest,
    ChatTurnResponse,
    ChatTurnResult,
    CollectHandoff,
    CompetitorProfile,
    CompetitorEvidenceBundle,
    DecisionContextSnapshot,
    DecisionHandoff,
    DraftHandoff,
    FieldRiskProfile,
    ManagerDecision,
    FieldEvidenceBundle,
    HandoffEnvelope,
    HandoffType,
    EventEnvelope,
    EventRecord,
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
    QuestionnaireDesign,
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
    TaskEnvelope,
    TaskResult,
    TaskStatus,
    TaskType,
    TicketStatus,
    TransitionReason,
    RecoveryState,
    RawEvidence,
)
from app.core.report_conversation import ReportConversationService
from app.core.schema_registry import CORE_SCHEMA_VERSION, get_domain_schema, registry_snapshot
from app.core.storage import SQLiteStore
from app.core.todo import TodoStateManager
from app.core.wjx_export import WjxExportUnavailableError, export_questionnaire_with_wjx_cli
from harness.subagents import SubagentExecutor
from harness.subagents.tracing import subagent_trace
from harness.tools.bootstrap import build_tool_runtime, register_internal_llm_tool, register_workflow_tools


logger = logging.getLogger(__name__)


class CompetitorWorkflowService:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.config = get_config()
        self.tools = build_tool_runtime(self.config, event_sink=self._tool_event_sink, store=self.store)
        self.tool_registry = self.tools.registry
        self.tool_router = self.tools.router
        self.hook_registry = HookRegistry()
        self.tool_router.hook_emitter = self._emit_hook
        self.planner_llm = PlannerLLMClient(self.config, self.store)
        self.agent_llm = AgentLLMClient(self.config, store, tool_router=self.tool_router)
        self.agent_llm.hook_registry = self.hook_registry
        self.collector = CollectorPipeline(self.config, self.store, tool_router=self.tool_router, provider_registry=self.tools.provider_registry)
        register_internal_llm_tool(self.tools, self.agent_llm.invoke_json)
        self.planner_llm.tool_router = self.tool_router
        self.subagent_executor = SubagentExecutor(llm=self.agent_llm, tool_router=self.tool_router, store=self.store)
        self.deep_dive = CollectorDeepDiveCoordinator(executor=self.subagent_executor, config=self.config)
        self.policy_engine = ApprovalPolicyEngine(store)
        self.orchestrator = OrchestratorAgent(max_rework_iterations=self.config.max_rework_iterations, planner=self.planner_llm)
        self.manager_agent = ManagerAgent(self.agent_llm, self.tool_router)
        self.collector_agent = CollectorAgent(self.collector, self.store, deep_dive=self.deep_dive)
        self.analyst_agent = AnalystAgent(self.agent_llm, self.store)
        self.writer_agent = WriterAgent(self.agent_llm)
        self.questionnaire_agent = QuestionnaireAgent(self.agent_llm)
        self.qa_critic_agent = QACriticAgent(self.agent_llm, self.store)
        register_workflow_tools(self.tools, lambda: self)
        self.runtime = WorkflowLangGraphRuntime(self)
        for hook_point in ('before_llm', 'before_tool', 'after_tool', 'after_stage', 'on_error'):
            self.hook_registry.register(hook_point, AuditHook(lambda _event_type, payload: self._save_hook_event(payload)))
        self._run_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='workflow-run')
        self._background_runs: dict[str, concurrent.futures.Future[None]] = {}
        self.chat_stream_broker = ChatStreamBroker()
        self.report_conversation = ReportConversationService(self)

    def _save_hook_event(self, payload: dict[str, object]) -> None:
        run_id = str(payload.get('run_id', '') or '')
        stage_name = str(payload.get('stage', '') or '')
        if not run_id:
            return
        try:
            stage = StageName(stage_name) if stage_name else StageName.plan
        except Exception:
            stage = StageName.plan
        try:
            self.store.append_event(EventRecord(run_id=run_id, stage=stage, event_type='hook_event', payload=payload))
        except Exception:
            return

    def _emit_hook(self, hook_point: str, context: dict[str, object]) -> None:
        metadata = context.get('metadata', {})
        run_id = str(context.get('run_id', '') or '')
        if not run_id and isinstance(metadata, dict):
            run_id = str(metadata.get('run_id', '') or '')
        hook_context = HookContext(
            hook_point=hook_point,
            run_id=run_id,
            attempt=int(context.get('attempt', 0) or 0),
            stage=str(context.get('stage', '') or ''),
            agent_name=str(context.get('agent_name', '') or ''),
            trace_name=str(context.get('trace_name', '') or ''),
            payload=context.get('payload', {}) if isinstance(context.get('payload', {}), dict) else {},
            error=context.get('error', None) if isinstance(context.get('error', None), dict) else None,
        )
        self.hook_registry.emit(hook_point, hook_context)

    def _tool_event_sink(self, payload: dict[str, object]) -> None:
        run_id = str(payload.get('metadata', {}).get('run_id', '') if isinstance(payload.get('metadata', {}), dict) else '')
        if not run_id:
            return
        node_name = str(payload.get('metadata', {}).get('node_name', '') if isinstance(payload.get('metadata', {}), dict) else '')
        stage_value = node_name.strip().lower() or 'collect'
        try:
            stage = StageName(stage_value)
        except Exception:
            stage = StageName.collect
        try:
            self.store.append_event(
                EventRecord(
                    run_id=run_id,
                    stage=stage,
                    event_type='tool_event',
                    payload=payload,
                )
            )
        except Exception:
            return

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
        def _normalize_hint_list(items: list[str] | None) -> list[str]:
            output: list[str] = []
            seen: set[str] = set()
            for item in items or []:
                value = str(item or '').strip()
                if not value:
                    continue
                key = value.casefold()
                if key in seen:
                    continue
                seen.add(key)
                output.append(value)
            return output

        normalized_competitor_hints = _normalize_hint_list(request.competitor_hints)
        normalized_aspect_hints = _normalize_hint_list(request.aspect_hints)
        state = RunState(
            industry=request.industry.strip().lower(),
            competitors=request.competitors,
            user_prompt=request.user_prompt.strip(),
            competitor_hints=normalized_competitor_hints,
            aspect_hints=normalized_aspect_hints,
            language=request.language,
            timeframe=request.timeframe,
            core_schema_version=CORE_SCHEMA_VERSION,
            domain_schema_version=self.store.get_active_domain_schema(request.industry).get('version', 'v1'),
            max_turns=self.config.runtime_max_turns,
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
            {
                'competitors': request.competitors,
                'user_prompt': request.user_prompt.strip(),
                'competitor_hints': normalized_competitor_hints,
                'aspect_hints': normalized_aspect_hints,
            },
        )
        self._init_todo_plan(state)
        return state

    def _init_todo_plan(self, state: RunState) -> None:
        manager = TodoStateManager(state)
        plan = manager.init_from_run_state()
        self._save_and_event(state, StageName.plan, 'todo.plan.initialized', {'todo_plan': plan.model_dump(mode='json')})

    def _build_decision_context(self, state: RunState) -> DecisionContextSnapshot:
        gap_summary: list[dict[str, object]] = []
        reanalyze_candidates: list[dict[str, object]] = []
        for record in state.competitor_analyses:
            candidate_fields: list[str] = []
            for field in record.fields:
                if field.evidence_gaps:
                    gap_summary.append(
                        {
                            'competitor': record.product_name,
                            'field_name': field.field_name,
                            'gaps': field.evidence_gaps,
                        }
                    )
                    candidate_fields.append(field.field_name)
            if candidate_fields:
                reanalyze_candidates.append(
                    {
                        'competitor': record.product_name,
                        'fields': sorted(set(candidate_fields)),
                    }
                )
        latest_ticket = state.tickets[-1] if state.tickets else None
        latest_ticket_summary = {}
        if latest_ticket is not None:
            latest_ticket_summary = {
                'ticket_id': latest_ticket.ticket_id,
                'target_agent': latest_ticket.target_agent,
                'issue_count': len(latest_ticket.issues),
                'status': latest_ticket.status.value,
            }
        recent_failures = []
        if state.last_error:
            recent_failures.append(state.last_error)
        planned_competitors = state.planned_competitors or state.competitors
        schema_fields = [item.field_name for item in state.analysis_schema_plan]
        record_map = {record.product_name: record for record in state.competitor_analyses}
        missing_competitors = [competitor for competitor in planned_competitors if competitor not in record_map]
        missing_schema_fields: list[str] = []
        for field_name in schema_fields:
            if any(field_name not in {field.field_name for field in (record_map.get(competitor).fields if record_map.get(competitor) else [])} for competitor in planned_competitors):
                missing_schema_fields.append(field_name)
        report_section_count = len(state.report.sections) if state.report is not None else 0
        report_ready = state.report is not None and bool(str(state.report.markdown).strip())
        plan_ready = bool(planned_competitors) and bool(schema_fields)
        collect_ready = bool(state.evidences)
        analyze_ready = bool(state.competitor_analyses) and bool(state.findings)
        draft_ready = report_ready
        qa_collect_round_used = bool(state.planner_meta.get('qa_collect_round_used', False))
        latest_collect_ticket_pending = bool(
            latest_ticket_summary.get('target_agent') == 'Collect'
        )
        last_qa_checked = bool(state.planner_meta.get('last_qa_checked', False))
        last_qa_passed = bool(state.planner_meta.get('last_qa_passed', False))
        try:
            last_qa_issue_count = int(state.planner_meta.get('last_qa_issue_count', 0) or 0)
        except Exception:
            last_qa_issue_count = 0
        qa_collect_plan = state.planner_meta.get('qa_collect_plan') if isinstance(state.planner_meta, dict) else None
        qa_collect_items = qa_collect_plan.get('items', []) if isinstance(qa_collect_plan, dict) else []
        qa_collect_item_count = len(qa_collect_items) if isinstance(qa_collect_items, list) else 0
        qa_collect_pending = (
            isinstance(qa_collect_plan, dict)
            and bool(qa_collect_plan.get('enabled', False))
            and isinstance(qa_collect_items, list)
            and bool(qa_collect_items)
            and not qa_collect_round_used
        )
        qa_reanalyze_targets = state.planner_meta.get('qa_reanalyze_targets') if isinstance(state.planner_meta, dict) else None
        qa_reanalyze_pending = isinstance(qa_reanalyze_targets, dict) and any(
            isinstance(fields, list) and bool(fields) for fields in qa_reanalyze_targets.values()
        )
        qa_collect_allowed = (latest_collect_ticket_pending or qa_collect_pending) and not qa_collect_round_used
        qa_ready = draft_ready and last_qa_checked and last_qa_passed
        coverage_summary = self._calc_analyze_coverage(state)
        coverage = float(coverage_summary.get('coverage', 0.0) or 0.0)
        critical_gaps_count = len(gap_summary)
        analyze_eval = state.self_eval.get('analyze') if isinstance(state.self_eval, dict) else None
        evidence_quality = float(getattr(analyze_eval, 'evidence_quality', 0.0) or 0.0) if analyze_eval is not None else (0.7 if analyze_ready else 0.0)
        qa_delivery_approved = bool(report_ready and analyze_ready and last_qa_checked and last_qa_passed)
        static_quality_approved = bool(report_ready and analyze_ready and coverage >= 0.8 and critical_gaps_count == 0)
        quality_gate = {
            'coverage_ok': coverage >= 0.8,
            'coverage_threshold': 0.8,
            'coverage': coverage,
            'critical_gaps_count': critical_gaps_count,
            'evidence_quality_ok': evidence_quality >= 0.7,
            'evidence_quality': evidence_quality,
            'qa_delivery_approved': qa_delivery_approved,
            'static_quality_approved': static_quality_approved,
            'finalize_eligible': qa_delivery_approved or static_quality_approved,
        }
        if qa_delivery_approved:
            qa_recommendation = 'finalize_run'
        elif qa_reanalyze_pending:
            qa_recommendation = 'reanalyze_targets'
        elif qa_collect_pending:
            qa_recommendation = 'collect_gap'
        elif quality_gate['finalize_eligible']:
            qa_recommendation = 'finalize_run'
        elif report_ready and not last_qa_checked:
            qa_recommendation = 'run_qa'
        elif report_ready:
            qa_recommendation = 'redraft_report'
        else:
            qa_recommendation = 'redraft_report'
        last_action_type = ''
        last_action_status = ''
        last_action_changed_fields: list[str] = []
        if isinstance(state.latest_decision, ManagerDecision):
            last_action_type = state.latest_decision.action_type.value
        if isinstance(state.last_action_result, dict):
            last_action_status = str(state.last_action_result.get('status', '') or '')
            changed_fields = state.last_action_result.get('changed_fields', [])
            if isinstance(changed_fields, list):
                last_action_changed_fields = [str(item).strip() for item in changed_fields if str(item).strip()]
        routing_policy = self._build_routing_policy(
            plan_ready=plan_ready,
            collect_ready=collect_ready,
            analyze_ready=analyze_ready,
            draft_ready=draft_ready,
            qa_collect_allowed=qa_collect_allowed,
            report_ready=report_ready,
            qa_ready=qa_ready,
            quality_gate=quality_gate,
        )
        return DecisionContextSnapshot(
            run_id=state.run_id,
            status=state.status,
            turn_count=state.turn_count,
            current_stage=state.current_stage.value if isinstance(state.current_stage, StageName) else str(state.current_stage),
            planned_competitors=planned_competitors,
            schema_fields=schema_fields,
            plan_ready=plan_ready,
            collect_ready=collect_ready,
            analyze_ready=analyze_ready,
            draft_ready=draft_ready,
            qa_ready=qa_ready,
            evidence_count=len(state.evidences),
            competitor_analysis_count=len(state.competitor_analyses),
            finding_count=len(state.findings),
            report_ready=report_ready,
            report_section_count=report_section_count,
            missing_competitors=missing_competitors,
            missing_schema_fields=missing_schema_fields,
            reanalyze_candidates=reanalyze_candidates[:20],
            coverage_summary=coverage_summary,
            gap_summary=gap_summary[:20],
            latest_ticket_summary=latest_ticket_summary,
            self_eval_summary={key: value.model_dump(mode='json') for key, value in state.self_eval.items()},
            last_action_type=last_action_type,
            last_action_status=last_action_status,
            last_action_changed_fields=last_action_changed_fields,
            last_qa_checked=last_qa_checked,
            last_qa_passed=last_qa_passed,
            last_qa_issue_count=last_qa_issue_count,
            qa_reviewed=last_qa_checked,
            qa_passed=last_qa_passed,
            qa_issue_count=last_qa_issue_count,
            qa_collect_item_count=qa_collect_item_count,
            qa_recommendation=qa_recommendation,
            qa_collect_allowed=qa_collect_allowed,
            qa_collect_pending=qa_collect_pending,
            qa_reanalyze_pending=qa_reanalyze_pending,
            quality_gate=quality_gate,
            routing_policy=routing_policy,
            recent_failures=recent_failures,
        )

    @staticmethod
    def _build_routing_policy(
        *,
        plan_ready: bool,
        collect_ready: bool,
        analyze_ready: bool,
        draft_ready: bool,
        qa_collect_allowed: bool,
        report_ready: bool,
        qa_ready: bool,
        quality_gate: dict[str, object],
    ) -> dict[str, object]:
        return {
            'if_plan_missing_then_prefer': 'plan_scope',
            'if_plan_ready_then_prefer': 'collect_initial',
            'if_evidence_ready_but_analysis_missing_then_prefer': 'reanalyze_targets',
            'if_findings_ready_but_report_missing_then_prefer': 'redraft_report',
            'if_report_ready_then_evaluate_quality': ['run_qa', 'collect_gap', 'reanalyze_targets', 'redraft_report', 'finalize_run'],
            'if_quality_gate_finalize_eligible_then_allow': 'finalize_run',
            'collect_gap_requires_qa_ticket': True,
            'qa_collect_allowed': qa_collect_allowed,
            'allow_finalize_only_when': {
                'report_ready': report_ready,
                'analyze_ready': analyze_ready,
                'quality_gate_finalize_eligible': bool(quality_gate.get('finalize_eligible', False)),
            },
            'stage_readiness': {
                'plan_ready': plan_ready,
                'collect_ready': collect_ready,
                'analyze_ready': analyze_ready,
                'draft_ready': draft_ready,
                'qa_ready': qa_ready,
            },
            'quality_gate': quality_gate,
        }

    def _manager_decide(self, state: RunState) -> ManagerDecision:
        context = self._build_decision_context(state)
        self._save_and_event(
            state,
            StageName.plan,
            'manager.context.prepared',
            {'context': context.model_dump(mode='json')},
        )
        metadata = {
            'run_id': state.run_id,
            'attempt': state.attempt,
            'node_name': 'plan',
            'agent_name': 'ManagerAgent',
            'model': self.agent_llm.config.openai_model,
        }
        self._save_and_event(state, StageName.plan, 'manager.decision.started', {'turn': state.turn_count})
        try:
            decision = self.manager_agent.decide(context=context, metadata=metadata)
        except Exception as exc:
            logger.warning('manager decision failed, fallback used: %s', exc)
            decision = self.manager_agent.fallback_decide(context=context)
            decision.metadata['fallback_error'] = str(exc)
        decision = self._guard_manager_decision(state=state, context=context, decision=decision)
        state.latest_decision = decision
        state.decision_history.append(decision)
        handoff = DecisionHandoff(run_id=state.run_id, attempt=state.attempt, turn=state.turn_count, decision=decision, context_snapshot=context)
        self._save_and_event(
            state,
            self._stage_for_action(decision.action_type),
            'manager.decision.completed',
            {'decision': handoff.model_dump(mode='json')},
        )
        return decision

    def _guard_manager_decision(
        self,
        *,
        state: RunState,
        context: DecisionContextSnapshot,
        decision: ManagerDecision,
    ) -> ManagerDecision:
        replacement: tuple[ActionType, str, str] | None = None
        if decision.action_type == ActionType.collect_gap and not context.qa_collect_allowed:
            replacement = (ActionType.collect_initial, 'CollectorAgent', 'collect_gap_requires_active_qa_ticket')
        elif (
            decision.action_type == ActionType.run_qa
            and context.qa_reviewed
            and context.last_action_type == ActionType.run_qa.value
            and context.last_action_status == 'completed'
        ):
            if context.qa_reanalyze_pending:
                replacement = (ActionType.reanalyze_targets, 'AnalystAgent', 'repeat_qa_blocked_reanalyze_pending')
            elif context.qa_collect_pending and context.qa_collect_allowed:
                replacement = (ActionType.collect_gap, 'CollectorAgent', 'repeat_qa_blocked_collect_pending')
            elif bool(context.quality_gate.get('finalize_eligible', False)) or context.qa_passed:
                replacement = (ActionType.finalize_run, 'Finalizer', 'repeat_qa_blocked_finalize_ready')
            else:
                replacement = (ActionType.redraft_report, 'WriterAgent', 'repeat_qa_blocked_quality_not_met')
        elif decision.action_type == ActionType.finalize_run:
            allow_finalize = (
                context.report_ready
                and context.analyze_ready
            )
            if not allow_finalize:
                if not context.analyze_ready:
                    replacement = (ActionType.reanalyze_targets, 'AnalystAgent', 'finalize_requires_analysis_and_report')
                else:
                    replacement = (ActionType.redraft_report, 'WriterAgent', 'finalize_requires_analysis_and_report')
        elif decision.action_type == ActionType.run_qa and not context.report_ready:
            replacement = (ActionType.redraft_report, 'WriterAgent', 'qa_requires_report_ready')

        if replacement is None:
            return decision

        action_type, target_agent, reason_code = replacement
        guarded = ManagerDecision.model_validate(
            {
                **decision.model_dump(mode='json'),
                'action_type': action_type.value,
                'target_agent': target_agent,
                'targets': decision.targets.model_dump(mode='json'),
                'reason': f'{decision.reason}|guard:{reason_code}',
                'metadata': {
                    **decision.metadata,
                    'guard_rewritten': True,
                    'guard_reason': reason_code,
                    'original_action_type': decision.action_type.value,
                },
            }
        )
        self._save_and_event(
            state,
            self._stage_for_action(guarded.action_type),
            'manager.decision.guarded',
            {
                'original_action_type': decision.action_type.value,
                'guarded_action_type': guarded.action_type.value,
                'guard_reason': reason_code,
                'turn': state.turn_count,
            },
        )
        return guarded

    def _manager_act(self, state: RunState) -> tuple[ManagerDecision, ActionExecutionResult]:
        decision = self._manager_decide(state)
        result = self._execute_decision(state, decision)
        return decision, result

    def _execute_decision(self, state: RunState, decision: ManagerDecision) -> ActionExecutionResult:
        state.current_stage = self._stage_for_action(decision.action_type)
        state.next_stage = state.current_stage
        state.runtime_action_context = {
            'targets': decision.targets.model_dump(mode='json'),
            'decision_id': decision.decision_id,
            'action_type': decision.action_type.value,
            'target_agent': decision.target_agent,
        }
        self._save_and_event(
            state,
            state.current_stage,
            'action.dispatch.started',
            {'decision': decision.model_dump(mode='json')},
        )
        action_tool_name = self._action_tool_name(decision.action_type)
        routed = self.tool_router.invoke(
            self._tool_request_for_action(
                state=state,
                action_tool_name=action_tool_name,
                decision=decision,
            )
        )
        if not routed.ok:
            raise RuntimeError(routed.error_message or routed.error_code or 'action_dispatch_failed')
        payload = routed.output
        self._refresh_runtime_state_from_store(state)
        result = ActionExecutionResult.model_validate(
            {
                'action_type': decision.action_type,
                'target_agent': decision.target_agent,
                'status': payload.get('status', 'completed'),
                'summary': payload.get('summary', ''),
                'changed_fields': payload.get('changed_fields', []),
                'artifacts': payload.get('artifacts', {}),
                'next_hints': payload.get('next_hints', []),
            }
        )
        state.last_action_result = result.model_dump(mode='json')
        self._save_and_event(
            state,
            state.current_stage,
            'action.dispatch.completed',
            {'result': result.model_dump(mode='json')},
        )
        return result

    def _refresh_runtime_state_from_store(self, state: RunState) -> None:
        refreshed = self.store.get_state(state.run_id)
        if refreshed is None:
            return
        refreshed_payload = refreshed.model_dump(mode='python')
        current_payload = state.model_dump(mode='python')
        current_payload.update(refreshed_payload)
        synced = RunState.model_validate(current_payload)
        state.__dict__.clear()
        state.__dict__.update(synced.__dict__)

    def _tool_request_for_action(self, *, state: RunState, action_tool_name: str, decision: ManagerDecision):
        from harness.tools.types import ToolRequest

        return ToolRequest(
            name=action_tool_name,
            args={
                'competitors': decision.targets.competitors,
                'fields': decision.targets.fields,
                'sections': decision.targets.sections,
                'reason': decision.reason,
                'mode': decision.action_type.value,
            },
            metadata={
                'run_id': state.run_id,
                'attempt': state.attempt,
                'node_name': self._stage_for_action(decision.action_type).value,
                'agent_name': 'ManagerAgent',
                'trace_name': f'manager.dispatch.{decision.action_type.value}',
            },
        )

    @staticmethod
    def _stage_for_action(action_type: ActionType) -> StageName:
        mapping = {
            ActionType.plan_scope: StageName.plan,
            ActionType.collect_initial: StageName.collect,
            ActionType.collect_gap: StageName.collect,
            ActionType.normalize_evidence: StageName.normalize,
            ActionType.analyze_targets: StageName.analyze,
            ActionType.reanalyze_targets: StageName.analyze,
            ActionType.draft_report: StageName.draft,
            ActionType.redraft_report: StageName.draft,
            ActionType.run_qa: StageName.qa,
            ActionType.finalize_run: StageName.finalize,
        }
        return mapping[action_type]

    @staticmethod
    def _action_tool_name(action_type: ActionType) -> str:
        mapping = {
            ActionType.plan_scope: 'action.plan_scope',
            ActionType.collect_initial: 'action.collect_initial',
            ActionType.collect_gap: 'action.collect_gap',
            ActionType.normalize_evidence: 'action.normalize_evidence',
            ActionType.analyze_targets: 'action.reanalyze_targets',
            ActionType.reanalyze_targets: 'action.reanalyze_targets',
            ActionType.draft_report: 'action.redraft_report',
            ActionType.redraft_report: 'action.redraft_report',
            ActionType.run_qa: 'action.run_qa',
            ActionType.finalize_run: 'action.finalize_run',
        }
        return mapping[action_type]

    def _execute_run(self, state: RunState) -> RunState:
        state = self.runtime.execute(state)
        if state.status not in ('completed', 'failed'):
            state.status = 'failed'
            state.transition_reason = TransitionReason.terminal_error
            state.recovery_state = RecoveryState.halted
            state.last_error = {'reason': 'runtime_ended_without_terminal_status'}
            self._save_and_event(
                state,
                state.current_stage if isinstance(state.current_stage, StageName) else StageName.qa,
                'runtime.turn.terminated',
                {
                    'turn': state.turn_count,
                    'from_stage': state.current_stage.value if isinstance(state.current_stage, StageName) else str(state.current_stage),
                    'to_stage': None,
                    'transition_reason': TransitionReason.terminal_error.value,
                    'recovery_state': RecoveryState.halted.value,
                    'error': state.last_error,
                },
            )
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
        events = self.list_run_events(run_id)
        return {
            'run_id': run_id,
            'status': run.state.status,
            'timeline': timeline,
            'handoffs': handoffs,
            'llm_calls': llm_calls,
            'decision_history': [item.model_dump(mode='json') for item in run.state.decision_history],
            'last_action_result': run.state.last_action_result,
            'decision_summary': {
                'decision_count': len(run.state.decision_history),
                'latest_decision': run.state.latest_decision.model_dump(mode='json') if run.state.latest_decision else None,
                'action_result': run.state.last_action_result,
            },
            'tool_events': self._extract_tool_events(events),
            'todo_plan': run.state.todo_plan.model_dump(mode='json'),
            'todo_events': self._extract_events_by_type(events, 'todo.'),
            'hook_events': self._extract_events_by_type(events, 'hook_event'),
        }

    def replay_node(self, run_id: str, node_name: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            return {'run_id': run_id, 'node_name': node_name, 'io': [], 'status': 'not_found'}
        io = self.store.replay_node_io(run_id, node_name)
        handoffs = self.store.list_stage_handoffs(run_id, stage=node_name)
        llm_calls = self.store.list_llm_calls(run_id, node_name=node_name)
        return {
            'run_id': run_id,
            'node_name': node_name,
            'io': io,
            'handoffs': handoffs,
            'llm_calls': llm_calls,
            'decision_history': [item.model_dump(mode='json') for item in run.state.decision_history if self._stage_for_action(item.action_type).value == node_name],
        }

    def workspace_payload(self, run_id: str) -> dict[str, object]:
        run = self.get_run(run_id)
        if run is None:
            return {'run_id': run_id, 'status': 'not_found'}
        replay = self.replay_run(run_id)
        events = self.list_run_events(run_id)
        manual_interventions = self.store.list_manual_interventions(run_id)
        return self._build_workspace_payload(run=run, replay=replay, events=events, manual_interventions=manual_interventions)

    def update_report_markdown(self, run_id: str, markdown: str) -> RunResponse | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        state = run.state
        if state.report is None:
            return None
        state.report.markdown = markdown
        state.report.html = ''
        self.store.save_state(state)
        return RunResponse(summary=self._summary_for(state), state=state)

    def start_chat_turn(self, run_id: str, request: ChatTurnRequest) -> ChatTurnResponse | None:
        return self.report_conversation.start_turn(run_id, request)

    def chat_payload(self, run_id: str) -> dict[str, object]:
        return self.report_conversation.conversation_payload(run_id)

    def chat_turn_payload(self, run_id: str, turn_id: str) -> ChatTurnResult | None:
        return self.report_conversation.turn_payload(run_id, turn_id)

    def design_questionnaire_from_report(
        self,
        run_id: str,
        *,
        target_audience: str = '竞品相关潜在用户或现有用户',
        objective: str = '验证竞品差异点、用户感知与转化障碍',
    ) -> QuestionnaireDesign | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        state = run.state
        if state.report is None or not str(state.report.markdown or '').strip():
            return None
        design = self.questionnaire_agent.run_llm(
            state,
            target_audience=target_audience,
            objective=objective,
        )
        if not str(design.markdown or '').strip():
            design.markdown = self.questionnaire_agent._markdown_from_design(design)
        state.questionnaire = design
        state.questionnaire_export = {}
        self.store.save_state(state)
        return design

    def update_questionnaire_markdown(self, run_id: str, markdown: str) -> QuestionnaireDesign | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        state = run.state
        if state.questionnaire is None:
            return None
        state.questionnaire.markdown = markdown
        title = self._extract_markdown_title(markdown)
        if title:
            state.questionnaire.title = title
        state.questionnaire_export = {}
        self.store.save_state(state)
        return state.questionnaire

    def export_questionnaire_to_wenjuan(self, run_id: str) -> dict[str, object] | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        state = run.state
        if state.questionnaire is None or not str(state.questionnaire.markdown or '').strip():
            return {}
        if not self.config.wjx_export_enabled:
            raise WjxExportUnavailableError('问卷星导出未启用，请设置 WJX_EXPORT_ENABLED=true。')
        result = export_questionnaire_with_wjx_cli(
            run_id=run_id,
            title=state.questionnaire.title,
            markdown=state.questionnaire.markdown,
            export_dir=self.config.wjx_export_dir_obj,
            api_key=self.config.wjx_api_key,
            base_url=self.config.wjx_base_url,
            cli_path=self.config.wjx_cli_path,
            publish=self.config.wjx_export_publish,
            timeout_sec=self.config.wjx_export_timeout_sec,
        )
        state.questionnaire_export = result
        self.store.save_state(state)
        return result

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
        tool_events = replay.get('tool_events', []) if isinstance(replay.get('tool_events', []), list) else self._extract_tool_events(events)
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
            'tool_events': tool_events,
            'todo_plan': run.state.todo_plan.model_dump(mode='json'),
            'todo_events': self._extract_events_by_type(events, 'todo.'),
            'hook_events': self._extract_events_by_type(events, 'hook_event'),
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

    def collector_preview(
        self,
        *,
        prompt: str,
        industry_hint: str = '',
        competitor_hints: list[str] | None = None,
        deep_dive: bool = False,
    ) -> dict:
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
        comparison_search_plan = dynamic_plan.get('comparison_search_plan', {})
        comparison_corpus = dynamic_plan.get('comparison_corpus', [])
        comparison_corpus_summary = {
            'selected_count': len(comparison_corpus) if isinstance(comparison_corpus, list) else 0,
            'documents': [
                {
                    'corpus_id': item.get('corpus_id', ''),
                    'title': item.get('title', ''),
                    'url': item.get('source_url', ''),
                    'published_at': item.get('published_at', ''),
                    'date_confidence': item.get('date_confidence', 'unknown'),
                }
                for item in comparison_corpus
            ] if isinstance(comparison_corpus, list) else [],
        }
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
                'deep_dive': deep_dive,
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
                'comparison_search_plan': comparison_search_plan,
                'comparison_corpus_summary': comparison_corpus_summary,
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
            if deep_dive:
                rows = []
                for item in result.evidences:
                    item['competitor'] = competitor
                    rows.append(item)
                enriched = self.deep_dive.enrich(
                    run_id='preview',
                    attempt=0,
                    industry=inferred_industry,
                    competitors=[competitor],
                    schema_plan=analysis_schema_plan,
                    evidences=rows,
                )
                result.evidences = enriched.evidences
                result.provider_events.extend(enriched.provider_events)
                result.errors.extend(enriched.errors)
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
                'tool_events': result.tool_events,
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
                for event in result_data.get('tool_events', []):
                    execution_timeline.append({'seq': seq, 'competitor': competitor, **event})
                    seq += 1
                errors.extend(result_data['errors'])
        response = {
            'prompt': prompt,
            'industry_hint': industry_hint,
            'inferred_industry': inferred_industry,
            'effective_max_urls': effective_max_urls,
            'max_urls_note': 'server uses COLLECTOR_MAX_URLS from .env',
            'deep_dive': deep_dive,
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
            'comparison_search_plan': comparison_search_plan,
            'comparison_corpus_summary': comparison_corpus_summary,
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
                competitor_hints=state.competitor_hints,
                aspect_hints=state.aspect_hints,
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
        state.planner_meta['user_competitor_hint_count'] = len(state.competitor_hints)
        state.planner_meta['user_aspect_hint_count'] = len(state.aspect_hints)
        state.planner_meta['final_refine_status'] = str(dynamic_plan.get('final_refine_status', 'fallback'))
        state.planner_meta['comparison_search_plan'] = dynamic_plan.get('comparison_search_plan', {})
        comparison_corpus = dynamic_plan.get('comparison_corpus', [])
        state.planner_meta['comparison_corpus_count'] = len(comparison_corpus) if isinstance(comparison_corpus, list) else 0
        state.planner_meta['comparison_decision_evidence_refs'] = dynamic_plan.get('comparison_decision_evidence_refs', [])
        if isinstance(comparison_corpus, list):
            state.evidences = self._comparison_corpus_evidences(comparison_corpus)
        split_strategy = 'by_competitor' if len(state.competitors) <= 4 else 'by_topic'
        state.split_strategy = split_strategy
        state.self_eval['plan'] = SelfEval(coverage=1.0, consistency=0.9, evidence_quality=0.8, uncertainty=0.2)
        self._save_and_event(
            state,
            StageName.plan,
            'plan.user_hints_received',
            {
                'competitor_hint_count': len(state.competitor_hints),
                'aspect_hint_count': len(state.aspect_hints),
                'competitor_hints': state.competitor_hints,
                'aspect_hints': state.aspect_hints,
            },
        )
        self._save_and_event(
            state,
            StageName.plan,
            'plan.final_lists_refined',
            {
                'final_refine_status': state.planner_meta.get('final_refine_status', 'fallback'),
                'planned_competitor_count': len(state.planned_competitors),
                'schema_field_count': len(state.analysis_schema_plan),
            },
        )
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

    def _run_action_tool(self, action_name: str, args: dict[str, object], metadata: dict[str, object]) -> dict[str, object]:
        run_id = str(metadata.get('run_id', '') or '')
        state = self.store.get_run_state(run_id)
        if state is None:
            return {'status': 'failed', 'summary': 'run_not_found', 'changed_fields': [], 'artifacts': {}, 'next_hints': []}
        competitors = [str(item).strip() for item in args.get('competitors', []) if str(item).strip()] if isinstance(args.get('competitors', []), list) else []
        fields = [str(item).strip() for item in args.get('fields', []) if str(item).strip()] if isinstance(args.get('fields', []), list) else []
        sections = [str(item).strip() for item in args.get('sections', []) if str(item).strip()] if isinstance(args.get('sections', []), list) else []
        state.runtime_action_context = {
            **(state.runtime_action_context or {}),
            'target_competitors': competitors,
            'target_fields': fields,
            'target_sections': sections,
            'action_name': action_name,
            'reason': str(args.get('reason', '') or ''),
            'mode': str(args.get('mode', '') or ''),
        }
        if action_name == 'plan_scope':
            result = self._execute_plan_action(state)
        elif action_name == 'collect_initial':
            result = self._execute_collect_action(state, competitors=competitors, fields=fields)
        elif action_name == 'collect_gap':
            result = self._execute_collect_action(state, competitors=competitors, fields=fields)
        elif action_name == 'normalize_evidence':
            result = self._execute_normalize_action(state)
        elif action_name == 'reanalyze_targets':
            result = self._execute_analyze_action(state, competitors=competitors, fields=fields)
        elif action_name == 'redraft_report':
            result = self._execute_draft_action(state, sections=sections)
        elif action_name == 'run_qa':
            qa_result = self._qa(state)
            state.planner_meta['last_qa_checked'] = True
            state.planner_meta['last_qa_passed'] = bool(qa_result.passed)
            state.planner_meta['last_qa_issue_count'] = len(qa_result.issues)
            if not qa_result.passed and qa_result.collect_plan is not None:
                state.planner_meta['qa_collect_plan'] = qa_result.collect_plan.model_dump(mode='json')
            result = {
                'status': 'completed',
                'summary': 'qa completed' if qa_result.passed else 'qa flagged issues',
                'changed_fields': [],
                'artifacts': {'passed': qa_result.passed, 'issue_count': len(qa_result.issues)},
                'next_hints': [qa_result.target_agent] if qa_result.target_agent else [],
            }
        elif action_name == 'finalize_run':
            qa_passed = bool(state.planner_meta.get('last_qa_passed', False))
            qa_issue_count = int(state.planner_meta.get('last_qa_issue_count', 0) or 0)
            qa_finalize_with_risk = bool(state.planner_meta.get('last_qa_checked', False)) and not qa_passed
            if qa_finalize_with_risk:
                state.planner_meta['qa_exhausted'] = True
                state.planner_meta['qa_finalize_with_risk'] = True
            self._finalize(state)
            state.status = 'completed'
            state.transition_reason = TransitionReason.completed
            result = {
                'status': 'completed',
                'summary': 'run finalized',
                'changed_fields': [],
                'artifacts': {
                    'ticket_count': len(state.tickets),
                    'report_ready': state.report is not None,
                    'qa_passed': qa_passed,
                    'qa_issue_count': qa_issue_count,
                    'qa_finalize_with_risk': qa_finalize_with_risk,
                },
                'next_hints': [],
            }
        else:
            raise ValueError(f'unsupported_action_tool: {action_name}')
        self.store.save_state(state)
        return result

    def _execute_plan_action(self, state: RunState) -> dict[str, object]:
        before_competitors = list(state.planned_competitors)
        before_fields = [item.field_name for item in state.analysis_schema_plan]
        self._plan(state)
        after_competitors = list(state.planned_competitors)
        after_fields = [item.field_name for item in state.analysis_schema_plan]
        return {
            'status': 'completed',
            'summary': f'scope planned: {len(after_competitors)} competitors, {len(after_fields)} fields',
            'changed_fields': after_fields,
            'artifacts': {
                'planned_competitors_before': before_competitors,
                'planned_competitors_after': after_competitors,
                'schema_fields_before': before_fields,
                'schema_fields_after': after_fields,
            },
            'next_hints': ['collect_initial' if after_competitors else 'plan_scope'],
        }

    def _execute_collect_action(self, state: RunState, *, competitors: list[str], fields: list[str]) -> dict[str, object]:
        before_evidence_count = len(state.evidences)
        before_hosts = len({str(item.source_url) for item in state.evidences if item.source_url})
        self._collect(state)
        after_evidence_count = len(state.evidences)
        after_hosts = len({str(item.source_url) for item in state.evidences if item.source_url})
        added = max(0, after_evidence_count - before_evidence_count)
        next_hints = ['analyze_targets'] if added > 0 else ['collect_gap']
        return {
            'status': 'completed',
            'summary': f'evidence collected: +{added} evidences',
            'changed_fields': fields,
            'artifacts': {
                'target_competitors': competitors,
                'target_fields': fields,
                'evidence_count_before': before_evidence_count,
                'evidence_count_after': after_evidence_count,
                'source_host_count_before': before_hosts,
                'source_host_count_after': after_hosts,
            },
            'next_hints': next_hints,
        }

    def _execute_normalize_action(self, state: RunState) -> dict[str, object]:
        before_count = len(state.evidences)
        self._normalize(state)
        after_count = len(state.evidences)
        return {
            'status': 'completed',
            'summary': f'evidence normalized: {before_count} -> {after_count}',
            'changed_fields': [],
            'artifacts': {'evidence_count_before': before_count, 'evidence_count_after': after_count},
            'next_hints': ['analyze_targets'],
        }

    def _execute_analyze_action(self, state: RunState, *, competitors: list[str], fields: list[str]) -> dict[str, object]:
        before_findings = len(state.findings)
        before_profiles = len(state.profiles)
        before_analysis_count = len(state.competitor_analyses)
        self._analyze(state)
        after_findings = len(state.findings)
        after_profiles = len(state.profiles)
        after_analysis_count = len(state.competitor_analyses)
        changed_fields = fields or [item.field_name for record in state.competitor_analyses for item in record.fields]
        if 'qa_reanalyze_targets' in state.planner_meta:
            state.planner_meta.pop('qa_reanalyze_targets', None)
            state.planner_meta['qa_reanalyzed_after_collect'] = True
        next_hints = ['draft_report'] if after_findings > 0 else ['collect_gap']
        return {
            'status': 'completed',
            'summary': f'analysis updated: findings {before_findings}->{after_findings}',
            'changed_fields': changed_fields,
            'artifacts': {
                'target_competitors': competitors,
                'target_fields': fields,
                'analysis_count_before': before_analysis_count,
                'analysis_count_after': after_analysis_count,
                'profile_count_before': before_profiles,
                'profile_count_after': after_profiles,
                'finding_count_before': before_findings,
                'finding_count_after': after_findings,
            },
            'next_hints': next_hints,
        }

    def _execute_draft_action(self, state: RunState, *, sections: list[str]) -> dict[str, object]:
        before_ready = state.report is not None and bool(str(state.report.markdown if state.report else '').strip())
        before_sections = len(state.report.sections) if state.report else 0
        self._draft(state)
        after_ready = state.report is not None and bool(str(state.report.markdown if state.report else '').strip())
        after_sections = len(state.report.sections) if state.report else 0
        if after_ready:
            state.planner_meta['last_qa_checked'] = False
            state.planner_meta['last_qa_passed'] = False
            state.planner_meta['last_qa_issue_count'] = 0
        next_hints = ['finalize_run'] if after_ready else ['draft_report']
        return {
            'status': 'completed',
            'summary': f'report drafted: sections {before_sections}->{after_sections}',
            'changed_fields': sections,
            'artifacts': {
                'target_sections': sections,
                'report_ready_before': before_ready,
                'report_ready_after': after_ready,
                'section_count_before': before_sections,
                'section_count_after': after_sections,
            },
            'next_hints': next_hints,
        }

    def _collect(self, state: RunState) -> None:
        qa_collect_plan = self._consume_qa_collect_plan(state)
        action_context = state.runtime_action_context if isinstance(state.runtime_action_context, dict) else {}
        action_competitors = action_context.get('target_competitors', [])
        action_fields = action_context.get('target_fields', [])
        if not qa_collect_plan and (action_competitors or action_fields):
            qa_collect_plan = {
                'target_competitors': list(action_competitors) if isinstance(action_competitors, list) else [],
                'field_query_overrides': {},
                'reanalyze_targets': {
                    competitor: list(action_fields)
                    for competitor in (action_competitors if isinstance(action_competitors, list) else [])
                    if action_fields
                },
            }
        if qa_collect_plan and isinstance(state.planner_meta, dict):
            reanalyze_targets = qa_collect_plan.get('reanalyze_targets', {})
            if isinstance(reanalyze_targets, dict) and reanalyze_targets:
                state.planner_meta['qa_reanalyze_targets'] = reanalyze_targets
            state.planner_meta['qa_collect_round_used'] = True
        elif isinstance(state.planner_meta, dict) and 'qa_collect_round_used' not in state.planner_meta:
            state.planner_meta['qa_collect_round_used'] = False
        task = self._create_stage_task(
            state,
            task_type=TaskType.collect_evidence,
            owner_agent='CollectorAgent',
            input_payload={
                'target_competitors': qa_collect_plan.get('target_competitors') if qa_collect_plan else [],
                'field_query_overrides': qa_collect_plan.get('field_query_overrides') if qa_collect_plan else {},
            },
            success_criteria=['collect_evidence_for_target_scope'],
        )
        with subagent_trace(
            name='Collect',
            run_type='chain',
            inputs={'run_id': state.run_id, 'attempt': state.attempt, 'task_id': task.task_id},
            metadata={'parent_run_id': state.run_id, 'attempt': state.attempt, 'stage': 'collect'},
        ):
            task_result, result = self.collector_agent.consume_task(task, state)
        self._record_task_result(state, task_result)
        for pe in result.provider_events:
            self._save_and_event(state, StageName.collect, 'provider_event', pe)
        for te in result.tool_events:
            self._save_and_event(state, StageName.collect, 'tool_event', te)
        # Preserve Plan comparison-corpus evidence and append field-level collection.
        state.evidences = list(state.evidences) + list(result.raw_evidences)
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
        collect_handoff = self._build_collect_handoff(
            state,
            provider_events=result.provider_events,
            errors=result.errors,
            qa_collect_plan_used=bool(qa_collect_plan),
        )
        self._save_handoff(state, StageName.collect, collect_handoff)
        self._append_handoff_envelope(
            state,
            HandoffEnvelope(
                run_id=state.run_id,
                attempt=state.attempt,
                handoff_type=HandoffType.collect,
                from_agent='CollectorAgent',
                to_agent='AnalystAgent',
                related_task_id=task.task_id,
                payload_schema='CollectHandoff',
                payload=collect_handoff.model_dump(mode='json'),
                trace_context={'stage': StageName.collect.value},
            ),
        )

    @staticmethod
    def _comparison_corpus_evidences(documents: list[dict]) -> list[RawEvidence]:
        evidences: list[RawEvidence] = []
        for item in documents:
            if item.get('date_confidence') == 'out_of_range':
                continue
            corpus_id = str(item.get('corpus_id', '') or '').strip()
            source_url = str(item.get('source_url', '') or '').strip()
            if not corpus_id or not source_url:
                continue
            extract = item.get('llm_extract', {}) if isinstance(item.get('llm_extract', {}), dict) else {}
            evidences.append(
                RawEvidence(
                    evidence_id=f'evd_{corpus_id.removeprefix("corpus_")[:10]}',
                    source_url=source_url,
                    query=str(item.get('query', '') or ''),
                    title=str(item.get('title', '') or ''),
                    snippet=str(item.get('summary', '') or item.get('content', '') or '')[:1000],
                    source_type='report',
                    retrieval_method='plan_comparison_corpus',
                    retrieval_status='ok' if str(item.get('content', '') or '') else 'partial',
                    recency_score=0.45 if item.get('date_confidence') == 'unknown' else 0.8,
                    domain_extensions={
                        'origin': 'plan_comparison_corpus',
                        'scope': 'cross_competitor',
                        'corpus_id': corpus_id,
                        'topic_key': item.get('topic_key', ''),
                        'keywords': item.get('keywords', []),
                        'published_at': item.get('published_at', ''),
                        'date_confidence': item.get('date_confidence', 'unknown'),
                        'mentioned_competitors': extract.get('mentioned_competitors', []),
                        'comparison_dimensions': extract.get('comparison_dimensions', []),
                    },
                )
            )
        return evidences

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
        if not raw_targets and isinstance(state.runtime_action_context, dict):
            target_competitors = state.runtime_action_context.get('target_competitors', [])
            target_fields = state.runtime_action_context.get('target_fields', [])
            if isinstance(target_competitors, list) and isinstance(target_fields, list) and target_competitors and target_fields:
                raw_targets = {str(item): [str(field) for field in target_fields if str(field).strip()] for item in target_competitors if str(item).strip()}
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
        task = self._create_stage_task(
            state,
            task_type=TaskType.analyze_evidence,
            owner_agent='AnalystAgent',
            input_payload={
                'reanalyze_targets': {competitor: sorted(fields) for competitor, fields in reanalyze_targets.items()},
            },
            success_criteria=['produce_competitor_analyses'],
        )
        try:
            task_result, analyzed = self.analyst_agent.consume_task(task, state)
            self._record_task_result(state, task_result)
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
        analyze_handoff = self._build_analyze_handoff(state)
        self._save_handoff(state, StageName.analyze, analyze_handoff)
        self._append_handoff_envelope(
            state,
            HandoffEnvelope(
                run_id=state.run_id,
                attempt=state.attempt,
                handoff_type=HandoffType.analyze,
                from_agent='AnalystAgent',
                to_agent='WriterAgent',
                related_task_id=task.task_id,
                payload_schema='AnalyzeHandoff',
                payload=analyze_handoff.model_dump(mode='json'),
                trace_context={'stage': StageName.analyze.value},
            ),
        )

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
        task = self._create_stage_task(
            state,
            task_type=TaskType.draft_report,
            owner_agent='WriterAgent',
            input_payload={},
            success_criteria=['produce_report_markdown'],
        )
        try:
            task_result, drafted = self.writer_agent.consume_task(task, state)
            self._record_task_result(state, task_result)
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
        draft_handoff = self._build_draft_handoff(state)
        self._save_handoff(state, StageName.draft, draft_handoff)
        self._append_handoff_envelope(
            state,
            HandoffEnvelope(
                run_id=state.run_id,
                attempt=state.attempt,
                handoff_type=HandoffType.draft,
                from_agent='WriterAgent',
                to_agent='QACriticAgent',
                related_task_id=task.task_id,
                payload_schema='DraftHandoff',
                payload=draft_handoff.model_dump(mode='json'),
                trace_context={'stage': StageName.draft.value},
            ),
        )

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
        handoff: PlanHandoff | CollectHandoff | AnalyzeHandoff | DraftHandoff,
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

    def _create_stage_task(
        self,
        state: RunState,
        *,
        task_type: TaskType,
        owner_agent: str,
        input_payload: dict[str, object],
        success_criteria: list[str],
    ) -> TaskEnvelope:
        task = TaskEnvelope(
            run_id=state.run_id,
            attempt=state.attempt,
            task_type=task_type,
            requester_agent='ManagerAgent',
            owner_agent=owner_agent,
            input_payload=input_payload,
            success_criteria=success_criteria,
        )
        state.task_board.append(task)
        return task

    @staticmethod
    def _record_task_result(state: RunState, result: TaskResult) -> None:
        for task in state.task_board:
            if task.task_id != result.task_id:
                continue
            task.status = TaskStatus.completed if result.status == 'completed' else TaskStatus.failed
            break
        state.last_action_result = result.model_dump(mode='json')

    @staticmethod
    def _append_handoff_envelope(state: RunState, handoff: HandoffEnvelope) -> None:
        state.handoff_log.append(handoff)

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
            comparison_search_plan=state.planner_meta.get('comparison_search_plan', {}) if isinstance(state.planner_meta, dict) else {},
            comparison_corpus_refs=[
                str(ev.domain_extensions.get('corpus_id', ''))
                for ev in state.evidences
                if ev.domain_extensions.get('origin') == 'plan_comparison_corpus'
            ],
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
                    self.analyst_agent._coerce_raw_evidence(ev)
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

    @staticmethod
    def _build_draft_handoff(state: RunState) -> DraftHandoff:
        report = state.report
        section_status: list[dict[str, Any]] = []
        claim_coverage: list[dict[str, Any]] = []
        unresolved_gaps: list[dict[str, Any]] = []
        if report is not None:
            for section in report.sections:
                section_status.append(
                    {
                        'section_id': section.section_id,
                        'title': section.title,
                        'field_name': section.field_name,
                        'claim_count': len(section.claims),
                    }
                )
                for claim in section.claims:
                    claim_coverage.append(
                        {
                            'section_id': section.section_id,
                            'statement': claim.statement[:160],
                            'evidence_ref_count': len(claim.evidence_refs),
                            'confidence': claim.confidence,
                        }
                    )
                    if not claim.evidence_refs:
                        unresolved_gaps.append(
                            {
                                'section_id': section.section_id,
                                'statement': claim.statement[:160],
                                'reason': 'missing_evidence_refs',
                            }
                        )
        return DraftHandoff(
            run_id=state.run_id,
            attempt=state.attempt,
            competitors=state.planned_competitors or state.competitors,
            report=report,
            section_status=section_status,
            claim_coverage=claim_coverage,
            unresolved_gaps=unresolved_gaps,
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
            user_prompt=state.user_prompt,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _extract_markdown_title(markdown: str) -> str:
        for line in str(markdown or '').splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith('#'):
                return text.lstrip('#').strip()[:80]
            return text[:80]
        return ''

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
        # High-frequency collect events can flood terminal output.
        # Keep persisting them to storage/events, but skip console print for readability.
        if stage == StageName.collect and event_type in {'provider_event', 'tool_event'}:
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
        tool_events = replay.get('tool_events', []) if isinstance(replay.get('tool_events', []), list) else self._extract_tool_events(events)
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
                'competitor_hints': state.competitor_hints,
                'aspect_hints': state.aspect_hints,
                'language': state.language,
                'timeframe': state.timeframe,
            },
            'run': {
                'run_id': state.run_id,
                'status': state.status,
                'turn_count': state.turn_count,
                'max_turns': state.max_turns,
                'current_stage': state.current_stage.value if isinstance(state.current_stage, StageName) else str(state.current_stage),
                'next_stage': state.next_stage.value if isinstance(state.next_stage, StageName) else (str(state.next_stage) if state.next_stage else None),
                'transition_reason': state.transition_reason.value if isinstance(state.transition_reason, TransitionReason) else (str(state.transition_reason) if state.transition_reason else None),
                'recovery_state': state.recovery_state.value if isinstance(state.recovery_state, RecoveryState) else str(state.recovery_state),
                'last_error': state.last_error,
                'industry': state.industry,
                'planned_competitors': state.planned_competitors,
                'schema_fields': [item.field_name for item in state.analysis_schema_plan],
                'evidence_count': len(state.evidences),
                'finding_count': len(state.findings),
                'competitor_count': len(state.competitors),
                'latest_decision': state.latest_decision.model_dump(mode='json') if state.latest_decision else None,
                'last_action_result': state.last_action_result,
            },
            'workflow': {
                'dag': self._build_dag(timeline),
                'timeline': timeline,
                'agent_stages': self._build_agent_stage_cards(state=state, timeline=timeline, handoffs=handoffs),
                'agent_workflows': agent_workflows,
                'agent_handoffs': self._build_agent_handoffs(state=state, handoffs=handoffs, stage_io=stage_io),
                'decision_history': [item.model_dump(mode='json') for item in state.decision_history],
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
            'questionnaire': state.questionnaire.model_dump(mode='json') if state.questionnaire else None,
            'questionnaire_export': state.questionnaire_export,
            'chat': self.report_conversation.conversation_payload(state.run_id),
            'artifacts': self._build_workspace_artifacts(state),
            'todo_plan': state.todo_plan.model_dump(mode='json'),
            'todo_events': self._extract_events_by_type(events, 'todo.'),
            'hook_events': self._extract_events_by_type(events, 'hook_event'),
            'observability': {
                'decision_history': [item.model_dump(mode='json') for item in state.decision_history],
                'last_action_result': state.last_action_result,
                'llm_calls': llm_calls,
                'tool_events': tool_events,
                'todo_plan': state.todo_plan.model_dump(mode='json'),
                'todo_events': self._extract_events_by_type(events, 'todo.'),
                'hook_events': self._extract_events_by_type(events, 'hook_event'),
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
    def _extract_tool_events(events: list[dict]) -> list[dict]:
        tool_events: list[dict] = []
        for item in events:
            if not isinstance(item, dict):
                continue
            if str(item.get('event_type', '')) != 'tool_event':
                continue
            payload = item.get('payload', {})
            if isinstance(payload, dict):
                tool_events.append(payload)
        return tool_events

    @staticmethod
    def _extract_events_by_type(events: list[dict], event_type_prefix: str) -> list[dict]:
        filtered: list[dict] = []
        for item in events:
            if not isinstance(item, dict):
                continue
            event_type = str(item.get('event_type', ''))
            if event_type_prefix == 'hook_event':
                if event_type == 'hook_event':
                    filtered.append(item)
            elif event_type.startswith(event_type_prefix):
                filtered.append(item)
        return filtered

    def mark_todo_stage_started(self, state: RunState, stage: StageName, agent_name: str) -> None:
        task = TodoStateManager(state).mark_stage_started(stage, agent_name=agent_name)
        if task is None:
            return
        self._save_and_event(state, stage, 'todo.task.started', {'task': task.model_dump(mode='json')})
        self._save_and_event(state, stage, 'todo.plan.updated', {'todo_plan': state.todo_plan.model_dump(mode='json')})

    def mark_todo_stage_completed(self, state: RunState, stage: StageName, agent_name: str, notes: str = '') -> None:
        task = TodoStateManager(state).mark_stage_completed(stage, agent_name=agent_name, notes=notes)
        if task is None:
            return
        self._save_and_event(state, stage, 'todo.task.completed', {'task': task.model_dump(mode='json')})
        self._save_and_event(state, stage, 'todo.plan.updated', {'todo_plan': state.todo_plan.model_dump(mode='json')})

    def mark_todo_stage_blocked(self, state: RunState, stage: StageName, reason: str, agent_name: str) -> None:
        task = TodoStateManager(state).mark_stage_blocked(stage, reason=reason, agent_name=agent_name)
        if task is None:
            return
        self._save_and_event(state, stage, 'todo.task.blocked', {'task': task.model_dump(mode='json'), 'reason': reason})
        self._save_and_event(state, stage, 'todo.plan.updated', {'todo_plan': state.todo_plan.model_dump(mode='json')})

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
            'draft': 'DraftHandoff',
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
            'current_stage': state.current_stage.value if isinstance(state.current_stage, StageName) else str(state.current_stage),
            'next_stage': state.next_stage.value if isinstance(state.next_stage, StageName) else (str(state.next_stage) if state.next_stage else None),
            'turn_count': state.turn_count,
            'max_turns': state.max_turns,
            'transition_reason': state.transition_reason.value if isinstance(state.transition_reason, TransitionReason) else (str(state.transition_reason) if state.transition_reason else None),
            'recovery_state': state.recovery_state.value if isinstance(state.recovery_state, RecoveryState) else str(state.recovery_state),
            'last_error': dict(state.last_error),
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
        raw_current_stage = str(graph.get('current_stage', StageName.plan.value))
        try:
            current_stage = StageName(raw_current_stage)
        except ValueError:
            current_stage = StageName.finalize if str(graph.get('status', 'running')) == 'completed' else StageName.plan
        raw_next_stage = graph.get('next_stage')
        next_stage: StageName | None = None
        if raw_next_stage is not None:
            try:
                next_stage = StageName(str(raw_next_stage))
            except ValueError:
                next_stage = None
        raw_transition_reason = graph.get('transition_reason')
        transition_reason = None
        if raw_transition_reason is not None:
            try:
                transition_reason = TransitionReason(str(raw_transition_reason))
            except ValueError:
                transition_reason = None
        try:
            recovery_state = RecoveryState(str(graph.get('recovery_state', RecoveryState.none.value)))
        except ValueError:
            recovery_state = RecoveryState.none
        return RunState(
            run_id=graph['run_id'],
            attempt=graph['attempt'],
            parent_attempt=graph['parent_attempt'],
            ticket_id=graph.get('ticket_id'),
            turn_count=int(graph.get('turn_count', 0) or 0),
            max_turns=int(graph.get('max_turns', 40) or 40),
            current_stage=current_stage,
            next_stage=next_stage,
            transition_reason=transition_reason,
            recovery_state=recovery_state,
            last_error=graph.get('last_error', {}) if isinstance(graph.get('last_error', {}), dict) else {},
            industry=graph['industry'],
            competitors=graph['competitors'],
            language=graph['language'],
            timeframe=graph['timeframe'],
            core_schema_version=graph['core_schema_version'],
            domain_schema_version=graph['domain_schema_version'],
            status=graph['status'],  # type: ignore[arg-type]
        )
