export interface ApiEnvelope<T> { code: number; message: string; data: T }
export interface Page<T> { list: T[]; page: number; page_size: number; total: number }
export interface User { id: number; username: string; real_name: string; role: string }
export interface LoginResult { token: string; token_type: string; expires_in: number; user: User }
export interface Device {
  id: number; device_code: string; name: string; dev_type: string; vendor: string
  is_simulated: boolean; reference_vendor: string; reference_model: string
  station_code: string; group_name: string; created_at: string; updated_at: string
}
export interface Telemetry {
  device_id: number; device_code: string; run_id: string; seq: number; ts_ms: number
  received_at: string; voltage: number; current: number; temperature: number
  power: number; status: number; fault_code: number
}
export type Metric = 'voltage' | 'current' | 'temperature' | 'power'
export interface History { device_id: number; metric: Metric; points: [number | string, number][] }
export interface Summary {
  device_total: number; online: number; offline: number; fault: number
  current_power_kw: number; active_alarms: number; operational_health_percent: number
  today_energy_kwh: number | null; retained_energy_kwh: number | null
  energy_start_ms: number | null; energy_updated_at: string | null
  health_score_percent: number | null
  health_valid_count?: number; health_unknown_count?: number; health_provisional?: boolean; health_rule_version?: string; health_computed_at?: string
}
export type AlarmLevel = 'urgent' | 'major' | 'minor'
export type AlarmStatus = 'unhandled' | 'acked' | 'recovered'
export interface Alarm {
  id: number; device_id: number; rule_id: number | null; metric: string; level: AlarmLevel
  value: number; threshold: number; status: AlarmStatus; acked_by: number | null
  acked_at: string | null; recovered_at: string | null; triggered_at: string
  archived: boolean
  close_reason?: string | null; rule_operator?: string; rule_version?: string
}
export interface DeviceHealth {
  rank: number | null
  device_id: number; device_code: string; name: string; station_code: string
  score: number | null; state: string; active_level?: string; status?: number | null
  sampled_at_ms?: number | null; received_at?: string | null; matched_by: string; rule_version: string
}
export interface StationHealth {
  rank: number | null
  station_code: string; score: number | null; provisional: boolean
  device_total: number; valid_count: number; unknown_count: number; rule_version: string
}
export interface AvailabilityItem {
  device_id: number; device_code: string; station_code: string
  covered_ms: number; window_ms: number; running_ms: number; stopped_ms: number
  fault_ms: number; unknown_ms: number; observed_availability: number | null
  data_coverage: number | null; insufficient_data: boolean; rule_version: string
}
export interface StationAvailability {
  running_ms: number; stopped_ms: number; fault_ms: number; unknown_ms: number
  station_code: string; covered_ms: number; window_ms: number; non_fault_ms: number
  observed_availability: number | null; data_coverage: number | null
  insufficient_data: boolean; device_count: number
}
export interface TimelinessStat { count: number; mean_ms: number | null; median_ms: number | null; p90_ms: number | null }
export interface TimelinessResult {
  scope: string; ack_timeliness: TimelinessStat; recover_timeliness: TimelinessStat
  pending_ack: number; pending_recover: number; managed_or_unknown_recover: number
  telemetry_recover_timeliness: TimelinessStat; prediction_recover_timeliness: TimelinessStat
  managed_recover: number; unknown_recover: number; event_count: number; invalid_lifecycle: number; recovery_reasons: Record<string, number>
}
export interface PredictionListItem {
  device_id: number; device_code: string; name: string; station_code: string; group_name: string
  window_end_ms: number | null; probability: number | null; threshold: number | null
  risk_level: 'high' | 'low' | null; model_version: string | null; source: string | null
  stale: boolean
}
export interface Factor { feature: string; value: number; contribution: number }
export interface Prediction {
  device_id: number; window_end_ms: number; probability: number; threshold: number
  risk_level: 'high' | 'low'; model_version: string; top_factors: Factor[]
  source: string; created_at: string; stale: boolean
}
export type RealtimeEvent =
  | { type: 'interpretation_updated'; data: { id?: number; status?: string; updated_at: string } }
  | { type: 'agent_model_updated'; data: { available: boolean } }
  | { type: 'connected' | 'disconnected' }
  | { type: 'telemetry'; data: Telemetry }
  | { type: 'alarm_created' | 'alarm_acked' | 'alarm_recovered'; data: Alarm }
  | { type: 'prediction'; data: { device_id: number } }

export interface AgentEvidence {
  id: string; tool: string; status: string; source: string; collected_at: string; data_time: string | null
  data: Record<string, unknown>
}
export interface AgentClaim { text: string; evidence_ids: string[]; equipment_operation?: boolean }
export interface Interpretation {
  id: number; event_key: string; category: 'device_alarm' | 'prediction_risk' | 'platform_monitor'
  alarm_id: number | null; level: AlarmLevel; occurred_at: string; updated_at: string
  task_status: 'pending' | 'running' | 'completed' | 'degraded'; reason: string; active: boolean; read: boolean
  alarm: Alarm | null; monitor: Record<string, unknown> | null; evidence: AgentEvidence[] | null
  result: { status: string; conclusion: AgentClaim | null; suggestions: AgentClaim[]; limitations: string[]; model?: string } | null
}
export interface AgentModelStatus { available: boolean; reason: string; message: string }

export interface ChatEvidence extends AgentEvidence {
  kind: 'business' | 'document' | 'web'; document_version?: string; chapter?: string; url?: string
}
export interface AgentChatResponse {
  request_id: string; session_id: string; status: 'answered' | 'unable_to_determine' | 'degraded' | 'out_of_scope'
  conclusion: AgentClaim | null; suggestions: AgentClaim[]; evidence: ChatEvidence[]
  limitations: string[]; notices: string[]; model: string; knowledge_status: string; web_status: string; miss_record_status: string
  answer_mode: 'grounded' | 'hypothesis'; source_coverage_percent: number; sourced_claims: number; total_claims: number
  verified_facts?: AgentClaim[]; unverified_explanations?: AgentClaim[]
  resolved_devices?: { id: number; device_code: string }[]
}
export interface KnowledgeSource { id: string; document: string; heading: string; content: string; version: string; authority: string; document_date?: string; source_kind?: string; source_revision?: string }

export interface RAGSource { id: string; kind: 'business' | 'document' | 'web' | 'infrastructure'; title: string; url: string; chapter?: string; version?: string; page?: number | null; published_at?: string | null; collected_at: string; summary?: string }
export interface RAGToolCall { name: string; arguments: Record<string, unknown>; status: string; collected_at: string; result?: unknown; error?: string }
export interface RAGEvidenceCard { tool: string; status: string; device_id?: number | null; device_code?: string | null; queried_at?: string | null; sampled_at?: string | null; received_at?: string | null; ts_ms?: number | null; freshness: 'fresh' | 'stale' | 'future' | 'unknown'; stale?: boolean | null; error?: string }
export interface RAGEvidence { state: 'not_queried' | 'queried' | 'partial_failed' | 'failed'; success_count: number; failed_count: number; cards: RAGEvidenceCard[] }
export type VerificationCheck = 'latest_telemetry' | 'current_alarms' | 'prediction'
export interface VerificationInput { parent_turn_id: number; device_id: number; checks: VerificationCheck[]; verification_id: string }
export type OpsStatus = 'queued' | 'planning' | 'executing' | 'awaiting_approval' | 'succeeded' | 'failed' | 'cancelled' | 'timeout'
export interface SecurityReview { review_id: string; step_no: number; kind: string; state: string; decision?: string | null; reason?: string | null; rule_hits: string[]; evidence_refs: string[]; execution_epoch: number; created_at: string; finished_at?: string | null; expires_at?: string | null; error_code?: string | null; policy_version: string; duration_ms?: number | null }
export interface OpsApproval {
  approval_id: string; task_id: string; step_no: number; status: 'pending' | 'approved' | 'rejected' | 'expired' | 'invalidated' | 'cancelled'; expires_at: string; consumed_at?: string | null; can_decide?: boolean
  plan: { tool: string; tool_version: string; risk_class: 'R2'; target: string; args_hash: string; impact: string; rollback_limit: string; snapshot: { container_id: string; image_digest: string; config_hash: string; asset_version: number; policy_version: string; started_at: string; health: string } }
}
export interface OpsAttempt { attempt_id: string; approval_id: string; call_id: string; status: 'started' | 'succeeded' | 'failed' | 'execution_uncertain'; started_at: string; finished_at?: string | null; evidence?: unknown }
export interface InfrastructureEvidence { call_id: string; evidence_ref: string; tool: string; tool_version: string; asset_id?: string | null; asset_version?: number | null; asset_name?: string | null; status: 'ok' | 'error'; vantage?: 'host' | 'container' | 'database' | 'registry' | null; collected_at: string; sampled_at?: string | null; truncated: boolean; complete: boolean; filtered_lines?: number; filter_policy?: string; error?: { code: string; message: string; retryable: boolean } | null }
export interface InspectionItem { check_id: string; category: string; asset_id: string | null; asset_version: number | null; tool: string | null; status: 'passed' | 'finding' | 'inconclusive' | 'not_checked'; reason: string; rule: string | null; evidence_refs: string[]; sampled_at: string | null; collected_at: string | null; vantage: string | null; historical: boolean }
export interface InspectionReport { version: string; requested_scope: string[]; checked_scope: string[]; items: InspectionItem[]; confirmed_findings: InspectionItem[]; unchecked_items: InspectionItem[]; hypotheses: { review_id: string; decision: string; reason: string; evidence_refs: string[] }[]; recommendations: string[]; completeness: string; review_status: string }
export interface BusinessQuality { version: string; state: string; scope: string; render_state?: string; digest: string; facts: {id: string; kind: string; device_id: number | null; call_refs: string[]; text: string; data: Record<string, unknown>}[]; missing: string[] }
export interface OpsTask { business_quality?: BusinessQuality | null; task_kind?: 'ops' | 'security_inspection'; inspection_report?: InspectionReport | null; security_reviews?: SecurityReview[]; security_events?: { event_seq: number; review_id: string; state: string; reason: string; created_at: string }[]; approvals?: OpsApproval[]; execution_attempts?: OpsAttempt[]; operation_evidence?: Record<string, unknown>[]; evidence?: RAGEvidence; infrastructure_evidence?: InfrastructureEvidence[]; task_id: string; session_id: string; status: OpsStatus; intent: string; historical?: boolean; parent_turn_id?: number | null; steps?: { step_no: number; tool: string; status: string; result?: unknown; asset_id?: string | null; asset_version?: number | null }[]; tool_calls?: RAGToolCall[]; sources?: RAGSource[]; diagnosis?: { summary?: string; hypotheses?: unknown[]; recommendations?: unknown[]; clarification?: string }; warnings?: string[]; error?: { code: string; message: string; retryable: boolean } | null; created_at?: string; finished_at?: string | null; model_calls?: number; link_pending?: boolean }
export interface RAGChatResponse { business_quality?: BusinessQuality | null; request_id: string; session_id: string; status: 'answered' | 'degraded' | 'out_of_scope'; answer_markdown: string; sources: RAGSource[]; tool_calls: RAGToolCall[]; warnings: string[]; model: string; response_mode?: 'model' | 'preset' | 'boundary' | 'task_dispatched'; task_cards?: OpsTask[]; model_called?: boolean; boundary?: { action: 'allow' | 'preset' | 'block'; reason: string; rule_id: string; rule_version: string }; turn_id?: number; evidence?: RAGEvidence; verification_available?: boolean; kind?: 'chat' | 'verification'; parent_turn_id?: number; device?: { id: number; device_code: string; name: string }; verification_results?: Record<string, unknown> }
export interface RAGSession { id: string; title: string; created_at: string; updated_at: string }
export interface RAGTurn { id?: number; kind?: 'chat' | 'verification'; parent_turn_id?: number; question: string; response: RAGChatResponse; created_at?: string }
