/**
 * CSV 与后端的对照测试。
 *
 * 关键点：输入走的是**真实 HTTP 响应体**（tests/fixtures/report-availability.json，
 * 由后端 curl 抓下来的紧凑 JSON），经 JSON.parse 后再交给生成器——而不是把后端 CSV
 * 反解成对象再生成一遍（那样是循环论证，验不出序列化与解析的精度问题）。
 *
 * 两份样本来自同一夹具、同一窗口的相邻两次请求，所以除「计算时间」列外必须逐列相同。
 * 及时性报表没有真实导出样本，用夹具验证列序与格式规则，缺口写在测试名里。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { AvailabilityReport, TimelinessReport } from '../src/lib/api'
import { availabilityCsv, timelinessCsv } from '../src/lib/report-csv'

const dir = resolve(__dirname, 'fixtures')
const payload = JSON.parse(readFileSync(resolve(dir, 'report-availability.json'), 'utf8')) as {
  code: number
  message: string
  data: AvailabilityReport
}
const backendCsv = readFileSync(resolve(dir, 'report-availability.csv'), 'utf8')

const rows = (text: string): string[] => {
  const list = text.replace(/^\uFEFF/, '').split('\n')
  if (list[list.length - 1] === '') list.pop()
  return list
}

/** 真实 JSON 里 computed_at 的列位置：设备行与场站行共用尾部，第 16 列（0 基） */
const COMPUTED_AT_COL = 15

describe('可用率：真实响应 → CSV 生成器', () => {
  const report = payload.data
  const mine = rows(availabilityCsv(report))
  const theirs = rows(backendCsv)

  it('响应本身是完整结果：143 台设备 + 7 个场站，无分页截断', () => {
    expect(payload.code).toBe(0)
    expect(report.devices).toHaveLength(143)
    expect(report.stations).toHaveLength(7)
    expect(mine).toHaveLength(1 + 143 + 7)
    expect(theirs).toHaveLength(1 + 143 + 7)
  })

  it('行数与行序一致：设备在前、场站在后，与后端导出同序', () => {
    expect(mine.map((line) => line.split(',')[0])).toEqual(theirs.map((line) => line.split(',')[0]))
    expect(mine[1].split(',')[1]).toBe(report.devices[0].device_code)
    expect(mine[mine.length - 1].split(',')[2]).toBe(report.stations[report.stations.length - 1].station_code)
  })

  it('除「计算时间」外逐字节相同（两次请求的时间戳不同，其余统计值必须一致）', () => {
    const differences: string[] = []
    for (let i = 0; i < theirs.length; i += 1) {
      if (mine[i] === theirs[i]) continue
      const a = mine[i].split(',')
      const b = theirs[i].split(',')
      for (let col = 0; col < Math.max(a.length, b.length); col += 1) {
        if (a[col] !== b[col]) differences.push(`行${i} 列${col}`)
      }
    }
    /* 差异必须全部落在第 16 列，且只有它 */
    expect(differences.length).toBeGreaterThan(0)
    expect(new Set(differences.map((item) => item.split(' 列')[1]))).toEqual(
      new Set([String(COMPUTED_AT_COL)]),
    )
  })

  it('把生成时间对齐后整份逐字节相同（BOM、列序、浮点、空值、转义全部一致）', () => {
    const aligned = mine.map((line) => {
      const cells = line.split(',')
      if (cells.length > COMPUTED_AT_COL) cells[COMPUTED_AT_COL] = report.computed_at
      return cells.join(',')
    })
    const expected = theirs.map((line) => {
      const cells = line.split(',')
      if (cells.length > COMPUTED_AT_COL) cells[COMPUTED_AT_COL] = report.computed_at
      return cells.join(',')
    })
    expect(aligned.join('\n')).toBe(expected.join('\n'))
  })

  it('后端产物本身带 BOM，且以单个换行结尾（Go encoding/csv 行为）', () => {
    expect(backendCsv.startsWith('\ufeff')).toBe(true)
    expect(backendCsv.endsWith('\n')).toBe(true)
    expect(availabilityCsv(report).startsWith('\ufeff')).toBe(true)
    expect(availabilityCsv(report).endsWith('\n')).toBe(true)
  })
})

describe('可用率：格式规则（用真实响应覆盖不到的边界）', () => {
  const base = payload.data

  const withDevice = (patch: Record<string, unknown>): AvailabilityReport => ({
    ...base,
    devices: [{ ...base.devices[0], ...patch } as AvailabilityReport['devices'][number]],
    stations: [],
  })

  it('空值写成空串，不写 null 或 0', () => {
    const csv = availabilityCsv(withDevice({ observed_availability: null, data_coverage: null }))
    const cells = csv.split('\n')[1].split(',')
    expect(cells[9]).toBe('')
    expect(cells[10]).toBe('')
    expect(cells[11]).toBe('false')
  })

  it('以 = + - @ 开头的字段前置单引号，防公式注入', () => {
    const csv = availabilityCsv(withDevice({ device_code: '=cmd|calc' }))
    expect(csv.split('\n')[1].split(',')[1]).toBe("'=cmd|calc")
  })

  it('含逗号的字段加引号，内部引号翻倍', () => {
    const csv = availabilityCsv(withDevice({ device_code: 'A,B"c' }))
    /* 这一行不能按逗号切：切了正好把带引号的字段拆开，改为整行断言 */
    expect(csv.split('\n')[1]).toContain('device,"A,B""c",')
  })
})

describe('及时性：夹具验证列序与格式（缺真实导出样本，见文档待补项）', () => {
  const report: TimelinessReport = {
    rule_version: 'timeliness-v1',
    computed_at: '2026-11-24T06:36:18.681261Z',
    start: '2026-11-10T00:13:00Z',
    end: '2026-11-17T00:13:00Z',
    scope: 'all',
    result: {
      scope: 'all',
      ack_timeliness: { count: 120, mean_ms: 1834.5, median_ms: 1200, p90_ms: 3600 },
      recover_timeliness: { count: 40, mean_ms: 7200, median_ms: null, p90_ms: null },
      pending_ack: 3,
      pending_recover: 2,
      managed_or_unknown_recover: 9,
      telemetry_recover_timeliness: { count: 30, mean_ms: 5400.25, median_ms: 5000, p90_ms: 9000 },
      prediction_recover_timeliness: { count: 10, mean_ms: 0.0000001, median_ms: 0.0000001, p90_ms: 0.0000001 },
      managed_recover: 5,
      unknown_recover: 4,
      event_count: 175,
      invalid_lifecycle: 0,
      recovery_reasons: {},
    },
  }

  const csv = timelinessCsv(report)
  const line = rows(csv)

  it('16 列，4 个指标行，列名与 report_export.go 逐字对齐', () => {
    expect(line).toHaveLength(5)
    expect(line[0]).toBe(
      '范围,指标（平台记录时间）,有效样本数,平均(ms),中位数(ms),P90(ms),未确认,未恢复,管理关闭,历史或原因未知,事件总数,异常生命周期,规则版本,开始,结束,计算时间',
    )
  })

  it('指标行写的是后端那四个固定名称，顺序不能换', () => {
    expect(line.slice(1).map((row) => row.split(',')[1])).toEqual([
      '告警确认耗时',
      '平台告警恢复耗时（合计）',
      '遥测恢复耗时',
      '预测风险下降耗时',
    ])
  })

  it('中位数为 null 时留空，计数与规则版本按后端取值', () => {
    const cells = line[2].split(',')
    expect(cells[2]).toBe('40')
    expect(cells[3]).toBe('7200')
    expect(cells[4]).toBe('')
    expect(cells[5]).toBe('')
    expect(cells[6]).toBe('3')
    expect(cells[12]).toBe('timeliness-v1')
  })

  it('小于 1e-6 的比率不写成指数记法', () => {
    const cells = line[4].split(',')
    expect(cells[3]).toBe('0.0000001')
    expect(cells[3]).not.toContain('e')
  })
})
