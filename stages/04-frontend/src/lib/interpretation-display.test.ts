import { describe, expect, it } from 'vitest'
import { alarmMetricLabel, alarmOriginLabel, evidenceSourceLabel, evidenceStatusLabel, evidenceToolLabel, isLegacySimulationMetric, legacySimulationNote, monitorAlertLabel, monitorSeverityLabel, normalAlarmSamplingNote, readableInterpretationText } from './interpretation-display'

describe('告警解读展示文案', () => {
  it('将常见告警及监控字段转为易懂中文', () => {
    expect(alarmMetricLabel('ai_failure_risk')).toBe('AI 故障风险')
    expect(monitorAlertLabel('PowerSystemAPIDown')).toBe('平台接口监控抓取异常')
    expect(monitorSeverityLabel('critical')).toBe('紧急')
    expect(evidenceToolLabel('get_alarm_detail')).toBe('告警详情')
    expect(evidenceStatusLabel('ok')).toBe('已获取')
  })

  it('保留技术定位符，只替换已知的英文描述', () => {
    expect(readableInterpretationText('PowerSystem API scrape failed for over one minute；实例 api:8080，job=api，级别 critical，状态 firing'))
      .toBe('平台接口监控连续超过一分钟抓取失败；实例 api:8080，job=api，级别 紧急，状态 告警中')
    expect(readableInterpretationText('风险等级 high，来源 synthetic-trained，top_factors 为空；metric 为 temperature，min 41、max 70、mean 55.5'))
      .toBe('风险等级 高风险，来源 合成数据训练，主要影响因素 为空；指标 为 温度，最小值 41、最大值 70、平均值 55.5')
    expect(readableInterpretationText('核查 API 实例 api:8080 与 /api/v1/devices/3/telemetry?metric=temperature'))
      .toBe('核查 接口 实例 api:8080 与 /api/v1/devices/3/telemetry?metric=temperature')
    expect(readableInterpretationText('触发于 2026/10/15 14:37:25')).toBe('触发于 2026.10.15 14:37:25')
    expect(readableInterpretationText('device_id 为 3，threshold 为 0.35；保持 job=api 和 test_run=go-outage'))
      .toBe('设备编号 为 3，阈值 为 0.35；保持 job=api 和 test_run=go-outage')
  })

  it('用来源说明代替接口路径', () => {
    expect(evidenceSourceLabel({ tool: 'get_alarm_detail', source: '/api/v1/alarms/98' })).toBe('告警详情记录')
    expect(evidenceSourceLabel({ tool: 'monitor_event', source: 'alertmanager/webhook' })).toBe('平台监控通知')
  })

  it('区分自然规则告警与历史演示记录', () => {
    expect(alarmMetricLabel('temperature')).toBe('温度')
    expect(alarmMetricLabel('ai_failure_risk')).toBe('AI 故障风险')
    expect(alarmMetricLabel('simulation_scenario')).toBe('模拟演示告警（历史）')
    expect(alarmOriginLabel('normal')).toBe('自然规则告警')
    expect(alarmOriginLabel('simulation')).toBe('历史演示告警')
    expect(alarmOriginLabel(undefined)).toBe('来源未标注')
    expect(isLegacySimulationMetric('simulation_scenario')).toBe(true)
    expect(isLegacySimulationMetric('temperature')).toBe(false)
  })

  it('对自然规则告警只作条件性合成来源提醒，不把设备断言成合成设备', () => {
    expect(normalAlarmSamplingNote).toContain('若该设备属于合成车队')
    expect(normalAlarmSamplingNote).toContain('可能来自真实设备')
    expect(normalAlarmSamplingNote).not.toContain('采样来自合成设备')
    expect(normalAlarmSamplingNote).toContain('不代表已确认物理现场故障')
    expect(legacySimulationNote).toContain('仅作历史留存')
    expect(legacySimulationNote).not.toContain('10分钟后')
  })
})
