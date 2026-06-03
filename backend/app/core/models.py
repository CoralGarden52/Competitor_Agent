from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class Severity(StrEnum):
    low = 'low'
    medium = 'medium'
    high = 'high'
    critical = 'critical'


class TicketStatus(StrEnum):
    created = 'created'
    in_progress = 'in_progress'
    resolved = 'resolved'
    rejected = 'rejected'


class ProposalStatus(StrEnum):
    proposed = 'proposed'
    reviewed = 'reviewed'
    activated = 'activated'
    rejected = 'rejected'


class RiskLevel(StrEnum):
    low = 'low'
    medium = 'medium'
    high = 'high'


class PolicyDecision(StrEnum):
    approved = 'approved'
    rejected = 'rejected'
    review_required = 'review_required'


class StageName(StrEnum):
    plan = 'plan'
    collect = 'collect'
    normalize = 'normalize'
    analyze = 'analyze'
    draft = 'draft'
    qa = 'qa'
    finalize = 'finalize'


class TransitionReason(StrEnum):
    stage_succeeded = 'stage_succeeded'
    qa_passed = 'qa_passed'
    qa_rework_collect = 'qa_rework_collect'
    qa_recollect_skipped = 'qa_recollect_skipped'
    retryable_error = 'retryable_error'
    terminal_error = 'terminal_error'
    max_turns_reached = 'max_turns_reached'
    completed = 'completed'


class RecoveryState(StrEnum):
    none = 'none'
    retrying = 'retrying'
    fallback = 'fallback'
    reworking = 'reworking'
    halted = 'halted'


class TodoTaskStatus(StrEnum):
    pending = 'pending'
    in_progress = 'in_progress'
    completed = 'completed'
    blocked = 'blocked'


class EventType(StrEnum):
    plan_started = 'plan.started'
    collect_completed = 'collect.completed'
    analyze_completed = 'analyze.completed'
    draft_completed = 'draft.completed'
    qa_rework_ticket_created = 'qa.rework_ticket_created'


class FeatureNode(BaseModel):
    name: str
    capability: str
    children: list['FeatureNode'] = Field(default_factory=list)


class PricingTier(BaseModel):
    name: str
    price_range: str
    billing_cycle: str
    limits: list[str] = Field(default_factory=list)


class PricingModel(BaseModel):
    model_config = {'protected_namespaces': ()}
    model_type: str
    free_tier: bool
    billing_dimensions: list[str] = Field(default_factory=list)
    tiers: list[PricingTier] = Field(default_factory=list)


class FeedbackSummary(BaseModel):
    positive_themes: list[str] = Field(default_factory=list)
    negative_themes: list[str] = Field(default_factory=list)
    representative_quotes: list[str] = Field(default_factory=list)
    sentiment_distribution: dict[str, float] = Field(default_factory=dict)


class Evidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: f'evd_{uuid4().hex[:10]}')
    source_url: str
    query: str = ''
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    title: str = ''
    snippet: str
    claim_tags: list[str] = Field(default_factory=list)
    credibility_score: float = Field(default=0.7, ge=0.0, le=1.0)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    recency_score: float = Field(default=0.5, ge=0.0, le=1.0)
    raw_content_path: str = ''
    extract_fields: dict[str, Any] = Field(default_factory=dict)
    license_or_tos_note: str = ''
    source_type: Literal['official', 'news', 'review', 'community', 'report'] = 'official'
    retrieval_method: str = 'tool_search'
    retrieval_status: Literal['ok', 'partial', 'failed'] = 'ok'
    domain_extensions: dict[str, Any] = Field(default_factory=dict)


class RawEvidence(Evidence):
    """Protocol-level alias for collector output evidence."""


class CompetitorProfile(BaseModel):
    industry: str
    product_name: str
    positioning: str
    feature_tree: list[FeatureNode]
    advantages: list[str]
    disadvantages: list[str]
    pricing_model: PricingModel
    user_feedback: FeedbackSummary
    evidence_refs: list[str] = Field(default_factory=list)
    domain_extensions: dict[str, Any] = Field(default_factory=dict)


class Finding(BaseModel):
    finding_id: str = Field(default_factory=lambda: f'fdg_{uuid4().hex[:10]}')
    statement: str
    category: Literal['feature', 'pricing', 'feedback', 'risk']
    evidence_refs: list[str] = Field(min_length=1)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    risk_flag: bool = False


class AnalysisSchemaField(BaseModel):
    field_name: str
    query_templates: list[str] = Field(default_factory=list)
    recommended_sources: list[str] = Field(default_factory=list)
    priority: int = 1
    corpus_refs: list[str] = Field(default_factory=list)


class FieldEvidenceBundle(BaseModel):
    field_name: str
    evidences: list[RawEvidence] = Field(default_factory=list)


class CompetitorEvidenceBundle(BaseModel):
    product_name: str
    fields: list[FieldEvidenceBundle] = Field(default_factory=list)


class AnalysisFieldResult(BaseModel):
    field_name: str
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    normalized_value: dict[str, Any] = Field(default_factory=dict)
    evidence_gaps: list[str] = Field(default_factory=list)


class CompetitorAnalysisRecord(BaseModel):
    product_name: str
    fields: list[AnalysisFieldResult] = Field(default_factory=list)


class PlanHandoff(BaseModel):
    run_id: str
    attempt: int
    inferred_industry: str
    planned_competitors: list[str] = Field(default_factory=list)
    candidate_groups: dict[str, Any] = Field(default_factory=dict)
    analysis_schema_plan: list[AnalysisSchemaField] = Field(default_factory=list)
    split_strategy: str = 'by_competitor'
    planner_meta: dict[str, Any] = Field(default_factory=dict)
    comparison_search_plan: dict[str, Any] = Field(default_factory=dict)
    comparison_corpus_refs: list[str] = Field(default_factory=list)


class CollectHandoff(BaseModel):
    run_id: str
    attempt: int
    competitors: list[str] = Field(default_factory=list)
    schema_fields: list[str] = Field(default_factory=list)
    evidence_bundles: list[CompetitorEvidenceBundle] = Field(default_factory=list)
    provider_events: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    total_evidence_count: int = 0
    qa_collect_plan_used: bool = False


class AnalyzeHandoff(BaseModel):
    run_id: str
    attempt: int
    competitors: list[str] = Field(default_factory=list)
    competitor_analyses: list[CompetitorAnalysisRecord] = Field(default_factory=list)
    profiles: list[CompetitorProfile] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    coverage_summary: list[dict[str, Any]] = Field(default_factory=list)
    evidence_gap_summary: list[dict[str, Any]] = Field(default_factory=list)


class ReportClaim(BaseModel):
    statement: str
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class ReportSection(BaseModel):
    section_id: str
    title: str
    field_name: str = ''
    claims: list[ReportClaim] = Field(default_factory=list)
    content_markdown: str = ''


class Report(BaseModel):
    executive_summary: str
    comparison_matrix: list[dict[str, Any]] = Field(default_factory=list)
    swot: dict[str, list[str]] = Field(default_factory=lambda: {'strengths': [], 'weaknesses': [], 'opportunities': [], 'threats': []})
    opportunities: list[str] = Field(default_factory=list)
    appendix_sources: list[str] = Field(default_factory=list)
    sections: list[ReportSection] = Field(default_factory=list)
    markdown: str = ''
    html: str = ''


class QuestionnaireQuestion(BaseModel):
    question_id: str
    question_type: Literal['single_choice', 'multiple_choice', 'scale', 'open_text', 'matrix'] = 'open_text'
    title: str
    intent: str = ''
    options: list[str] = Field(default_factory=list)
    scale_min: int | None = None
    scale_max: int | None = None
    required: bool = True
    field_refs: list[str] = Field(default_factory=list)


class QuestionnaireSection(BaseModel):
    section_id: str
    title: str
    objective: str = ''
    questions: list[QuestionnaireQuestion] = Field(default_factory=list)


class QuestionnaireDesign(BaseModel):
    title: str
    target_audience: str
    objective: str
    introduction: str
    estimated_minutes: int = 8
    sections: list[QuestionnaireSection] = Field(default_factory=list)
    closing_message: str = ''
    markdown: str = ''


class QuestionnaireSignalChunk(BaseModel):
    chunk_id: str
    chunk_title: str
    key_points: list[str] = Field(default_factory=list)
    candidate_dimensions: list[str] = Field(default_factory=list)
    candidate_questions: list[str] = Field(default_factory=list)
    user_phrases: list[str] = Field(default_factory=list)
    decision_factors: list[str] = Field(default_factory=list)
    risk_points: list[str] = Field(default_factory=list)


class ReworkIssue(BaseModel):
    code: str
    message: str
    stage: StageName


class ReworkTicket(BaseModel):
    ticket_id: str = Field(default_factory=lambda: f'tkt_{uuid4().hex[:10]}')
    target_agent: Literal['Collect', 'Analyze', 'Draft']
    issues: list[ReworkIssue]
    evidence_refs: list[str] = Field(default_factory=list)
    qa_rules: list[str] = Field(default_factory=list)
    severity: Severity = Severity.medium
    deadline: str = ''
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: TicketStatus = TicketStatus.created
    domain_extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_rework_ticket(self):
        if not self.target_agent:
            raise ValueError('target_agent is required')
        if not self.issues:
            raise ValueError('issues is required')
        return self


class SelfEval(BaseModel):
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    consistency: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    uncertainty: float = Field(default=0.0, ge=0.0, le=1.0)


class SchemaEvolutionProposal(BaseModel):
    proposal_id: str = Field(default_factory=lambda: f'sep_{uuid4().hex[:10]}')
    industry: str
    missing_dimension: str
    rationale: str
    suggested_fields: list[str] = Field(default_factory=list)
    impact_scope: list[str] = Field(default_factory=list)
    status: ProposalStatus = ProposalStatus.proposed
    auto_decision: Literal['approved', 'rejected', 'pending'] = 'pending'
    reviewed_by: str = 'auto-rule-engine'
    review_notes: str = ''
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    target_version: str = 'v1'


class ApprovalPolicy(BaseModel):
    policy_id: str = Field(default_factory=lambda: f'pol_{uuid4().hex[:10]}')
    industry: str = 'global'
    enabled: bool = True
    priority: int = 100
    max_fields: int = 6
    max_qa_failures: int = 3
    max_allowed_risk: RiskLevel = RiskLevel.medium
    denied_scopes: list[str] = Field(default_factory=list)
    decision: PolicyDecision = PolicyDecision.approved
    version: str = 'v1'
    notes: str = ''
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FieldRiskProfile(BaseModel):
    profile_id: str = Field(default_factory=lambda: f'frp_{uuid4().hex[:10]}')
    industry: str = 'global'
    field_name: str
    risk_level: RiskLevel = RiskLevel.medium
    notes: str = ''
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PolicyAuditRecord(BaseModel):
    audit_id: str = Field(default_factory=lambda: f'aud_{uuid4().hex[:10]}')
    proposal_id: str
    industry: str
    matched_policy_id: str | None = None
    decision: PolicyDecision
    reason: str
    risk_summary: dict[str, str] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PolicyDecisionResult(BaseModel):
    decision: PolicyDecision
    matched_policy_id: str | None = None
    reason: str
    risk_summary: dict[str, str] = Field(default_factory=dict)


class RunRequest(BaseModel):
    industry: str
    competitors: list[str] = Field(default_factory=list)
    user_prompt: str = ''
    competitor_hints: list[str] = Field(default_factory=list)
    aspect_hints: list[str] = Field(default_factory=list)
    language: str = 'zh-CN'
    timeframe: str = 'last_12_months'


class TodoTask(BaseModel):
    task_id: str
    title: str
    owner_agent: str
    stage: StageName
    status: TodoTaskStatus = TodoTaskStatus.pending
    depends_on: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    notes: str = ''


class TodoPlan(BaseModel):
    tasks: list[TodoTask] = Field(default_factory=list)
    current_task_id: str | None = None
    version: int = 1


class RunState(BaseModel):
    run_id: str = Field(default_factory=lambda: f'run_{uuid4().hex[:12]}')
    attempt: int = 1
    parent_attempt: int | None = None
    ticket_id: str | None = None
    turn_count: int = 0
    max_turns: int = 40
    current_stage: StageName = StageName.plan
    next_stage: StageName | None = StageName.plan
    transition_reason: TransitionReason | None = None
    recovery_state: RecoveryState = RecoveryState.none
    last_error: dict[str, Any] = Field(default_factory=dict)
    industry: str
    competitors: list[str]
    user_prompt: str = ''
    competitor_hints: list[str] = Field(default_factory=list)
    aspect_hints: list[str] = Field(default_factory=list)
    language: str = 'zh-CN'
    timeframe: str = 'last_12_months'
    split_strategy: str = 'by_competitor'
    core_schema_version: str = 'core_v1'
    domain_schema_version: str = 'v1'
    planned_competitors: list[str] = Field(default_factory=list)
    planner_meta: dict[str, Any] = Field(default_factory=dict)
    analysis_schema_plan: list[AnalysisSchemaField] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)
    competitor_analyses: list[CompetitorAnalysisRecord] = Field(default_factory=list)
    profiles: list[CompetitorProfile] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    report: Report | None = None
    tickets: list[ReworkTicket] = Field(default_factory=list)
    self_eval: dict[str, SelfEval] = Field(default_factory=dict)
    schema_evolution_proposals: list[SchemaEvolutionProposal] = Field(default_factory=list)
    todo_plan: TodoPlan = Field(default_factory=TodoPlan)
    status: Literal['running', 'failed', 'completed'] = 'running'


class EventRecord(BaseModel):
    run_id: str
    stage: StageName
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunSummary(BaseModel):
    run_id: str
    industry: str
    status: str
    competitor_count: int
    user_prompt: str = ''
    created_at: datetime
    updated_at: datetime


class RunResponse(BaseModel):
    summary: RunSummary
    state: RunState


class StageSnapshot(BaseModel):
    run_id: str
    stage: StageName
    input_hash: str
    output_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CollectOutput(BaseModel):
    raw_evidences: list[RawEvidence] = Field(default_factory=list)
    provider_events: list[dict[str, Any]] = Field(default_factory=list)
    tool_events: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AnalyzeOutput(BaseModel):
    competitors: list[CompetitorAnalysisRecord] = Field(default_factory=list)
    profiles: list[CompetitorProfile] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class DraftOutput(BaseModel):
    report: Report


class QACollectPlanItem(BaseModel):
    competitor: str
    field_name: str
    reason: str
    query_list: list[str] = Field(default_factory=list, min_length=2, max_length=4)
    priority: int = Field(default=1, ge=1, le=10)


class QACollectPlan(BaseModel):
    enabled: bool = False
    items: list[QACollectPlanItem] = Field(default_factory=list)
    global_notes: str = ''


class QAOutput(BaseModel):
    passed: bool
    issues: list[ReworkIssue] = Field(default_factory=list)
    target_agent: Literal['Collect', 'Analyze', 'Draft'] | None = None
    ticket: ReworkTicket | None = None
    collect_plan: QACollectPlan | None = None

    @model_validator(mode='after')
    def validate_qa_output(self):
        if not self.passed and self.target_agent is None:
            raise ValueError('target_agent is required when QA fails')
        if (not self.passed) and self.target_agent == 'Collect' and self.collect_plan is None:
            raise ValueError('collect_plan is required when QA fails and target_agent=Collect')
        return self


class LLMCallTrace(BaseModel):
    trace_id: str = Field(default_factory=lambda: f'llm_{uuid4().hex[:12]}')
    run_id: str = ''
    attempt: int = 0
    node_name: str = ''
    agent_name: str = ''
    trace_name: str
    model: str = ''
    status: Literal['completed', 'failed'] = 'completed'
    system_prompt: str = ''
    user_payload: dict[str, Any] = Field(default_factory=dict)
    raw_response: dict[str, Any] = Field(default_factory=dict)
    parsed_response: dict[str, Any] = Field(default_factory=dict)
    error_reason: str = ''
    error_message: str = ''
    finish_reason: str = ''
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage_source: Literal['provider', 'estimated', 'missing'] = 'missing'
    usage_details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EventEnvelope(BaseModel):
    event_type: str
    stage: StageName
    run_id: str
    attempt: int
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProposalReviewRequest(BaseModel):
    decision: Literal['reviewed', 'rejected']
    reviewer: str = 'human-reviewer'
    notes: str = ''


class ProposalActivateRequest(BaseModel):
    activated_by: str = 'human-reviewer'
    force: bool = False


class PolicyUpsertRequest(BaseModel):
    industry: str = 'global'
    enabled: bool = True
    priority: int = 100
    max_fields: int = 6
    max_qa_failures: int = 3
    max_allowed_risk: RiskLevel = RiskLevel.medium
    denied_scopes: list[str] = Field(default_factory=list)
    decision: PolicyDecision = PolicyDecision.approved
    version: str = 'v1'
    notes: str = ''
    policy_id: str | None = None


class FieldRiskUpsertItem(BaseModel):
    industry: str = 'global'
    field_name: str
    risk_level: RiskLevel = RiskLevel.medium
    notes: str = ''


class FieldRiskUpsertRequest(BaseModel):
    items: list[FieldRiskUpsertItem] = Field(min_length=1)


class QAResult(BaseModel):
    passed: bool
    issues: list[ReworkIssue] = Field(default_factory=list)
    target_agent: Literal['Collect', 'Analyze', 'Draft'] | None = None

    @model_validator(mode='after')
    def validate_target(self):
        if not self.passed and self.target_agent is None:
            raise ValueError('target_agent is required when QA fails')
        return self
