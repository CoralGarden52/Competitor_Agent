import { loadMockBundle } from './mock'

export type AppSource = 'mock' | 'live'

export interface PreviewPayload {
  prompt: string
  inferred_industry: string
  planned_competitors: string[]
  analysis_schema_plan: Array<{ field_name: string }>
  execution_timeline: Array<Record<string, unknown>>
  preview: Array<{ competitor: string; evidence_count: number }>
  planner_meta?: Record<string, unknown>
}

export interface WorkspacePayload {
  summary: {
    run_id: string
    industry: string
    status: string
    competitor_count: number
    created_at: string
    updated_at: string
  }
  request: {
    industry: string
    user_prompt: string
    competitors: string[]
    language: string
    timeframe: string
  }
  run: {
    run_id: string
    status: string
    industry: string
    planned_competitors: string[]
    schema_fields: string[]
    evidence_count: number
    finding_count: number
    competitor_count: number
  }
  workflow: {
    dag: {
      nodes: string[]
      edges: Array<{ from: string; to: string }>
    }
    timeline: Array<Record<string, unknown>>
    agent_stages: Array<{
      stage: string
      agent: string
      status: string
      duration_ms?: number | null
      summary: string
      handoff_type?: string
      handoff_summary?: string
    }>
    handoffs: Array<{
      stage: string
      attempt: number
      handoff_type: string
      created_at: string
      summary: string
      highlights: string[]
      payload: Record<string, unknown>
    }>
    agent_workflows?: Record<
      string,
      {
        nodes: string[]
        edges: Array<{ from: string; to: string }>
      }
    >
  }
  qa: {
    passed: boolean
    target_agent: string | null
    issue_count: number
    issues: Array<{ code: string; message: string; stage?: string }>
    collect_items: Array<{
      competitor: string
      field_name: string
      reason: string
      query_list: string[]
      priority: number
    }>
  }
  report: {
    markdown: string
    sources: string[]
  }
  observability: {
    llm_calls: Array<{
      trace_id?: string
      agent_name?: string
      trace_name?: string
      node_name?: string
      system_prompt?: string
      user_payload?: Record<string, unknown>
      raw_response?: Record<string, unknown>
      parsed_response?: Record<string, unknown>
      error_message?: string
      status?: string
      total_tokens?: number
      prompt_tokens?: number
      completion_tokens?: number
      latency_ms?: number
      finish_reason?: string
      created_at?: string
    }>
    stage_logs?: Record<
      string,
      {
        io: Array<Record<string, unknown>>
        inputs: Array<Record<string, unknown>>
        outputs: Array<Record<string, unknown>>
        events: Array<Record<string, unknown>>
        handoffs: Array<Record<string, unknown>>
        llm_calls: Array<Record<string, unknown>>
      }
    >
    events: Array<Record<string, unknown>>
    manual_interventions: Array<Record<string, unknown>>
    log_download_path: string
  }
}

export interface LiveProgress {
  run_id: string
  status: string
  completed_stages: string[]
  latest_stage: string
  workspace: WorkspacePayload | null
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    throw new Error(`${path} -> ${response.status}`)
  }
  return response.json() as Promise<T>
}

async function fetchJsonWithTimeout<T>(path: string, timeoutMs: number): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetchJson<T>(path, { signal: controller.signal })
  } finally {
    window.clearTimeout(timer)
  }
}

function parseSources(markdown: string): string[] {
  const set = new Set<string>()
  for (const match of markdown.matchAll(/\((https?:\/\/[^)]+)\)/g)) {
    const url = match[1]?.trim()
    if (url) set.add(url)
  }
  for (const line of markdown.split('\n')) {
    const trimmed = line.trim()
    if (trimmed.startsWith('- http')) set.add(trimmed.slice(2).trim())
  }
  return Array.from(set)
}

function fallbackDag(timeline: Array<{ stage: string; title: string }>) {
  const nodes = timeline.map((item) => item.stage)
  const dedupedNodes = Array.from(new Set(nodes))
  const edges: Array<{ from: string; to: string }> = []
  for (let index = 0; index < nodes.length - 1; index += 1) {
    const from = nodes[index]
    const to = nodes[index + 1]
    if (!from || !to || from === to) continue
    const edge = { from, to }
    if (!edges.find((item) => item.from === from && item.to === to)) {
      edges.push(edge)
    }
  }
  return { nodes: dedupedNodes, edges }
}

function adaptMockWorkspace() {
  return loadMockBundle().then((bundle) => {
    const timeline = bundle.timeline.map((item) => ({
      node_name: item.stage,
      status: item.status,
      duration_ms: null,
      event_type: item.title,
    }))
    return {
      preview: {
        prompt: 'mock',
        inferred_industry: bundle.summary.industry,
        planned_competitors: bundle.summary.competitors,
        analysis_schema_plan: bundle.summary.schema_fields.map((fieldName) => ({ field_name: fieldName })),
        execution_timeline: timeline,
        preview: bundle.summary.competitors.map((competitor) => ({ competitor, evidence_count: 0 })),
      } satisfies PreviewPayload,
      workspace: {
        summary: {
          run_id: bundle.summary.run_id,
          industry: bundle.summary.industry,
          status: 'completed',
          competitor_count: bundle.summary.competitors.length,
          created_at: '',
          updated_at: '',
        },
        request: {
          industry: bundle.summary.industry,
          user_prompt: 'mock',
          competitors: bundle.summary.competitors,
          language: 'zh-CN',
          timeframe: 'last_12_months',
        },
        run: {
          run_id: bundle.summary.run_id,
          status: 'completed',
          industry: bundle.summary.industry,
          planned_competitors: bundle.summary.competitors,
          schema_fields: bundle.summary.schema_fields,
          evidence_count: bundle.summary.evidence_count,
          finding_count: bundle.summary.findings_count,
          competitor_count: bundle.summary.competitors.length,
        },
        workflow: {
          dag: fallbackDag(bundle.timeline.map((item) => ({ stage: item.stage, title: item.title }))),
          timeline,
          agent_stages: bundle.timeline.map((item) => ({
            stage: item.stage,
            agent: item.title,
            status: item.status,
            duration_ms: null,
            summary: item.description,
            handoff_type: bundle.handoffs.find((handoff) => handoff.stage === item.stage)?.handoffType ?? '',
            handoff_summary: bundle.handoffs.find((handoff) => handoff.stage === item.stage)?.summary ?? '',
          })),
          handoffs: bundle.handoffs.map((item) => ({
            stage: item.stage,
            attempt: 1,
            handoff_type: item.handoffType,
            created_at: '',
            summary: item.summary,
            highlights: item.payloadHighlights,
            payload: {},
          })),
          agent_workflows: {},
        },
        qa: {
          passed: bundle.qaRework.qa_summary.passed,
          target_agent: bundle.qaRework.qa_summary.target_agent || null,
          issue_count: bundle.qaRework.qa_summary.issue_count,
          issues: [],
          collect_items: bundle.qaRework.rework.collect_items,
        },
        report: {
          markdown: bundle.reportMarkdown,
          sources: bundle.sources.map((item) => item.url),
        },
        observability: {
          llm_calls: bundle.traces.map((item) => ({
            agent_name: item.agent,
            trace_name: item.traceName,
            status: item.status,
            total_tokens: item.totalTokens,
            prompt_tokens: item.promptTokens,
            completion_tokens: item.completionTokens,
            finish_reason: item.decision,
          })),
          stage_logs: {},
          events: [],
          manual_interventions: [],
          log_download_path: '',
        },
      } satisfies WorkspacePayload,
    }
  })
}

export async function loadMockWorkspace(): Promise<{ preview: PreviewPayload; workspace: WorkspacePayload }> {
  try {
    const [preview, workspace] = await Promise.all([
      fetchJsonWithTimeout<PreviewPayload>('/demo_workspace/latest_preview.json', 1500),
      fetchJsonWithTimeout<WorkspacePayload>('/demo_workspace/latest_workspace.json', 1500),
    ])
    return { preview, workspace }
  } catch {
    return adaptMockWorkspace()
  }
}

export async function loadLiveWorkspace(runId: string): Promise<WorkspacePayload> {
  return fetchJson<WorkspacePayload>(`/runs/${runId}/workspace`)
}

export async function runLivePrompt(
  prompt: string,
  onProgress?: (progress: LiveProgress) => void
): Promise<{ preview: PreviewPayload; workspace: WorkspacePayload }> {
  const preview = await fetchJson<PreviewPayload>('/collector/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, industry_hint: '', competitor_hints: [] }),
  })
  if (!preview.planned_competitors.length) {
    throw new Error('未发现可用竞品，建议补充更明确的产品定位或功能关键词。')
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
      timeframe: 'last_12_months',
    }),
  })

  let latestWorkspace: WorkspacePayload | null = null
  const emitProgress = (workspace: WorkspacePayload | null, status: string) => {
    const completedStages =
      workspace?.workflow.agent_stages
        .filter((item) => item.status === 'completed')
        .map((item) => item.stage) ?? []
    const latestStage =
      workspace?.workflow.agent_stages
        .filter((item) => item.status === 'completed' || item.status === 'running' || item.status === 'failed')
        .slice(-1)[0]?.stage ?? ''
    onProgress?.({
      run_id: run.summary.run_id,
      status,
      completed_stages: completedStages,
      latest_stage: latestStage,
      workspace,
    })
  }

  try {
    latestWorkspace = await loadLiveWorkspace(run.summary.run_id)
    emitProgress(latestWorkspace, latestWorkspace.run.status)
  } catch {
    emitProgress(null, run.state.status)
  }

  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (run.state.status !== 'running') break
    await new Promise((resolve) => setTimeout(resolve, 1500))
    run = await fetchJson(`/runs/${run.summary.run_id}`)
    try {
      latestWorkspace = await loadLiveWorkspace(run.summary.run_id)
      emitProgress(latestWorkspace, latestWorkspace.run.status)
    } catch {
      emitProgress(latestWorkspace, run.state.status)
    }
  }

  const workspace = latestWorkspace ?? (await loadLiveWorkspace(run.summary.run_id))
  return { preview, workspace }
}

export async function downloadLogs(path: string, fallback: WorkspacePayload): Promise<void> {
  const payload = path ? await fetchJson<Record<string, unknown>>(path) : fallback
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${fallback.run.run_id || 'workspace_logs'}.json`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export function downloadMarkdown(runId: string, markdown: string): void {
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${runId || 'competitor_report'}.md`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export function deriveSourceLinks(workspace: WorkspacePayload): string[] {
  if (workspace.report.sources.length) return workspace.report.sources
  return parseSources(workspace.report.markdown)
}
