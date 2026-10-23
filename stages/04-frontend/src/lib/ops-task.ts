import type { OpsStatus, OpsTask } from '../types'

export const activeTaskStatuses: readonly OpsStatus[] = ['queued', 'planning', 'executing', 'awaiting_approval']

export const terminalTask = (status: OpsStatus): boolean => ['succeeded', 'failed', 'cancelled', 'timeout'].includes(status)
export const taskLabels: Record<OpsStatus, string> = { queued: '已受理 · 排队中', planning: '正在规划', executing: '正在执行', awaiting_approval: '等待管理员审批', succeeded: '任务完成', failed: '任务失败', cancelled: '已取消后续步骤', timeout: '任务超时' }

export function canDecideApproval(role: string | undefined, taskStatus: OpsStatus, approvalStatus: string, expiresAt: string, now = Date.now()): boolean {
  return role === 'admin' && taskStatus === 'awaiting_approval' && approvalStatus === 'pending' && Number.isFinite(Date.parse(expiresAt)) && Date.parse(expiresAt) > now
}
export const approvalLabels = { pending: '等待决定', approved: '已批准', rejected: '已拒绝', expired: '已过期', invalidated: '已失效', cancelled: '已取消' }
export const attemptLabels = { started: '正在执行', succeeded: '执行成功', failed: '执行失败', execution_uncertain: '结果不确定' }
export function taskErrorText(error: { code: string; message: string }): string {
  const labels: Record<string, string> = { target_ambiguous: '目标不明确，请指定在册资产或明确本项目范围。', inspection_scope_too_large: '巡检范围超过任务预算，请缩小范围。', inspection_scope_blocked: '巡检目标不在本轮许可范围内。', required_tool_failed: '必要读取失败，已取得的程序事实保留。', security_denied: '安全审查否决，受审动作未执行。', security_clarification_required: '安全审查需要补充信息，请查看审查理由后新建任务。', security_unavailable: '安全审查不可用，受审动作已停止。', security_invalid: '审查记录无效，受审动作已停止。', security_review_stale: '审查已失效，请重新发起任务。', security_projection_blocked: '材料超出外发许可，程序已阻断。', approval_rejected: '审批已拒绝，未执行重启。', approval_expired: '审批已过期，未执行重启。', approval_invalidated: '目标或策略已变化，原审批失效；请重新发起任务。', execution_uncertain: '动作结果不确定；禁止自动重试，请另行只读核对。' }
  return labels[error.code] || `${error.code} · ${error.message}`
}
export function pollDelay(status: OpsStatus, hidden: boolean, retryAfter = 0): number | null {
  return terminalTask(status) || hidden ? null : Math.max(2000, retryAfter * 1000)
}

/** Only finished tasks may be removed from the records list; running ones must be cancelled first. */
export const canDeleteTask = (status: OpsStatus): boolean => terminalTask(status)

export function toggleSelection(ids: readonly string[], id: string): string[] {
  return ids.includes(id) ? ids.filter(value => value !== id) : [...ids, id]
}

/** Drop selections that vanished from the list or are no longer deletable, keeping the original order. */
export function retainedSelection(ids: readonly string[], records: readonly OpsTask[]): string[] {
  const deletable = new Set(records.filter(task => canDeleteTask(task.status)).map(task => task.task_id))
  return ids.filter(id => deletable.has(id))
}

export function allDeletableSelected(ids: readonly string[], records: readonly OpsTask[]): boolean {
  const deletable = records.filter(task => canDeleteTask(task.status))
  return deletable.length > 0 && deletable.every(task => ids.includes(task.task_id))
}
