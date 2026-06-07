"use client";

import { startTransition, type FormEvent, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Bot, History, MessageSquarePlus } from "lucide-react";
import { EventStreamPanel } from "@/components/event-stream-panel";
import { fieldLabelZh } from "@/components/field-labels";
import { ReportPreviewPanel } from "@/components/report-preview-panel";
import { StageDetailPanel } from "@/components/stage-detail-panel";
import { WorkspaceDagBoard } from "@/components/workspace-dag-board";
import {
  deriveDraftPreviewFromEvents,
  DRAFT_STREAM_WARNING_MESSAGE,
  resolveDraftStreamError,
} from "@/components/draft-stream-state";
import type {
  AgentHandoff,
  AgentStageCard,
  AgentTrace,
  AnalyzeCardCompetitorSummaryPayload,
  AnalyzeCardFieldSummaryPayload,
  CollectCardSourceFoundPayload,
  ConfirmPlanCardSummaryPayload,
  AgentWorkflow,
  EvidenceItem,
  PlanCardCompetitorsPayload,
  PlanCardSchemaPayload,
  QaCardFinalSummaryPayload,
  QaCardReviewStartedPayload,
  QaCardReviewSummaryPayload,
  StageName,
  WorkspaceEvent,
  WorkspacePayload,
  WorkspaceQuestionnaire,
  WorkspaceQuestionnaireExport,
  WorkspaceReportBlock,
} from "@/components/workspace-types";

type ViewMode = "welcome" | "workspace";

type RunStatusResponse = { state?: { status?: string } };
type RunListItem = { run_id: string; industry: string; status: string; competitor_count: number; user_prompt?: string; task_summary?: string; created_at: string; updated_at: string };
type ChatMessage = { id: string; role: "user" | "assistant" | "system"; content: string };
type ReportChatPayload = {
  run_id: string;
  conversation?: { conversation_id?: string };
  messages?: Array<{ message_id?: string; turn_id?: string; role?: "user" | "assistant" | "system" | "tool"; content?: string; created_at?: string }>;
  turns?: Array<{ turn_id?: string; status?: string }>;
  memory?: { next_work_memory?: string; mid_summary?: string };
  report_revisions?: Array<{ revision_id?: string; patch_summary?: string; created_at?: string }>;
};
type ChatTurnResponse = { run_id: string; conversation_id: string; turn_id: string; status: string; message: string };
type ChatTurnResult = {
  status: string;
  assistant_answer?: string;
  actions_taken?: string[];
  report_updated?: boolean;
  report_revision_id?: string;
  source_refs?: string[];
  error_message?: string;
};
type ReferenceLinkItem = { label: string; url: string };

type StoredSession = {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  active_run_id: string;
  task_summary: string;
  chat_messages: ChatMessage[];
  workspace_snapshot: WorkspacePayload | null;
};

type StreamState = "idle" | "streaming" | "reconnecting" | "polling";

type AgentCardStreamState = {
  lines: string[];
  urls?: Array<{ label: string; url: string }>;
  totalCount?: number;
  isStreaming?: boolean;
};

type AgentCardStreams = Partial<Record<StageName, AgentCardStreamState>>;

const STAGE_ORDER: StageName[] = ["plan", "confirm_plan", "collect", "normalize", "analyze", "qa", "draft", "finalize"];
const BACKEND_BASE_URL = (process.env.NEXT_PUBLIC_BACKEND_URL || "http://127.0.0.1:8010").replace(/\/$/, "");
const PENDING_TASK_TITLE = "生成标题中";
const COLLECT_PREVIEW_LIMIT = 6;

function backendUrl(path: string) {
  return `${BACKEND_BASE_URL}${path}`;
}

function isStageName(value: string): value is StageName {
  return STAGE_ORDER.includes(value as StageName);
}

function makeEventKey(event: WorkspaceEvent, index: number): string {
  if (typeof event.event_id === "number") return `event:${event.event_id}`;
  return `${event.created_at || "unknown"}:${event.stage || "none"}:${event.event_type || "event"}:${index}`;
}

function mergeEvents(previous: WorkspaceEvent[], incoming: WorkspaceEvent[]): WorkspaceEvent[] {
  const merged = [...previous, ...incoming];
  const seen = new Set<string>();
  const deduped: WorkspaceEvent[] = [];

  for (let index = merged.length - 1; index >= 0; index -= 1) {
    const item = merged[index];
    const key = makeEventKey(item, index);
    if (seen.has(key)) continue;
    seen.add(key);
    deduped.push(item);
  }

  deduped.reverse();
  deduped.sort((left, right) => {
    const leftId = typeof left.event_id === "number" ? left.event_id : -1;
    const rightId = typeof right.event_id === "number" ? right.event_id : -1;
    if (leftId >= 0 && rightId >= 0 && leftId !== rightId) return leftId - rightId;
    return (left.created_at || "").localeCompare(right.created_at || "");
  });
  return deduped.slice(-300);
}

function getDefaultSelectedStage(workspace: WorkspacePayload | null): StageName | null {
  const stages = workspace?.workflow?.agent_stages ?? [];
  const runningStage = stages.find((item) => item.status === "running" && isStageName(item.stage));
  if (runningStage && isStageName(runningStage.stage)) return runningStage.stage;

  const completedStages = stages.filter((item) => item.status === "completed" && isStageName(item.stage));
  if (completedStages.length) {
    const lastCompleted = completedStages[completedStages.length - 1];
    if (isStageName(lastCompleted.stage)) return lastCompleted.stage;
  }

  const dagNodes = workspace?.workflow?.dag?.nodes ?? [];
  const firstDagNode = dagNodes.find((item) => isStageName(item));
  return firstDagNode ?? null;
}

function getStageCard(workspace: WorkspacePayload | null, stage: StageName | null): AgentStageCard | null {
  if (!workspace || !stage) return null;
  return workspace.workflow?.agent_stages?.find((item) => item.stage === stage) ?? null;
}

function getStageWorkflow(workspace: WorkspacePayload | null, stage: StageName | null): AgentWorkflow | null {
  if (!workspace || !stage) return null;
  return workspace.workflow?.agent_workflows?.[stage] ?? null;
}

function getStageHandoff(workspace: WorkspacePayload | null, stage: StageName | null): AgentHandoff | null {
  if (!workspace || !stage) return null;
  return workspace.workflow?.agent_handoffs?.find((item) => item.stage === stage) ?? null;
}

function getStageTrace(workspace: WorkspacePayload | null, stage: StageName | null): AgentTrace | null {
  if (!workspace || !stage) return null;
  return workspace.observability?.agent_traces?.find((item) => item.stage === stage) ?? null;
}

function stageLabel(stage: string): string {
  const map: Record<string, string> = {
    plan: "计划智能体",
    confirm_plan: "确认节点",
    collect: "采集智能体",
    normalize: "标准化阶段",
    analyze: "分析智能体",
    qa: "QA智能体",
    draft: "写作智能体",
    finalize: "完成阶段",
  };
  return map[stage] || stage;
}

function unwrapEventPayload(event: WorkspaceEvent): Record<string, unknown> {
  const payload = event.payload;
  if (!payload || typeof payload !== "object") return {};
  if ("snapshot" in payload && payload.snapshot && typeof payload.snapshot === "object") {
    const outputPayload = (payload.snapshot as { output_payload?: unknown }).output_payload;
    if (outputPayload && typeof outputPayload === "object") return outputPayload as Record<string, unknown>;
  }
  if ("envelope" in payload && payload.envelope && typeof payload.envelope === "object") {
    const envelopePayload = (payload.envelope as { payload?: unknown }).payload;
    if (envelopePayload && typeof envelopePayload === "object") return envelopePayload as Record<string, unknown>;
  }
  return payload;
}

function dedupeLines(lines: string[]): string[] {
  const seen = new Set<string>();
  return lines.filter((line) => {
    const key = line.trim();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function trimLines(lines: string[], limit = 20): string[] {
  return dedupeLines(lines).slice(-limit);
}

function stripLinksFromSummary(value: string): string {
  return String(value || "")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/gi, "$1")
    .replace(/https?:\/\/\S+/gi, "")
    .replace(/\s+/g, " ")
    .trim();
}

function mergeCardUrls(
  previous: Array<{ label: string; url: string }> | undefined,
  nextItem: { label: string; url: string },
  limit = 20,
): Array<{ label: string; url: string }> {
  const current = previous ?? [];
  if (!nextItem.url.trim()) return current;
  if (current.some((item) => item.url === nextItem.url)) return current;
  return [...current, nextItem].slice(0, limit);
}

function toStepStatus(status?: string): "pending" | "running" | "done" | "failed" {
  if (status === "completed") return "done";
  if (status === "awaiting_user_confirmation" || status === "replanning") return "running";
  if (status === "running") return "running";
  if (status === "failed") return "failed";
  return "pending";
}

function stepStatusText(status: "pending" | "running" | "done" | "failed"): string {
  if (status === "running") return "执行中";
  if (status === "done") return "已完成";
  if (status === "failed") return "失败";
  return "待执行";
}

function isReportFollowupMessage(message: ChatMessage): boolean {
  return (
    message.id.startsWith("report-chat:") ||
    message.id.startsWith("local-report-chat:") ||
    message.id.startsWith("pending-report-chat:")
  );
}

type ParsedAssistantContent = {
  answer: string;
  operations: string;
  basisLines: string[];
  webUrls: string[];
  corpusLines: string[];
  reportLines: string[];
};

function isUrl(value: string): boolean {
  return /^https?:\/\//i.test(value.trim());
}

function parseAssistantContent(content: string): ParsedAssistantContent {
  const lines = content.split(/\r?\n/);
  const answerLines: string[] = [];
  const basisLines: string[] = [];
  const webUrls: string[] = [];
  const corpusLines: string[] = [];
  const reportLines: string[] = [];
  let operations = "";
  let section: "answer" | "basis" | "web" | "corpus" | "report" = "answer";

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      if (section === "answer" && answerLines.length) answerLines.push("");
      continue;
    }
    if (line.startsWith("本轮操作：")) {
      operations = line.replace(/^本轮操作：/, "").trim();
      section = "basis";
      continue;
    }
    if (line === "本轮依据：") {
      section = "basis";
      continue;
    }
    if (line === "新采集网页：") {
      section = "web";
      continue;
    }
    if (line === "本地语料：") {
      section = "corpus";
      continue;
    }
    if (line === "报告上下文：") {
      section = "report";
      continue;
    }

    if (section === "answer") {
      answerLines.push(line);
    } else if (section === "basis") {
      basisLines.push(line);
    } else if (section === "web") {
      const cleaned = line.replace(/^\d+\.\s*/, "").trim();
      if (isUrl(cleaned)) webUrls.push(cleaned);
    } else if (section === "corpus") {
      corpusLines.push(line);
    } else if (section === "report") {
      reportLines.push(line);
    }
  }

  return { answer: answerLines.join("\n").trim(), operations, basisLines, webUrls, corpusLines, reportLines };
}

function ChatMessageContent({ message }: { message: ChatMessage }) {
  if (message.role !== "assistant") {
    return <>{message.content}</>;
  }
  const parsed = parseAssistantContent(message.content);
  const hasMeta = Boolean(parsed.operations || parsed.basisLines.length || parsed.webUrls.length || parsed.corpusLines.length || parsed.reportLines.length);
  if (!hasMeta) {
    return <>{message.content}</>;
  }
  return (
    <div className="assistant-rich-message">
      {parsed.answer ? <div className="assistant-answer">{parsed.answer}</div> : null}
      <section className="assistant-evidence-card" aria-label="本轮操作和依据">
        {parsed.operations ? (
          <div className="assistant-evidence-block">
            <h3>本轮操作</h3>
            <p>{parsed.operations}</p>
          </div>
        ) : null}
        {parsed.basisLines.length ? (
          <div className="assistant-evidence-block">
            <h3>本轮依据</h3>
            <ul>
              {parsed.basisLines.map((line, index) => <li key={`basis-${index}`}>{line.replace(/^-\s*/, "")}</li>)}
            </ul>
          </div>
        ) : null}
        {parsed.webUrls.length ? (
          <div className="assistant-evidence-block">
            <h3>新采集网页</h3>
            <ol>
              {parsed.webUrls.map((url) => (
                <li key={url}>
                  <a href={url} target="_blank" rel="noreferrer">{url}</a>
                </li>
              ))}
            </ol>
          </div>
        ) : null}
        {parsed.corpusLines.length ? (
          <div className="assistant-evidence-block compact">
            <h3>本地语料</h3>
            <ol>
              {parsed.corpusLines.map((line, index) => <li key={`corpus-${index}`}>{line.replace(/^\d+\.\s*/, "")}</li>)}
            </ol>
          </div>
        ) : null}
        {parsed.reportLines.length ? (
          <div className="assistant-evidence-block compact">
            <h3>报告上下文</h3>
            <ol>
              {parsed.reportLines.map((line, index) => <li key={`report-${index}`}>{line.replace(/^\d+\.\s*/, "")}</li>)}
            </ol>
          </div>
        ) : null}
      </section>
    </div>
  );
}

type HomeWorkspaceProps = {
  initialRunId?: string;
};

export function HomeWorkspace({ initialRunId = "" }: HomeWorkspaceProps) {
  const router = useRouter();
  const params = useParams<{ runId?: string }>();
  const routeRunId = typeof params?.runId === "string" ? params.runId : "";
  const requestedInitialRunId = (routeRunId || initialRunId).trim();
  const [activeMenu, setActiveMenu] = useState<"new" | "agent" | "history">(requestedInitialRunId ? "history" : "new");
  const [viewMode, setViewMode] = useState<ViewMode>(requestedInitialRunId ? "workspace" : "welcome");
  const [query, setQuery] = useState("");
  const [competitorHintsText, setCompetitorHintsText] = useState("");
  const [aspectHintsText, setAspectHintsText] = useState("");
  const [workspaceData, setWorkspaceData] = useState<WorkspacePayload | null>(null);
  const [selectedStage, setSelectedStage] = useState<StageName | null>(null);
  const [eventFeed, setEventFeed] = useState<WorkspaceEvent[]>([]);
  const [agentCardStreams, setAgentCardStreams] = useState<AgentCardStreams>({});
  const [streamState, setStreamState] = useState<StreamState>("idle");
  const [eventFilterMode, setEventFilterMode] = useState<"all" | "stage">("all");
  const [expandedEventKeys, setExpandedEventKeys] = useState<string[]>([]);
  const [expandedCallKeys, setExpandedCallKeys] = useState<string[]>([]);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [isReportChatSubmitting, setIsReportChatSubmitting] = useState(false);
  const [sessions, setSessions] = useState<StoredSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState("");
  const [isManagingSessions, setIsManagingSessions] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState<string[]>([]);
  const [openSessionMenuId, setOpenSessionMenuId] = useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [isEditingReport, setIsEditingReport] = useState(false);
  const [isDraftStreaming, setIsDraftStreaming] = useState(false);
  const [draftStreamError, setDraftStreamError] = useState("");
  const [reportSourceRunId, setReportSourceRunId] = useState("");
  const [originalReportContent, setOriginalReportContent] = useState("");
  const [reportDraft, setReportDraft] = useState("");
  const [questionnaireOpen, setQuestionnaireOpen] = useState(false);
  const [isEditingQuestionnaire, setIsEditingQuestionnaire] = useState(false);
  const [isGeneratingQuestionnaire, setIsGeneratingQuestionnaire] = useState(false);
  const [isSavingReport, setIsSavingReport] = useState(false);
  const [isSavingQuestionnaire, setIsSavingQuestionnaire] = useState(false);
  const [isExportingQuestionnaire, setIsExportingQuestionnaire] = useState(false);
  const [questionnaireExport, setQuestionnaireExport] = useState<WorkspaceQuestionnaireExport | null>(null);
  const [questionnaireDraft, setQuestionnaireDraft] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [planSupplementText, setPlanSupplementText] = useState("");
  const [isSubmittingPlanConfirmation, setIsSubmittingPlanConfirmation] = useState(false);
  const [error, setError] = useState("");

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const reportChatStreamRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const pollingTimerRef = useRef<number | null>(null);
  const sessionMenuRef = useRef<HTMLDivElement | null>(null);
  const activeRunIdRef = useRef<string>("");
  const reconnectAttemptRef = useRef(0);

  const maxReconnectAttempts = 4;
  const maxSessionCount = 30;
  const sessionChatStoragePrefix = "home_workspace_chat_messages:";
  const activeRunId = (workspaceData?.run?.run_id || activeRunIdRef.current || "").trim();
  const planConfirmation = workspaceData?.workflow?.plan_confirmation;
  const showPlanConfirmationDialog = planConfirmation?.status === "awaiting_user_confirmation";
  const activeSession =
    sessions.find((item) => item.session_id === activeSessionId)
    || sessions.find((item) => item.active_run_id === activeRunId)
    || null;
  const visibleSessions = sessions.filter(shouldShowSession);
  const activeSessionTaskSummary = (activeSession?.task_summary || "").trim();
  const displaySummary = activeSessionTaskSummary.replace(/^任务(目标|分析)\s*[:：]\s*/u, "").trim();
  const stageCards = workspaceData?.workflow?.agent_stages ?? [];
  const selectedStageCard = getStageCard(workspaceData, selectedStage);
  const selectedStageWorkflow = getStageWorkflow(workspaceData, selectedStage);
  const selectedStageHandoff = getStageHandoff(workspaceData, selectedStage);
  const selectedStageTrace = getStageTrace(workspaceData, selectedStage);
  const selectedStageEvents = selectedStage
    ? eventFeed.filter((item) => item.stage === selectedStage)
    : [];
  const qaStageCard = getStageCard(workspaceData, "qa");
  const draftStageCard = getStageCard(workspaceData, "draft");
  const derivedDraftState = deriveDraftPreviewFromEvents(eventFeed, workspaceData);
  const persistedBlocks = workspaceData?.report?.blocks ?? [];
  const currentReportBlocks: WorkspaceReportBlock[] = derivedDraftState.blocks.length
    ? derivedDraftState.blocks
    : persistedBlocks;
  const reportHtml = String(workspaceData?.report?.html || "").trim();
  const hasReport = Boolean(
    workspaceData
    && (
      (workspaceData.report?.markdown || "").trim()
      || reportHtml
      || persistedBlocks.length
    ),
  );
  const hasStreamingReportPreview = Boolean(reportDraft.trim() || currentReportBlocks.length);
  const shouldShowReportCard = Boolean(
    hasReport
    || hasStreamingReportPreview
    || draftStreamError
    || isDraftStreaming
    || qaStageCard?.status === "completed"
    || draftStageCard?.status === "running"
    || draftStageCard?.status === "completed"
    || draftStageCard?.status === "failed"
  );
  const referenceItems = workspaceData ? buildReferenceItems(workspaceData) : [];
  const collectPreviewItems = referenceItems.slice(0, COLLECT_PREVIEW_LIMIT);
  const currentReportContent = isEditingReport ? reportDraft : reportDraft || workspaceData?.report?.markdown || "";
  const reportCardStatusText = hasReport
    ? "Structured report"
    : isDraftStreaming
      ? "报告生成中..."
      : draftStreamError
        ? "预览中断，等待最终报告..."
        : draftStageCard?.status === "running"
          ? "写作阶段进行中，等待流式输出..."
          : draftStageCard?.status === "completed"
            ? "报告已生成，等待同步..."
            : "报告待生成";
  const reportPreviewBody = currentReportContent
    || (isDraftStreaming
      ? "正在生成报告..."
      : draftStreamError
        ? draftStreamError
        : "报告尚未开始生成，进入写作阶段后会在这里流式输出。");
  const currentQuestionnaireContent = isEditingQuestionnaire
    ? questionnaireDraft
    : questionnaireDraft || workspaceData?.questionnaire?.markdown || "";
  const hasQuestionnaire = Boolean(workspaceData?.questionnaire?.markdown?.trim());
  const currentQuestionnaireExport = questionnaireExport || workspaceData?.questionnaire_export || null;
  const topChatMessages = chatMessages.filter((message) => !isReportFollowupMessage(message));
  const reportFollowupMessages = chatMessages.filter(isReportFollowupMessage);
  const isRouteSessionLoading = Boolean(requestedInitialRunId && viewMode === "workspace" && !workspaceData && !error && !activeSessionTaskSummary && chatMessages.length === 0);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (!sessionMenuRef.current) return;
      if (!sessionMenuRef.current.contains(event.target as Node)) setOpenSessionMenuId(null);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  useEffect(() => {
    const runId = workspaceData?.run?.run_id || "";
    const incomingReport = workspaceData?.report?.markdown || "";
    if (!runId) return;

    if (runId !== reportSourceRunId) {
      setReportSourceRunId(runId);
      setOriginalReportContent(incomingReport);
      setReportDraft(incomingReport);
      setIsEditingReport(false);
      setDraftStreamError("");
      return;
    }

    if (!isEditingReport && !isDraftStreaming && reportDraft === originalReportContent && incomingReport !== originalReportContent) {
      setOriginalReportContent(incomingReport);
      setReportDraft(incomingReport);
    }
    if (incomingReport && isDraftStreaming) {
      setOriginalReportContent(incomingReport);
      setReportDraft(incomingReport);
      setIsDraftStreaming(false);
      setDraftStreamError("");
    }
    if (incomingReport && draftStreamError) {
      setDraftStreamError("");
    }
  }, [workspaceData?.run?.run_id, workspaceData?.report?.markdown, reportSourceRunId, isEditingReport, isDraftStreaming, reportDraft, originalReportContent]);

  useEffect(() => {
    const runId = workspaceData?.run?.run_id || "";
    if (!runId) {
      setAgentCardStreams({});
      return;
    }
    setAgentCardStreams({});
    const relevantEvents = (workspaceData?.observability?.events ?? []).filter((event) => {
      const eventType = String(event.event_type || "").trim();
      return eventType.includes(".card.");
    });
    if (!relevantEvents.length) return;
    relevantEvents.forEach((event) => applyAgentCardEvent(event));
    if (workspaceData) finalizeAgentCardStreams(workspaceData);
  }, [workspaceData?.run?.run_id]);

  useEffect(() => {
    if (!showPlanConfirmationDialog) {
      setPlanSupplementText("");
    }
  }, [showPlanConfirmationDialog, workspaceData?.run?.run_id]);

  useEffect(() => {
    const runId = workspaceData?.run?.run_id || "";
    if (!runId || isEditingReport) return;

    if (!derivedDraftState.hasActivity) return;

    if (isDraftStreaming !== derivedDraftState.isStreaming) {
      setIsDraftStreaming(derivedDraftState.isStreaming);
    }
    const resolvedError = resolveDraftStreamError(workspaceData, derivedDraftState.error);
    if (draftStreamError !== resolvedError) {
      setDraftStreamError(resolvedError);
    }
    if (derivedDraftState.markdown && reportDraft !== derivedDraftState.markdown) {
      setReportDraft(derivedDraftState.markdown);
    }
  }, [workspaceData, derivedDraftState, isEditingReport, isDraftStreaming, draftStreamError, reportDraft]);

  useEffect(() => {
    if (isEditingQuestionnaire) return;
    setQuestionnaireDraft(workspaceData?.questionnaire?.markdown || "");
  }, [workspaceData?.run?.run_id, workspaceData?.questionnaire?.markdown, isEditingQuestionnaire]);

  useEffect(() => {
    const runId = workspaceData?.run?.run_id || "";
    if (!runId) {
      return;
    }
    let canceled = false;
    void (async () => {
      try {
        const payload = await fetchRunChat(runId);
        if (!canceled) applyReportChatPayload(runId, payload);
      } catch {
        // keep existing local messages if chat history is unavailable
      }
    })();
    return () => {
      canceled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceData?.run?.run_id]);

  useEffect(() => {
    setQuestionnaireExport(workspaceData?.questionnaire_export || null);
  }, [workspaceData?.run?.run_id, workspaceData?.questionnaire_export]);

  function nowIso() {
    return new Date().toISOString();
  }

  function pushRunRoute(runId: string) {
    if (!runId.trim()) return;
    router.push(`/chats/${runId}` as never);
  }

  function pushHomeRoute() {
    router.push("/" as never);
  }

  function isActiveRun(runId: string): boolean {
    return Boolean(runId) && activeRunIdRef.current === runId;
  }

  function downloadReport(content: string, runId: string) {
    downloadMarkdown(content, `report_${(runId || "latest").slice(0, 16)}`);
  }

  function downloadQuestionnaire(content: string, runId: string) {
    downloadMarkdown(content, `questionnaire_${(runId || "latest").slice(0, 16)}`);
  }

  function downloadMarkdown(content: string, filenameStem: string) {
    if (!content.trim()) return;
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const objectUrl = window.URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = `${filenameStem}.md`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.URL.revokeObjectURL(objectUrl);
  }

  function startReportEdit() {
    setReportDraft(workspaceData?.report?.markdown || originalReportContent || "");
    setPreviewOpen(true);
    setIsEditingReport(true);
  }

  function cancelReportEdit() {
    setReportDraft(workspaceData?.report?.markdown || originalReportContent || "");
    setIsEditingReport(false);
  }

  function closeReportPreview() {
    if (isSavingReport) return;
    if (isEditingReport) {
      cancelReportEdit();
    }
    setPreviewOpen(false);
  }

  async function handleSaveReport() {
    const runId = activeRunIdRef.current;
    const markdown = reportDraft.trim();
    if (!runId || !markdown) {
      setError("报告内容不能为空。");
      return;
    }
    setError("");
    setIsSavingReport(true);
    try {
      const response = await fetch(backendUrl(`/runs/${runId}/report`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ markdown: reportDraft }),
      });
      const payload = (await response.json().catch(() => ({}))) as {
        detail?: string;
        state?: {
          report?: {
            markdown?: string;
            html?: string;
            appendix_sources?: string[];
            blocks?: WorkspaceReportBlock[];
            citations?: Array<Record<string, unknown>>;
            render_version?: string;
          };
        };
      };
      if (!response.ok) {
        throw new Error(payload.detail || "报告保存失败，请稍后重试。");
      }
      const savedReport = payload.state?.report;
      const savedMarkdown = savedReport?.markdown ?? reportDraft;
      const savedSources = savedReport?.appendix_sources;
      const savedHtml = savedReport?.html ?? "";
      const savedBlocks = savedReport?.blocks ?? [];
      const savedCitations = savedReport?.citations ?? [];
      const savedRenderVersion = savedReport?.render_version ?? "v2_structured_manual_markdown";
      setWorkspaceData((previous) => {
        if (!previous) return previous;
        return {
          ...previous,
          report: {
            ...(previous.report || {}),
            markdown: savedMarkdown,
            html: savedHtml,
            sources: savedSources ?? previous.report?.sources ?? [],
            blocks: savedBlocks,
            citations: savedCitations,
            render_version: savedRenderVersion,
          },
        };
      });
      setSessions((previous) =>
        previous.map((session) => {
          if (session.active_run_id !== runId || !session.workspace_snapshot) return session;
          return {
            ...session,
            updated_at: nowIso(),
            workspace_snapshot: {
              ...session.workspace_snapshot,
              report: {
                ...(session.workspace_snapshot.report || {}),
                markdown: savedMarkdown,
                html: savedHtml,
                sources: savedSources ?? session.workspace_snapshot.report?.sources ?? [],
                blocks: savedBlocks,
                citations: savedCitations,
                render_version: savedRenderVersion,
              },
            },
          };
        })
      );
      setOriginalReportContent(savedMarkdown);
      setReportDraft(savedMarkdown);
      setIsEditingReport(false);
    } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : "报告保存失败，请稍后重试。";
      setError(message);
    } finally {
      setIsSavingReport(false);
    }
  }

  function syncQuestionnaireSnapshot(runId: string, questionnaire: WorkspaceQuestionnaire) {
    setQuestionnaireExport(null);
    setWorkspaceData((previous) => {
      if (!previous) return previous;
      return {
        ...previous,
        questionnaire: {
          ...(previous.questionnaire || {}),
          ...questionnaire,
        },
        questionnaire_export: null,
      };
    });
    setSessions((previous) =>
      previous.map((session) => {
        if (session.active_run_id !== runId || !session.workspace_snapshot) return session;
        return {
          ...session,
          updated_at: nowIso(),
          workspace_snapshot: {
            ...session.workspace_snapshot,
            questionnaire: {
              ...(session.workspace_snapshot.questionnaire || {}),
              ...questionnaire,
            },
            questionnaire_export: null,
          },
        };
      })
    );
  }

  function syncQuestionnaireExportSnapshot(runId: string, exportResult: WorkspaceQuestionnaireExport) {
    setWorkspaceData((previous) => {
      if (!previous) return previous;
      return {
        ...previous,
        questionnaire_export: exportResult,
      };
    });
    setSessions((previous) =>
      previous.map((session) => {
        if (session.active_run_id !== runId || !session.workspace_snapshot) return session;
        return {
          ...session,
          updated_at: nowIso(),
          workspace_snapshot: {
            ...session.workspace_snapshot,
            questionnaire_export: exportResult,
          },
        };
      })
    );
  }

  async function handleGenerateQuestionnaire() {
    const runId = activeRunIdRef.current;
    if (!runId) return;
    setError("");
    setIsGeneratingQuestionnaire(true);
    try {
      const response = await fetch(backendUrl(`/runs/${runId}/questionnaire`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_audience: "竞品相关潜在用户或现有用户",
          objective: "验证竞品差异点、用户感知与转化障碍",
        }),
      });
      const payload = (await response.json().catch(() => ({}))) as WorkspaceQuestionnaire & { detail?: string };
      if (!response.ok) {
        throw new Error(payload.detail || "问卷生成失败，请稍后重试。");
      }
      syncQuestionnaireSnapshot(runId, payload);
      setQuestionnaireDraft(payload.markdown || "");
      setIsEditingQuestionnaire(false);
      setQuestionnaireOpen(true);
    } catch (questionnaireError) {
      const message = questionnaireError instanceof Error ? questionnaireError.message : "问卷生成失败，请稍后重试。";
      setError(message);
    } finally {
      setIsGeneratingQuestionnaire(false);
    }
  }

  function openQuestionnairePreview() {
    setQuestionnaireDraft(workspaceData?.questionnaire?.markdown || "");
    setIsEditingQuestionnaire(false);
    setQuestionnaireOpen(true);
  }

  function startQuestionnaireEdit() {
    setQuestionnaireDraft(workspaceData?.questionnaire?.markdown || currentQuestionnaireContent || "");
    setQuestionnaireOpen(true);
    setIsEditingQuestionnaire(true);
  }

  function cancelQuestionnaireEdit() {
    setQuestionnaireDraft(workspaceData?.questionnaire?.markdown || "");
    setIsEditingQuestionnaire(false);
  }

  function closeQuestionnairePreview() {
    if (isSavingQuestionnaire) return;
    if (isEditingQuestionnaire) {
      cancelQuestionnaireEdit();
    }
    setQuestionnaireOpen(false);
  }

  async function handleSaveQuestionnaire() {
    const runId = activeRunIdRef.current;
    const markdown = questionnaireDraft.trim();
    if (!runId || !markdown) {
      setError("问卷内容不能为空。");
      return;
    }
    setError("");
    setIsSavingQuestionnaire(true);
    try {
      const response = await fetch(backendUrl(`/runs/${runId}/questionnaire`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ markdown: questionnaireDraft }),
      });
      const payload = (await response.json().catch(() => ({}))) as WorkspaceQuestionnaire & { detail?: string };
      if (!response.ok) {
        throw new Error(payload.detail || "问卷保存失败，请稍后重试。");
      }
      syncQuestionnaireSnapshot(runId, payload);
      setQuestionnaireDraft(payload.markdown || questionnaireDraft);
      setIsEditingQuestionnaire(false);
    } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : "问卷保存失败，请稍后重试。";
      setError(message);
    } finally {
      setIsSavingQuestionnaire(false);
    }
  }

  async function handleExportQuestionnaireToWjx() {
    const runId = activeRunIdRef.current;
    if (!runId) return;
    setError("");
    setIsExportingQuestionnaire(true);
    try {
      const response = await fetch(backendUrl(`/runs/${runId}/questionnaire/export/wenjuan`), {
        method: "POST",
      });
      const payload = (await response.json().catch(() => ({}))) as WorkspaceQuestionnaireExport & { detail?: string };
      if (!response.ok) {
        throw new Error(payload.detail || "导出到问卷星失败，请稍后重试。");
      }
      setQuestionnaireExport(payload);
      syncQuestionnaireExportSnapshot(runId, payload);
    } catch (exportError) {
      const message = exportError instanceof Error ? exportError.message : "导出到问卷星失败，请稍后重试。";
      setError(message);
    } finally {
      setIsExportingQuestionnaire(false);
    }
  }

  async function handleConfirmPlan() {
    const runId = activeRunIdRef.current;
    if (!runId) return;
    setError("");
    setIsSubmittingPlanConfirmation(true);
    try {
      await confirmPlanAndContinue(runId);
      const workspace = await fetchRunWorkspace(runId);
      applyWorkspace(workspace);
      startRunStream(runId);
    } catch (confirmationError) {
      const message = confirmationError instanceof Error ? confirmationError.message : "确认采集结果失败，请稍后重试。";
      setError(message);
    } finally {
      setIsSubmittingPlanConfirmation(false);
    }
  }

  async function handleSubmitPlanSupplement() {
    const runId = activeRunIdRef.current;
    const message = planSupplementText.trim();
    if (!runId || !message) return;
    setError("");
    setIsSubmittingPlanConfirmation(true);
    try {
      await submitPlanSupplement(runId, message);
      setPlanSupplementText("");
      const workspace = await fetchRunWorkspace(runId);
      applyWorkspace(workspace);
      startRunStream(runId);
    } catch (supplementError) {
      const messageText = supplementError instanceof Error ? supplementError.message : "补充要求提交失败，请稍后重试。";
      setError(messageText);
    } finally {
      setIsSubmittingPlanConfirmation(false);
    }
  }

  function parseHintList(input: string): string[] {
    const parts = String(input || "").split(/[,\n，、;；]+/);
    const output: string[] = [];
    const seen = new Set<string>();
    for (const part of parts) {
      const value = part.trim();
      if (!value) continue;
      const key = value.toLocaleLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      output.push(value);
    }
    return output;
  }

  function initialPromptMessageId(runId: string): string {
    return `initial-user-prompt:${runId}`;
  }

  function makeInitialPromptMessage(runId: string, prompt: string): ChatMessage {
    return { id: initialPromptMessageId(runId), role: "user", content: prompt };
  }

  function ensureInitialPromptMessage(runId: string, messages: ChatMessage[], prompt: string): ChatMessage[] {
    const normalizedRunId = runId.trim();
    const normalizedPrompt = prompt.trim();
    if (!normalizedRunId || !normalizedPrompt) return messages;

    const deduped = messages.filter(
      (item) =>
        item.id !== initialPromptMessageId(normalizedRunId) &&
        !(item.role === "user" && item.content.trim() === normalizedPrompt)
    );
    const initial = makeInitialPromptMessage(normalizedRunId, normalizedPrompt);
    return [initial, ...deduped];
  }

  function storageKeyForRun(runId: string): string {
    return `${sessionChatStoragePrefix}${runId}`;
  }

  function readStoredMessages(runId: string): ChatMessage[] {
    if (typeof window === "undefined" || !runId) return [];
    try {
      const raw = window.localStorage.getItem(storageKeyForRun(runId));
      if (!raw) return [];
      const parsed = JSON.parse(raw) as ChatMessage[];
      if (!Array.isArray(parsed)) return [];
      return parsed.filter((item) => typeof item?.content === "string" && (item.role === "user" || item.role === "assistant" || item.role === "system"));
    } catch {
      return [];
    }
  }

  function writeStoredMessages(runId: string, messages: ChatMessage[]) {
    if (typeof window === "undefined" || !runId) return;
    try {
      if (!messages.length) {
        window.localStorage.removeItem(storageKeyForRun(runId));
        return;
      }
      window.localStorage.setItem(storageKeyForRun(runId), JSON.stringify(messages));
    } catch {
      // ignore storage errors
    }
  }

  function clearStoredMessages(runId: string) {
    if (typeof window === "undefined" || !runId) return;
    try {
      window.localStorage.removeItem(storageKeyForRun(runId));
    } catch {
      // ignore storage errors
    }
  }

  function mergeSessionsPreserveState(mapped: StoredSession[], previous: StoredSession[]): StoredSession[] {
    const previousById = new Map(previous.map((item) => [item.session_id, item]));
    return mapped.map((item) => {
      const existing = previousById.get(item.session_id);
      const promptFromSnapshot = (existing?.workspace_snapshot?.request?.user_prompt || "").trim();
      if (!existing) {
        const cached = readStoredMessages(item.session_id);
        const title = resolveSessionTitle({
          runId: item.session_id,
          prompt: "",
          taskSummary: item.task_summary,
          currentTitle: item.title,
        });
        return cached.length ? { ...item, title, chat_messages: cached } : { ...item, title };
      }
      const taskSummary = item.task_summary || existing.task_summary;
      const title = resolveSessionTitle({
        runId: item.session_id,
        prompt: promptFromSnapshot,
        taskSummary,
        currentTitle: item.title || existing.title,
      });
      return {
        ...item,
        title,
        task_summary: taskSummary,
        chat_messages: existing.chat_messages.length ? existing.chat_messages : readStoredMessages(item.session_id),
        workspace_snapshot: existing.workspace_snapshot || item.workspace_snapshot,
      };
    });
  }

  function makeSessionFromRunSummary(run: RunListItem): StoredSession {
    const title = resolveSessionTitle({
      runId: run.run_id,
      prompt: run.user_prompt || "",
      taskSummary: run.task_summary || "",
      currentTitle: run.run_id,
    });
    return {
      session_id: run.run_id,
      title,
      created_at: run.created_at || nowIso(),
      updated_at: run.updated_at || run.created_at || nowIso(),
      active_run_id: run.run_id,
      task_summary: run.task_summary || "",
      chat_messages: [],
      workspace_snapshot: null,
    };
  }

  function makeSessionPlaceholder(runId: string): StoredSession {
    return makeSessionFromRunSummary({
      run_id: runId,
      industry: "",
      status: "running",
      competitor_count: 0,
      created_at: nowIso(),
      updated_at: nowIso(),
    });
  }

  function resolveSessionTitle(args: { runId: string; prompt?: string; taskSummary?: string; currentTitle?: string }): string {
    const taskSummary = (args.taskSummary || "").trim();
    return taskSummary || PENDING_TASK_TITLE;
  }

  async function fetchRuns(limit = maxSessionCount): Promise<RunListItem[]> {
    const response = await fetch(`/runs?limit=${limit}`);
    if (!response.ok) throw new Error(`runs list failed: ${response.status}`);
    return (await response.json()) as RunListItem[];
  }

  async function deleteRunById(runId: string): Promise<void> {
    const response = await fetch(`/runs/${runId}`, { method: "DELETE" });
    if (!response.ok) throw new Error(`delete run failed: ${response.status}`);
  }

  function stopPolling() {
    if (pollingTimerRef.current !== null) {
      window.clearInterval(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
  }

  function stopStream() {
    if (streamRef.current) {
      streamRef.current.close();
      streamRef.current = null;
    }
  }

  function stopReconnectTimer() {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }

  function stopRealtime() {
    stopStream();
    stopPolling();
    stopReconnectTimer();
    stopReportChatStream();
    setStreamState("idle");
  }

  function stopReportChatStream() {
    if (reportChatStreamRef.current) {
      reportChatStreamRef.current.close();
      reportChatStreamRef.current = null;
    }
  }

  async function fetchRunWorkspace(runId: string): Promise<WorkspacePayload> {
    const response = await fetch(`/runs/${runId}/workspace`);
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`workspace fetch failed: ${response.status}${body ? ` - ${body}` : ""}`);
    }
    return (await response.json()) as WorkspacePayload;
  }

  async function fetchRunStatus(runId: string): Promise<RunStatusResponse> {
    const response = await fetch(`/runs/${runId}`);
    if (!response.ok) throw new Error(`run status failed: ${response.status}`);
    return (await response.json()) as RunStatusResponse;
  }

  async function confirmPlanAndContinue(runId: string): Promise<void> {
    const response = await fetch(backendUrl(`/runs/${runId}/plan-confirmation/confirm`), { method: "POST" });
    const payload = (await response.json().catch(() => ({}))) as { detail?: string };
    if (!response.ok) throw new Error(payload.detail || "确认采集结果失败，请稍后重试。");
  }

  async function submitPlanSupplement(runId: string, message: string): Promise<void> {
    const response = await fetch(backendUrl(`/runs/${runId}/plan-confirmation/supplement`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    const payload = (await response.json().catch(() => ({}))) as { detail?: string };
    if (!response.ok) throw new Error(payload.detail || "补充要求提交失败，请稍后重试。");
  }

  function mapReportChatPayload(payload: ReportChatPayload): ChatMessage[] {
    const messages = payload.messages ?? [];
    return messages
      .filter((item) => typeof item.content === "string" && item.content.trim())
      .map((item) => ({
        id: `report-chat:${item.message_id || `${item.turn_id || "turn"}:${item.role || "assistant"}:${item.created_at || ""}`}`,
        role: item.role === "user" || item.role === "system" ? item.role : "assistant",
        content: item.content || "",
      }));
  }

  function syncMainChatMessages(runId: string, updater: (previous: ChatMessage[]) => ChatMessage[]) {
    setChatMessages((previous) => {
      const next = updater(previous);
      writeStoredMessages(runId, next);
      return next;
    });
    setSessions((previous) =>
      previous.map((session) => {
        if (session.session_id !== runId) return session;
        const nextMessages = updater(session.chat_messages || []);
        writeStoredMessages(runId, nextMessages);
        return { ...session, chat_messages: nextMessages, updated_at: nowIso() };
      })
    );
  }

  function applyReportChatPayload(runId: string, payload: ReportChatPayload) {
    const mapped = mapReportChatPayload(payload);
    syncMainChatMessages(runId, (previous) => [
      ...previous.filter((item) => !item.id.startsWith("report-chat:") && !item.id.startsWith("local-report-chat:") && !item.id.startsWith("pending-report-chat:")),
      ...mapped,
    ]);
  }

  async function fetchRunChat(runId: string): Promise<ReportChatPayload> {
    const response = await fetch(backendUrl(`/runs/${runId}/chat`));
    const payload = (await response.json().catch(() => ({}))) as ReportChatPayload & { detail?: string };
    if (!response.ok) throw new Error(payload.detail || `chat fetch failed: ${response.status}`);
    return payload;
  }

  async function refreshReportChat(runId: string) {
    const payload = await fetchRunChat(runId);
    applyReportChatPayload(runId, payload);
  }

  function updatePendingReportMessage(runId: string, pendingAssistantId: string, content: string) {
    syncMainChatMessages(runId, (previous) =>
      previous.map((item) => (item.id === pendingAssistantId ? { ...item, content } : item))
    );
  }

  function appendPendingReportMessage(runId: string, pendingAssistantId: string, delta: string) {
    if (!delta) return;
    syncMainChatMessages(runId, (previous) =>
      previous.map((item) => {
        if (item.id !== pendingAssistantId) return item;
        const current = item.content || "";
        const seed = current.startsWith("正在") ? "" : current;
        return { ...item, content: `${seed}${delta}` };
      })
    );
  }

  async function streamReportChatTurn(runId: string, turnId: string, pendingAssistantId: string): Promise<ChatTurnResult> {
    return await new Promise<ChatTurnResult>((resolve, reject) => {
      stopReportChatStream();
      const source = new window.EventSource(backendUrl(`/runs/${runId}/chat/${turnId}/stream`));
      reportChatStreamRef.current = source;
      let latestResult: ChatTurnResult = { status: "running" };
      let sawDelta = false;

      source.addEventListener("chat_progress", (event) => {
        try {
          const payload = JSON.parse((event as MessageEvent<string>).data) as { message?: string };
          if (!sawDelta && payload.message) updatePendingReportMessage(runId, pendingAssistantId, payload.message);
        } catch {
          return;
        }
      });

      source.addEventListener("chat_snapshot", (event) => {
        try {
          const payload = JSON.parse((event as MessageEvent<string>).data) as { assistant_answer?: string };
          if (payload.assistant_answer?.trim()) {
            sawDelta = true;
            updatePendingReportMessage(runId, pendingAssistantId, payload.assistant_answer);
          }
        } catch {
          return;
        }
      });

      source.addEventListener("chat_delta", (event) => {
        try {
          const payload = JSON.parse((event as MessageEvent<string>).data) as { delta?: string };
          if (payload.delta) {
            sawDelta = true;
            appendPendingReportMessage(runId, pendingAssistantId, payload.delta);
          }
        } catch {
          return;
        }
      });

      source.addEventListener("chat_done", (event) => {
        try {
          const payload = JSON.parse((event as MessageEvent<string>).data) as { result?: ChatTurnResult };
          latestResult = payload.result || { status: "completed" };
          stopReportChatStream();
          resolve(latestResult);
        } catch (error) {
          stopReportChatStream();
          reject(error instanceof Error ? error : new Error("chat stream parse failed"));
        }
      });

      source.addEventListener("chat_error", (event) => {
        try {
          const payload = JSON.parse((event as MessageEvent<string>).data) as { error?: string };
          stopReportChatStream();
          reject(new Error(payload.error || "报告追问处理失败。"));
        } catch {
          stopReportChatStream();
          reject(new Error("报告追问处理失败。"));
        }
      });

      source.addEventListener("error", () => {
        stopReportChatStream();
        reject(new Error("报告追问流中断，请稍后重试。"));
      });
    });
  }

  async function submitReportChatMessage(message: string) {
    const runId = activeRunIdRef.current;
    if (!runId || !message) return;
    setError("");
    setIsReportChatSubmitting(true);
    const pendingAssistantId = `pending-report-chat:${Date.now()}`;
    syncMainChatMessages(runId, (previous) => [
      ...previous,
      { id: `local-report-chat:user:${Date.now()}`, role: "user", content: message },
      { id: pendingAssistantId, role: "assistant", content: "正在读取报告、memory 和相关语料..." },
    ]);
    try {
      const response = await fetch(backendUrl(`/runs/${runId}/chat`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          mode: "answer_only",
          allow_web_collect: true,
          auto_apply: false,
        }),
      });
      const payload = (await response.json().catch(() => ({}))) as ChatTurnResponse & { detail?: string };
      if (!response.ok) throw new Error(payload.detail || "报告追问提交失败，请稍后重试。");

      const result = await streamReportChatTurn(runId, payload.turn_id, pendingAssistantId);
      await refreshReportChat(runId);
      if (result.report_updated) {
        const workspace = await fetchRunWorkspace(runId);
        applyWorkspace(workspace);
        setOriginalReportContent(workspace.report?.markdown || "");
        setReportDraft(workspace.report?.markdown || "");
      }
      if (result.status === "failed") {
        setError(result.error_message || "报告追问处理失败。");
      }
    } catch (chatError) {
      const messageText = chatError instanceof Error ? chatError.message : "报告追问提交失败，请稍后重试。";
      setError(messageText);
      syncMainChatMessages(runId, (previous) => previous.map((item) => (item.id === pendingAssistantId ? { ...item, content: messageText } : item)));
    } finally {
      stopReportChatStream();
      setIsReportChatSubmitting(false);
    }
  }

  function buildReferenceItems(workspace: WorkspacePayload): ReferenceLinkItem[] {
    const items: ReferenceLinkItem[] = [];
    const evidences = workspace.artifacts?.evidences ?? [];
    for (const ev of evidences) {
      const url = String(ev.source_url || "").trim();
      if (!url) continue;
      const label = String(ev.title || "").trim() || url;
      items.push({ label, url });
    }
    const seen = new Set<string>();
    const deduped = items.filter((item) => {
      if (seen.has(item.url)) return false;
      seen.add(item.url);
      return true;
    });
    if (deduped.length) return deduped;
    return (workspace.report?.sources ?? []).map((url) => String(url || "").trim()).filter(Boolean).map((url) => ({ label: url, url }));
  }

  function applyAgentCardEvent(event: WorkspaceEvent) {
    const eventType = String(event.event_type || "").trim();
    const payload = unwrapEventPayload(event);
    setAgentCardStreams((prev) => {
      const next: AgentCardStreams = { ...prev };
      if (eventType === "plan.card.competitors_stream") {
        const data = payload as PlanCardCompetitorsPayload;
        const subjectNames = (data.analysis_subjects || [])
          .map((item) => String(item.name || "").trim())
          .filter(Boolean);
        next.plan = {
          ...(next.plan || {}),
          isStreaming: true,
          lines: trimLines([`已规划分析对象：${subjectNames.join("、") || (data.planned_competitors || []).join("、") || "暂无"}`], 2),
        };
      } else if (eventType === "plan.card.schema_stream") {
        const data = payload as PlanCardSchemaPayload;
        const labels = (data.schema_fields || []).map((field) => data.schema_field_labels?.[field] || fieldLabelZh(field));
        next.plan = {
          ...(next.plan || {}),
          isStreaming: true,
          lines: trimLines([...(next.plan?.lines || []).slice(0, 1), `已规划字段：${labels.join("、") || "暂无"}`], 2),
        };
      } else if (eventType === "confirm_plan.card.summary_started") {
        next.confirm_plan = {
          ...(next.confirm_plan || {}),
          isStreaming: true,
          lines: [],
        };
      } else if (eventType === "confirm_plan.card.summary_delta") {
        const data = payload as ConfirmPlanCardSummaryPayload;
        next.confirm_plan = {
          ...(next.confirm_plan || {}),
          isStreaming: true,
          lines: trimLines([...(next.confirm_plan?.lines || []), String(data.delta || "").trim()].filter(Boolean), 12),
        };
      } else if (eventType === "confirm_plan.card.summary_completed") {
        const data = payload as ConfirmPlanCardSummaryPayload;
        const finalLines = String(data.message || "").split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
        next.confirm_plan = {
          ...(next.confirm_plan || {}),
          isStreaming: false,
          lines: trimLines(finalLines.length ? finalLines : next.confirm_plan?.lines || [], 12),
        };
      } else if (eventType === "collect.card.source_found") {
        const data = payload as CollectCardSourceFoundPayload;
        const nextCount = Number(data.total_found || data.rank || (next.collect?.totalCount || 0) + 1);
        next.collect = {
          lines: trimLines([
            `正在采集网页，已纳入 ${nextCount} 条来源。`,
            ...(next.collect?.lines || []).filter((line) => !line.startsWith("正在采集网页")),
          ]),
          totalCount: nextCount,
          urls: mergeCardUrls(
            next.collect?.urls,
            {
              label: String(data.title || data.source_url || "").trim(),
              url: String(data.source_url || ""),
            },
            COLLECT_PREVIEW_LIMIT,
          ),
          isStreaming: true,
        };
      } else if (eventType === "analyze.card.field_summary") {
        const data = payload as AnalyzeCardFieldSummaryPayload;
        const fieldText = String(data.field_label || fieldLabelZh(String(data.field_name || ""))).trim() || "字段";
        const competitor = String(data.competitor || "").trim() || "竞品";
        next.analyze = {
          ...(next.analyze || {}),
          isStreaming: true,
          lines: trimLines([
            `正在分析 ${competitor} 的 ${fieldText}。`,
            ...(next.analyze?.lines || []).filter((line) => !line.startsWith("正在分析 ")),
            `${competitor} · ${fieldText}：${String(data.summary || "").trim() || "unknown"}`,
          ]),
        };
      } else if (eventType === "analyze.card.competitor_summary") {
        const data = payload as AnalyzeCardCompetitorSummaryPayload;
        next.analyze = {
          ...(next.analyze || {}),
          isStreaming: true,
          lines: trimLines([...(next.analyze?.lines || []), ...(data.summary_lines || [])]),
        };
      } else if (eventType === "qa.card.review_started") {
        const data = payload as QaCardReviewStartedPayload;
        next.qa = {
          ...(next.qa || {}),
          isStreaming: true,
          lines: trimLines([`正在质检 ${Number(data.competitor_count || 0)} 个分析对象、${Number(data.schema_field_count || 0)} 个字段。`], 80),
        };
      } else if (eventType === "qa.card.review_summary") {
        const data = payload as QaCardReviewSummaryPayload;
        next.qa = {
          ...(next.qa || {}),
          isStreaming: true,
          lines: trimLines([...(next.qa?.lines || []), String(data.summary_text || "").trim()].filter(Boolean), 80),
        };
      } else if (eventType === "qa.card.final_summary") {
        const data = payload as QaCardFinalSummaryPayload;
        next.qa = {
          ...(next.qa || {}),
          isStreaming: false,
          lines: trimLines([...(next.qa?.lines || []), String(data.summary_text || "").trim()].filter(Boolean), 80),
        };
      }
      return next;
    });
  }

  function finalizeAgentCardStreams(workspace: WorkspacePayload) {
    const stages = workspace.workflow?.agent_stages ?? [];
    const finalCollectUrls = buildReferenceItems(workspace);
    setAgentCardStreams((prev) => {
      const next: AgentCardStreams = { ...prev };
      for (const stage of stages) {
        const stageName = String(stage.stage || "").trim() as StageName;
        if (!stageName || !next[stageName]) continue;
        if (stageName === "collect" && stage.status === "completed") {
          next.collect = {
            ...(next.collect || {}),
            isStreaming: false,
            totalCount: finalCollectUrls.length,
            lines: trimLines([
              `采集完成，最终保留 ${finalCollectUrls.length} 条网页来源。`,
              ...(next.collect?.lines || []).filter((line) => !line.startsWith("正在采集网页") && !line.startsWith("采集完成，最终保留 ")),
            ]),
          };
          continue;
        }
        if (stage.status === "completed" || stage.status === "failed") {
          next[stageName] = { ...next[stageName], isStreaming: false };
        }
      }
      return next;
    });
  }

  function applyWorkspace(workspace: WorkspacePayload, options?: { preserveSelectedStage?: boolean }) {
    const runId = workspace.run?.run_id || "";
    if (runId && activeRunIdRef.current && runId !== activeRunIdRef.current) return;
    const preserveSelectedStage = options?.preserveSelectedStage ?? true;
    startTransition(() => {
      setWorkspaceData(workspace);
      setEventFeed((prev) => mergeEvents(prev, workspace.observability?.events ?? []));
      setSelectedStage((current) => {
        if (preserveSelectedStage && current && getStageCard(workspace, current)) return current;
        return getDefaultSelectedStage(workspace);
      });
    });
    finalizeAgentCardStreams(workspace);
    if ((workspace.report?.markdown || "").trim() || workspace.run?.status === "completed") {
      setIsDraftStreaming(false);
      setDraftStreamError("");
    } else if (workspace.run?.status === "failed" && !(workspace.report?.markdown || "").trim()) {
      setIsDraftStreaming(false);
      setDraftStreamError(resolveDraftStreamError(workspace, draftStreamError));
    }
    const targetRunId = runId || activeRunIdRef.current;
    const prompt = (workspace.request?.user_prompt || "").trim();
    const workspaceTaskSummary = (workspace.request?.task_summary || workspace.run?.task_summary || workspace.summary?.task_summary || "").trim();
    if (targetRunId) {
      setSessions((prev) => {
        const next = prev.map((s) =>
          s.session_id === targetRunId
            ? {
                ...s,
                active_run_id: targetRunId,
                workspace_snapshot: workspace,
                task_summary: workspaceTaskSummary || s.task_summary,
                title: resolveSessionTitle({
                  runId: targetRunId,
                  prompt,
                  taskSummary: workspaceTaskSummary || s.task_summary,
                  currentTitle: s.title,
                }),
                chat_messages: ensureInitialPromptMessage(targetRunId, s.chat_messages || [], prompt),
                updated_at: nowIso(),
              }
            : s
        );
        const updated = next.find((s) => s.session_id === targetRunId);
        if (updated) writeStoredMessages(targetRunId, updated.chat_messages);
        return next;
      });
      setChatMessages((prev) => {
        const next = ensureInitialPromptMessage(targetRunId, prev, prompt);
        writeStoredMessages(targetRunId, next);
        return next;
      });
    }
  }

  function startPolling(runId: string) {
    stopPolling();
    setStreamState("polling");
    pollingTimerRef.current = window.setInterval(async () => {
      try {
        const [workspace, status] = await Promise.all([fetchRunWorkspace(runId), fetchRunStatus(runId)]);
        if (!isActiveRun(runId)) return;
        applyWorkspace(workspace);
        const runStatus = status.state?.status;
        if (runStatus === "completed" || runStatus === "failed") stopPolling();
      } catch {
        // silent fallback
      }
    }, 2500);
  }

  function scheduleReconnect(runId: string) {
    if (!isActiveRun(runId)) return;
    stopReconnectTimer();
    reconnectAttemptRef.current += 1;
    setStreamState("reconnecting");
    if (reconnectAttemptRef.current > maxReconnectAttempts) {
      startPolling(runId);
      return;
    }
    const delay = Math.min(1000 * 2 ** (reconnectAttemptRef.current - 1), 10000);
    reconnectTimerRef.current = window.setTimeout(() => startRunStream(runId, true), delay);
  }

  function startRunStream(runId: string, isReconnect = false) {
    if (typeof window === "undefined" || !runId) return;
    if (isReconnect && activeRunIdRef.current && activeRunIdRef.current !== runId) return;
    stopStream();
    stopPolling();
    stopReconnectTimer();
    activeRunIdRef.current = runId;
    setStreamState(isReconnect ? "reconnecting" : "streaming");
    const source = new window.EventSource(`/runs/${runId}/stream`);
    streamRef.current = source;
    if (!isReconnect) reconnectAttemptRef.current = 0;

    source.addEventListener("open", () => {
      // Hydrate workspace immediately after stream is connected,
      // so execution cards do not wait for the next workspace SSE push.
      void (async () => {
        try {
          const workspace = await fetchRunWorkspace(runId);
          if (!isActiveRun(runId)) return;
          applyWorkspace(workspace);
        } catch {
          // keep waiting for stream/polling updates
        }
      })();
    });

    source.addEventListener("workspace", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { workspace?: WorkspacePayload };
        if (payload.workspace) {
          if (!isActiveRun(runId)) return;
          applyWorkspace(payload.workspace);
          reconnectAttemptRef.current = 0;
          setStreamState("streaming");
        }
      } catch {
        // ignore malformed event
      }
    });

    source.addEventListener("task_summary", (event) => {
      if (!isActiveRun(runId)) return;
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { run_id?: string; task_summary?: string };
        const nextSummary = (payload.task_summary || "").trim();
        if (!nextSummary) return;
        setSessions((prev) =>
          prev.map((item) =>
            item.session_id === runId
              ? {
                  ...item,
                  task_summary: nextSummary,
                  title: resolveSessionTitle({
                    runId,
                    taskSummary: nextSummary,
                    currentTitle: item.title,
                  }),
                  updated_at: nowIso(),
                }
              : item
          )
        );
      } catch {
        // ignore malformed event
      }
    });

    source.addEventListener("run_event", (event) => {
      if (!isActiveRun(runId)) return;
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as WorkspaceEvent;
        const eventType = String(payload.event_type || "");
        const eventPayload = unwrapEventPayload(payload);
        if (eventType === "draft_report.started" || eventType === "draft_markdown.started") {
          if (!isEditingReport) {
            setIsDraftStreaming(true);
            setDraftStreamError("");
            setReportDraft("");
            setPreviewOpen(true);
          }
        } else if (eventType === "draft_report.block_delta" || eventType === "draft_report.block_completed") {
          if (!isEditingReport) {
            setIsDraftStreaming(true);
            setDraftStreamError("");
            setPreviewOpen(true);
          }
        } else if (eventType === "draft_markdown.delta") {
          const delta = typeof eventPayload.delta === "string" ? eventPayload.delta : "";
          if (delta && !isEditingReport) {
            setIsDraftStreaming(true);
            setDraftStreamError("");
            setPreviewOpen(true);
            setReportDraft((prev) => `${prev}${delta}`);
          }
        } else if (eventType === "draft_report.completed" || eventType === "draft_markdown.completed") {
          setIsDraftStreaming(false);
          setDraftStreamError("");
        } else if (eventType === "draft_markdown.recovered") {
          setIsDraftStreaming(false);
          setDraftStreamError("");
        } else if (eventType === "draft_report.failed" || eventType === "draft_markdown.failed") {
          setIsDraftStreaming(false);
          setPreviewOpen(true);
          const terminal = eventPayload.terminal === true;
          setDraftStreamError(
            terminal
              ? (typeof eventPayload.error === "string" ? eventPayload.error : "报告生成失败，请稍后重试。")
              : DRAFT_STREAM_WARNING_MESSAGE,
          );
        }
        if (eventType.includes(".card.")) {
          applyAgentCardEvent(payload);
        }
        setEventFeed((prev) => mergeEvents(prev, [payload]));
      } catch {
        // ignore malformed event
      }
    });

    source.addEventListener("run_done", () => {
      const currentRunId = activeRunIdRef.current;
      void (async () => {
        try {
          if (currentRunId && isActiveRun(currentRunId)) {
            const workspace = await fetchRunWorkspace(currentRunId);
            if (!isActiveRun(currentRunId)) return;
            applyWorkspace(workspace);
          }
        } catch (error) {
          // Avoid unhandled rejection noise when backend returns 5xx.
          console.error("fetchRunWorkspace on run_done failed:", error);
        } finally {
          if (currentRunId && isActiveRun(currentRunId)) {
            setStreamState("idle");
            stopRealtime();
          }
        }
      })();
    });

    source.addEventListener("error", () => {
      if (!isActiveRun(runId)) return;
      stopStream();
      scheduleReconnect(runId);
    });
  }

  async function loadSessionToUI(session: StoredSession, options?: { missingRunMessage?: string; clearWhenMissingSnapshot?: boolean }) {
    setActiveSessionId(session.session_id);
    setActiveMenu("history");
    setChatMessages(session.chat_messages?.length ? session.chat_messages : readStoredMessages(session.session_id));
    setWorkspaceData(session.workspace_snapshot || null);
    setEventFeed(session.workspace_snapshot?.observability?.events ?? []);
    setSelectedStage(getDefaultSelectedStage(session.workspace_snapshot));
    setExpandedEventKeys([]);
    setExpandedCallKeys([]);
    setViewMode("workspace");
    activeRunIdRef.current = session.active_run_id || "";
    if (session.active_run_id) {
      let shouldStartStream = Boolean(session.workspace_snapshot);
      try {
        const latestWorkspace = await fetchRunWorkspace(session.active_run_id);
        if (!isActiveRun(session.active_run_id)) return;
        applyWorkspace(latestWorkspace, { preserveSelectedStage: false });
        shouldStartStream = true;
      } catch {
        setError(options?.missingRunMessage || "加载该会话失败，可能已被删除或后端不可用。");
      } finally {
        if (shouldStartStream && isActiveRun(session.active_run_id)) startRunStream(session.active_run_id);
      }
    }
  }

  function handleSwitchSession(sessionId: string) {
    if (isManagingSessions) return;
    setOpenSessionMenuId(null);
    const target = sessions.find((item) => item.session_id === sessionId);
    if (!target) return;
    stopRealtime();
    pushRunRoute(sessionId);
    void loadSessionToUI(target);
  }

  function shouldShowSession(_session: StoredSession): boolean {
    return true;
  }

  async function refreshSessions(preferredRunId = ""): Promise<StoredSession[]> {
    const runs = await fetchRuns(maxSessionCount);
    const mapped = runs.map(makeSessionFromRunSummary);
    const merged = mergeSessionsPreserveState(mapped, sessions);
    setSessions(merged);

    if (!merged.length) {
      setActiveSessionId("");
      setViewMode("welcome");
      return [];
    }

    const targetId = preferredRunId || activeSessionId || merged[0].session_id;
    const target = merged.find((s) => s.session_id === targetId) || merged[0];
    stopRealtime();
    await loadSessionToUI(target);
    return merged;
  }

  function resetNewConversationState() {
    stopRealtime();
    setOpenSessionMenuId(null);
    setIsManagingSessions(false);
    setSelectedSessionIds([]);
    setActiveSessionId("");
    setViewMode("welcome");
    setActiveMenu("new");
    setQuery("");
    setCompetitorHintsText("");
    setAspectHintsText("");
    setWorkspaceData(null);
    setSelectedStage(null);
    setEventFeed([]);
    setStreamState("idle");
    setEventFilterMode("all");
    setExpandedEventKeys([]);
    setExpandedCallKeys([]);
    setIsEditingReport(false);
    setReportSourceRunId("");
    setOriginalReportContent("");
    setReportDraft("");
    setDraftStreamError("");
    setIsReportChatSubmitting(false);
    setChatMessages([]);
    setError("");
    setPreviewOpen(false);
    activeRunIdRef.current = "";
  }

  function handleNewConversation() {
    pushHomeRoute();
    resetNewConversationState();
  }

  function toggleSessionManagement() {
    setOpenSessionMenuId(null);
    setIsManagingSessions((prev) => {
      if (prev) setSelectedSessionIds([]);
      return !prev;
    });
  }

  function toggleSessionSelection(sessionId: string) {
    setSelectedSessionIds((prev) => (
      prev.includes(sessionId)
        ? prev.filter((item) => item !== sessionId)
        : [...prev, sessionId]
    ));
  }

  function clearDeletedSessions(sessionIds: string[]) {
    if (!sessionIds.length) return;
    sessionIds.forEach(clearStoredMessages);
    setSessions((prev) => prev.filter((item) => !sessionIds.includes(item.session_id)));
    setSelectedSessionIds((prev) => prev.filter((item) => !sessionIds.includes(item)));
    if (sessionIds.includes(activeSessionId)) {
      setActiveSessionId("");
      setChatMessages([]);
      activeRunIdRef.current = "";
    }
  }

  async function handleDeleteSession(sessionId: string) {
    const target = sessions.find((item) => item.session_id === sessionId);
    if (!target) return;
    const confirmed = window.confirm("确认删除该会话？");
    if (!confirmed) return;
    try {
      setOpenSessionMenuId(null);
      await deleteRunById(sessionId);
      clearDeletedSessions([sessionId]);
      const remaining = await refreshSessions();
      if (!remaining.length) {
        handleNewConversation();
      } else if (sessionId === activeSessionId) {
        pushRunRoute(remaining[0].session_id);
      }
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : "删除失败，请稍后重试。";
      setError(message);
    }
  }

  async function handleBatchDeleteSessions() {
    if (!selectedSessionIds.length) return;
    const confirmed = window.confirm(`确认删除选中的 ${selectedSessionIds.length} 个会话？`);
    if (!confirmed) return;
    try {
      setOpenSessionMenuId(null);
      const deletingIds = [...selectedSessionIds];
      await Promise.all(deletingIds.map((sessionId) => deleteRunById(sessionId)));
      clearDeletedSessions(deletingIds);
      setIsManagingSessions(false);
      const remaining = await refreshSessions();
      if (!remaining.length) {
        handleNewConversation();
      } else if (deletingIds.includes(activeSessionId)) {
        pushRunRoute(remaining[0].session_id);
      }
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : "批量删除失败，请稍后重试。";
      setError(message);
    }
  }

  useEffect(() => {
    let canceled = false;
    const effectRunId = requestedInitialRunId;
    void (async () => {
      try {
        const requestedRunId = requestedInitialRunId;
        if (requestedRunId) {
          const placeholder = makeSessionPlaceholder(requestedRunId);
          setSessions((prev) => {
            const merged = mergeSessionsPreserveState([placeholder], prev);
            return merged.some((item) => item.session_id === requestedRunId) ? merged : [placeholder, ...prev].slice(0, maxSessionCount);
          });
          void (async () => {
            try {
              const runs = await fetchRuns(maxSessionCount);
              if (canceled) return;
              const mapped = runs.map(makeSessionFromRunSummary);
              setSessions((prev) => mergeSessionsPreserveState(mapped, prev));
            } catch {
              // keep the requested session visible while the sidebar refresh recovers
            }
          })();
          await loadSessionToUI(placeholder, {
            missingRunMessage: `未找到会话 ${requestedRunId}，请确认链接是否正确。`,
            clearWhenMissingSnapshot: true,
          });
          return;
        }
        const runs = await fetchRuns(maxSessionCount);
        if (canceled) return;
        const mapped = runs.map(makeSessionFromRunSummary);
        const merged = mergeSessionsPreserveState(mapped, []);
        setSessions(merged);
        resetNewConversationState();
      } catch (initError) {
        if (canceled) return;
        const message = initError instanceof Error ? initError.message : "加载历史会话失败";
        setError(message);
        resetNewConversationState();
      }
    })();

    return () => {
      canceled = true;
      if (!effectRunId || activeRunIdRef.current === effectRunId) stopRealtime();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedInitialRunId]);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const text = query.trim();
    if (!text) {
      setError("请输入分析任务后再提交。");
      return;
    }

    setError("");
    const activeRunId = activeRunIdRef.current;
    if (viewMode === "workspace" && activeRunId && hasReport) {
      setQuery("");
      setIsSubmitting(true);
      try {
        await submitReportChatMessage(text);
      } finally {
        setIsSubmitting(false);
      }
      return;
    }

    setIsSubmitting(true);
    // Optimistic UI transition: switch to workspace immediately after submit.
    setViewMode("workspace");
    setActiveMenu("history");
    setExpandedEventKeys([]);
    setExpandedCallKeys([]);
    setWorkspaceData(null);
    setEventFeed([]);
    setSelectedStage(null);
    try {
      const competitorHints = parseHintList(competitorHintsText);
      const aspectHints = parseHintList(aspectHintsText);
      const runResponse = await fetch("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          industry: "",
          competitors: [],
          user_prompt: text,
          language: "zh-CN",
          timeframe: "last_12_months",
          competitor_hints: competitorHints,
          aspect_hints: aspectHints,
        }),
      });

      const runPayload = (await runResponse.json().catch(() => ({}))) as { message?: string; summary?: { run_id?: string; task_summary?: string } };
      if (!runResponse.ok) throw new Error(runPayload.message || "任务提交失败，请稍后重试。");

      const runId = runPayload.summary?.run_id || "";
      const initialSummary = runPayload.summary?.task_summary?.trim() || "";
      setQuery("");
      setStreamState("streaming");
      setCompetitorHintsText("");
      setAspectHintsText("");
      if (runId) {
        setViewMode("workspace");
        setActiveMenu("history");
        const next = makeSessionFromRunSummary({
          run_id: runId,
          industry: "",
          status: "running",
          competitor_count: 0,
          task_summary: initialSummary,
          created_at: nowIso(),
          updated_at: nowIso(),
        });
        setSessions((prev) => {
          if (prev.some((item) => item.session_id === next.session_id)) {
            return prev.map((item) =>
              item.session_id === runId
                ? {
                    ...item,
                    title: resolveSessionTitle({
                      runId,
                      prompt: text,
                      taskSummary: item.task_summary || initialSummary,
                      currentTitle: item.title,
                    }),
                    chat_messages: item.chat_messages || [],
                    task_summary: item.task_summary || initialSummary,
                  }
                : item
            );
          }
          return [{
            ...next,
            title: resolveSessionTitle({
              runId,
              prompt: text,
              taskSummary: initialSummary,
              currentTitle: next.title,
            }),
            task_summary: initialSummary,
            chat_messages: next.chat_messages || [],
          }, ...prev].slice(0, maxSessionCount);
        });
        setActiveSessionId(runId);
        activeRunIdRef.current = runId;
        pushRunRoute(runId);
        startRunStream(runId);
        // Proactively fetch initial workspace snapshot to avoid "needs refresh" UX.
        void (async () => {
          try {
            const workspace = await fetchRunWorkspace(runId);
            applyWorkspace(workspace);
          } catch {
            // stream fallback will continue updating
          }
        })();
        window.setTimeout(() => {
          void (async () => {
          try {
            const refreshed = await fetchRuns(maxSessionCount);
            const mapped = refreshed.map(makeSessionFromRunSummary);
            setSessions((prev) => {
              const merged = mergeSessionsPreserveState(mapped, prev);
              return merged.some((item) => item.session_id === runId) ? merged : prev;
            });
          } catch {
            // optimistic session remains visible
          }
          })();
        }, 1200);
      }
    } catch (submitError) {
      const message = submitError instanceof Error ? submitError.message : "提交失败，请稍后重试。";
      setError(message);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-fixed">
          <div className="brand">
            <div className="brand-mark" aria-hidden="true">
              <img className="brand-mark-image" src="/logo.png" alt="" />
            </div>
            <div>
              <h2>竞品分析智能体</h2>
              <p>CompeteAI</p>
            </div>
          </div>

          <nav className="menu" aria-label="主导航">
            <button className={activeMenu === "new" ? "menu-item active" : "menu-item"} onClick={() => { setActiveMenu("new"); handleNewConversation(); }}>
              <span className="menu-icon" aria-hidden="true"><MessageSquarePlus size={17} strokeWidth={2.2} /></span>
              <span>新对话</span>
            </button>
            <button className={activeMenu === "agent" ? "menu-item active" : "menu-item"} onClick={() => setActiveMenu("agent")}>
              <span className="menu-icon" aria-hidden="true"><Bot size={17} strokeWidth={2.2} /></span>
              <span>智能体协作</span>
            </button>
          </nav>
        </div>

        <div className="history-pane">
          <nav className="menu history-menu" aria-label="历史导航">
            <button className={activeMenu === "history" ? "menu-item active" : "menu-item"} onClick={() => setActiveMenu("history")}>
              <span className="menu-icon" aria-hidden="true"><History size={17} strokeWidth={2.2} /></span>
              <span>演示对话</span>
            </button>
          </nav>

          <div className="session-manage-bar">
            <button type="button" className={isManagingSessions ? "session-manage-btn active" : "session-manage-btn"} onClick={toggleSessionManagement}>
              {isManagingSessions ? "完成管理" : "管理对话"}
            </button>
            {isManagingSessions ? (
              <div className="session-manage-actions">
                <button
                  type="button"
                  className="session-batch-btn"
                  onClick={() => setSelectedSessionIds(selectedSessionIds.length === visibleSessions.length ? [] : visibleSessions.map((item) => item.session_id))}
                >
                  {selectedSessionIds.length === visibleSessions.length && visibleSessions.length ? "取消全选" : "全选"}
                </button>
                <button
                  type="button"
                  className="session-batch-btn danger"
                  disabled={!selectedSessionIds.length}
                  onClick={() => void handleBatchDeleteSessions()}
                >
                  批量删除（{selectedSessionIds.length}）
                </button>
              </div>
            ) : null}
          </div>

          <div className="session-list" aria-label="历史会话列表">
            {visibleSessions.map((session) => (
              <div key={session.session_id} className={session.session_id === activeSessionId ? "session-row active" : "session-row"}>
                <div className={session.session_id === activeSessionId ? "session-item active" : "session-item"}>
                  {isManagingSessions ? (
                    <label className="session-select-toggle" aria-label={`选择会话 ${session.title || PENDING_TASK_TITLE}`}>
                      <input
                        type="checkbox"
                        checked={selectedSessionIds.includes(session.session_id)}
                        onChange={() => toggleSessionSelection(session.session_id)}
                      />
                    </label>
                  ) : null}
                  <button
                    type="button"
                    className="session-title-btn"
                    aria-label={session.title || PENDING_TASK_TITLE}
                    onClick={() => handleSwitchSession(session.session_id)}
                    disabled={isManagingSessions}
                  >
                    {session.title || PENDING_TASK_TITLE}
                  </button>
                  {!isManagingSessions ? (
                    <div className="session-actions" ref={openSessionMenuId === session.session_id ? sessionMenuRef : null}>
                      <button
                        type="button"
                        className="session-more-btn"
                        aria-label="会话操作"
                        aria-expanded={openSessionMenuId === session.session_id}
                        onClick={(event) => {
                          event.stopPropagation();
                          setOpenSessionMenuId((prev) => (prev === session.session_id ? null : session.session_id));
                        }}
                      >
                        ⋮
                      </button>
                      {openSessionMenuId === session.session_id ? (
                        <div className="session-menu" role="menu">
                          <button
                            type="button"
                            className="session-menu-danger"
                            role="menuitem"
                            onClick={(event) => {
                              event.stopPropagation();
                              void handleDeleteSession(session.session_id);
                            }}
                          >
                            删除会话
                          </button>
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        </div>
      </aside>

      <main className="main-area">
        {viewMode === "welcome" ? (
          <div className="hero-card">
            <h1>AI 驱动的竞品分析 Agent 协作系统</h1>
            <p>多智能体协同收集信息、深度分析竞品、生成结构化洞察与报告，助力更明智的决策。</p>
            {error ? <div className="error-banner" role="alert">{error}</div> : null}
            <form className="query-box" onSubmit={handleSubmit}>
              <input aria-label="分析任务输入" placeholder="输入目标产品、竞品、行业或分析任务" value={query} onChange={(event) => setQuery(event.target.value)} disabled={isSubmitting} />
              <button type="submit" aria-label="提交" disabled={isSubmitting}>{isSubmitting ? "…" : "↑"}</button>
            </form>
            <div className="query-hint-grid">
              <input
                aria-label="想分析的竞品"
                className="query-hint-input"
                placeholder="想分析的竞品（可选，逗号/换行分隔）"
                value={competitorHintsText}
                onChange={(event) => setCompetitorHintsText(event.target.value)}
                disabled={isSubmitting}
              />
              <input
                aria-label="竞品分析包含哪些方面"
                className="query-hint-input"
                placeholder="竞品分析包含哪些方面（可选，逗号/换行分隔）"
                value={aspectHintsText}
                onChange={(event) => setAspectHintsText(event.target.value)}
                disabled={isSubmitting}
              />
            </div>
          </div>
        ) : (
          <section className="workspace-chat-shell" aria-label="演示对话工作区">
            <header className="workspace-topbar">
              <h1>{displaySummary || PENDING_TASK_TITLE}</h1>
              {activeMenu === "agent" ? (
                <div className="workspace-topbar-status">
                  <span className="workspace-topbar-label">Stream</span>
                  <strong>{streamState}</strong>
                </div>
              ) : null}
            </header>
            <div className="workspace-scroll" ref={scrollRef}>
              {error ? <div className="error-banner" role="alert">{error}</div> : null}
              {isRouteSessionLoading ? (
                <div className="workspace-conversation">
                  <section className="thought-chain-panel" aria-label="演示对话加载中">
                    <h2>正在加载演示对话</h2>
                    <p className="empty-state">正在同步会话内容与运行状态...</p>
                  </section>
                </div>
              ) : (
              <div className="workspace-conversation">
                {topChatMessages.map((message) => (
                  <div key={message.id} className={`message-row ${message.role}`}><div className={`message-bubble ${message.role}`}><ChatMessageContent message={message} /></div></div>
                ))}
                <div className="workspace-lower">
                  {activeMenu === "agent" ? (
                    <>
                      <section className="workspace-panel workspace-summary-panel" aria-label="运行总览">
                        <div className="workspace-panel-header">
                          <div>
                            <p className="workspace-eyebrow">Workspace</p>
                            <h2>智能体协作总览</h2>
                          </div>
                        </div>
                        <div className="workspace-summary-grid">
                          <article>
                            <span>Run Status</span>
                            <strong>{workspaceData?.run?.status || "running"}</strong>
                          </article>
                          <article>
                            <span>Competitors</span>
                            <strong>{workspaceData?.run?.competitor_count ?? workspaceData?.run?.planned_competitors?.length ?? 0}</strong>
                          </article>
                          <article>
                            <span>Schema Fields</span>
                            <strong>{workspaceData?.run?.schema_fields?.length ?? 0}</strong>
                          </article>
                          <article>
                            <span>Evidence</span>
                            <strong>{workspaceData?.run?.evidence_count ?? 0}</strong>
                          </article>
                          <article>
                            <span>Findings</span>
                            <strong>{workspaceData?.run?.finding_count ?? 0}</strong>
                          </article>
                          <article>
                            <span>QA Issues</span>
                            <strong>{workspaceData?.qa?.issue_count ?? 0}</strong>
                          </article>
                        </div>
                      </section>

                      <WorkspaceDagBoard
                        dag={workspaceData?.workflow?.dag}
                        stages={stageCards}
                        selectedStage={selectedStage}
                        onSelectStage={setSelectedStage}
                      />

                      <div className="workspace-detail-grid">
                        <EventStreamPanel
                          events={eventFeed}
                          selectedStage={selectedStage}
                          filterMode={eventFilterMode}
                          onChangeFilterMode={setEventFilterMode}
                          expandedEventKeys={expandedEventKeys}
                          onToggleEvent={(key) =>
                            setExpandedEventKeys((prev) =>
                              prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key]
                            )
                          }
                        />

                        <StageDetailPanel
                          stage={selectedStage}
                          stageCard={selectedStageCard}
                          handoff={selectedStageHandoff}
                          workflow={selectedStageWorkflow}
                          trace={selectedStageTrace}
                          events={selectedStageEvents}
                          expandedCallKeys={expandedCallKeys}
                          onToggleCall={(key) =>
                            setExpandedCallKeys((prev) =>
                              prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key]
                            )
                          }
                          expandedEventKeys={expandedEventKeys}
                          onToggleEvent={(key) =>
                            setExpandedEventKeys((prev) =>
                              prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key]
                            )
                          }
                          todoPlan={workspaceData?.todo_plan ?? workspaceData?.observability?.todo_plan ?? null}
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      <section className="thought-chain-panel" aria-label="智能体执行卡片">
                        <h2>智能体执行卡片</h2>
                        {!stageCards.length ? (
                          <p className="empty-state">等待智能体分析中...</p>
                        ) : (
                          <ol className="thought-list agent-card-list">
                            {stageCards.map((step, index) => {
                              const stepStatus = toStepStatus(step.status);
                              const streamCard = agentCardStreams[step.stage as StageName];
                              const summaryLines = streamCard?.lines?.length
                                ? streamCard.lines
                                : [step.summary?.trim() || ""].filter(Boolean);
                              const collectTotalCount = step.stage === "collect"
                                ? (streamCard?.totalCount || referenceItems.length)
                                : 0;
                              const collectUrls = step.stage === "collect"
                                ? (streamCard?.urls?.length ? streamCard.urls : collectPreviewItems)
                                : [];
                              const qaImprovementDetails = step.stage === "qa" ? (workspaceData?.qa?.improvement_details || []) : [];
                              const qaCollectItems = step.stage === "qa" ? (workspaceData?.qa?.collect_items || []) : [];
                              return (
                                <li key={`${step.stage}-${index}`} className={`thought-item agent-card stage-${step.stage}`}>
                                  <div className="thought-head">
                                    <span>{`${index + 1}. ${stageLabel(step.stage)}`}</span>
                                    <span className={`status-pill ${stepStatus}`}>{stepStatusText(stepStatus)}</span>
                                  </div>
                                  {step.stage === "qa" ? (
                                    <div className="qa-card-details">
                                      <div className="collect-web-preview">
                                        <strong>质检过程与结果</strong>
                                        {summaryLines.length ? summaryLines.map((line, lineIndex) => <p key={`${step.stage}-line-${lineIndex}`}>{line}</p>) : <p>等待执行...</p>}
                                      </div>
                                      {qaImprovementDetails.length ? (
                                        <div className="collect-web-preview">
                                          <strong>质检前后字段内容</strong>
                                          {qaImprovementDetails.map((item, itemIndex) => (
                                            <div key={`qa-after-${itemIndex}`}>
                                              <p>{`${item.competitor || "分析对象"} · ${item.field_label || item.field_name || "字段"}`}</p>
                                              <p>{`质检前：${stripLinksFromSummary(String(item.before_summary || "unknown"))}（证据 ${item.before_evidence_ref_count || 0} 条）`}</p>
                                              <p>{`质检后：${stripLinksFromSummary(String(item.after_summary || "unknown"))}（证据 ${item.after_evidence_ref_count || 0} 条）`}</p>
                                            </div>
                                          ))}
                                        </div>
                                      ) : null}
                                      {qaCollectItems.length ? (
                                        <div className="collect-web-preview">
                                          <strong>质检补采后的链接</strong>
                                          {qaCollectItems.map((item, itemIndex) => (
                                            <div key={`qa-collect-${itemIndex}`}>
                                              <p>{`${item.competitor || "分析对象"} · ${item.field_label || item.field_name || "字段"}`}</p>
                                              {(item.collected_urls || []).length ? (
                                                item.collected_urls?.map((url, urlIndex) => (
                                                  <p key={`qa-collect-url-${itemIndex}-${urlIndex}`}>
                                                    <a className="collect-web-link" href={url} target="_blank" rel="noreferrer">{url}</a>
                                                  </p>
                                                ))
                                              ) : (
                                                <p>补采待执行或当前未返回 URL。</p>
                                              )}
                                            </div>
                                          ))}
                                        </div>
                                      ) : null}
                                    </div>
                                  ) : (
                                    <div className="agent-summary-block">
                                      {summaryLines.length ? summaryLines.map((line, lineIndex) => <p key={`${step.stage}-line-${lineIndex}`}>{line}</p>) : <p>等待执行...</p>}
                                    </div>
                                  )}
                                  {step.stage === "collect" && collectUrls.length ? (
                                    <div className="collect-web-preview">
                                      <strong>{`采集网页（已展示 ${collectUrls.length} 条，共 ${collectTotalCount} 条）`}</strong>
                                      {collectUrls.map((item, idx) => (
                                        <p key={`collect-web-${idx}-${item.url}`}>
                                          <a className="collect-web-link" href={item.url} target="_blank" rel="noreferrer">
                                            {`${idx + 1}. ${item.label}`}
                                          </a>
                                        </p>
                                      ))}
                                      {collectTotalCount > collectUrls.length ? <p>......</p> : null}
                                    </div>
                                  ) : null}
                                </li>
                              );
                            })}
                          </ol>
                        )}
                      </section>

                      {shouldShowReportCard ? (
                        <section className="workspace-panel report-card-section workspace-panel-narrow" aria-label="报告下载区">
                          <button type="button" className="report-card" onClick={() => setPreviewOpen(true)}>
                            <div className="report-card-main">
                              <span className="report-card-icon" aria-hidden="true">📖</span>
                              <div>
                                <strong>{`report_${(activeRunIdRef.current || "latest").slice(0, 16)}.md`}</strong>
                                <small>{reportCardStatusText}</small>
                              </div>
                            </div>
                          </button>
                          <button
                            type="button"
                            className="report-download-btn"
                            onClick={() => downloadReport(currentReportContent, activeRunIdRef.current)}
                            disabled={isDraftStreaming || !currentReportContent.trim()}
                          >
                            下载
                          </button>
                          <button
                            type="button"
                            className="report-download-btn"
                            onClick={() => void handleGenerateQuestionnaire()}
                            disabled={isDraftStreaming || isGeneratingQuestionnaire || !currentReportContent.trim()}
                          >
                            {isGeneratingQuestionnaire ? "生成中..." : "生成问卷"}
                          </button>
                        </section>
                      ) : null}

                      {hasQuestionnaire ? (
                        <section className="workspace-panel report-card-section workspace-panel-narrow" aria-label="问卷预览区">
                          <button type="button" className="report-card" onClick={openQuestionnairePreview}>
                            <div className="report-card-main">
                              <span className="report-card-icon" aria-hidden="true">📝</span>
                              <div>
                                <strong>{`questionnaire_${(activeRunIdRef.current || "latest").slice(0, 16)}.md`}</strong>
                                <small>{workspaceData?.questionnaire?.title || "Markdown file"}</small>
                              </div>
                            </div>
                          </button>
                          <button
                            type="button"
                            className="report-download-btn"
                            onClick={startQuestionnaireEdit}
                            disabled={!currentQuestionnaireContent.trim()}
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            className="report-download-btn"
                            onClick={() => downloadQuestionnaire(currentQuestionnaireContent, activeRunIdRef.current)}
                            disabled={!currentQuestionnaireContent.trim()}
                          >
                            下载
                          </button>
                          <button
                            type="button"
                            className="report-download-btn"
                            onClick={() => void handleExportQuestionnaireToWjx()}
                            disabled={isExportingQuestionnaire || !currentQuestionnaireContent.trim()}
                          >
                            {isExportingQuestionnaire ? "导出中..." : "导出到问卷星"}
                          </button>
                          {currentQuestionnaireExport?.url ? (
                            <a
                              className="report-download-btn"
                              href={currentQuestionnaireExport.url}
                              target="_blank"
                              rel="noreferrer"
                            >
                              打开问卷星
                            </a>
                          ) : null}
                        </section>
                      ) : null}

                      {referenceItems.length ? (
                        <section className="workspace-panel reference-section workspace-panel-narrow" aria-label="参考文献">
                          <details>
                            <summary>全部参考资料</summary>
                            <div className="reference-list">
                              {referenceItems.map((item, idx) => (
                                <div className="reference-item" key={`ref-${idx}-${item.url}`}><a href={item.url} target="_blank" rel="noreferrer">{idx + 1}. {item.label}</a></div>
                              ))}
                            </div>
                          </details>
                        </section>
                      ) : null}

                      {reportFollowupMessages.length ? (
                        <div className="report-followup-thread report-followup-thread-narrow" aria-label="报告后续对话">
                          {reportFollowupMessages.map((message) => (
                            <div key={message.id} className={`message-row ${message.role}`}>
                              <div className={`message-bubble ${message.role}`}><ChatMessageContent message={message} /></div>
                            </div>
                          ))}
                        </div>
                      ) : null}
                    </>
                  )}
                </div>
              </div>
              )}
            </div>
            {activeMenu === "agent" ? null : (
              <div className="workspace-composer">
                <form className="composer-box composer-box-narrow" onSubmit={handleSubmit}>
                  <input
                    aria-label="聊天输入"
                    placeholder={hasReport ? "继续追问，或要求补充/修改报告..." : "继续输入分析需求或追问..."}
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    disabled={isSubmitting || isReportChatSubmitting}
                  />
                  <button type="submit" aria-label="发送" disabled={isSubmitting || isReportChatSubmitting}>{isSubmitting || isReportChatSubmitting ? "…" : "↑"}</button>
                </form>
              </div>
            )}
          </section>
        )}
      </main>

      {showPlanConfirmationDialog ? (
        <div className="plan-confirmation-overlay" role="presentation">
          <div className="plan-confirmation-dialog" role="dialog" aria-modal="true" aria-labelledby="plan-confirmation-title">
            <div className="plan-confirmation-header">
              <h2 id="plan-confirmation-title">确认采集结果</h2>
              <span>{`Plan v${planConfirmation?.revision_number || workspaceData?.run?.plan_revision || 1}`}</span>
            </div>
            <div className="plan-confirmation-body">
              <pre className="plan-confirmation-message">{planConfirmation?.confirmation_message || ""}</pre>
              <textarea
                className="plan-confirmation-input"
                value={planSupplementText}
                onChange={(event) => setPlanSupplementText(event.target.value)}
                disabled={isSubmittingPlanConfirmation}
              />
            </div>
            <div className="plan-confirmation-actions">
              <button
                type="button"
                className="plan-confirmation-btn secondary"
                onClick={() => void handleSubmitPlanSupplement()}
                disabled={isSubmittingPlanConfirmation || !planSupplementText.trim()}
              >
                {isSubmittingPlanConfirmation ? "提交中..." : "补充要求并重新规划"}
              </button>
              <button
                type="button"
                className="plan-confirmation-btn primary"
                onClick={() => void handleConfirmPlan()}
                disabled={isSubmittingPlanConfirmation}
              >
                {isSubmittingPlanConfirmation ? "处理中..." : "确认并继续"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {previewOpen ? (
        <div className="report-preview-overlay" onClick={closeReportPreview} role="presentation">
          <aside className="report-preview-drawer" onClick={(event) => event.stopPropagation()}>
            <div className="report-preview-header">
              <strong>{`report_${(activeRunIdRef.current || "latest").slice(0, 16)}.md`}</strong>
              <div className="report-preview-actions">
                {!isEditingReport ? (
                  <>
                    <button
                      type="button"
                      className="report-preview-icon-btn"
                      onClick={startReportEdit}
                      disabled={isDraftStreaming || !currentReportContent.trim()}
                    >
                      编辑
                    </button>
                    <button
                      className="report-preview-icon-btn"
                      type="button"
                      aria-label="下载报告"
                      title="下载报告"
                      onClick={() => downloadReport(currentReportContent, activeRunIdRef.current)}
                      disabled={isDraftStreaming || !currentReportContent.trim()}
                    >
                      下载
                    </button>
                    <button
                      className="report-preview-icon-btn"
                      type="button"
                      aria-label="生成问卷"
                      title="生成问卷"
                      onClick={() => void handleGenerateQuestionnaire()}
                      disabled={isDraftStreaming || isGeneratingQuestionnaire || !currentReportContent.trim()}
                    >
                      {isGeneratingQuestionnaire ? "生成中..." : "生成问卷"}
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      className="report-preview-icon-btn"
                      onClick={() => void handleSaveReport()}
                      disabled={isSavingReport || !reportDraft.trim()}
                    >
                      {isSavingReport ? "保存中..." : "保存"}
                    </button>
                    <button
                      type="button"
                      className="report-preview-icon-btn"
                      onClick={cancelReportEdit}
                      disabled={isSavingReport}
                    >
                      取消
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="report-preview-icon-btn"
                  onClick={closeReportPreview}
                  aria-label="关闭预览"
                  title="关闭预览"
                  disabled={isSavingReport}
                >
                  关闭
                </button>
              </div>
            </div>
            <div className="report-preview-body">
              {isDraftStreaming ? <p className="report-preview-status">正在生成报告...</p> : null}
              {!isDraftStreaming && draftStreamError ? <p className="report-preview-status error">{draftStreamError}</p> : null}
              {isEditingReport ? (
                <textarea
                  className="report-preview-editor"
                  value={reportDraft}
                  onChange={(event) => setReportDraft(event.target.value)}
                  aria-label="编辑报告内容"
                  disabled={isSavingReport}
                />
              ) : (
                <ReportPreviewPanel blocks={currentReportBlocks} html={reportHtml} markdown={reportPreviewBody} />
              )}
            </div>
          </aside>
        </div>
      ) : null}

      {questionnaireOpen ? (
        <div className="report-preview-overlay" onClick={closeQuestionnairePreview} role="presentation">
          <aside className="report-preview-drawer" onClick={(event) => event.stopPropagation()}>
            <div className="report-preview-header">
              <strong>{`questionnaire_${(activeRunIdRef.current || "latest").slice(0, 16)}.md`}</strong>
              <div className="report-preview-actions">
                {!isEditingQuestionnaire ? (
                  <>
                    <button
                      type="button"
                      className="report-preview-icon-btn"
                      onClick={startQuestionnaireEdit}
                      disabled={!currentQuestionnaireContent.trim()}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      className="report-preview-icon-btn"
                      onClick={() => downloadQuestionnaire(currentQuestionnaireContent, activeRunIdRef.current)}
                      disabled={!currentQuestionnaireContent.trim()}
                    >
                      下载
                    </button>
                    <button
                      type="button"
                      className="report-preview-icon-btn"
                      onClick={() => void handleExportQuestionnaireToWjx()}
                      disabled={isExportingQuestionnaire || !currentQuestionnaireContent.trim()}
                    >
                      {isExportingQuestionnaire ? "导出中..." : "导出到问卷星"}
                    </button>
                    {currentQuestionnaireExport?.url ? (
                      <a
                        className="report-preview-icon-btn"
                        href={currentQuestionnaireExport.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        打开问卷星
                      </a>
                    ) : null}
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      className="report-preview-icon-btn"
                      onClick={() => void handleSaveQuestionnaire()}
                      disabled={isSavingQuestionnaire || !questionnaireDraft.trim()}
                    >
                      {isSavingQuestionnaire ? "保存中..." : "保存"}
                    </button>
                    <button
                      type="button"
                      className="report-preview-icon-btn"
                      onClick={cancelQuestionnaireEdit}
                      disabled={isSavingQuestionnaire}
                    >
                      取消
                    </button>
                  </>
                )}
                <button
                  type="button"
                  className="report-preview-icon-btn"
                  onClick={closeQuestionnairePreview}
                  disabled={isSavingQuestionnaire}
                >
                  关闭
                </button>
              </div>
            </div>
            <div className="report-preview-body">
              {isEditingQuestionnaire ? (
                <textarea
                  className="report-preview-editor"
                  value={questionnaireDraft}
                  onChange={(event) => setQuestionnaireDraft(event.target.value)}
                  aria-label="编辑问卷内容"
                  disabled={isSavingQuestionnaire}
                />
              ) : (
                <pre>{currentQuestionnaireContent || "暂无问卷内容"}</pre>
              )}
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
