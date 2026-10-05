import type { AgentClaim, AgentEvidence, ChatEvidence } from '../types'

export const operationNotice = '涉及实际设备操作：须由专业人员核对实际机型、厂家手册和现场条件后决定并执行。AI 不执行操作。'

export function requiresEquipmentReview(claim: AgentClaim): boolean {
  if (claim.equipment_operation !== undefined) return claim.equipment_operation
  if (/检查|检测|测试|调整|开启|关闭|设备|风机|逆变器|光伏|机型|机舱|机柜|配电|电缆|冷却|风扇|部件|现场/.test(claim.text)) return true
  if (/断电|停机|启机|启动|重启|复位|拆卸|拆开|更换|短接|拔插|改线|接线|调参|修改参数|远控|送电|合闸|拉闸|验电|测量|测试绝缘|清洁|清理|除冰|润滑|紧固|校准|登高|检修|维修|接地线/.test(claim.text)) return true
  return !/^(?:建议)?(?:人工)?(?:先|优先|请)?(?:(?:查阅|阅读|查看|核对|比对|收集|整理|记录|咨询|联系|提交|打开|展开|检索|搜索)(?:厂家|供应商|专业|引用|本条|相关|现有|受控|原始|平台|本轮|本次|完整|具体|公开|知识库)?(?:手册|资料|文档|来源|记录|日志|曲线|告警|页面|证据|工程师|专业人员|参数说明|现有数据|预测)(?:内容|章节|版本|原文|来源|记录|曲线|页面|日志|证据)?|确认告警(?:并标记已读)?|标记已读)[。；;.!！]?$/.test(claim.text.trim())
}

export function claimEvidence(claim: AgentClaim, evidence: (AgentEvidence | ChatEvidence)[]): ChatEvidence[] {
  return claim.evidence_ids.flatMap(id => {
    const item = evidence.find(value => value.id === id)
    return item ? [{ ...item, kind: 'kind' in item ? item.kind : 'business' } as ChatEvidence] : []
  })
}

export function publicLink(value?: string): string | null {
  try {
    const url = new URL(value || '')
    if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443') || url.search || url.hash) return null
    const host = url.hostname.toLowerCase()
    if (!host.includes('.') || host.endsWith('.local') || host.endsWith('.internal') || host === 'localhost' || host.startsWith('[') || /^\d+\.\d+\.\d+\.\d+$/.test(host)) return null
    return url.href
  } catch { return null }
}

export function evidenceRoute(evidence: ChatEvidence): string | null {
  if (evidence.kind !== 'business') return null
  const alarm = evidence.source.match(/^\/api\/v1\/alarms\/(\d+)(?:\?|$)/)
  if (alarm) return `/alarms?id=${alarm[1]}`
  const device = evidence.source.match(/^\/api\/v1\/devices\/(\d+)(?:\/|\?|$)/)
  if (device) return `/devices/${device[1]}`
  if (evidence.tool === 'list_devices') return '/devices'
  if (evidence.tool === 'list_alarms') return '/alarms'
  if (evidence.tool === 'get_dashboard_summary') return '/dashboard'
  return null
}

export const chatStatusLabels = { answered: '已回答', unable_to_determine: '依据不足', degraded: '服务降级' }
