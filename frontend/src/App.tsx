import { startTransition, useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'
import { AppSource, LiveProgress, PreviewPayload, WorkspacePayload, deriveSourceLinks, downloadLogs, downloadMarkdown, loadMockWorkspace, runLivePrompt } from './lib/workspace'

type Role = 'user' | 'assistant' | 'system'
type TabKey = 'workflow' | 'quality' | 'trace' | 'report'

interface ChatMessage {
  id: string
  role: Role
  content: string
}

const DEFAULT_PROMPT =
  '请分析在线会议软件领域的竞品，重点关注功能树、AI会议能力、定价模型、用户反馈和私有化部署支持。'

function markdownToHtml(markdown: string): string {
  const escaped = markdown
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  const lines = escaped.split('\n')
  const html: string[] = []
  let inList = false

  for (const rawLine of lines) {
    const line = rawLine.trimEnd()
    if (!line.trim()) {
      if (inList) {
        html.push('</ul>')
        inList = false
      }
      html.push('<br/>')
      continue
    }
    if (/^#{1,6}\s/.test(line)) {
      if (inList) {
        html.push('</ul>')
        inList = false
      }
      const level = Math.min(6, line.match(/^#+/)?.[0].length ?? 1)
      html.push(`<h${level}>${line.replace(/^#{1,6}\s*/, '')}</h${level}>`)
      continue
    }
    if (/^- /.test(line)) {
      if (!inList) {
        html.push('<ul>')
        inList = true
      }
      html.push(`<li>${line.replace(/^- /, '')}</li>`)
      continue
    }
    if (inList) {
      html.push('</ul>')
      inList = false
    }
    html.push(`<p>${line}</p>`)
  }

  if (inList) html.push('</ul>')

  return html
    .join('\n')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\[(.*?)\]\((https?:\/\/.*?)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
}

function draftStorageKey(runId: string): string {
  return `competitor-report-draft:${runId}`
}

function buildDagLayout(nodes: string[]) {
  const positions = new Map<string, { x: number; y: number }>()
  const columns = 3
  nodes.forEach((node, index) => {
    const row = Math.floor(index / columns)
    const col = row % 2 === 0 ? index % columns : columns - 1 - (index % columns)
    positions.set(node, { x: 140 + col * 270, y: 120 + row * 200 })
  })
  const rows = Math.max(1, Math.ceil(nodes.length / columns))
  return { positions, width: 860, height: 180 + rows * 200 }
}

function wrapSvgLines(text: string, maxChars: number, maxLines: number): string[] {
  const normalized = text.replace(/\s+/g, ' ').trim()
  if (!normalized) return []
  const lines: string[] = []
  let current = ''
  const chars = Array.from(normalized)
  let consumed = 0

  for (const char of chars) {
    const candidate = `${current}${char}`
    if (candidate.length <= maxChars) {
      current = candidate
      consumed += 1
      continue
    }
    if (current.trim()) lines.push(current.trim())
    current = char.trim() ? char : ''
    consumed += 1
    if (lines.length >= maxLines) break
  }

  if (lines.length < maxLines && current.trim()) lines.push(current.trim())

  const truncated = consumed < chars.length
  return lines.slice(0, maxLines).map((line, index, all) => {
    if (index !== all.length - 1 || !truncated) return line
    return `${line.slice(0, Math.max(0, maxChars - 1))}…`
  })
}

function splitHighlightItem(item: string): { label: string; value: string } | null {
  const normalized = item.trim()
  if (!normalized.includes(':')) return null
  const [label, ...rest] = normalized.split(':')
  const value = rest.join(':').trim()
  if (!label.trim() || !value) return null
  return { label: label.trim(), value }
}

function humanizeStepLabel(value: string): string {
  const cleaned = value
    .replace(/^agent\./, '')
    .replace(/^planner\./, '')
    .replace(/^collector\./, '')
    .replace(/^plan\./, '')
    .replace(/^analyze\./, '')
    .replace(/^draft\./, '')
    .replace(/^qa\./, '')
    .replace(/^field\./, 'field ')
    .replace(/_/g, ' ')
    .replace(/\./g, ' / ')
    .trim()
  return cleaned || value
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return '{}'
  }
}

function normalizeProviderStep(eventType: string): string {
  if (eventType.includes('search')) return 'search'
  if (eventType.includes('fetch')) return 'fetch'
  if (eventType.includes('fallback')) return 'fallback'
  if (eventType.includes('pricing') || eventType.includes('reranked')) return 'rerank'
  if (eventType.includes('persist')) return 'persist'
  return eventType.replace(/^collector\./, '')
}

function buildAgentWorkflow(args: {
  stage: string
  traces: Array<{ trace_name?: string; node_name?: string }>
  handoffs: Array<{ handoff_type?: string; payload?: Record<string, unknown> }>
  preview: { planner_meta?: Record<string, unknown> } | null
}): { nodes: string[]; edges: Array<{ from: string; to: string }> } {
  const { stage, traces, handoffs, preview } = args
  const nodes: string[] = []
  const seen = new Set<string>()

  const add = (value: string) => {
    const normalized = value.trim()
    if (!normalized || seen.has(normalized)) return
    seen.add(normalized)
    nodes.push(normalized)
  }

  add('input')

  if (stage === 'plan') {
    const plannerMeta =
      (handoffs[0]?.payload?.planner_meta as Record<string, unknown> | undefined) ??
      (preview?.planner_meta as Record<string, unknown> | undefined) ??
      {}
    const byStep = (plannerMeta?.llm_call_status_by_step as Record<string, unknown> | undefined) ?? {}
    for (const step of ['infer_industry', 'infer_product_profile', 'generate_search_queries', 'discover_competitors_grouped', 'plan_dynamic_schema']) {
      if (step in byStep) add(humanizeStepLabel(step))
    }
  }

  if (stage === 'collect') {
    const payload = handoffs[0]?.payload ?? {}
    const providerEvents = Array.isArray(payload.provider_events) ? payload.provider_events : []
    for (const event of providerEvents.slice(0, 60)) {
      const eventType = String((event as Record<string, unknown>).event_type ?? '').trim()
      if (!eventType) continue
      add(humanizeStepLabel(normalizeProviderStep(eventType)))
    }
  }

  for (const trace of traces) {
    const traceName = String(trace.trace_name ?? '').trim()
    if (!traceName) continue
    const scoped = traceName.replace(new RegExp(`^agent\\.${stage}\\.`), '')
    add(humanizeStepLabel(scoped || traceName))
  }

  if (handoffs.length) add('handoff')
  add('output')

  const edges: Array<{ from: string; to: string }> = []
  for (let index = 0; index < nodes.length - 1; index += 1) {
    edges.push({ from: nodes[index], to: nodes[index + 1] })
  }
  return { nodes, edges }
}

function App() {
  const [source, setSource] = useState<AppSource>('mock')
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT)
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content: '请输入你的竞品分析任务。我会先完成竞品发现和字段规划，再返回完整工作流、QA 结果、日志与可编辑报告。',
    },
  ])
  const [preview, setPreview] = useState<PreviewPayload | null>(null)
  const [workspace, setWorkspace] = useState<WorkspacePayload | null>(null)
  const [reportDraft, setReportDraft] = useState('')
  const [loading, setLoading] = useState(false)
  const [tab, setTab] = useState<TabKey>('workflow')
  const [selectedStage, setSelectedStage] = useState<string>('')
  const messageListRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    messageListRef.current?.scrollTo({ top: messageListRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const sourceLinks = useMemo(() => (workspace ? deriveSourceLinks(workspace) : []), [workspace])
  const totalTokens = useMemo(
    () => workspace?.observability.llm_calls.reduce((sum, item) => sum + Number(item.total_tokens ?? 0), 0) ?? 0,
    [workspace]
  )
  const deferredReportDraft = useDeferredValue(reportDraft)
  const reportHtml = useMemo(() => markdownToHtml(deferredReportDraft), [deferredReportDraft])
  const dagLayout = useMemo(
    () => buildDagLayout(workspace?.workflow.dag.nodes ?? []),
    [workspace?.workflow.dag.nodes]
  )
  const selectedStageRecord = useMemo(
    () => workspace?.workflow.agent_stages.find((item) => item.stage === selectedStage) ?? workspace?.workflow.agent_stages[0] ?? null,
    [workspace, selectedStage]
  )
  const selectedHandoffs = useMemo(
    () => workspace?.workflow.handoffs.filter((item) => item.stage === (selectedStageRecord?.stage ?? '')) ?? [],
    [workspace, selectedStageRecord]
  )
  const selectedTraces = useMemo(() => {
    const stage = selectedStageRecord?.stage ?? ''
    const stageLogs = workspace?.observability.stage_logs?.[stage]
    if (stageLogs?.llm_calls && Array.isArray(stageLogs.llm_calls)) {
      return stageLogs.llm_calls as Array<Record<string, unknown>>
    }
    return workspace?.observability.llm_calls.filter((item) => item.node_name === stage) ?? []
  }, [workspace, selectedStageRecord])
  const selectedAgentWorkflow = useMemo(
    () =>
      workspace?.workflow.agent_workflows?.[selectedStageRecord?.stage ?? ''] ??
      buildAgentWorkflow({
        stage: selectedStageRecord?.stage ?? '',
        traces: selectedTraces as Array<{ trace_name?: string; node_name?: string }>,
        handoffs: selectedHandoffs,
        preview,
      }),
    [workspace, selectedStageRecord, selectedTraces, selectedHandoffs, preview]
  )
  const savedDraftAvailable = useMemo(() => {
    if (!workspace || typeof window === 'undefined') return false
    return Boolean(window.localStorage.getItem(draftStorageKey(workspace.run.run_id)))
  }, [workspace, reportDraft])

  function pushMessage(role: Role, content: string) {
    setMessages((prev) => [...prev, { id: `${role}-${Date.now()}-${Math.random()}`, role, content }])
  }

  function updateMessage(id: string, content: string) {
    setMessages((prev) => prev.map((message) => (message.id === id ? { ...message, content } : message)))
  }

  useEffect(() => {
    if (!workspace || typeof window === 'undefined') return
    const key = draftStorageKey(workspace.run.run_id)
    const saved = window.localStorage.getItem(key)
    if (saved !== null) {
      setReportDraft(saved)
      return
    }
    setReportDraft(workspace.report.markdown)
  }, [workspace?.run.run_id, workspace?.report.markdown])

  useEffect(() => {
    if (!workspace || typeof window === 'undefined') return
    window.localStorage.setItem(draftStorageKey(workspace.run.run_id), reportDraft)
  }, [workspace?.run.run_id, reportDraft, workspace])

  useEffect(() => {
    if (!workspace?.workflow.agent_stages.length) return
    setSelectedStage((current) => current || workspace.workflow.agent_stages[0].stage)
  }, [workspace?.workflow.agent_stages])

  async function handleSubmit() {
    const value = prompt.trim()
    if (!value || loading) return
    setLoading(true)
    setTab('workflow')
    pushMessage('user', value)

    try {
      if (source === 'mock') {
        pushMessage('system', '正在载入演示快照。')
        const result = await loadMockWorkspace()
        startTransition(() => {
          setPreview(result.preview)
          setWorkspace(result.workspace)
          setReportDraft(result.workspace.report.markdown)
        })
        pushMessage(
          'assistant',
          `演示结果已准备好：行业 ${result.preview.inferred_industry}，竞品 ${result.preview.planned_competitors.join('、')}。你现在可以查看工作流、QA 和最终报告。`
        )
      } else {
        const progressId = `system-progress-${Date.now()}`
        setMessages((prev) => [
          ...prev,
          { id: progressId, role: 'system', content: '正在执行 planner、collect、analyze、qa 和 report 流程。' },
        ])
        let lastProgressKey = ''
        const result = await runLivePrompt(value, (progress: LiveProgress) => {
          if (progress.workspace) {
            startTransition(() => {
              setWorkspace(progress.workspace)
            })
          }
          const completed = progress.completed_stages.length ? `已完成：${progress.completed_stages.join(' → ')}` : '正在初始化运行。'
          const current = progress.latest_stage ? `当前阶段：${progress.latest_stage}` : '当前阶段：waiting'
          const content = `正在执行 planner、collect、analyze、qa 和 report 流程。\n\n${completed}\n${current}\nrun_id: ${progress.run_id}\nstatus: ${progress.status}`
          const progressKey = `${progress.completed_stages.join('|')}::${progress.latest_stage}::${progress.status}`
          if (progressKey !== lastProgressKey) {
            lastProgressKey = progressKey
            updateMessage(progressId, content)
          }
        })
        startTransition(() => {
          setPreview(result.preview)
          setWorkspace(result.workspace)
          setReportDraft(result.workspace.report.markdown)
        })
        updateMessage(
          progressId,
          `分析完成。\n\n已完成：${result.workspace.workflow.agent_stages
            .filter((item) => item.status === 'completed')
            .map((item) => item.stage)
            .join(' → ')}\n当前阶段：finalize\nrun_id: ${result.workspace.run.run_id}\nstatus: ${result.workspace.run.status}`
        )
        pushMessage(
          'assistant',
          `分析完成：行业 ${result.preview.inferred_industry}，竞品 ${result.preview.planned_competitors.join('、')}，日志和报告已可查看与下载。`
        )
      }
    } catch (error) {
      pushMessage('assistant', `执行失败：${error instanceof Error ? error.message : 'unknown error'}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <h1>Competitor Analysis Assistant</h1>
          <p>对话发起竞品分析，系统自动完成发现、采集、分析、质检与报告交付。</p>
        </div>
      </header>

      <main className="workspace-layout">
        <section className="chat-pane">
          <div className="chat-toolbar">
            <label className="toolbar-field">
              <span>数据源</span>
              <select value={source} onChange={(event) => setSource(event.target.value as AppSource)} disabled={loading}>
                <option value="mock">Mock</option>
                <option value="live">Live API</option>
              </select>
            </label>
            <span className="toolbar-status">{loading ? '运行中…' : '准备就绪'}</span>
          </div>

          <div className="message-list" ref={messageListRef}>
            {messages.map((message) => (
              <article key={message.id} className={`message-bubble ${message.role}`}>
                <span className="message-role">{message.role}</span>
                <p>{message.content}</p>
              </article>
            ))}
          </div>

          <div className="composer">
            <textarea
              rows={4}
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="描述你的竞品分析任务，例如：评估在线会议软件的功能、定价、用户反馈与风险。"
              disabled={loading}
            />
            <div className="composer-actions">
              <button type="button" onClick={() => void handleSubmit()} disabled={loading || !prompt.trim()}>
                {source === 'mock' ? '载入演示' : '发送'}
              </button>
            </div>
          </div>
        </section>

        <section className="result-pane">
          {!workspace ? (
            <div className="empty-state">
              <h2>等待任务</h2>
              <p>提交问题后，这里会展示完整工作流、Agent 交接、QA 结果、日志与可编辑报告。</p>
            </div>
          ) : (
            <>
              <div className="result-header">
                <div>
                  <h2>{preview?.inferred_industry || workspace.run.industry}</h2>
                  <p>{preview?.planned_competitors.join('、') || workspace.run.planned_competitors.join('、')}</p>
                </div>
                <div className="result-meta">
                  <span>{workspace.run.status}</span>
                  <span>{workspace.run.evidence_count} 条证据</span>
                  <span>{totalTokens} tokens</span>
                </div>
              </div>

              <div className="tab-bar">
                {(['workflow', 'quality', 'trace', 'report'] as const).map((item) => (
                  <button key={item} type="button" className={tab === item ? 'active' : ''} onClick={() => setTab(item)}>
                    {item.toUpperCase()}
                  </button>
                ))}
              </div>

              <div className="panel-body">
                {tab === 'workflow' ? (
                  <div className="panel-section">
                    <div className="dag-canvas-card">
                      <div className="dag-canvas-head">
                        <div>
                          <h3>Workflow Graph</h3>
                          <p>从用户提问到报告交付的完整 Agent 执行路径。</p>
                        </div>
                        <div className="token-list">
                          {workspace.workflow.dag.nodes.map((node) => (
                            <span key={node}>{node}</span>
                          ))}
                        </div>
                      </div>
                      <div className="dag-canvas">
                        <svg viewBox={`0 0 ${dagLayout.width} ${dagLayout.height}`} className="dag-svg" role="img" aria-label="workflow dag">
                          <defs>
                            <marker id="dag-arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
                              <path d="M0,0 L8,3 L0,6 Z" fill="#98a2b3" />
                            </marker>
                          </defs>
                          {workspace.workflow.dag.edges.map((edge) => {
                            const from = dagLayout.positions.get(edge.from)
                            const to = dagLayout.positions.get(edge.to)
                            if (!from || !to) return null
                            const controlX = (from.x + to.x) / 2
                            return (
                              <path
                                key={`${edge.from}-${edge.to}`}
                                d={`M ${from.x} ${from.y} C ${controlX} ${from.y}, ${controlX} ${to.y}, ${to.x} ${to.y}`}
                                className="dag-edge"
                                markerEnd="url(#dag-arrow)"
                              />
                            )
                          })}
                          {workspace.workflow.agent_stages.map((stage) => {
                            const position = dagLayout.positions.get(stage.stage)
                            if (!position) return null
                            const agentLines = wrapSvgLines(stage.agent, 16, 2)
                            const summaryLines = wrapSvgLines(stage.summary || '', 20, 3)
                            return (
                              <g
                                key={stage.stage}
                                transform={`translate(${position.x - 104}, ${position.y - 60})`}
                                className="dag-node-group"
                                onClick={() => setSelectedStage(stage.stage)}
                              >
                                <rect
                                  width="208"
                                  height="120"
                                  rx="20"
                                  className={`dag-rect ${stage.status} ${selectedStageRecord?.stage === stage.stage ? 'selected' : ''}`}
                                />
                                <text x="18" y="24" className="dag-stage-label">
                                  {stage.stage}
                                </text>
                                <text x="18" y="46" className="dag-agent-label">
                                  {agentLines.map((line, index) => (
                                    <tspan key={`${stage.stage}-agent-${index}`} x="18" dy={index === 0 ? 0 : 16}>
                                      {line}
                                    </tspan>
                                  ))}
                                </text>
                                <text x="18" y="76" className="dag-summary-label">
                                  {summaryLines.map((line, index) => (
                                    <tspan key={`${stage.stage}-summary-${index}`} x="18" dy={index === 0 ? 0 : 14}>
                                      {line}
                                    </tspan>
                                  ))}
                                </text>
                              </g>
                            )
                          })}
                        </svg>
                      </div>
                    </div>

                    <div className="stage-grid">
                      {workspace.workflow.agent_stages.map((stage) => (
                        <article
                          key={stage.stage}
                          className={`stage-card ${selectedStageRecord?.stage === stage.stage ? 'selected' : ''}`}
                          onClick={() => setSelectedStage(stage.stage)}
                        >
                          <div className="stage-head">
                            <span>{stage.stage}</span>
                            <strong>{stage.agent}</strong>
                          </div>
                          <p>{stage.summary}</p>
                          {stage.handoff_summary ? <small>{stage.handoff_summary}</small> : null}
                        </article>
                      ))}
                    </div>

                    {selectedStageRecord ? (
                      <div className="workflow-inspector">
                        <div className="inspector-head">
                          <div>
                            <h3>{selectedStageRecord.agent}</h3>
                            <p>{selectedStageRecord.summary}</p>
                          </div>
                          <span className="inspector-stage">{selectedStageRecord.stage}</span>
                        </div>

                        <div className="inspector-grid">
                          <div className="inspector-panel">
                            <h4>Agent Workflow</h4>
                            {selectedAgentWorkflow.nodes.length > 1 ? (
                              <div className="agent-workflow-strip">
                                {selectedAgentWorkflow.nodes.map((node, index) => (
                                  <div key={`${selectedStageRecord.stage}-${node}-${index}`} className="agent-workflow-step">
                                    <span>{node}</span>
                                    {index < selectedAgentWorkflow.nodes.length - 1 ? <i>→</i> : null}
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <p className="inspector-empty">这个阶段当前没有足够的内部步骤可视化数据。</p>
                            )}
                          </div>

                          <div className="inspector-panel">
                            <h4>Handoff</h4>
                            {selectedHandoffs.length ? (
                              selectedHandoffs.map((handoff) => (
                                <article key={`${handoff.stage}-${handoff.handoff_type}-${handoff.attempt}`} className="handoff-card">
                                  <h3>{handoff.handoff_type}</h3>
                                  <p>{handoff.summary}</p>
                                  <div className="handoff-highlight-list">
                                    {handoff.highlights.map((item) => (
                                      (() => {
                                        const detail = splitHighlightItem(item)
                                        if (detail) {
                                          return (
                                            <div key={item} className="handoff-highlight-item">
                                              <strong>{detail.label}</strong>
                                              <span>{detail.value}</span>
                                            </div>
                                          )
                                        }
                                        return (
                                          <div key={item} className="handoff-highlight-item plain">
                                            <span>{item}</span>
                                          </div>
                                        )
                                      })()
                                    ))}
                                  </div>
                                </article>
                              ))
                            ) : (
                              <p className="inspector-empty">这个阶段当前没有单独的 handoff 记录。</p>
                            )}
                          </div>

                          <div className="inspector-panel">
                            <h4>LLM Trace</h4>
                            {selectedTraces.length ? (
                              <div className="trace-grid compact">
                                {selectedTraces.map((trace) => (
                                  <article key={`${trace.trace_id ?? trace.trace_name ?? Math.random()}`} className="trace-card">
                                    <h3>{String(trace.trace_name ?? trace.agent_name ?? 'trace')}</h3>
                                    <p>{String(trace.agent_name ?? trace.node_name ?? '')}</p>
                                    <div className="trace-meta">
                                      <span>{String(trace.status ?? 'unknown')}</span>
                                      <span>{Number(trace.total_tokens ?? 0)} tokens</span>
                                      <span>{Number(trace.latency_ms ?? 0)} ms</span>
                                    </div>
                                    <details className="trace-details">
                                      <summary>查看 Prompt / 输入 / 输出</summary>
                                      <div className="trace-details-body">
                                        <h5>System Prompt</h5>
                                        <pre>{String(trace.system_prompt ?? '')}</pre>
                                        <h5>User Payload</h5>
                                        <pre>{safeJson(trace.user_payload)}</pre>
                                        <h5>Parsed Output</h5>
                                        <pre>{safeJson(trace.parsed_response)}</pre>
                                      </div>
                                    </details>
                                  </article>
                                ))}
                              </div>
                            ) : (
                              <p className="inspector-empty">这个阶段当前没有单独的 LLM 调用记录。</p>
                            )}
                          </div>
                        </div>
                      </div>
                    ) : null}
                  </div>
                ) : null}

                {tab === 'quality' ? (
                  <div className="panel-section">
                    <div className="summary-cards">
                      <article className="info-card">
                        <strong>QA 状态</strong>
                        <p>{workspace.qa.passed ? '已通过' : '需回采或重做'}</p>
                      </article>
                      <article className="info-card">
                        <strong>目标 Agent</strong>
                        <p>{workspace.qa.target_agent || '无'}</p>
                      </article>
                      <article className="info-card">
                        <strong>问题数量</strong>
                        <p>{workspace.qa.issue_count}</p>
                      </article>
                    </div>

                    <div className="detail-grid">
                      {workspace.qa.collect_items.map((item) => (
                        <article key={`${item.competitor}-${item.field_name}`} className="detail-card">
                          <h3>
                            {item.competitor} / {item.field_name}
                          </h3>
                          <p>{item.reason}</p>
                          <small>Priority {item.priority}</small>
                        </article>
                      ))}
                    </div>

                    <div className="source-section">
                      <h3>来源链接</h3>
                      <div className="source-list">
                        {sourceLinks.map((link) => (
                          <a key={link} href={link} target="_blank" rel="noreferrer">
                            {link}
                          </a>
                        ))}
                      </div>
                    </div>
                  </div>
                ) : null}

                {tab === 'trace' ? (
                  <div className="panel-section">
                    <div className="trace-grid">
                      {workspace.observability.llm_calls.map((trace) => (
                        <article key={`${trace.trace_id ?? trace.trace_name ?? Math.random()}`} className="trace-card">
                          <h3>{String(trace.trace_name ?? trace.agent_name ?? 'trace')}</h3>
                          <p>{String(trace.agent_name ?? trace.node_name ?? '')}</p>
                          <div className="trace-meta">
                            <span>{String(trace.status ?? 'unknown')}</span>
                            <span>{Number(trace.total_tokens ?? 0)} tokens</span>
                            <span>{Number(trace.latency_ms ?? 0)} ms</span>
                          </div>
                          <details className="trace-details">
                            <summary>查看 Prompt / 输入 / 输出</summary>
                            <div className="trace-details-body">
                              <h5>System Prompt</h5>
                              <pre>{String(trace.system_prompt ?? '')}</pre>
                              <h5>User Payload</h5>
                              <pre>{safeJson(trace.user_payload)}</pre>
                              <h5>Parsed Output</h5>
                              <pre>{safeJson(trace.parsed_response)}</pre>
                            </div>
                          </details>
                        </article>
                      ))}
                    </div>
                    <div className="download-row">
                      <button type="button" onClick={() => void downloadLogs(workspace.observability.log_download_path, workspace)}>
                        下载日志 JSON
                      </button>
                    </div>
                  </div>
                ) : null}

                {tab === 'report' ? (
                  <div className="panel-section report-panel">
                    <div className="report-actions">
                      <button type="button" onClick={() => setReportDraft(workspace.report.markdown)}>
                        重置为原始报告
                      </button>
                      <button type="button" onClick={() => downloadMarkdown(workspace.run.run_id, reportDraft)}>
                        下载当前报告 .md
                      </button>
                    </div>
                    <div className="draft-status">
                      <span>Run {workspace.run.run_id}</span>
                      <span>{savedDraftAvailable ? '草稿已自动保存到本地' : '当前使用原始报告内容'}</span>
                    </div>
                    <div className="report-split">
                      <div className="report-pane">
                        <div className="report-pane-head">
                          <h3>Markdown Editor</h3>
                          <p>面向交付前的最终润色与人工修订。</p>
                        </div>
                        <textarea className="report-editor" value={reportDraft} onChange={(event) => setReportDraft(event.target.value)} />
                      </div>
                      <div className="report-pane">
                        <div className="report-pane-head">
                          <h3>Live Preview</h3>
                          <p>当前草稿的即时渲染预览。</p>
                        </div>
                        <article className="report-preview" dangerouslySetInnerHTML={{ __html: reportHtml }} />
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>
            </>
          )}
        </section>
      </main>
    </div>
  )
}

export default App
