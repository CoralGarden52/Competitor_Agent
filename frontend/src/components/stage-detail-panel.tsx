import type {
  AgentHandoff,
  AgentStageCard,
  AgentTrace,
  AgentTraceLlmCallStep,
  AgentTraceStep,
  AgentWorkflow,
  StageName,
  WorkspaceEvent,
} from "@/components/workspace-types";

type StageDetailPanelProps = {
  stage: StageName | null;
  stageCard: AgentStageCard | null;
  handoff: AgentHandoff | null;
  workflow: AgentWorkflow | null;
  trace: AgentTrace | null;
  events: WorkspaceEvent[];
  expandedCallKeys: string[];
  onToggleCall: (key: string) => void;
  expandedEventKeys: string[];
  onToggleEvent: (key: string) => void;
  todoPlan?: Record<string, unknown> | null;
};

const STAGE_TITLES: Record<StageName, string> = {
  plan: "计划智能体",
  confirm_plan: "确认节点",
  collect: "采集智能体",
  normalize: "标准化阶段",
  analyze: "分析智能体",
  draft: "写作智能体",
  qa: "QA 智能体",
  finalize: "完成阶段",
};

function formatDuration(durationMs?: number | null): string {
  if (!durationMs || durationMs <= 0) return "--";
  if (durationMs >= 1000) return `${(durationMs / 1000).toFixed(1)}s`;
  return `${durationMs}ms`;
}

function formatEventTime(input?: string): string {
  if (!input) return "--:--:--";
  const timeText = input.split("T")[1] || input;
  return timeText.slice(0, 8);
}

function statusText(status?: string): string {
  if (status === "completed") return "已完成";
  if (status === "awaiting_user_confirmation") return "待确认";
  if (status === "replanning") return "重规划中";
  if (status === "running") return "进行中";
  if (status === "failed") return "失败";
  return "待执行";
}

function buildStepKey(step: AgentTraceStep, index: number): string {
  if (step.step_type === "llm_call") {
    return `llm:${step.trace_name || step.display_name || "call"}:${step.step_order || index}`;
  }
  return `${step.step_type}:${step.display_name || "step"}:${step.created_at || index}`;
}

function isLlmCallStep(step: AgentTraceStep): step is AgentTraceLlmCallStep {
  return step.step_type === "llm_call";
}

function renderJsonBlock(payload: unknown) {
  if (!payload) return <p className="empty-state">暂无数据</p>;
  return <pre className="json-block">{JSON.stringify(payload, null, 2)}</pre>;
}

export function StageDetailPanel({
  stage,
  stageCard,
  handoff,
  workflow,
  trace,
  events,
  expandedCallKeys,
  onToggleCall,
  expandedEventKeys,
  onToggleEvent,
  todoPlan,
}: StageDetailPanelProps) {
  const steps = trace?.steps ?? [];
  const llmCalls = steps.filter(isLlmCallStep);
  const workflowNodes = workflow?.nodes ?? [];
  const todoTasks = Array.isArray(todoPlan?.tasks) ? (todoPlan.tasks as Array<Record<string, unknown>>) : [];
  const stageTodoTasks = todoTasks.filter((task) => String(task.stage || "") === String(stage || ""));

  return (
    <aside className="workspace-panel stage-detail-panel" aria-label="阶段详情">
      <div className="workspace-panel-header">
        <div>
          <p className="workspace-eyebrow">Selected Stage</p>
          <h2>{stage ? STAGE_TITLES[stage] : "阶段详情"}</h2>
        </div>
        {stageCard?.status ? <span className={`status-pill ${stageCard.status}`}>{statusText(stageCard.status)}</span> : null}
      </div>

      <div className="panel-scroll-body compact-scroll">
        {!stage || !stageCard ? (
          <p className="empty-state">请选择一个阶段查看协作细节。</p>
        ) : (
          <div className="detail-stack">
            <section className="detail-section">
              <div className="detail-grid">
                <article>
                  <span>Agent</span>
                  <strong>{stageCard.agent || stage}</strong>
                </article>
                <article>
                  <span>Duration</span>
                  <strong>{formatDuration(stageCard.duration_ms)}</strong>
                </article>
                <article>
                  <span>LLM Calls</span>
                  <strong>{trace?.summary?.llm_call_count ?? 0}</strong>
                </article>
                <article>
                  <span>Events</span>
                  <strong>{events.length}</strong>
                </article>
              </div>
              {stageCard.summary ? <p>{stageCard.summary}</p> : null}
            </section>

            <section className="detail-section">
              <h3>工作流节点</h3>
              {workflowNodes.length ? (
                <div className="chip-row">
                  {workflowNodes.map((node) => <span key={node} className="info-chip">{node}</span>)}
                </div>
              ) : (
                <p className="empty-state">暂无工作流节点。</p>
              )}
            </section>

            <section className="detail-section">
              <h3>待办任务</h3>
              {stageTodoTasks.length ? (
                <div className="detail-list">
                  {stageTodoTasks.map((task, index) => (
                    <article key={`todo-${index}`}>
                      <strong>{String(task.title || "任务")}</strong>
                      <p>{String(task.status || "pending")}</p>
                    </article>
                  ))}
                </div>
              ) : (
                <p className="empty-state">暂无阶段待办。</p>
              )}
            </section>

            <section className="detail-section">
              <h3>阶段交接</h3>
              {handoff ? (
                <>
                  <p>{handoff.handoff_summary || "已生成阶段交接数据。"}</p>
                  {renderJsonBlock(handoff.output_schema?.payload)}
                </>
              ) : (
                <p className="empty-state">暂无 handoff。</p>
              )}
            </section>

            <section className="detail-section">
              <h3>LLM 调用</h3>
              {llmCalls.length ? (
                <div className="detail-list">
                  {llmCalls.map((call, index) => {
                    const key = buildStepKey(call, index);
                    const expanded = expandedCallKeys.includes(key);
                    return (
                      <article key={key}>
                        <button type="button" className="detail-toggle" onClick={() => onToggleCall(key)}>
                          <strong>{call.display_name || call.trace_name || `LLM Call ${index + 1}`}</strong>
                          <span>{expanded ? "收起" : "展开"}</span>
                        </button>
                        <p>{`${call.model || "model"} · ${call.total_tokens || 0} tokens · ${formatDuration(call.latency_ms)}`}</p>
                        {expanded ? renderJsonBlock(call.parsed_response || call.raw_response || {}) : null}
                      </article>
                    );
                  })}
                </div>
              ) : (
                <p className="empty-state">暂无 LLM 调用记录。</p>
              )}
            </section>

            <section className="detail-section">
              <h3>阶段事件</h3>
              {events.length ? (
                <div className="detail-list">
                  {events.map((event, index) => {
                    const key = `${event.event_id || index}:${event.event_type || "event"}`;
                    const expanded = expandedEventKeys.includes(key);
                    return (
                      <article key={key}>
                        <button type="button" className="detail-toggle" onClick={() => onToggleEvent(key)}>
                          <strong>{String(event.event_type || "event")}</strong>
                          <span>{formatEventTime(event.created_at)}</span>
                        </button>
                        {expanded ? renderJsonBlock(event.payload || {}) : null}
                      </article>
                    );
                  })}
                </div>
              ) : (
                <p className="empty-state">暂无事件记录。</p>
              )}
            </section>
          </div>
        )}
      </div>
    </aside>
  );
}
