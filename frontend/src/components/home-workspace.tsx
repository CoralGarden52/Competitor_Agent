"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";
import { Bot, History, MessageSquarePlus } from "lucide-react";

type ViewMode = "welcome" | "workspace";
type StepStatus = "pending" | "running" | "done" | "failed";

type SubmitResponse = { summary_text: string };

type WorkspaceStage = { stage: string; status: string; summary?: string };
type WorkspaceEvent = {
  stage?: string;
  event_type?: string;
  created_at?: string;
  payload?: { envelope?: { attempt?: number; payload?: Record<string, unknown> } };
};
type EvidenceItem = { title?: string; source_url?: string };
type WorkspacePayload = {
  run?: { run_id?: string; status?: string; planned_competitors?: string[]; schema_fields?: string[] };
  request?: { user_prompt?: string };
  workflow?: { agent_stages?: WorkspaceStage[] };
  qa?: {
    passed?: boolean;
    issue_count?: number;
    target_agent?: string | null;
    issues?: Array<{ code?: string; message?: string }>;
    collect_items?: Array<{ competitor?: string; field_name?: string }>;
  };
  report?: { markdown?: string; sources?: string[] };
  artifacts?: { evidences?: EvidenceItem[] };
  observability?: { events?: WorkspaceEvent[] };
};

type RunStatusResponse = { state?: { status?: string } };
type RunListItem = { run_id: string; industry: string; status: string; competitor_count: number; user_prompt?: string; created_at: string; updated_at: string };
type AgentCard = { id: string; title: string; status: StepStatus; summaryLines: string[] };
type ChatMessage = { id: string; role: "user" | "assistant" | "system"; content: string };
type ReferenceLinkItem = { label: string; url: string };
type CollectWebItem = { title: string; url: string; isEllipsis?: boolean };

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

export function HomeWorkspace() {
  const [activeMenu, setActiveMenu] = useState<"new" | "agent" | "history">("new");
  const [viewMode, setViewMode] = useState<ViewMode>("welcome");
  const [query, setQuery] = useState("");
  const [taskSummary, setTaskSummary] = useState("");
  const [workspaceData, setWorkspaceData] = useState<WorkspacePayload | null>(null);
  const [agentCards, setAgentCards] = useState<AgentCard[]>([]);
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

  function mapStageStatus(status: string): StepStatus {
    if (status === "completed") return "done";
    if (status === "running") return "running";
    if (status === "failed") return "failed";
    return "pending";
  }

  function stageTitle(stage: string): string {
    const map: Record<string, string> = {
      plan: "计划智能体",
      collect: "采集智能体",
      normalize: "标准化阶段",
      analyze: "分析智能体",
      qa: "QA智能体",
      draft: "写作智能体",
      finalize: "完成阶段",
    };
    return map[stage] || stage;
  }

  function formatEventTime(input?: string): string {
    if (!input) return "--:--:--";
    const t = input.split("T")[1] || input;
    return t.slice(0, 8);
  }

  function buildCollectLogLines(workspace: WorkspacePayload): string[] {
    const events = workspace.observability?.events ?? [];
    const lines: string[] = [];
    for (const item of events) {
      const type = item.event_type || "";
      const payload = (item.payload?.envelope?.payload ?? {}) as Record<string, unknown>;
      if (type === "collector.competitor.started") {
        const competitor = String(payload.competitor || "").trim();
        if (competitor) lines.push(`[${formatEventTime(item.created_at)}] 开始采集: ${competitor}`);
      }
      if (type === "collector.competitor.completed") {
        const competitor = String(payload.competitor || "").trim();
        const elapsed = Number(payload.elapsed_sec || 0).toFixed(2);
        const evidenceCount = Number(payload.evidence_count || 0);
        if (competitor) lines.push(`[${formatEventTime(item.created_at)}] 完成采集: ${competitor} (耗时=${elapsed}s, 证据数=${evidenceCount})`);
      }
    }
    return lines;
  }

  function buildQaSummaryLines(workspace: WorkspacePayload): string[] {
    const qa = workspace.qa;
    if (!qa) return [];
    const lines = [`质检结论：${qa.passed ? "通过" : "未通过"}`, `问题数：${qa.issue_count ?? 0}`];
    if (qa.target_agent) lines.push(`打回目标：${qa.target_agent}`);
    const issuePreview = (qa.issues ?? []).slice(0, 2).map((item) => `${item.code || "issue"}: ${item.message || ""}`);
    return [...lines, ...issuePreview];
  }

  function buildPlanSummaryLines(workspace: WorkspacePayload): string[] {
    const competitors = workspace.run?.planned_competitors ?? [];
    const fields = workspace.run?.schema_fields ?? [];
    const competitorText = competitors.length ? competitors.join("、") : "待规划";
    const fieldText = fields.length ? fields.join("、") : "待生成";
    return [`竞品数：${competitors.length}`, `竞品名称：${competitorText}`, `字段数：${fields.length}`, `字段：${fieldText}`];
  }

  function buildAgentCardsFromWorkspace(workspace: WorkspacePayload): AgentCard[] {
    const stages = workspace.workflow?.agent_stages ?? [];
    const cards: AgentCard[] = stages.map((stage, index) => {
      const summaryLines: string[] = [];
      if (stage.stage === "plan") summaryLines.push(...buildPlanSummaryLines(workspace));
      if (stage.summary?.trim()) summaryLines.push(stage.summary.trim());
      if (stage.stage === "collect") summaryLines.push(...buildCollectLogLines(workspace));
      if (stage.stage === "qa") summaryLines.push(...buildQaSummaryLines(workspace));
      if (!summaryLines.length) summaryLines.push("等待执行...");
      return { id: `${stage.stage}-${index}`, title: `${index + 1}. ${stageTitle(stage.stage)}`, status: mapStageStatus(stage.status), summaryLines };
    });

    const recollectItems = workspace.qa?.collect_items ?? [];
    const events = workspace.observability?.events ?? [];
    const recollectCollectEvents = events.filter((item) => Number(item.payload?.envelope?.attempt || 1) > 1 && item.stage === "collect");
    const recollectAnalyzeEvents = events.filter((item) => Number(item.payload?.envelope?.attempt || 1) > 1 && item.stage === "analyze");
    const collectDone = recollectCollectEvents.some((item) => item.event_type === "collect.completed");
    const analyzeDone = recollectAnalyzeEvents.some((item) => item.event_type === "analyze.completed");
    if (recollectItems.length) {
      const competitors = Array.from(new Set(recollectItems.map((item) => String(item.competitor || "").trim()).filter(Boolean)));
      cards.splice(4, 0,
        {
          id: "recollect-route",
          title: "回采链路",
          status: "running",
          summaryLines: [`需回采：${recollectItems.length} 项`, `目标竞品：${competitors.join("、") || "待确认"}`, "执行链路：采集智能体 -> 分析智能体 -> 写作智能体"],
        },
        {
          id: "recollect-collect",
          title: "采集智能体（回采）",
          status: collectDone ? "done" : recollectCollectEvents.length ? "running" : "pending",
          summaryLines: [`回采目标竞品：${competitors.join("、") || "待确认"}`, `回采任务数：${recollectItems.length}`],
        },
        {
          id: "recollect-analyze",
          title: "分析智能体（回采）",
          status: analyzeDone ? "done" : recollectAnalyzeEvents.length ? "running" : "pending",
          summaryLines: ["根据回采证据重新生成结构化结论。"],
        }
      );
    }
    return cards;
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

  function buildCollectWebPreview(workspace: WorkspacePayload): CollectWebItem[] {
    const refs = buildReferenceItems(workspace);
    const top: CollectWebItem[] = refs.slice(0, 6).map((item, i) => ({ title: `${i + 1}. ${item.label}`, url: item.url }));
    if (refs.length > 6) top.push({ title: "……", url: "", isEllipsis: true });
    return top;
  }

  function applyWorkspace(workspace: WorkspacePayload) {
    setWorkspaceData(workspace);
    const cards = buildAgentCardsFromWorkspace(workspace);
    if (cards.length) setAgentCards(cards);
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
    const source = new window.EventSource(`/runs/${runId}/stream`);
    streamRef.current = source;
    if (!isReconnect) reconnectAttemptRef.current = 0;

    source.addEventListener("workspace", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { workspace?: WorkspacePayload };
        if (payload.workspace) {
          applyWorkspace(payload.workspace);
          reconnectAttemptRef.current = 0;
        }
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
    setAgentCards(session.workspace_snapshot ? buildAgentCardsFromWorkspace(session.workspace_snapshot) : []);
    setViewMode("workspace");
    setActiveMenu("history");
    activeRunIdRef.current = session.active_run_id || "";
    if (session.active_run_id) {
      try {
        const latestWorkspace = await fetchRunWorkspace(session.active_run_id);
        applyWorkspace(latestWorkspace);
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
    setTaskSummary("");
    setWorkspaceData(null);
    setAgentCards([]);
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
      const [summaryResponse, runResponse] = await Promise.all([
        fetch("/runs/summary", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, language: "zh-CN" }),
        }),
        fetch("/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ industry: "", competitors: [], user_prompt: text, language: "zh-CN", timeframe: "last_12_months" }),
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
      setAgentCards([
        { id: "boot-plan", title: "1. 计划智能体", status: "running", summaryLines: [`任务摘要：${summary}`] },
        { id: "boot-collect", title: "2. 采集智能体", status: "pending", summaryLines: ["等待采集启动..."] },
        { id: "boot-analyze", title: "3. 分析智能体", status: "pending", summaryLines: ["等待分析启动..."] },
        { id: "boot-qa", title: "4. QA智能体", status: "pending", summaryLines: ["等待质检启动..."] },
        { id: "boot-draft", title: "5. 写作智能体", status: "pending", summaryLines: ["等待报告生成..."] },
      ]);
      setViewMode("workspace");
      setActiveMenu("history");
      setQuery("");
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
          </div>
        ) : (
          <section className="workspace-chat-shell" aria-label="演示对话工作区">
            <header className="workspace-topbar"><h1>{displaySummary || "分析进行中"}</h1></header>
            <div className="workspace-scroll" ref={scrollRef}>
              {error ? <div className="error-banner" role="alert">{error}</div> : null}
              <div className="workspace-conversation">
                {chatMessages.map((message) => (
                  <div key={message.id} className={`message-row ${message.role}`}><div className={`message-bubble ${message.role}`}>{message.content}</div></div>
                ))}
                <div className="workspace-lower">
                  <section className="thought-chain-panel" aria-label="智能体思考链">
                    <h2>智能体执行卡片</h2>
                    {agentCards.length === 0 ? <p className="empty-state">等待智能体分析中...</p> : (
                      <ol className="thought-list agent-card-list">
                        {agentCards.map((step) => (
                          <li key={step.id} className="thought-item agent-card">
                            <div className="thought-head">
                              <span>{step.title}</span>
                              <span className={`status-pill ${step.status}`}>{step.status === "pending" ? "待执行" : step.status === "running" ? "执行中" : step.status === "failed" ? "失败" : "已完成"}</span>
                            </div>
                            <div className="agent-summary-block">{step.summaryLines.map((line, idx) => <p key={`${step.id}-${idx}`}>{line}</p>)}</div>
                            {step.id.includes("collect") && workspaceData ? (
                              <div className="collect-web-preview">
                                <strong>采集网页（前6条）</strong>
                                {buildCollectWebPreview(workspaceData).map((item, idx) => (
                                  <p key={`collect-web-${idx}`}>{item.isEllipsis ? <span>……</span> : <a className="collect-web-link" href={item.url} target="_blank" rel="noreferrer">{item.title}</a>}</p>
                                ))}
                              </div>
                            ) : null}
                          </li>
                        ))}
                      </ol>
                    )}

                    {workspaceData && ((workspaceData.report?.markdown || "").trim() || agentCards.some((item) => item.id.includes("draft") && item.status === "done")) ? (
                      <section className="report-card-section" aria-label="报告下载区">
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

                    {workspaceData && ((workspaceData.report?.markdown || "").trim() || agentCards.some((item) => item.id.includes("draft") && item.status === "done")) ? (
                      <section className="reference-section" aria-label="参考文献">
                        <details>
                          <summary>全部参考资料</summary>
                          <div className="reference-list">
                            {buildReferenceItems(workspaceData).map((item, idx) => (
                              <div className="reference-item" key={`ref-${idx}-${item.url}`}><a href={item.url} target="_blank" rel="noreferrer">{idx + 1}. {item.label}</a></div>
                            ))}
                          </div>
                        </details>
                      </section>
                    ) : null}
                  </section>
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
