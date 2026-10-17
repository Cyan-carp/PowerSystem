import { describe, expect, it } from 'vitest'
import { alarmComparison, contributionNumber, fixedNumber, predictionFeature } from './detailPresentation'

describe('detail number presentation', () => {
  it('preserves zero and missing data, and limits display precision without negative zero', () => {
    expect(fixedNumber(.3500000000000001)).toBe('0.35')
    expect(fixedNumber(0)).toBe('0.00')
    expect(fixedNumber(-.004)).toBe('0.00')
    for (const value of [null, undefined, NaN, Infinity]) expect(fixedNumber(value)).toBe('—')
    expect(contributionNumber(-1.10849)).toBe('-1.108')
    expect(contributionNumber(.70376)).toBe('+0.704')
  })

  it('compares unrounded values even when both displayed numbers are equal', () => {
    const chart = alarmComparison(.3501, .35, true)!
    expect(fixedNumber(.3501)).toBe(fixedNumber(.35))
    expect(chart.above).toBe(true)
    expect(chart.excessWidth).toBeGreaterThan(0)
    expect(chart.differenceText).toContain('差值小于 0.01')
    expect(chart.limit).toBeCloseTo(35)
  })

  it('handles lower, equal, negative, and zero thresholds without invalid geometry', () => {
    for (const [value, threshold] of [[0, 0], [0, 5], [-20, -10], [-5, -10], [1000, 0]]) {
      const chart = alarmComparison(value, threshold)!
      for (const point of [chart.actual, chart.limit, chart.fillStart, chart.fillWidth, chart.excessWidth]) {
        expect(Number.isFinite(point)).toBe(true)
        expect(point).toBeGreaterThanOrEqual(0)
        expect(point).toBeLessThanOrEqual(100)
      }
      expect(chart.excessWidth > 0).toBe(value > threshold)
    }
    expect(alarmComparison(NaN, .35)).toBeNull()
    expect(alarmComparison(1, null)).toBeNull()
  })

  it('translates the previously exposed feature keys and keeps unfamiliar features distinguishable', () => {
    expect(predictionFeature('voltage_std').label).toBe('电压波动')
    expect(predictionFeature('temperature_residual_slope').label).toBe('修正后温度变化速度')
    expect(predictionFeature('new_feature').description).toContain('new_feature')
  })
})
