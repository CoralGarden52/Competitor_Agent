import { useEffect, useMemo, useRef, useState } from 'react'
import { loadApiBundle, submitIntervention } from './lib/api'
import { loadMockBundle } from './lib/mock'
import type { DataMode, DemoBundle } from './types'

type Role = 'user' | 'assistant' | 'system'
type WorkspaceTab = 'overview' | 'dag' | 'qa' | 'trace' | 'report'
type LoadState = 'idle' | 'loading' | 'ready' | 'error'

interface ChatMessage {
  id: string
  role: Role
  content: string
  timestamp: number
}

const DEFAULT_PROMPT =
  '请分析在线会议软件领域的竞品，重点关注功能树、定价模型、AI会议能力、用户反馈和私有化部署支持。'

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    throw new Error(`${path} -> ${response.status}`)
  }
  return response.json() as Promise<T>
}

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
      const content = line.replace(/^#{1,6}\s*/, '')
      html.push(`<h${level}>${content}</h${level}>`)
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

async function createLiveRun(prompt: string): Promise<string> {
  const preview = await fetchJson<{
    inferred_industry: string
    planned_competitors: string[]
  }>('/collector/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, industry_hint: '', competitor_hints: [] })
  })

  if (!preview.planned_competitors.length) {
    throw new Error('未发现可用竞品，建议补充更明确的业务关键词。')
  }

  let run = await fetchJson<{
    summary: { run_id: string }
    state: { status: 'running' | 'failed' | 'completed' }
  }>('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      industry: preview.inferred_industry || 'general',
      competitors: preview.planned_competitors.slice(0, 5),
      user_prompt: prompt,
      language: 'zh-CN',
      timeframe: 'last_12_months'
    })
  })

  for (let i = 0; i < 120; i += 1) {
    if (run.state.status !== 'running') break
    await sleep(1500)
    run = await fetchJson(`/runs/${run.summary.run_id}`)
  }

  return run.summary.run_id
}

function buildSystemSummary(bundle: DemoBundle): string {
  const qa = bundle.summary.qa_summary
  return [
    `已载入 ${bundle.mode === 'mock' ? 'Mock' : 'API'} 数据。`,
    `行业：${bundle.summary.industry}。`,
    `竞品：${bundle.summary.competitors.join('、')}。`,
    `证据 ${bundle.summary.evidence_count} 条，结构化 findings ${bundle.summary.findings_count} 条。`,
    qa.passed
      ? 'QA 已通过，可直接进入报告审阅。'
      : `QA 识别 ${qa.issue_count} 个问题，并向 ${qa.target_agent} 打回 ${qa.collect_items} 个补采项。`
  ].join('')
}

function App() {
  const [mode, setMode] = useState<DataMode>('mock')
  const [loadState, setLoadState] = useState<LoadState>('idle')
  const [tab, setTab] = useState<WorkspaceTab>('overview')
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT)
  const [bundle, setBundle] = useState<DemoBundle | null>(null)
  const [reportDraft, setReportDraft] = useState('')
  const [error, setError] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content:
        '这里是竞品分析演示台。你可以像 GPT 一样输入分析任务，左侧保持对话，右侧查看 DAG 流转、QA 闭环、Trace、可编辑报告与下载。',
      timestamp: Date.now()
    }
  ])
  const listRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    void handleLoad('mock', false)
  }, [])

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    if (!bundle) return
    setReportDraft(bundle.reportMarkdown)
  }, [bundle])

  const reportHtml = useMemo(() => markdownToHtml(reportDraft), [reportDraft])
  const totalTokens = useMemo(() => bundle?.traces.reduce((sum, item) => sum + item.totalTokens, 0) ?? 0, [bundle])
  const topFindings = useMemo(() => bundle?.findings.slice(0, 6) ?? [], [bundle])
  const topSources = useMemo(() => bundle?.sources.slice(0, 8) ?? [], [bundle])
  const topQaItems = useMemo(() => bundle?.qaRework.rework.collect_items.slice(0, 8) ?? [], [bundle])
  const artifactMetrics = useMemo(
    () =>
      bundle
        ? [
            { label: '证据总量', value: String(bundle.summary.evidence_count), detail: '采集与字段归因完成' },
            { label: 'Findings', value: String(bundle.summary.findings_count), detail: '结构化结论可直接展示' },
            { label: 'QA 打回', value: String(bundle.summary.qa_summary.collect_items), detail: '真实闭环，不是伪通过' },
            { label: 'Trace Tokens', value: String(totalTokens), detail: 'Prompt / Token / 决策可查' }
          ]
        : [],
    [bundle, totalTokens]
  )

  function appendMessage(role: Role, content: string) {
    setMessages((prev) => [
      ...prev,
      {
        id: `${role}-${Date.now()}-${Math.random()}`,
        role,
        content,
        timestamp: Date.now()
      }
    ])
  }

  async function handleLoad(nextMode: DataMode, logMessage = true) {
    setMode(nextMode)
    setLoadState('loading')
    setError('')
    if (logMessage) {
      appendMessage('system', `正在加载 ${nextMode === 'mock' ? 'Mock 数据' : 'API 最近运行'}。`)
    }
    try {
      const nextBundle = nextMode === 'mock' ? await loadMockBundle() : await loadApiBundle()
      setBundle(nextBundle)
      setLoadState('ready')
      setTab('overview')
      if (logMessage) {
        appendMessage('assistant', buildSystemSummary(nextBundle))
      }
    } catch (loadError) {
      const message = loadError instanceof Error ? loadError.message : 'load failed'
      setBundle(null)
      setLoadState('error')
      setError(message)
      if (logMessage) {
        appendMessage('assistant', `加载失败：${message}`)
      }
    }
  }

  async function handleSubmit() {
    const value = prompt.trim()
    if (!value || loadState === 'loading') return
    appendMessage('user', value)
    setLoadState('loading')
    setError('')
    setTab('overview')
    try {
      if (mode === 'mock') {
        appendMessage('system', 'Mock 模式下将直接刷新演示结果。')
        const nextBundle = await loadMockBundle()
        setBundle(nextBundle)
        setLoadState('ready')
        appendMessage('assistant', buildSystemSummary(nextBundle))
        return
      }

      appendMessage('system', '正在执行竞品发现、创建 run 并轮询运行状态。')
      const runId = await createLiveRun(value)
      const nextBundle = await loadApiBundle(runId)
      setBundle(nextBundle)
      setLoadState('ready')
      appendMessage(
        'assistant',
        `真实运行 ${runId} 已完成。${buildSystemSummary(nextBundle)}你现在可以在右侧回放 DAG、查看 QA 打回项、编辑报告并下载 Markdown。`
      )
    } catch (runError) {
      const message = runError instanceof Error ? runError.message : 'run failed'
      setLoadState('error')
      setError(message)
      appendMessage('assistant', `执行失败：${message}`)
    }
  }

  async function handleIntervention() {
    if (!bundle || bundle.mode !== 'api') return
    try {
      appendMessage('system', '正在向 API 运行提交人工干预 patch。')
      await submitIntervention(bundle.summary.run_id, { analysis_schema_plan: [] }, 'plan')
      const refreshed = await loadApiBundle(bundle.summary.run_id)
      setBundle(refreshed)
      appendMessage('assistant', '人工干预已提交，右侧内容已按最新运行态刷新。')
    } catch (interveneError) {
      const message = interveneError instanceof Error ? interveneError.message : 'intervention failed'
      appendMessage('assistant', `人工干预失败：${message}`)
    }
  }

  function downloadReport() {
    const text = reportDraft || bundle?.reportMarkdown || '# Report\n'
    const filename = `${bundle?.summary.run_id ?? 'competitor_report'}.md`
    const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    document.body.appendChild(anchor)
    anchor.click()
    document.body.removeChild(anchor)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="app-shell">
      <header className="app-topbar">
        <div>
          <h1>Competitor Agent</h1>
          <p>像 GPT 一样发起竞品分析，对话驱动，多 Agent 工作流可见，报告可编辑可下载。</p>
        </div>
        <div className="topbar-actions">
          <button type="button" className={mode === 'mock' ? 'active' : ''} onClick={() => void handleLoad('mock')}>
            Mock
          </button>
          <button type="button" className={mode === 'api' ? 'active' : ''} onClick={() => void handleLoad('api')}>
            API
          </button>
        </div>
      </header>

      <main className="workspace">
        <section className="chat-pane">
          <div className="chat-hero">
            <div className="chat-hero-copy">
              <span className="section-kicker">Chat Workspace</span>
              <h2>像 GPT 一样提问，让多 Agent 在右侧把过程讲清楚。</h2>
              <p>
                左边负责自然语言任务输入与运行反馈，右边负责把 DAG、QA、Trace、报告编辑和溯源可视化，方便直接对照评分细则演示。
              </p>
            </div>
            <div className="chat-status">
              <span className={`status-dot ${loadState}`} />
              <p>
                {error
                  ? `状态异常：${error}`
                  : loadState === 'loading'
                    ? '系统正在处理请求。'
                    : mode === 'mock'
                      ? '当前为 Mock 演示模式。'
                      : '当前为 API 实时运行模式。'}
              </p>
            </div>
            <div className="chat-meta">
              <span>{mode === 'mock' ? '稳定演示数据' : '真实运行态'}</span>
              <span>{bundle ? `竞品 ${bundle.summary.competitors.length}` : '等待载入'}</span>
              <span>{bundle ? `Schema ${bundle.summary.schema_fields.length}` : '等待任务'}</span>
            </div>
          </div>

          <div className="chat-list" ref={listRef}>
            {messages.map((item) => (
              <article key={item.id} className={`bubble ${item.role}`}>
                <span className="bubble-role">{item.role}</span>
                <p>{item.content}</p>
              </article>
            ))}
          </div>

          <div className="composer">
            <textarea
              rows={4}
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="描述你的竞品分析任务，例如关注功能树、定价、AI能力、客户规模与私有化部署。"
              disabled={loadState === 'loading'}
            />
            <div className="composer-actions">
              <div className="composer-hint">
                <span>评分重点</span>
                <p>多 Agent、DAG、结构化 Schema、QA 打回、溯源、报告编辑与下载</p>
              </div>
              <button type="button" onClick={() => void handleSubmit()} disabled={loadState === 'loading' || !prompt.trim()}>
                {mode === 'mock' ? '刷新 Mock 演示' : '发送并运行'}
              </button>
            </div>
          </div>
        </section>

        <section className="result-pane">
          <div className="result-header">
            <div>
              <h2>{bundle?.summary.industry ?? '等待结果'}</h2>
              <p>
                {bundle
                  ? `Run ${bundle.summary.run_id} · ${bundle.summary.competitors.join('、')}`
                  : '左侧发起任务后，这里会展示结构化结果与回放信息。'}
              </p>
            </div>
            {bundle ? (
              <div className="result-stats">
                <span>证据 {bundle.summary.evidence_count}</span>
                <span>Findings {bundle.summary.findings_count}</span>
                <span>Tokens {totalTokens}</span>
              </div>
            ) : null}
          </div>

          <div className="tabs">
            {(['overview', 'dag', 'qa', 'trace', 'report'] as const).map((item) => (
              <button key={item} type="button" className={tab === item ? 'active' : ''} onClick={() => setTab(item)}>
                {item.toUpperCase()}
              </button>
            ))}
          </div>

          <div className="panel-body">
            {!bundle ? (
              <div className="empty-panel">
                <h3>准备就绪</h3>
                <p>默认会先读入 mock 演示数据。你也可以切换到 API 模式发起真实运行。</p>
              </div>
            ) : null}

            {bundle && tab === 'overview' ? (
              <div className="artifacts-grid">
                <article className="artifact-hero">
                  <div className="artifact-heading">
                    <span className="section-kicker">Artifact Overview</span>
                    <h3>{bundle.summary.industry} 竞品分析工件</h3>
                    <p>
                      当前 run 已将竞品分析拆成结构化 Schema、阶段 handoff、QA 返工计划和报告产物，右侧所有卡片都能直接服务现场讲解。
                    </p>
                  </div>
                  <div className="artifact-chip-row">
                    <span>{bundle.summary.run_id}</span>
                    <span>{bundle.summary.competitors.join('、')}</span>
                    <span>{bundle.summary.qa_summary.passed ? 'QA Passed' : `Rework -> ${bundle.summary.qa_summary.target_agent}`}</span>
                  </div>
                </article>

                <div className="artifact-metric-grid">
                  {artifactMetrics.map((item) => (
                    <article key={item.label} className="artifact-metric-card">
                      <span>{item.label}</span>
                      <strong>{item.value}</strong>
                      <p>{item.detail}</p>
                    </article>
                  ))}
                </div>

                <article className="summary-card artifact-schema-card">
                  <h3>Schema Scope</h3>
                  <div className="token-list">
                    {bundle.summary.schema_fields.map((field) => (
                      <span key={field}>{field}</span>
                    ))}
                  </div>
                </article>

                <article className="summary-card artifact-score-card">
                  <h3>评分映射</h3>
                  {bundle.scorePoints.map((item) => (
                    <div key={item.title} className="score-line">
                      <strong>{item.title}</strong>
                      <span>{item.weight}</span>
                    </div>
                  ))}
                </article>

                <article className="summary-card artifact-findings-card">
                  <h3>关键结论</h3>
                  <div className="finding-list">
                    {topFindings.map((item) => (
                      <div key={`${item.statement}-${item.category}`} className="finding-item">
                        <strong>{item.competitor ?? item.category}</strong>
                        <p>{item.statement}</p>
                      </div>
                    ))}
                  </div>
                </article>

                <article className="summary-card artifact-sources-card">
                  <h3>来源溯源</h3>
                  <div className="source-list">
                    {topSources.map((item) => (
                      <a key={item.url} href={item.url} target="_blank" rel="noreferrer">
                        {item.label}
                      </a>
                    ))}
                  </div>
                </article>
              </div>
            ) : null}

            {bundle && tab === 'dag' ? (
              <div className="dag-layout">
                <div className="dag-flowchart">
                  {bundle.timeline.map((item, index) => (
                    <div key={item.id} className="dag-segment">
                      <div className={`dag-node ${item.status}`}>
                        <span>{item.stage}</span>
                        <strong>{item.title}</strong>
                        <p>{item.description}</p>
                        <div className="token-list">
                          {item.outputs.slice(0, 2).map((output) => (
                            <span key={output}>{output}</span>
                          ))}
                        </div>
                      </div>
                      {index < bundle.timeline.length - 1 ? <div className="dag-connector" aria-hidden="true" /> : null}
                    </div>
                  ))}
                </div>
                <div className="detail-grid">
                  {bundle.handoffs.map((item) => (
                    <article key={`${item.stage}-${item.handoffType}`} className="detail-card">
                      <h4>
                        {item.stage} · {item.handoffType}
                      </h4>
                      <p>{item.summary}</p>
                      <div className="token-list">
                        {item.payloadHighlights.map((highlight) => (
                          <span key={highlight}>{highlight}</span>
                        ))}
                      </div>
                    </article>
                  ))}
                </div>
              </div>
            ) : null}

            {bundle && tab === 'qa' ? (
              <div className="qa-layout">
                <article className="summary-card">
                  <h3>QA 闭环</h3>
                  <p>是否通过：{bundle.summary.qa_summary.passed ? '是' : '否'}</p>
                  <p>目标 Agent：{bundle.summary.qa_summary.target_agent || '-'}</p>
                  <p>问题数：{bundle.summary.qa_summary.issue_count}</p>
                  <p>补采项：{bundle.summary.qa_summary.collect_items}</p>
                </article>
                <article className="summary-card full">
                  <h3>打回明细</h3>
                  <div className="qa-list">
                    {topQaItems.map((item) => (
                      <div key={`${item.competitor}-${item.field_name}`} className="qa-item">
                        <strong>
                          {item.competitor} / {item.field_name} / P{item.priority}
                        </strong>
                        <p>{item.reason}</p>
                      </div>
                    ))}
                  </div>
                </article>
                <article className="summary-card full">
                  <h3>人工介入</h3>
                  <p>API 模式下可以直接向 `/runs/{`{run_id}`}/ops/intervene` 提交 patch，演示人工修正和流程恢复能力。</p>
                  <button type="button" className="inline-action" disabled={bundle.mode !== 'api'} onClick={() => void handleIntervention()}>
                    提交示例干预
                  </button>
                </article>
              </div>
            ) : null}

            {bundle && tab === 'trace' ? (
              <div className="detail-grid">
                {bundle.traces.map((item) => (
                  <article key={`${item.agent}-${item.traceName}`} className={`detail-card ${item.status}`}>
                    <h4>{item.traceName}</h4>
                    <p>Agent：{item.agent}</p>
                    <p>Status：{item.status}</p>
                    <p>Tokens：{item.totalTokens}</p>
                    <p>{item.decision}</p>
                  </article>
                ))}
              </div>
            ) : null}

            {bundle && tab === 'report' ? (
              <div className="report-layout">
                <div className="report-toolbar">
                  <div className="report-toolbar-copy">
                    <h3>报告工作区</h3>
                    <p>左侧直接编辑 Markdown，右侧实时预览，支持一键下载当前版本。</p>
                  </div>
                  <button type="button" className="inline-action" onClick={() => setReportDraft(bundle.reportMarkdown)}>
                    重置原始报告
                  </button>
                  <button type="button" className="inline-action" onClick={downloadReport}>
                    下载 Markdown
                  </button>
                </div>
                <div className="report-grid">
                  <section className="report-panel">
                    <div className="report-panel-head">
                      <span>Markdown 编辑器</span>
                    </div>
                    <textarea
                      className="report-editor"
                      value={reportDraft}
                      onChange={(event) => setReportDraft(event.target.value)}
                      placeholder="运行完成后会在这里展示 Markdown 报告。"
                    />
                  </section>
                  <section className="report-panel">
                    <div className="report-panel-head">
                      <span>实时预览</span>
                    </div>
                    <article className="report-preview" dangerouslySetInnerHTML={{ __html: reportHtml }} />
                  </section>
                </div>
              </div>
            ) : null}
          </div>
        </section>
      </main>
    </div>
  )
}

export default App
