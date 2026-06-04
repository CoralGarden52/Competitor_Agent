export type StageName =
  | "plan"
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
  created_at?: string;
  updated_at?: string;
};

export type WorkspaceRequest = {
  industry?: string;
  user_prompt?: string;
  competitors?: string[];
  language?: string;
  timeframe?: string;
};

export type WorkspaceRun = {
  run_id?: string;
  status?: string;
  industry?: string;
  planned_competitors?: string[];
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
  }>;
};

export type WorkspaceReport = {
  markdown?: string;
  sources?: string[];
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
    handoffs?: Array<Record<string, unknown>>;
  };
  qa?: WorkspaceQa;
  report?: WorkspaceReport;
  questionnaire?: WorkspaceQuestionnaire | null;
  questionnaire_export?: WorkspaceQuestionnaireExport | null;
  artifacts?: WorkspaceArtifacts;
  todo_plan?: Record<string, unknown>;
  observability?: WorkspaceObservability;
};
