import type {
  ApiRunListItem,
  CompetitorAnalysisView,
  DemoBundle,
  FindingView,
  HandoffView,
  ProfileView,
  QaReworkView,
  RoleCard,
  RunSummarySnapshot,
  ScorePoint,
  SourceLink,
  StrategyCard,
  TimelineStep,
  TraceView
} from '../types'

interface ApiRunState {
  run_id: string
  industry: string
  task_summary?: string
  competitors: string[]
  analysis_schema_plan?: Array<{ field_name: string }>
  evidences?: unknown[]
  competitor_analyses?: Array<{
    product_name: string
    fields: CompetitorAnalysisView['fields']
  }>
  profiles?: ProfileView[]
  findings?: FindingView[]
  report?: {
    markdown?: string
    appendix_sources?: string[]
  } | null
  tickets?: Array<{
    target_agent?: string
    issues?: unknown[]
    domain_extensions?: {
      collect_plan?: {
        items?: RunSummarySnapshot['qa_rework']['collect_items']
      }
    }
  }>
  status?: string
}

interface ApiRunResponse {
  summary: ApiRunListItem
  state: ApiRunState
}

interface ApiReplayResponse {
  run_id: string
  status: string
  timeline: Array<Record<string, unknown>>
  handoffs: Array<Record<string, unknown>>
  llm_calls: Array<Record<string, unknown>>
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    throw new Error(`failed to fetch ${path}: ${response.status}`)
  }
  return response.json() as Promise<T>
}

function parseMarkdownLinks(markdown: string): SourceLink[] {
  const deduped = new Map<string, SourceLink>()
  for (const match of markdown.matchAll(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g)) {
    const label = match[1]?.trim() || 'source'
    const url = match[2]?.trim()
    if (!url || deduped.has(url)) continue
    deduped.set(url, { label, url })
  }
  return Array.from(deduped.values())
}

function normalizeSummary(run: ApiRunResponse): RunSummarySnapshot {
  const state = run.state
  const tickets = state.tickets ?? []
  const firstTicket = tickets[0]
  const collectItems = firstTicket?.domain_extensions?.collect_plan?.items ?? []
  const issueCount = tickets.reduce((total, ticket) => total + (ticket.issues?.length ?? 0), 0)
  return {
    run_id: state.run_id,
    industry: state.industry,
    task_summary: state.task_summary || run.summary.task_summary || '',
    competitors: state.competitors ?? [],
    schema_fields: (state.analysis_schema_plan ?? []).map((item) => item.field_name),
    evidence_count: state.evidences?.length ?? 0,
    analyses_count: state.competitor_analyses?.length ?? 0,
    profiles_count: state.profiles?.length ?? 0,
    findings_count: state.findings?.length ?? 0,
    qa_summary: {
      passed: tickets.length === 0,
      issue_count: issueCount,
      target_agent: firstTicket?.target_agent ?? '',
      collect_items: collectItems.length
    },
    qa_rework: {
      triggered: tickets.length > 0,
      collect_items: collectItems
    },
    report_exists: Boolean(state.report?.markdown),
    report_length: state.report?.markdown?.length ?? 0,
    report_path: '',
    qa_rework_result_path: '',
    elapsed_seconds: 0
  }
}

function buildQaView(summary: RunSummarySnapshot): QaReworkView {
  return {
    run_id: summary.run_id,
    qa_summary: summary.qa_summary,
    rework: {
      triggered: summary.qa_rework.triggered,
      updated_files: [],
      backup_files: [],
      collect_items: summary.qa_rework.collect_items
    }
  }
}

function buildTimeline(timeline: ApiReplayResponse['timeline']): TimelineStep[] {
  return timeline.slice(0, 12).map((event, index) => {
    const stage = String(event.stage ?? event.node_name ?? event.event_type ?? `step_${index + 1}`)
    const payload = event.payload && typeof event.payload === 'object' ? (event.payload as Record<string, unknown>) : {}
    return {
      id: `${stage}-${index}`,
      stage,
      title: String(event.event_type ?? stage),
      status: String(event.status ?? '').includes('fail') ? 'warning' : 'completed',
      description: Object.keys(payload).length
        ? `关键载荷字段：${Object.keys(payload).slice(0, 4).join(' / ')}`
        : '运行事件已记录，可用于回放与问题定位。',
      outputs: Object.entries(payload)
        .slice(0, 3)
        .map(([key, value]) => `${key}=${Array.isArray(value) ? value.length : String(value).slice(0, 48)}`)
    }
  })
}

function buildHandoffs(handoffs: ApiReplayResponse['handoffs']): HandoffView[] {
  return handoffs.slice(0, 8).map((item) => {
    const payload = item.payload_json && typeof item.payload_json === 'object' ? (item.payload_json as Record<string, unknown>) : {}
    return {
      stage: String(item.stage ?? 'unknown'),
      handoffType: String(item.handoff_type ?? 'Handoff'),
      summary: `记录于阶段 ${String(item.stage ?? 'unknown')}，供下游 Agent 与 replay 直接消费。`,
      payloadHighlights: Object.entries(payload)
        .slice(0, 4)
        .map(([key, value]) => `${key}: ${Array.isArray(value) ? `${value.length} items` : String(value).slice(0, 56)}`)
    }
  })
}

function buildTraces(llmCalls: ApiReplayResponse['llm_calls']): TraceView[] {
  return llmCalls.slice(0, 12).map((item) => ({
    agent: String(item.agent_name ?? item.node_name ?? 'agent'),
    traceName: String(item.trace_name ?? 'trace'),
    status: String(item.status ?? 'completed') === 'failed' ? 'failed' : 'completed',
    promptTokens: Number(item.prompt_tokens ?? 0),
    completionTokens: Number(item.completion_tokens ?? 0),
    totalTokens: Number(item.total_tokens ?? 0),
    decision: String(item.finish_reason ?? item.error_reason ?? item.node_name ?? 'completed')
  }))
}

function buildStrategies(): StrategyCard[] {
  return [
    {
      title: 'Schema-first 结构化工作流',
      detail: '前后端共用竞品字段语义，演示时可以直接证明输出不是自由散文而是可检查的知识对象。',
      codeRef: 'backend/app/core/models.py'
    },
    {
      title: '运行回放与手动干预',
      detail: 'API 模式下可直接读取 replay / intervene 接口，展示真实运行态而不是纯 mock 画面。',
      codeRef: 'backend/app/api/runs.py'
    },
    {
      title: 'Prompt / Token / 决策过程可查',
      detail: 'LLM 调用级 tracing 能把模型成本、异常与降级路径显式呈现给评委。',
      codeRef: 'backend/app/core/agent_llm.py'
    }
  ]
}

function buildRoles(): RoleCard[] {
  return [
    {
      name: 'Planner / Orchestrator',
      stage: 'plan',
      responsibility: '理解业务问题、发现竞品、生成 schema 与执行 DAG。',
      protocol: ['RunRequest', 'PlanHandoff']
    },
    {
      name: 'Collector',
      stage: 'collect',
      responsibility: '搜索、抓取、聚合字段证据并保留 provider 事件。',
      protocol: ['RawEvidence', 'CollectHandoff']
    },
    {
      name: 'Analyst',
      stage: 'analyze',
      responsibility: '从 evidence bundle 抽取字段结论、profile 与 findings。',
      protocol: ['CompetitorAnalysisRecord', 'AnalyzeHandoff']
    },
    {
      name: 'QA',
      stage: 'qa',
      responsibility: '生成报告并在失败时打回 Collect / Analyze。',
      protocol: ['QAOutput', 'ReworkTicket']
    }
  ]
}

function buildScorePoints(): ScorePoint[] {
  return [
    {
      title: '多 Agent 协作与输出可信度',
      weight: '35%',
      bullets: [
        '专职 Agent 分工、DAG 回放、结构化 handoff、真实 QA 闭环、知识 Schema 对齐、引用溯源'
      ]
    },
    {
      title: '技术深度与工程完整度',
      weight: '25%',
      bullets: [
        '端到端接口、LLM trace、token 统计、上下文控制、异常恢复、降级容错'
      ]
    },
    {
      title: '业务价值与产品体验',
      weight: '20%',
      bullets: [
        'mock 演示稳定、报告查看顺畅、人工介入可操作、回放与指标同屏可讲'
      ]
    },
    {
      title: '代码质量与文档',
      weight: '10%',
      bullets: [
        '模块边界清晰、单页展示框架明确、可继续接 README/架构图/部署文档'
      ]
    }
  ]
}

export async function loadApiBundle(runId?: string): Promise<DemoBundle> {
  const runs = await fetchJson<ApiRunListItem[]>('/runs?limit=10')
  if (runs.length === 0) {
    throw new Error('API mode found no runs. Please start one run first.')
  }
  const selectedRunId = runId ?? runs[0].run_id
  const run = await fetchJson<ApiRunResponse>(`/runs/${selectedRunId}`)
  const replay = await fetchJson<ApiReplayResponse>(`/runs/${selectedRunId}/replay`)
  const summary = normalizeSummary(run)
  const reportMarkdown = run.state.report?.markdown ?? '当前运行尚未生成 Markdown 报告。'
  const appendixSources = (run.state.report?.appendix_sources ?? []).map((url) => ({
    label: url.replace(/^https?:\/\//, '').slice(0, 64),
    url
  }))
  const markdownSources = parseMarkdownLinks(reportMarkdown)
  const mergedSources = new Map<string, SourceLink>()
  for (const item of [...appendixSources, ...markdownSources]) {
    if (!mergedSources.has(item.url)) mergedSources.set(item.url, item)
  }

  const analyses: CompetitorAnalysisView[] = (run.state.competitor_analyses ?? []).map((item) => ({
    competitor: item.product_name,
    run_id: summary.run_id,
    fields: item.fields
  }))

  return {
    mode: 'api',
    summary,
    reportMarkdown,
    profiles: run.state.profiles ?? [],
    findings: run.state.findings ?? [],
    qaRework: buildQaView(summary),
    analyses,
    sources: Array.from(mergedSources.values()),
    timeline: buildTimeline(replay.timeline),
    handoffs: buildHandoffs(replay.handoffs),
    traces: buildTraces(replay.llm_calls),
    strategies: buildStrategies(),
    roles: buildRoles(),
    scorePoints: buildScorePoints()
  }
}

export async function submitIntervention(runId: string, patch: Record<string, unknown>, nodeName: string): Promise<void> {
  await fetchJson(`/runs/${runId}/ops/intervene`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      node_name: nodeName,
      action: 'manual_update',
      actor: 'frontend-demo',
      reason: 'manual intervention from frontend console',
      patch
    })
  })
}
