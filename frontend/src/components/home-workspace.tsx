"use client";

import { startTransition, type FormEvent, useEffect, useRef, useState } from "react";
import { Bot, History, MessageSquarePlus } from "lucide-react";
import { EventStreamPanel } from "@/components/event-stream-panel";
import { StageDetailPanel } from "@/components/stage-detail-panel";
import { WorkspaceDagBoard } from "@/components/workspace-dag-board";
import type {
  AgentHandoff,
  AgentStageCard,
  AgentTrace,
  AgentWorkflow,
  EvidenceItem,
  StageName,
  WorkspaceEvent,
  WorkspacePayload,
} from "@/components/workspace-types";

type ViewMode = "welcome" | "workspace";

type SubmitResponse = { summary_text: string };

type RunStatusResponse = { state?: { status?: string } };
type RunListItem = { run_id: string; industry: string; status: string; competitor_count: number; user_prompt?: string; created_at: string; updated_at: string };
type ChatMessage = { id: string; role: "user" | "assistant" | "system"; content: string };
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

const STAGE_ORDER: StageName[] = ["plan", "collect", "normalize", "analyze", "draft", "qa", "finalize"];

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

export function HomeWorkspace() {
  const [activeMenu, setActiveMenu] = useState<"new" | "agent" | "history">("new");
  const [viewMode, setViewMode] = useState<ViewMode>("welcome");
  const [query, setQuery] = useState("");
  const [competitorHintsText, setCompetitorHintsText] = useState("");
  const [aspectHintsText, setAspectHintsText] = useState("");
  const [taskSummary, setTaskSummary] = useState("");
  const [workspaceData, setWorkspaceData] = useState<WorkspacePayload | null>(null);
  const [selectedStage, setSelectedStage] = useState<StageName | null>(null);
  const [eventFeed, setEventFeed] = useState<WorkspaceEvent[]>([]);
  const [streamState, setStreamState] = useState<StreamState>("idle");
  const [eventFilterMode, setEventFilterMode] = useState<"all" | "stage">("all");
  const [expandedEventKeys, setExpandedEventKeys] = useState<string[]>([]);
  const [expandedCallKeys, setExpandedCallKeys] = useState<string[]>([]);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<StoredSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState("");
  const [openSessionMenuId, setOpenSessionMenuId] = useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState("");

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const pollingTimerRef = useRef<number | null>(null);
  const sessionMenuRef = useRef<HTMLDivElement | null>(null);
  const activeRunIdRef = useRef<string>("");
  const reconnectAttemptRef = useRef(0);

  const maxReconnectAttempts = 4;
  const maxSessionCount = 30;
  const displaySummary = taskSummary.replace(/^任务(目标|分析)\s*[:：]\s*/u, "").trim();
  const sessionChatStoragePrefix = "home_workspace_chat_messages:";
  const stageCards = workspaceData?.workflow?.agent_stages ?? [];
  const selectedStageCard = getStageCard(workspaceData, selectedStage);
  const selectedStageWorkflow = getStageWorkflow(workspaceData, selectedStage);
  const selectedStageHandoff = getStageHandoff(workspaceData, selectedStage);
  const selectedStageTrace = getStageTrace(workspaceData, selectedStage);
  const selectedStageEvents = selectedStage
    ? eventFeed.filter((item) => item.stage === selectedStage)
    : [];
  const hasReport = Boolean(workspaceData && (workspaceData.report?.markdown || "").trim());
  const referenceItems = workspaceData ? buildReferenceItems(workspaceData) : [];

  useEffect(() => {
    if (viewMode === "workspace" && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [chatMessages, viewMode]);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (!sessionMenuRef.current) return;
      if (!sessionMenuRef.current.contains(event.target as Node)) setOpenSessionMenuId(null);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  function nowIso() {
    return new Date().toISOString();
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
      const taskSummary = existing.task_summary || item.task_summary;
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
      taskSummary: "",
      currentTitle: run.run_id,
    });
    return {
      session_id: run.run_id,
      title,
      created_at: run.created_at || nowIso(),
      updated_at: run.updated_at || run.created_at || nowIso(),
      active_run_id: run.run_id,
      task_summary: "",
      chat_messages: [],
      workspace_snapshot: null,
    };
  }

  function resolveSessionTitle(args: { runId: string; prompt?: string; taskSummary?: string; currentTitle?: string }): string {
    const runId = (args.runId || "").trim();
    const prompt = (args.prompt || "").trim();
    const taskSummary = (args.taskSummary || "").trim();
    const currentTitle = (args.currentTitle || "").trim();
    return prompt || taskSummary || currentTitle || runId || "未命名会话";
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
    setStreamState("idle");
  }

  async function fetchRunWorkspace(runId: string): Promise<WorkspacePayload> {
    const response = await fetch(`/runs/${runId}/workspace`);
    if (!response.ok) throw new Error(`workspace fetch failed: ${response.status}`);
    return (await response.json()) as WorkspacePayload;
  }

  async function fetchRunStatus(runId: string): Promise<RunStatusResponse> {
    const response = await fetch(`/runs/${runId}`);
    if (!response.ok) throw new Error(`run status failed: ${response.status}`);
    return (await response.json()) as RunStatusResponse;
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

  function applyWorkspace(workspace: WorkspacePayload, options?: { preserveSelectedStage?: boolean }) {
    const preserveSelectedStage = options?.preserveSelectedStage ?? true;
    startTransition(() => {
      setWorkspaceData(workspace);
      setEventFeed((prev) => mergeEvents(prev, workspace.observability?.events ?? []));
      setSelectedStage((current) => {
        if (preserveSelectedStage && current && getStageCard(workspace, current)) return current;
        return getDefaultSelectedStage(workspace);
      });
    });
    const runId = workspace.run?.run_id || activeRunIdRef.current;
    const prompt = (workspace.request?.user_prompt || "").trim();
    if (runId) {
      setSessions((prev) => {
        const next = prev.map((s) =>
          s.session_id === runId
            ? {
                ...s,
                active_run_id: runId,
                workspace_snapshot: workspace,
                task_summary: s.task_summary || prompt,
                title: resolveSessionTitle({
                  runId,
                  prompt,
                  taskSummary: s.task_summary || prompt,
                  currentTitle: s.title,
                }),
                chat_messages: ensureInitialPromptMessage(runId, s.chat_messages || [], prompt),
                updated_at: nowIso(),
              }
            : s
        );
        const updated = next.find((s) => s.session_id === runId);
        if (updated) writeStoredMessages(runId, updated.chat_messages);
        return next;
      });
      setChatMessages((prev) => {
        const next = ensureInitialPromptMessage(runId, prev, prompt);
        writeStoredMessages(runId, next);
        return next;
      });
    }
    if (prompt && !taskSummary) setTaskSummary(prompt);
  }

  function startPolling(runId: string) {
    stopPolling();
    setStreamState("polling");
    pollingTimerRef.current = window.setInterval(async () => {
      try {
        const [workspace, status] = await Promise.all([fetchRunWorkspace(runId), fetchRunStatus(runId)]);
        applyWorkspace(workspace);
        const runStatus = status.state?.status;
        if (runStatus === "completed" || runStatus === "failed") stopPolling();
      } catch {
        // silent fallback
      }
    }, 2500);
  }

  function scheduleReconnect(runId: string) {
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
    stopStream();
    stopPolling();
    stopReconnectTimer();
    activeRunIdRef.current = runId;
    setStreamState(isReconnect ? "reconnecting" : "streaming");
    const source = new window.EventSource(`/runs/${runId}/stream`);
    streamRef.current = source;
    if (!isReconnect) reconnectAttemptRef.current = 0;

    source.addEventListener("workspace", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { workspace?: WorkspacePayload };
        if (payload.workspace) {
          applyWorkspace(payload.workspace);
          reconnectAttemptRef.current = 0;
          setStreamState("streaming");
        }
      } catch {
        // ignore malformed event
      }
    });

    source.addEventListener("run_event", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as WorkspaceEvent;
        setEventFeed((prev) => mergeEvents(prev, [payload]));
      } catch {
        // ignore malformed event
      }
    });

    source.addEventListener("run_done", () => {
      const currentRunId = activeRunIdRef.current;
      void (async () => {
        try {
          if (currentRunId) {
            const workspace = await fetchRunWorkspace(currentRunId);
            applyWorkspace(workspace);
          }
        } finally {
          setStreamState("idle");
          stopRealtime();
        }
      })();
    });

    source.addEventListener("error", () => {
      stopStream();
      scheduleReconnect(runId);
    });
  }

  async function loadSessionToUI(session: StoredSession) {
    setActiveSessionId(session.session_id);
    setTaskSummary(session.task_summary || "");
    setChatMessages(session.chat_messages?.length ? session.chat_messages : readStoredMessages(session.session_id));
    setWorkspaceData(session.workspace_snapshot || null);
    setEventFeed(session.workspace_snapshot?.observability?.events ?? []);
    setSelectedStage(getDefaultSelectedStage(session.workspace_snapshot));
    setExpandedEventKeys([]);
    setExpandedCallKeys([]);
    setViewMode("workspace");
    setActiveMenu("history");
    activeRunIdRef.current = session.active_run_id || "";
    if (session.active_run_id) {
      try {
        const latestWorkspace = await fetchRunWorkspace(session.active_run_id);
        applyWorkspace(latestWorkspace, { preserveSelectedStage: false });
      } catch {
        // keep known state if workspace fetch fails
      } finally {
        startRunStream(session.active_run_id);
      }
    }
  }

  function handleSwitchSession(sessionId: string) {
    setOpenSessionMenuId(null);
    const target = sessions.find((item) => item.session_id === sessionId);
    if (!target) return;
    stopRealtime();
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

  function handleNewConversation() {
    stopRealtime();
    setOpenSessionMenuId(null);
    setActiveSessionId("");
    setViewMode("welcome");
    setActiveMenu("new");
    setQuery("");
    setCompetitorHintsText("");
    setAspectHintsText("");
    setTaskSummary("");
    setWorkspaceData(null);
    setSelectedStage(null);
    setEventFeed([]);
    setStreamState("idle");
    setEventFilterMode("all");
    setExpandedEventKeys([]);
    setExpandedCallKeys([]);
    setChatMessages([]);
    setError("");
    setPreviewOpen(false);
    activeRunIdRef.current = "";
  }

  async function handleDeleteSession(sessionId: string) {
    const target = sessions.find((item) => item.session_id === sessionId);
    if (!target) return;
    const confirmed = window.confirm("确认删除该会话？");
    if (!confirmed) return;
    try {
      setOpenSessionMenuId(null);
      await deleteRunById(sessionId);
      const remaining = await refreshSessions();
      if (!remaining.length) handleNewConversation();
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : "删除失败，请稍后重试。";
      setError(message);
    }
  }

  useEffect(() => {
    let canceled = false;
    void (async () => {
      try {
        const runs = await fetchRuns(maxSessionCount);
        if (canceled) return;
        const mapped = runs.map(makeSessionFromRunSummary);
        const merged = mergeSessionsPreserveState(mapped, []);
        setSessions(merged);
        if (!merged.length) {
          handleNewConversation();
          return;
        }
        await loadSessionToUI(merged[0]);
      } catch (initError) {
        if (canceled) return;
        const message = initError instanceof Error ? initError.message : "加载历史会话失败";
        setError(message);
        handleNewConversation();
      }
    })();

    return () => {
      canceled = true;
      stopRealtime();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const text = query.trim();
    if (!text) {
      setError("请输入分析任务后再提交。");
      return;
    }

    setError("");
    setIsSubmitting(true);
    try {
      const competitorHints = parseHintList(competitorHintsText);
      const aspectHints = parseHintList(aspectHintsText);
      const [summaryResponse, runResponse] = await Promise.all([
        fetch("/runs/summary", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, language: "zh-CN" }),
        }),
        fetch("/runs", {
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
        }),
      ]);

      const runPayload = (await runResponse.json().catch(() => ({}))) as { message?: string; summary?: { run_id?: string } };
      if (!runResponse.ok) throw new Error(runPayload.message || "任务提交失败，请稍后重试。");

      let summary = text.length <= 40 ? text : `${text.slice(0, 40)}...`;
      if (summaryResponse.ok) {
        const payload = (await summaryResponse.json().catch(() => ({}))) as SubmitResponse;
        summary = payload.summary_text?.trim() || summary;
      }

      const runId = runPayload.summary?.run_id || "";
      setTaskSummary(summary);
      setViewMode("workspace");
      setActiveMenu("history");
      setQuery("");
      setWorkspaceData(null);
      setSelectedStage(null);
      setEventFeed([]);
      setStreamState("streaming");
      setExpandedEventKeys([]);
      setExpandedCallKeys([]);
      setCompetitorHintsText("");
      setAspectHintsText("");
      if (runId) {
        const refreshed = await fetchRuns(maxSessionCount);
        const mapped = refreshed.map(makeSessionFromRunSummary);
        setSessions((prev) => mergeSessionsPreserveState(mapped, prev));
        const next = mapped.find((item) => item.session_id === runId) || makeSessionFromRunSummary({
          run_id: runId,
          industry: "",
          status: "running",
          competitor_count: 0,
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
                      taskSummary: item.task_summary || summary,
                      currentTitle: item.title,
                    }),
                    chat_messages: item.chat_messages || [],
                    task_summary: item.task_summary || summary,
                  }
                : item
            );
          }
          return [{
            ...next,
            title: resolveSessionTitle({
              runId,
              prompt: text,
              taskSummary: summary,
              currentTitle: next.title,
            }),
            task_summary: summary,
            chat_messages: next.chat_messages || [],
          }, ...prev].slice(0, maxSessionCount);
        });
        setActiveSessionId(runId);
        activeRunIdRef.current = runId;
        startRunStream(runId);
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

          <div className="session-list" aria-label="历史会话列表">
            {sessions.filter(shouldShowSession).map((session) => (
              <div key={session.session_id} className={session.session_id === activeSessionId ? "session-row active" : "session-row"}>
                <div className={session.session_id === activeSessionId ? "session-item active" : "session-item"}>
                  <button type="button" className="session-title-btn" onClick={() => handleSwitchSession(session.session_id)}>
                    {session.title || "未命名会话"}
                  </button>
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
              <input aria-label="分析任务输入" placeholder="输入竞品、行业或分析任务" value={query} onChange={(event) => setQuery(event.target.value)} disabled={isSubmitting} />
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
              <h1>{displaySummary || "分析进行中"}</h1>
              {activeMenu === "agent" ? (
                <div className="workspace-topbar-status">
                  <span className="workspace-topbar-label">Stream</span>
                  <strong>{streamState}</strong>
                </div>
              ) : null}
            </header>
            <div className="workspace-scroll" ref={scrollRef}>
              {error ? <div className="error-banner" role="alert">{error}</div> : null}
              <div className="workspace-conversation">
                {chatMessages.map((message) => (
                  <div key={message.id} className={`message-row ${message.role}`}><div className={`message-bubble ${message.role}`}>{message.content}</div></div>
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
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      {hasReport ? (
                        <section className="workspace-panel report-card-section" aria-label="报告下载区">
                          <button type="button" className="report-card" onClick={() => setPreviewOpen(true)}>
                            <div className="report-card-main">
                              <span className="report-card-icon" aria-hidden="true">📖</span>
                              <div>
                                <strong>{`report_${(activeRunIdRef.current || "latest").slice(0, 16)}.md`}</strong>
                                <small>Markdown file</small>
                              </div>
                            </div>
                          </button>
                          <a className="report-download-btn" href={activeRunIdRef.current ? `/runs/${activeRunIdRef.current}/report.md` : "#"} target="_blank" rel="noreferrer" onClick={(event) => { if (!activeRunIdRef.current) event.preventDefault(); }}>
                            下载
                          </a>
                        </section>
                      ) : null}

                      {referenceItems.length ? (
                        <section className="workspace-panel reference-section" aria-label="参考文献">
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
                    </>
                  )}
                </div>
              </div>
            </div>
            <div className="workspace-composer">
              <form className="composer-box" onSubmit={handleSubmit}>
                <input aria-label="聊天输入" placeholder="继续输入分析需求或追问..." value={query} onChange={(event) => setQuery(event.target.value)} disabled={isSubmitting} />
                <button type="submit" aria-label="发送" disabled={isSubmitting}>{isSubmitting ? "…" : "↑"}</button>
              </form>
            </div>
          </section>
        )}
      </main>

      {previewOpen ? (
        <div className="report-preview-overlay" onClick={() => setPreviewOpen(false)} role="presentation">
          <aside className="report-preview-drawer" onClick={(event) => event.stopPropagation()}>
            <div className="report-preview-header">
              <strong>{`report_${(activeRunIdRef.current || "latest").slice(0, 16)}.md`}</strong>
              <div className="report-preview-actions">
                <a
                  className="report-preview-icon-btn"
                  href={activeRunIdRef.current ? `/runs/${activeRunIdRef.current}/report.md` : "#"}
                  target="_blank"
                  rel="noreferrer"
                  aria-label="下载报告"
                  title="下载报告"
                  onClick={(event) => {
                    if (!activeRunIdRef.current) event.preventDefault();
                  }}
                >
                  下载
                </a>
                <button
                  type="button"
                  className="report-preview-icon-btn"
                  onClick={() => setPreviewOpen(false)}
                  aria-label="关闭预览"
                  title="关闭预览"
                >
                  关闭
                </button>
              </div>
            </div>
            <div className="report-preview-body">
              <pre>{workspaceData?.report?.markdown || "暂无报告内容"}</pre>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
