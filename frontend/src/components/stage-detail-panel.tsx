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
};

const STAGE_TITLES: Record<StageName, string> = {
  plan: "计划智能体",
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
}: StageDetailPanelProps) {
  const steps = trace?.steps ?? [];
  const llmCalls = steps.filter(isLlmCallStep);
  const workflowNodes = workflow?.nodes ?? [];

  return (
    <aside className="workspace-panel stage-detail-panel" aria-label="阶段详情">
      <div className="workspace-panel-header">
        <div>
          <p className="workspace-eyebrow">Selected Stage</p>
          <h2>{stage ? STAGE_TITLES[stage] : "阶段详情"}</h2>
        </div>
        {stageCard?.status ? (
          <span className={`status-pill ${stageCard.status}`}>{statusText(stageCard.status)}</span>
        ) : null}
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
                <span>Total Tokens</span>
                <strong>{trace?.summary?.total_tokens ?? 0}</strong>
              </article>
            </div>
            <p className="detail-copy">{stageCard.summary || "暂无阶段摘要。"}</p>
            <div className="token-strip">
              <span>Prompt {trace?.summary?.prompt_tokens ?? 0}</span>
              <span>Completion {trace?.summary?.completion_tokens ?? 0}</span>
              <span>Events {trace?.summary?.event_count ?? events.length}</span>
              <span>Handoffs {trace?.summary?.handoff_count ?? 0}</span>
            </div>
          </section>

          <section className="detail-section">
            <h3>输入 {"->"} 输出</h3>
            {handoff ? (
              <>
                <div className="schema-flow-card">
                  <div className="schema-flow-node">
                    <span>Input</span>
                    <strong>{handoff.input_schema?.schema_name || "UnknownInput"}</strong>
                  </div>
                  <div className="schema-flow-arrow" aria-hidden="true">
                    →
                  </div>
                  <div className="schema-flow-node">
                    <span>Output</span>
                    <strong>{handoff.output_schema?.schema_name || "UnknownOutput"}</strong>
                  </div>
                </div>
                <p className="detail-copy">{handoff.handoff_summary || "暂无交接摘要。"}</p>
                {handoff.handoff_highlights?.length ? (
                  <div className="highlight-row">
                    {handoff.handoff_highlights.map((item) => (
                      <span key={item}>{item}</span>
                    ))}
                  </div>
                ) : null}
                <details className="detail-disclosure">
                  <summary>查看交接原始负载</summary>
                  {renderJsonBlock({
                    input: handoff.input_schema?.payload || {},
                    output: handoff.output_schema?.payload || {},
                  })}
                </details>
              </>
            ) : (
              <p className="empty-state">该阶段暂无 handoff 数据。</p>
            )}
          </section>

          <section className="detail-section">
            <h3>内部流程</h3>
            {workflowNodes.length ? (
              <div className="mini-flow compact-scroll" role="list">
                {workflowNodes.map((node, index) => (
                  <div key={`${node}-${index}`} className="mini-flow-segment" role="listitem">
                    <span className="mini-flow-node">{node}</span>
                    {index < workflowNodes.length - 1 ? <span className="mini-flow-link" aria-hidden="true" /> : null}
                  </div>
                ))}
              </div>
            ) : (
              <p className="empty-state">该阶段暂无内部流程结构。</p>
            )}
          </section>

          <section className="detail-section">
            <details className="detail-disclosure">
              <summary>{`LLM Calls (${llmCalls.length})`}</summary>
              {!llmCalls.length ? (
                <p className="empty-state">该阶段没有记录到 LLM 调用。</p>
              ) : (
                <div className="call-list compact-scroll">
                  {llmCalls.map((call, index) => {
                    const callKey = buildStepKey(call, index);
                    const expanded = expandedCallKeys.includes(callKey);
                    return (
                      <article key={callKey} className="call-card compact">
                        <div className="call-card-head">
                          <div>
                            <strong>{call.display_name || call.trace_name || `Call ${index + 1}`}</strong>
                            <p>{call.model || "unknown model"}</p>
                          </div>
                          <div className="call-stats">
                            <span>{call.total_tokens ?? 0} tok</span>
                            <span>{call.latency_ms ?? 0} ms</span>
                          </div>
                        </div>
                        <button
                          type="button"
                          className="inline-toggle"
                          onClick={() => onToggleCall(callKey)}
                        >
                          {expanded ? "收起完整调用" : "展开完整调用"}
                        </button>
                        {expanded ? (
                          <div className="call-expanded">
                            <div className="call-preview-grid">
                              <div>
                                <span>Input Preview</span>
                                <pre>{call.input_preview || "暂无输入预览"}</pre>
                              </div>
                              <div>
                                <span>Output Preview</span>
                                <pre>{call.output_preview || "暂无输出预览"}</pre>
                              </div>
                            </div>
                            <div className="detail-grid compact">
                              <article>
                                <span>Prompt Tokens</span>
                                <strong>{call.prompt_tokens ?? 0}</strong>
                              </article>
                              <article>
                                <span>Completion Tokens</span>
                                <strong>{call.completion_tokens ?? 0}</strong>
                              </article>
                              <article>
                                <span>Finish Reason</span>
                                <strong>{call.finish_reason || "--"}</strong>
                              </article>
                              <article>
                                <span>Status</span>
                                <strong>{call.status || "--"}</strong>
                              </article>
                            </div>
                            <details className="detail-disclosure">
                              <summary>System Prompt</summary>
                              <pre className="json-block text-block">{call.system_prompt || "暂无 system prompt"}</pre>
                            </details>
                            <details className="detail-disclosure">
                              <summary>User Payload</summary>
                              {renderJsonBlock(call.user_payload || {})}
                            </details>
                            <details className="detail-disclosure">
                              <summary>Parsed Response</summary>
                              {renderJsonBlock(call.parsed_response || {})}
                            </details>
                            <details className="detail-disclosure">
                              <summary>Raw Response</summary>
                              {renderJsonBlock(call.raw_response || {})}
                            </details>
                          </div>
                        ) : null}
                      </article>
                    );
                  })}
                </div>
              )}
            </details>
          </section>

          <section className="detail-section">
            <details className="detail-disclosure">
              <summary>{`阶段事件 (${events.length})`}</summary>
              {!events.length ? (
                <p className="empty-state">该阶段暂无事件。</p>
              ) : (
                <div className="event-list stage-event-list compact-scroll" role="list">
                  {events.map((event, index) => {
                    const eventKey = `${event.event_id || "stage"}:${event.created_at || index}:${event.event_type || "event"}`;
                    const expanded = expandedEventKeys.includes(eventKey);
                    const preview = event.payload ? JSON.stringify(event.payload).slice(0, 120) : "";
                    return (
                      <article key={eventKey} className="event-row compact" role="listitem">
                        <div className="event-row-meta">
                          <span>{formatEventTime(event.created_at)}</span>
                          <strong>{event.event_type || "event"}</strong>
                        </div>
                        {preview ? <p className="event-inline-preview">{preview}{preview.length >= 120 ? "..." : ""}</p> : null}
                        {event.payload ? (
                          <>
                            <button
                              type="button"
                              className="inline-toggle"
                              onClick={() => onToggleEvent(eventKey)}
                            >
                              {expanded ? "收起详情" : "查看详情"}
                            </button>
                            {expanded ? renderJsonBlock(event.payload) : null}
                          </>
                        ) : null}
                      </article>
                    );
                  })}
                </div>
              )}
            </details>
          </section>
          </div>
        )}
      </div>
    </aside>
  );
}
