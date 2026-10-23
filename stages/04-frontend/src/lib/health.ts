export const healthState = (state: string): string => ({ offline: '连接离线', fault: '遥测故障状态', online: '在线', unknown: '数据不足' }[state] ?? state)
export const healthBasis = (basis: string): string => ({
  'no valid sample': '从未取得合法样本', 'latest cache unavailable': '最新数据读取失败', 'latest payload invalid': '最新数据格式异常',
  'latest payload incomplete or invalid': '最新数据缺少必要字段或状态异常', 'reception time anomaly': '接收时间异常', 'sample time anomaly': '采样时间异常',
  'stale reception (>15s)': '接收超过 15 秒，连接离线；不代表已证实硬件故障', 'stale sample (>15s)': '接收新鲜但采样已过期',
  'status=2 fault': '最新遥测 status=2', 'highest active alarm level': '按最高活动告警级别评分', 'online with no active alarm': '在线且未查到活动告警',
}[basis] ?? basis)

export function alarmClosure(reason?: string | null): string {
  return ({simulation_demo_ended: '模拟演示已结束（不代表实际指标恢复）', telemetry_recovered: '遥测恢复', prediction_risk_resolved: '预测风险下降', rule_disabled: '规则禁用（管理关闭）', rule_deleted: '规则删除（管理关闭）', device_deleted: '设备删除（管理关闭）'}[reason ?? ''] ?? '关闭原因未知（不能据此认定故障消除）')
}
