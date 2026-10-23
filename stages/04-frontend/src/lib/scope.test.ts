import { describe, expect, it } from 'vitest'
import { allowsDailyCurve, reconcileStation, scopeOptions } from './scope'
import type { Station } from '../types'

const businessStations: Station[] = [
  { station_code: 'PV-01', station_name: '示范园区 A', scope: 'business', kind: 'pv', expected_device_count: 48, registered_device_count: 48 },
  { station_code: 'PV-02', station_name: '示范园区 B', scope: 'business', kind: 'pv', expected_device_count: 40, registered_device_count: 40 },
]
const baselineStation: Station = { station_code: 'ST-01', station_name: '基线站', scope: 'baseline', kind: 'pv', expected_device_count: 3, registered_device_count: 3 }
const allStations = [...businessStations, baselineStation]

describe('scope curve policy', () => {
  it('allows the full-day device curves only for the baseline scope', () => {
    expect(allowsDailyCurve('baseline')).toBe(true)
    expect(allowsDailyCurve('business')).toBe(false)
    expect(allowsDailyCurve('all')).toBe(false)
  })

  it('keeps business/all/baseline switchable in the UI', () => {
    expect(scopeOptions.map((item) => item.value)).toEqual(['business', 'all', 'baseline'])
  })
})

describe('station reconciliation on scope change', () => {
  it('clears a business station when switching to the baseline scope', () => {
    expect(reconcileStation('PV-01', 'baseline', [baselineStation])).toBe('')
  })

  it('clears a baseline station when switching to the business scope', () => {
    expect(reconcileStation('ST-01', 'business', businessStations)).toBe('')
  })

  it('keeps a station that belongs to the selected scope', () => {
    expect(reconcileStation('PV-02', 'business', businessStations)).toBe('PV-02')
    expect(reconcileStation('ST-01', 'baseline', [baselineStation])).toBe('ST-01')
  })

  it('keeps any listed station in the all scope', () => {
    expect(reconcileStation('ST-01', 'all', allStations)).toBe('ST-01')
    expect(reconcileStation('PV-01', 'all', allStations)).toBe('PV-01')
    expect(reconcileStation('PV-99', 'all', allStations)).toBe('')
  })

  it('returns empty for an empty selection', () => {
    expect(reconcileStation('', 'business', businessStations)).toBe('')
  })
})
