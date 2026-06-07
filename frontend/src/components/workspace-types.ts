export type StageName =
  | "plan"
  | "confirm_plan"
  | "collect"
  | "normalize"
  | "analyze"
  | "draft"
  | "qa"
  | "finalize";

export type StepStatus = "pending" | "running" | "done" | "failed";

export type WorkspaceSummary = {
  run_id?: string;
  industry?: string;
  status?: string;
  competitor_count?: number;
  task_summary?: string;
  created_at?: string;
  updated_at?: string;
};

export type WorkspaceRequest = {
  industry?: string;
  user_prompt?: string;
  task_summary?: string;
  competitors?: string[];
  language?: string;
  timeframe?: string;
};

export type WorkspaceRun = {
  run_id?: string;
  status?: string;
  task_summary?: string;
  industry?: string;
  target_product?: string;
  plan_revision?: number;
  planned_competitors?: string[];
  analysis_subjects?: Array<{ name?: string; role?: string; is_target?: boolean }>;
  schema_fields?: string[];
  evidence_count?: number;
  finding_count?: number;
  competitor_count?: number;
};

export type WorkflowDag = {
  nodes?: StageName[];
  edges?: Array<{ from: string; to: string }>;
};

export type WorkflowTimelineItem = {
  trace_id?: number;
  node_name?: string;
  attempt?: number;
  status?: string;
  started_at?: string;
  ended_at?: string | null;
  duration_ms?: number | null;
  error_text?: string | null;
};

export type AgentStageCard = {
  stage: string;
  agent?: string;
  status?: string;
  duration_ms?: number | null;
  summary?: string;
  handoff_type?: string;
  handoff_summary?: string;
};

export type AgentWorkflow = {
  nodes?: string[];
  edges?: Array<{ from: string; to: string }>;
};

export type AgentHandoffSchema = {
  schema_name?: string;
  payload?: Record<string, unknown>;
  created_at?: string;
};

export type AgentHandoff = {
  stage: string;
  agent_name?: string;
  status?: string;
  input_schema?: AgentHandoffSchema;
  output_schema?: AgentHandoffSchema;
  handoff_summary?: string;
  handoff_highlights?: string[];
};

export type WorkspaceEvent = {
  event_id?: number;
  stage?: string;
  event_type?: string;
  created_at?: string;
  payload?: Record<string, unknown>;
};

export type PlanCardCompetitorsPayload = {
  card_event?: boolean;
  planned_competitors?: string[];
  analysis_subjects?: Array<{ name?: string; role?: string; is_target?: boolean }>;
  count?: number;
  display_text?: string;
};

export type PlanCardSchemaPayload = {
  card_event?: boolean;
  schema_fields?: string[];
  schema_field_labels?: Record<string, string>;
  count?: number;
  display_text?: string;
};

export type ConfirmPlanCardSummaryPayload = {
  card_event?: boolean;
  revision_number?: number;
  delta?: string;
  message?: string;
};

export type CollectCardSourceFoundPayload = {
  card_event?: boolean;
  competitor?: string;
  field_name?: string;
  field_label?: string;
  title?: string;
  source_url?: string;
  source_provider?: string;
  rank?: number;
  total_found?: number;
};

export type AnalyzeCardFieldSummaryPayload = {
  card_event?: boolean;
  competitor?: string;
  field_name?: string;
  field_label?: string;
  summary?: string;
  confidence?: number;
  evidence_ref_count?: number;
  is_incremental?: boolean;
};

export type AnalyzeCardCompetitorSummaryPayload = {
  card_event?: boolean;
  competitor?: string;
  summary_lines?: string[];
  field_count?: number;
};

export type QaCardReviewStartedPayload = {
  card_event?: boolean;
  competitor_count?: number;
  schema_field_count?: number;
};

export type QaCardReviewSummaryPayload = {
  card_event?: boolean;
  competitor?: string;
  needs_recollect?: boolean;
  insufficient_fields?: Array<Record<string, unknown>>;
  field_reviews?: Array<Record<string, unknown>>;
  summary_text?: string;
};

export type QaCardFinalSummaryPayload = {
  card_event?: boolean;
  passed?: boolean;
  issue_count?: number;
  collect_item_count?: number;
  review_details?: Array<Record<string, unknown>>;
  collect_items?: Array<Record<string, unknown>>;
  improvement_details?: Array<Record<string, unknown>>;
  summary_text?: string;
};

export type AgentTraceSummary = {
  llm_call_count?: number;
  total_tokens?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  event_count?: number;
  handoff_count?: number;
  input_count?: number;
  output_count?: number;
};

export type AgentTraceInputStep = {
  step_type: "input";
  display_name?: string;
  created_at?: string;
  payload?: Record<string, unknown>;
};

export type AgentTraceEventStep = {
  step_type: "event";
  display_name?: string;
  created_at?: string;
  event_type?: string;
  payload?: Record<string, unknown>;
  payload_preview?: string;
};

export type AgentTraceLlmCallStep = {
  step_type: "llm_call";
  step_order?: number;
  display_name?: string;
  trace_name?: string;
  created_at?: string;
  status?: string;
  model?: string;
  system_prompt?: string;
  user_payload?: Record<string, unknown>;
  raw_response?: Record<string, unknown>;
  parsed_response?: Record<string, unknown>;
  input_preview?: string;
  output_preview?: string;
  latency_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  finish_reason?: string;
  error_reason?: string;
  error_message?: string;
};

export type AgentTraceHandoffStep = {
  step_type: "handoff";
  display_name?: string;
  created_at?: string;
  schema_name?: string;
  payload?: Record<string, unknown>;
  payload_preview?: string;
  summary?: string;
};

export type AgentTraceOutputStep = {
  step_type: "output";
  display_name?: string;
  created_at?: string;
  payload?: Record<string, unknown>;
};

export type AgentTraceStep =
  | AgentTraceInputStep
  | AgentTraceEventStep
  | AgentTraceLlmCallStep
  | AgentTraceHandoffStep
  | AgentTraceOutputStep;

export type AgentTrace = {
  stage: string;
  agent_name?: string;
  status?: string;
  summary?: AgentTraceSummary;
  steps?: AgentTraceStep[];
};

export type WorkspaceQa = {
  passed?: boolean;
  issue_count?: number;
  target_agent?: string | null;
  issues?: Array<{ code?: string; message?: string; stage?: string }>;
  collect_items?: Array<{
    competitor?: string;
    field_name?: string;
    reason?: string;
    query_list?: string[];
    priority?: number;
    field_label?: string;
    collected_urls?: string[];
  }>;
  review_details?: Array<{
    competitor?: string;
    needs_recollect?: boolean;
    field_reviews?: Array<{
      field_name?: string;
      field_label?: string;
      reason?: string;
      priority?: number;
      before_summary?: string;
      before_evidence_ref_count?: number;
      before_confidence?: number;
    }>;
  }>;
  improvement_details?: Array<{
    competitor?: string;
    field_name?: string;
    field_label?: string;
    before_summary?: string;
    after_summary?: string;
    before_evidence_ref_count?: number;
    after_evidence_ref_count?: number;
    before_confidence?: number;
    after_confidence?: number;
    collected_urls?: string[];
  }>;
};

export type WorkspaceReportCitation = {
  citation_id?: string;
  label?: string;
  url?: string;
  evidence_refs?: string[];
  source_title?: string;
};

export type WorkspaceReportBlock = {
  block_id?: string;
  block_type?: "title" | "executive_summary" | "comparison_matrix" | "section_paragraph" | "section_bullets" | "reference_list";
  section_id?: string;
  title?: string;
  order?: number;
  content?: unknown;
  citations?: WorkspaceReportCitation[];
  status?: "draft" | "completed";
};

export type WorkspaceReport = {
  markdown?: string;
  html?: string;
  sources?: string[];
  blocks?: WorkspaceReportBlock[];
  citations?: WorkspaceReportCitation[];
  render_version?: string;
};

export type WorkspaceQuestionnaire = {
  title?: string;
  target_audience?: string;
  objective?: string;
  introduction?: string;
  estimated_minutes?: number;
  sections?: unknown[];
  closing_message?: string;
  markdown?: string;
};

export type WorkspaceQuestionnaireExport = {
  provider?: string;
  status?: string;
  title?: string;
  url?: string;
  vid?: string;
  exported_at?: string;
};

export type EvidenceItem = {
  title?: string;
  source_url?: string;
  snippet?: string;
};

export type WorkspaceArtifacts = {
  evidences?: EvidenceItem[];
  plan_revisions?: Array<Record<string, unknown>>;
  pre_research_snapshot?: Record<string, unknown>;
  supplement_requests?: Array<Record<string, unknown>>;
  analysis_subjects?: Array<Record<string, unknown>>;
};

export type WorkflowPlanConfirmation = {
  status?: string;
  confirmation_message?: string;
  goal_summary?: string;
  industry_summary?: string;
  target_product_summary?: string;
  competitor_summary?: string;
  schema_summary?: string;
  revision_number?: number;
  supplement_request?: string;
  confirmed_at?: string;
  updated_at?: string;
};

export type WorkspaceObservability = {
  events?: WorkspaceEvent[];
  tool_events?: Array<Record<string, unknown>>;
  todo_plan?: Record<string, unknown>;
  todo_events?: WorkspaceEvent[];
  hook_events?: WorkspaceEvent[];
  stage_logs?: Record<string, unknown>;
  agent_traces?: AgentTrace[];
  manual_interventions?: Array<Record<string, unknown>>;
  log_download_path?: string;
};

export type WorkspacePayload = {
  summary?: WorkspaceSummary;
  request?: WorkspaceRequest;
  run?: WorkspaceRun;
  workflow?: {
    dag?: WorkflowDag;
    timeline?: WorkflowTimelineItem[];
    agent_stages?: AgentStageCard[];
    agent_workflows?: Record<string, AgentWorkflow>;
    agent_handoffs?: AgentHandoff[];
    plan_confirmation?: WorkflowPlanConfirmation;
    handoffs?: Array<Record<string, unknown>>;
  };
  qa?: WorkspaceQa;
  report?: WorkspaceReport;
  questionnaire?: WorkspaceQuestionnaire | null;
  questionnaire_export?: WorkspaceQuestionnaireExport | null;
  chat?: Record<string, unknown>;
  artifacts?: WorkspaceArtifacts;
  todo_plan?: Record<string, unknown>;
  observability?: WorkspaceObservability;
};
