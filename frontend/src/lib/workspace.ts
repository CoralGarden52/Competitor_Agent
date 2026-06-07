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
    task_summary?: string
    created_at: string
    updated_at: string
  }
  request: {
    industry: string
    user_prompt: string
    task_summary?: string
    competitors: string[]
    language: string
    timeframe: string
  }
  run: {
    run_id: string
    status: string
    task_summary?: string
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
    html?: string
    sources: string[]
    blocks?: Array<Record<string, unknown>>
    citations?: Array<Record<string, unknown>>
    render_version?: string
  }
  todo_plan?: Record<string, unknown>
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
    tool_events?: Array<Record<string, unknown>>
    todo_plan?: Record<string, unknown>
    todo_events?: Array<Record<string, unknown>>
    hook_events?: Array<Record<string, unknown>>
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
  preview?: PreviewPayload | null
  timed_out?: boolean
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
          html: '',
          sources: bundle.sources.map((item) => item.url),
          blocks: [],
          citations: [],
          render_version: 'v1_mock',
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
): Promise<{ preview: PreviewPayload; workspace: WorkspacePayload; timedOut: boolean }> {
  const preview = await fetchJson<PreviewPayload>('/collector/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, industry_hint: '', competitor_hints: [] }),
  })
  onProgress?.({
    run_id: '',
    status: 'preview_ready',
    completed_stages: ['plan'],
    latest_stage: 'plan',
    workspace: null,
    preview,
  })

  let run = await fetchJson<{
    summary: { run_id: string }
    state: { status: 'running' | 'failed' | 'completed' }
  }>('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      industry: preview.inferred_industry || 'general',
      competitors: preview.planned_competitors.slice(0, 3),
      user_prompt: prompt,
      language: 'zh-CN',
      timeframe: 'last_12_months',
    }),
  })

  let latestWorkspace: WorkspacePayload | null = null
  let timedOut = true
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
      preview,
      timed_out: timedOut,
    })
  }

  try {
    latestWorkspace = await loadLiveWorkspace(run.summary.run_id)
    emitProgress(latestWorkspace, latestWorkspace.run.status)
  } catch {
    emitProgress(null, run.state.status)
  }

  const sseSupported = typeof window !== 'undefined' && typeof window.EventSource !== 'undefined'
  if (sseSupported) {
    try {
      await new Promise<void>((resolve) => {
        const source = new window.EventSource(`/runs/${run.summary.run_id}/stream`)
        let settled = false

        const finish = (done: boolean) => {
          if (settled) return
          settled = true
          timedOut = !done
          source.close()
          resolve()
        }

        const timeout = window.setTimeout(() => finish(false), 180000)

        source.addEventListener('workspace', (event) => {
          try {
            const payload = JSON.parse((event as MessageEvent).data) as {
              status?: string
              workspace?: WorkspacePayload
            }
            if (payload.workspace) {
              latestWorkspace = payload.workspace
              emitProgress(payload.workspace, payload.workspace.run.status)
            }
          } catch {
            // Ignore malformed workspace events and keep stream alive.
          }
        })

        source.addEventListener('run_event', () => {
          if (latestWorkspace) {
            emitProgress(latestWorkspace, latestWorkspace.run.status)
          }
        })

        source.addEventListener('run_done', async (event) => {
          window.clearTimeout(timeout)
          try {
            const payload = JSON.parse((event as MessageEvent).data) as { status?: string }
            latestWorkspace = await loadLiveWorkspace(run.summary.run_id)
            emitProgress(latestWorkspace, payload.status || latestWorkspace.run.status)
          } catch {
            if (latestWorkspace) emitProgress(latestWorkspace, latestWorkspace.run.status)
          }
          finish(true)
        })

        source.addEventListener('error', () => {
          window.clearTimeout(timeout)
          finish(false)
        })
      })
    } catch {
      timedOut = true
    }
  }

  if (timedOut) {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const currentStatus = latestWorkspace?.run.status ?? run.state.status
      if (currentStatus !== 'running') {
        timedOut = false
        break
      }
      await new Promise((resolve) => setTimeout(resolve, 1500))
      run = await fetchJson(`/runs/${run.summary.run_id}`)
      try {
        latestWorkspace = await loadLiveWorkspace(run.summary.run_id)
        emitProgress(latestWorkspace, latestWorkspace.run.status)
      } catch {
        emitProgress(latestWorkspace, run.state.status)
      }
    }
  }

  const workspace = latestWorkspace ?? (await loadLiveWorkspace(run.summary.run_id))
  if (workspace.run.status !== 'running') {
    timedOut = false
  }
  return { preview, workspace, timedOut }
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
