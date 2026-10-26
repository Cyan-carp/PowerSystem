import type { Interpretation } from '../types'

export function attentionOrder(items: Interpretation[]): Interpretation[] {
  const rank = { urgent: 0, major: 1, minor: 2 }
  return [...items].sort((a, b) => rank[a.level] - rank[b.level] || Date.parse(b.occurred_at) - Date.parse(a.occurred_at) || b.id - a.id)
}

export const reasonLabels: Record<string, string> = {
  not_configured: '尚未接入可用模型', auth_failed: '模型认证失败', quota_exhausted: '供应商报告额度不足',
  rate_limited: '模型请求受到限流', unavailable: '模型服务暂不可用', answer_validation_failed: '回答未通过证据校验',
  answer_truncated: '模型输出被截断，系统最多额外重试一次',
  model_invalid_response: '模型返回格式无法解析，请管理员检查模型接口兼容性',
  model_timeout: '模型响应超时', model_unreachable: '无法连接模型服务',
  model_non_json: '供应商返回的内容不是JSON', model_empty_choices: '供应商没有返回候选答案',
  model_empty_content: '供应商返回空答案', model_unexpected_sse: '供应商返回了非预期的流式响应',
  model_invalid_envelope: '供应商响应结构异常', model_invalid_role: '供应商响应角色异常',
  model_unexpected_tools: '供应商返回了不允许的工具调用', thinking_mode_mismatch: '供应商未按配置关闭thinking',
  answer_schema_invalid: '模型答案结构不符合事件解读协议', invalid_fact_reference: '模型引用了无效的事件事实',
  unsupported_numeric_claim: '模型包含未绑定来源的数值或时间', prediction_value_changed: '模型混淆了概率或阈值',
  unsupported_causal_claim: '模型给出了证据不足的确定因果判断', circuit_open: '专属解读通道熔断中，等待恢复',
  model_state_unavailable: '无法安全读取解读通道状态，已停止调用', call_budget_exhausted: '已达到每事件两次调用上限',
  retry_deadline_exceeded: '已超过事件自动重试时限', evidence_unavailable: '事件证据获取失败',
}
