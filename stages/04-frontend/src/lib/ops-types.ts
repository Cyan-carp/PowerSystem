/**
 * 运维任务（09 ops-agent）的类型定义。
 *
 * 字段口径与 stages/09-ops-agent 的契约一致：前端只读展示，不推断缺失字段。
 * 这些类型从 PowerSystem 原前端 types.ts 原样迁入，除去掉 Element Plus 依赖外未改字段。
 */

export type OpsStatus =
  | 'queued'
  | 'planning'
  | 'executing'
  | 'awaiting_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'timeout'

export interface SecurityReview {
  review_id: string
  step_no: number
  kind: string
  state: string
  decision?: string | null
  reason?: string | null
  rule_hits: string[]
  evidence_refs: string[]
  execution_epoch: number
  created_at: string
  finished_at?: string | null
  expires_at?: string | null
  error_code?: string | null
  policy_version: string
  duration_ms?: number | null
}

export interface OpsApproval {
  approval_id: string
  task_id: string
  step_no: number
  status: 'pending' | 'approved' | 'rejected' | 'expired' | 'invalidated' | 'cancelled'
  expires_at: string
  consumed_at?: string | null
  can_decide?: boolean
  plan: {
    tool: string
    tool_version: string
    risk_class: 'R2'
    target: string
    args_hash: string
    impact: string
    rollback_limit: string
    snapshot: {
      container_id: string
      image_digest: string
      config_hash: string
      asset_version: number
      policy_version: string
      started_at: string
      health: string
    }
  }
}

export interface OpsAttempt {
  attempt_id: string
  approval_id: string
  call_id: string
  status: 'started' | 'succeeded' | 'failed' | 'execution_uncertain'
  started_at: string
  finished_at?: string | null
  evidence?: unknown
}

export interface InfrastructureEvidence {
  call_id: string
  evidence_ref: string
  tool: string
  tool_version: string
  asset_id?: string | null
  asset_version?: number | null
  asset_name?: string | null
  status: 'ok' | 'error'
  vantage?: 'host' | 'container' | 'database' | 'registry' | null
  collected_at: string
  sampled_at?: string | null
  truncated: boolean
  complete: boolean
  filtered_lines?: number
  filter_policy?: string
  error?: { code: string; message: string; retryable: boolean } | null
}

export interface InspectionItem {
  check_id: string
  category: string
  asset_id: string | null
  asset_version: number | null
  tool: string | null
  status: 'passed' | 'finding' | 'inconclusive' | 'not_checked'
  reason: string
  rule: string | null
  evidence_refs: string[]
  sampled_at: string | null
  collected_at: string | null
  vantage: string | null
  historical: boolean
}

export interface InspectionReport {
  version: string
  requested_scope: string[]
  checked_scope: string[]
  items: InspectionItem[]
  confirmed_findings: InspectionItem[]
  unchecked_items: InspectionItem[]
  hypotheses: { review_id: string; decision: string; reason: string; evidence_refs: string[] }[]
  recommendations: string[]
  completeness: string
  review_status: string
}

export interface BusinessQuality {
  version: string
  state: string
  scope: string
  render_state?: string
  digest: string
  facts: {
    id: string
    kind: string
    device_id: number | null
    call_refs: string[]
    text: string
    data: Record<string, unknown>
  }[]
  missing: string[]
}

export interface RAGSource {
  id: string
  kind: 'business' | 'document' | 'web' | 'infrastructure'
  title: string
  url: string
  chapter?: string
  version?: string
  page?: number | null
  published_at?: string | null
  collected_at: string
  summary?: string
}

export interface RAGToolCall {
  name: string
  arguments: Record<string, unknown>
  status: string
  collected_at: string
  result?: unknown
  error?: string
}

export interface RAGEvidenceCard {
  tool: string
  status: string
  device_id?: number | null
  device_code?: string | null
  queried_at?: string | null
  sampled_at?: string | null
  received_at?: string | null
  ts_ms?: number | null
  freshness: 'fresh' | 'stale' | 'future' | 'unknown'
  stale?: boolean | null
  error?: string
}

export interface RAGEvidence {
  state: 'not_queried' | 'queried' | 'partial_failed' | 'failed'
  success_count: number
  failed_count: number
  cards: RAGEvidenceCard[]
}

export interface OpsTask {
  business_quality?: BusinessQuality | null
  task_kind?: 'ops' | 'security_inspection'
  inspection_report?: InspectionReport | null
  security_reviews?: SecurityReview[]
  security_events?: { event_seq: number; review_id: string; state: string; reason: string; created_at: string }[]
  approvals?: OpsApproval[]
  execution_attempts?: OpsAttempt[]
  operation_evidence?: Record<string, unknown>[]
  evidence?: RAGEvidence
  infrastructure_evidence?: InfrastructureEvidence[]
  task_id: string
  session_id: string
  status: OpsStatus
  intent: string
  historical?: boolean
  parent_turn_id?: number | null
  steps?: {
    step_no: number
    tool: string
    status: string
    result?: unknown
    asset_id?: string | null
    asset_version?: number | null
  }[]
  tool_calls?: RAGToolCall[]
  sources?: RAGSource[]
  diagnosis?: {
    summary?: string
    hypotheses?: unknown[]
    recommendations?: unknown[]
    clarification?: string
  }
  warnings?: string[]
  error?: { code: string; message: string; retryable: boolean } | null
  created_at?: string
  finished_at?: string | null
  model_calls?: number
  link_pending?: boolean
}
