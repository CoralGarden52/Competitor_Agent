export type DataMode = 'mock' | 'api'

export interface RunSummarySnapshot {
  run_id: string
  industry: string
  competitors: string[]
  schema_fields: string[]
  evidence_count: number
  analyses_count: number
  profiles_count: number
  findings_count: number
  qa_summary: {
    passed: boolean
    issue_count: number
    target_agent: string
    collect_items: number
  }
  qa_rework: {
    triggered: boolean
    collect_items: Array<{
      competitor: string
      field_name: string
      reason: string
      query_list: string[]
      priority: number
    }>
  }
  report_exists: boolean
  report_length: number
  report_path: string
  qa_rework_result_path: string
  elapsed_seconds: number
}

export interface ProfileNode {
  name: string
  capability: string
}

export interface PricingTierView {
  name: string
  price_range: string
  billing_cycle: string
  limits: string[]
}

export interface ProfileView {
  product_name: string
  feature_tree: ProfileNode[]
  advantages: string[]
  disadvantages: string[]
  pricing_model: {
    model_type: string
    free_tier: boolean
    billing_dimensions: string[]
    tiers: PricingTierView[]
  }
  user_feedback: {
    positive_themes: string[]
    negative_themes: string[]
    representative_quotes: string[]
    sentiment_distribution: Record<string, number>
  }
}

export interface FindingView {
  statement: string
  category: string
  evidence_refs: string[]
  competitor?: string
  confidence?: number
  impact?: string
}

export interface CompetitorFieldAnalysis {
  field_name: string
  summary: string
  confidence: number
  evidence_refs: string[]
  normalized_value: Record<string, unknown>
  evidence_gaps: string[]
}

export interface CompetitorAnalysisView {
  competitor: string
  run_id: string
  fields: CompetitorFieldAnalysis[]
}

export interface QaReworkView {
  run_id: string
  qa_summary: RunSummarySnapshot['qa_summary']
  rework: {
    triggered: boolean
    updated_files: string[]
    backup_files: string[]
    collect_items: RunSummarySnapshot['qa_rework']['collect_items']
  }
}

export interface SourceLink {
  label: string
  url: string
}

export interface TimelineStep {
  id: string
  stage: string
  title: string
  status: 'completed' | 'active' | 'warning'
  description: string
  outputs: string[]
}

export interface HandoffView {
  stage: string
  handoffType: string
  summary: string
  payloadHighlights: string[]
}

export interface TraceView {
  agent: string
  traceName: string
  status: 'completed' | 'failed'
  promptTokens: number
  completionTokens: number
  totalTokens: number
  decision: string
}

export interface StrategyCard {
  title: string
  detail: string
  codeRef: string
}

export interface RoleCard {
  name: string
  stage: string
  responsibility: string
  protocol: string[]
}

export interface ScorePoint {
  title: string
  weight: string
  bullets: string[]
}

export interface DemoBundle {
  mode: DataMode
  summary: RunSummarySnapshot
  reportMarkdown: string
  profiles: ProfileView[]
  findings: FindingView[]
  qaRework: QaReworkView
  analyses: CompetitorAnalysisView[]
  sources: SourceLink[]
  timeline: TimelineStep[]
  handoffs: HandoffView[]
  traces: TraceView[]
  strategies: StrategyCard[]
  roles: RoleCard[]
  scorePoints: ScorePoint[]
}

export interface ApiRunListItem {
  run_id: string
  industry: string
  status: string
  competitor_count: number
  created_at: string
  updated_at: string
}
