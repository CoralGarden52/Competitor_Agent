import type {
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

const SUMMARY_PATH = '/complete_flow_result/complete_flow_result.json'

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) {
    throw new Error(`failed to fetch ${path}: ${response.status}`)
  }
  return response.json() as Promise<T>
}

async function fetchText(path: string): Promise<string> {
  const response = await fetch(path)
  if (!response.ok) {
    throw new Error(`failed to fetch ${path}: ${response.status}`)
  }
  return response.text()
}

function basename(absolutePath: string): string {
  const parts = absolutePath.split('/')
  return parts[parts.length - 1] ?? absolutePath
}

function parseReportSources(markdown: string): SourceLink[] {
  const matches = Array.from(markdown.matchAll(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g))
  const deduped = new Map<string, SourceLink>()
  for (const match of matches) {
    const label = match[1]?.trim() || 'source'
    const url = match[2]?.trim()
    if (!url || deduped.has(url)) continue
    deduped.set(url, { label, url })
  }

  for (const line of markdown.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('- http')) continue
    const url = trimmed.slice(2).trim()
    if (!deduped.has(url)) {
      deduped.set(url, { label: url.replace(/^https?:\/\//, '').slice(0, 72), url })
    }
  }
  return Array.from(deduped.values())
}

function buildTimeline(summary: RunSummarySnapshot, qa: QaReworkView): TimelineStep[] {
  return [
    {
      id: 'plan',
      stage: 'plan',
      title: 'Planner / Orchestrator',
      status: 'completed',
      description: `根据行业与用户意图生成竞品清单、分析 Schema 和候选分组。当前行业为 ${summary.industry}。`,
      outputs: [`planned_competitors=${summary.competitors.join(' / ')}`, `schema_fields=${summary.schema_fields.length}`]
    },
    {
      id: 'collect',
      stage: 'collect',
      title: 'Collector Agent',
      status: qa.qa_summary.target_agent === 'Collect' ? 'warning' : 'completed',
      description: '并行执行搜索、网页抓取、字段证据聚合，并将 provider 事件与 fallback trace 写入交接物。',
      outputs: [`evidence_count=${summary.evidence_count}`, `qa_collect_items=${summary.qa_summary.collect_items}`]
    },
    {
      id: 'analyze',
      stage: 'analyze',
      title: 'Analyst Agent',
      status: 'completed',
      description: '按字段提炼结构化结论，生成 competitor analyses、profiles 与 findings，并补充 evidence gaps。',
      outputs: [`analyses=${summary.analyses_count}`, `findings=${summary.findings_count}`]
    },
    {
      id: 'draft',
      stage: 'draft',
      title: 'Writer Agent',
      status: 'completed',
      description: '把矩阵、摘要、机会点、风险与引用整合为最终报告 Markdown。',
      outputs: [`report_length=${summary.report_length}`]
    },
    {
      id: 'qa',
      stage: 'qa',
      title: 'QA Critic Agent',
      status: summary.qa_summary.passed ? 'completed' : 'warning',
      description: '执行字段覆盖、引用完整性、unknown 过多、证据不足等规则检查，并真实打回 Collect。',
      outputs: [`passed=${String(summary.qa_summary.passed)}`, `issues=${summary.qa_summary.issue_count}`]
    }
  ]
}

function buildHandoffs(summary: RunSummarySnapshot): HandoffView[] {
  return [
    {
      stage: 'plan',
      handoffType: 'PlanHandoff',
      summary: '规划阶段把候选竞品、行业判断、Schema 与 split strategy 以结构化对象交给后续阶段。',
      payloadHighlights: [
        `planned_competitors: ${summary.competitors.join(', ')}`,
        `analysis_schema_plan fields: ${summary.schema_fields.join(', ')}`,
        'candidate_groups: direct / substitute'
      ]
    },
    {
      stage: 'collect',
      handoffType: 'CollectHandoff',
      summary: '采集阶段把按竞品、按字段整理的 evidence bundles 以及 provider events 交给分析阶段。',
      payloadHighlights: [
        `total_evidence_count: ${summary.evidence_count}`,
        'evidence_bundles: competitor -> field -> evidences',
        'provider_events / fallback_trace / errors'
      ]
    },
    {
      stage: 'analyze',
      handoffType: 'AnalyzeHandoff',
      summary: '分析阶段把 competitor analyses、profiles、findings 和 gap summary 交给写作与 QA。',
      payloadHighlights: [
        `competitor_analyses: ${summary.analyses_count}`,
        `profiles: ${summary.profiles_count}`,
        `findings: ${summary.findings_count}`
      ]
    }
  ]
}

function buildTraceSamples(): TraceView[] {
  return [
    {
      agent: 'PlannerLLMClient',
      traceName: 'planner.infer_product_profile',
      status: 'completed',
      promptTokens: 488,
      completionTokens: 164,
      totalTokens: 652,
      decision: '先抽产品画像，再决定 query generation 与 direct/substitute 判定约束。'
    },
    {
      agent: 'CollectorPipeline',
      traceName: 'collector.search.strategy',
      status: 'completed',
      promptTokens: 0,
      completionTokens: 0,
      totalTokens: 0,
      decision: '按 pricing / feature / feedback 不同策略执行 provider allowlist、prefetch 与 rerank。'
    },
    {
      agent: 'AnalystAgent',
      traceName: 'agent.analyze.field.pricing_model.reduce',
      status: 'completed',
      promptTokens: 1580,
      completionTokens: 702,
      totalTokens: 2282,
      decision: '使用多证据分块提取后统一汇总，减少超长上下文与单页误判。'
    },
    {
      agent: 'QACriticAgent',
      traceName: 'qa.analysis_review',
      status: 'completed',
      promptTokens: 1216,
      completionTokens: 436,
      totalTokens: 1652,
      decision: '识别 evidence insufficiency 与 unknown 聚集字段，生成真实 Collect rework plan。'
    },
    {
      agent: 'WriterAgent',
      traceName: 'agent.draft.generate_report',
      status: 'failed',
      promptTokens: 0,
      completionTokens: 0,
      totalTokens: 0,
      decision: '展示降级处理与 tracing 容错：日志写入失败不会阻断主流程。'
    }
  ]
}

function buildStrategies(): StrategyCard[] {
  return [
    {
      title: '引用强制与结论保守输出',
      detail: '无证据的 finding / report claim 不应进入最终报告；证据不足时优先输出已确认部分并显式保留 gaps。',
      codeRef: 'backend/app/agents/analyst_agent.py'
    },
    {
      title: '超长上下文分片',
      detail: 'pricing_model 已切换成多证据分块提取 + reduce 汇总，避免把一页截图或单条页面当成全部事实。',
      codeRef: 'backend/app/agents/analyst_agent.py'
    },
    {
      title: '真实 QA 闭环',
      detail: 'QA 会生成 Collect rework plan，要求补充具体竞品、字段与查询语句，而不是伪闭环通过。',
      codeRef: 'backend/app/core/workflow.py'
    },
    {
      title: 'LLM 调用级可观测性',
      detail: '每次 Agent 调用都有 trace_name、prompt / response、token 使用与 finish_reason 可查。',
      codeRef: 'backend/app/core/agent_llm.py'
    },
    {
      title: '结构化 Agent 交接',
      detail: 'PlanHandoff / CollectHandoff / AnalyzeHandoff 让下游直接消费标准化结果包，而不是读整份 RunState。',
      codeRef: 'backend/app/core/models.py'
    }
  ]
}

function buildRoles(): RoleCard[] {
  return [
    {
      name: 'Orchestrator Agent',
      stage: 'plan',
      responsibility: '整合行业判断、竞品发现、Schema 规划与 DAG 编排决策。',
      protocol: ['RunRequest', 'product_profile', 'PlanHandoff']
    },
    {
      name: 'Collector Agent',
      stage: 'collect',
      responsibility: '并行搜索、抓取网页、汇总证据，并附带 provider fallback / fetch trace。',
      protocol: ['AnalysisSchemaField', 'RawEvidence', 'CollectHandoff']
    },
    {
      name: 'Analyst Agent',
      stage: 'analyze',
      responsibility: '把 evidence bundle 转成字段分析、profile、finding 与 evidence gaps。',
      protocol: ['CompetitorEvidenceBundle', 'CompetitorAnalysisRecord', 'AnalyzeHandoff']
    },
    {
      name: 'Writer Agent',
      stage: 'draft',
      responsibility: '根据 profiles / findings / report sections 生成最终竞品报告。',
      protocol: ['ReportSection', 'ReportClaim', 'Report']
    },
    {
      name: 'QA Critic Agent',
      stage: 'qa',
      responsibility: '检查 unknown、字段缺口、溯源不足并生成真实 rework ticket / collect plan。',
      protocol: ['QACollectPlan', 'ReworkTicket', 'manual_intervention patch']
    }
  ]
}

function buildScorePoints(): ScorePoint[] {
  return [
    {
      title: '多 Agent 协作与输出可信度',
      weight: '35%',
      bullets: [
        '角色划分清晰，多个专职 Agent 负责采集 / 分析 / 撰写 / 质检',
        'LangGraph DAG 可回放，可展示任务流转与阶段输出',
        'Agent 间使用 PlanHandoff / CollectHandoff / AnalyzeHandoff 结构化交接',
        'QA 能真实识别问题并打回 Collect，重做后输出改善',
        '输出对齐预定义知识 Schema，包括功能树、定价模型、用户反馈等',
        '报告来源与 evidence refs 可展示溯源入口'
      ]
    },
    {
      title: '技术深度与工程完整度',
      weight: '25%',
      bullets: [
        '端到端链路覆盖：采集、编排、存储、后端接口、前端展示',
        'LLM 调用级 trace 可展示 Prompt / 输入输出 / token 消耗',
        '具备超长上下文分片、引用强制、fallback 与降级解释',
        '异常处理与运行回放可见，便于现场排障与解释'
      ]
    },
    {
      title: '业务价值与产品体验',
      weight: '20%',
      bullets: [
        '可量化展示效率、覆盖度、结构化一致性与 QA 重做率',
        '交互上覆盖报告查看、溯源跳转、人工修正、Agent 决策回放',
        'mock data 模式保证演示流畅，API 模式支持切回真实运行态'
      ]
    },
    {
      title: '代码质量与文档',
      weight: '10%',
      bullets: [
        '前端模块边界清晰：mock loader / api loader / view shell',
        '附带 README 说明运行方式、数据源与演示模式',
        '方便继续补充架构图、部署说明与分支协作规范'
      ]
    }
  ]
}

export async function loadMockBundle(): Promise<DemoBundle> {
  const summary = await fetchJson<RunSummarySnapshot>(SUMMARY_PATH)
  const reportMarkdown = await fetchText(`/complete_flow_result/${basename(summary.report_path)}`)
  const qaRework = await fetchJson<QaReworkView>(`/complete_flow_result/${basename(summary.qa_rework_result_path)}`)
  const profiles = await fetchJson<ProfileView[]>('/complete_flow_result/analyst_output/all_profiles.json')
  const findings = await fetchJson<FindingView[]>('/complete_flow_result/analyst_output/all_findings.json')

  const analyses = await Promise.all(
    summary.competitors.map((competitor) =>
      fetchJson<CompetitorAnalysisView>(`/complete_flow_result/analyst_output/${encodeURIComponent(`${competitor}_analysis.json`)}`)
    )
  )

  return {
    mode: 'mock',
    summary,
    reportMarkdown,
    profiles,
    findings,
    qaRework,
    analyses,
    sources: parseReportSources(reportMarkdown),
    timeline: buildTimeline(summary, qaRework),
    handoffs: buildHandoffs(summary),
    traces: buildTraceSamples(),
    strategies: buildStrategies(),
    roles: buildRoles(),
    scorePoints: buildScorePoints()
  }
}
