/**
 * report-csv 的列与格式回归：这些断言等于把后端 report_export.go 的输出当契约锁住。
 * 数值只是格式夹具，不追求业务自洽（可用率与时长不互相推导）。
 */
import { describe, expect, it } from 'vitest'
import type { AvailabilityReport, TimelinessReport } from './api'
import { availabilityCsv, timelinessCsv } from './report-csv'

const availability: AvailabilityReport = {
  rule_version: 'availability-v1',
  computed_at: '2026-11-24T01:30:03.123456789Z',
  start: '2026-11-17T01:30:00Z',
  end: '2026-11-24T01:30:00Z',
  devices: [
    {
      device_id: 1,
      device_code: 'INV-0001',
      station_code: 'ST-A',
      covered_ms: 604_800_000,
      window_ms: 604_800_000,
      running_ms: 600_000_000,
      stopped_ms: 0,
      fault_ms: 4_800_000,
      unknown_ms: 0,
      observed_availability: 0.5,
      data_coverage: 1,
      insufficient_data: false,
      rule_version: 'availability-v1',
    },
  ],
  stations: [
    {
      station_code: 'ST-A',
      device_count: 1,
      running_ms: 600_000_000,
      stopped_ms: 0,
      fault_ms: 4_800_000,
      unknown_ms: 0,
      covered_ms: 604_800_000,
      window_ms: 604_800_000,
      non_fault_ms: 600_000_000,
      observed_availability: 0.5,
      data_coverage: 1,
      insufficient_data: false,
    },
  ],
}

describe('availabilityCsv', () => {
  it('输出后端同款表头、设备行、场站行，BOM 开头且末行带换行', () => {
    expect(availabilityCsv(availability)).toBe(
      '\ufeff' +
        [
          '范围类型,设备编码,场站编码,有效窗口(ms),运行(ms),停机(ms),故障(ms),未知(ms),覆盖(ms),观测可用率(0-1),数据覆盖率(0-1),数据不足,规则版本,开始,结束,计算时间,归组依据',
          'device,INV-0001,ST-A,604800000,600000000,0,4800000,0,604800000,0.5,1,false,availability-v1,2026-11-17T01:30:00Z,2026-11-24T01:30:00Z,2026-11-24T01:30:03.123456789Z,当前台账（非历史归属）',
          'station,,ST-A,604800000,600000000,0,4800000,0,604800000,0.5,1,false,availability-v1,2026-11-17T01:30:00Z,2026-11-24T01:30:00Z,2026-11-24T01:30:03.123456789Z,当前台账（非历史归属）',
        ].join('\n') +
        '\n',
    )
  })

  it('含逗号引号的字段按 Go 规则加引号，= + - @ 开头加单引号防公式注入', () => {
    const csv = availabilityCsv({
      ...availability,
      devices: [{ ...availability.devices[0], device_code: 'A,"B' }],
      stations: [{ ...availability.stations[0], station_code: '=1+1' }],
    })
    expect(csv).toContain('device,"A,""B",')
    expect(csv).toContain('station,,\'=1+1,')
  })

  it('空值输出空串，不写 null / undefined', () => {
    const csv = availabilityCsv({
      ...availability,
      devices: [{ ...availability.devices[0], observed_availability: null, data_coverage: null }],
    })
    expect(csv).toContain(',604800000,,,false,')
    expect(csv).not.toContain('null')
    expect(csv).not.toContain('undefined')
  })
})

describe('timelinessCsv', () => {
  it('四个指标各一行，重复列取同一份汇总值', () => {
    const report: TimelinessReport = {
      rule_version: 'timeliness-v1',
      computed_at: '2026-11-24T01:30:03Z',
      start: '2026-11-17T01:30:00Z',
      end: '2026-11-24T01:30:00Z',
      scope: 'all',
      result: {
        scope: 'all',
        ack_timeliness: { count: 2, mean_ms: 100, median_ms: 100, p90_ms: 150 },
        recover_timeliness: { count: 0, mean_ms: null, median_ms: null, p90_ms: null },
        pending_ack: 1,
        pending_recover: 2,
        managed_or_unknown_recover: 0,
        telemetry_recover_timeliness: { count: 0, mean_ms: null, median_ms: null, p90_ms: null },
        prediction_recover_timeliness: { count: 0, mean_ms: null, median_ms: null, p90_ms: null },
        managed_recover: 0,
        unknown_recover: 0,
        recovery_reasons: {},
        event_count: 3,
        invalid_lifecycle: 0,
      },
    }
    const rows = timelinessCsv(report).trim().split('\n')
    expect(rows).toHaveLength(5)
    expect(rows[0]).toContain('指标（平台记录时间）')
    expect(rows[1]).toBe(
      'all,告警确认耗时,2,100,100,150,1,2,0,0,3,0,timeliness-v1,2026-11-17T01:30:00Z,2026-11-24T01:30:00Z,2026-11-24T01:30:03Z',
    )
    expect(rows[2]).toContain('平台告警恢复耗时（合计）,0,,,')
  })
})
