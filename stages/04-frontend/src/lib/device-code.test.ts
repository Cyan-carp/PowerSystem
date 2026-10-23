import { describe, expect, it } from 'vitest'
import { splitDeviceCode, splitDeviceName, stationTone } from './device-code'

describe('device code splitting', () => {
  it('separates the station, the device kind and the serial', () => {
    expect(splitDeviceCode('PV-01-INV-33')).toEqual({ station: 'PV-01', kind: 'INV', seq: '33' })
  })

  it('handles a code without a station segment', () => {
    expect(splitDeviceCode('INV-1001')).toEqual({ station: '', kind: 'INV', seq: '1001' })
  })

  it('keeps the code verbatim when it does not end in a serial', () => {
    // A station code is not a device code; only the device column uses this.
    expect(splitDeviceCode('weird code')).toEqual({ station: '', kind: '', seq: '' })
    expect(splitDeviceCode('INV-AB')).toEqual({ station: '', kind: '', seq: '' })
  })
})

describe('device name splitting', () => {
  it('isolates the serial shared by neighbouring devices', () => {
    expect(splitDeviceName('示范园区 A 屋顶光伏片区 01号逆变器')).toEqual({
      prefix: '示范园区 A 屋顶光伏片区 ', seq: '01', suffix: '逆变器',
    })
  })

  it('returns the whole name when there is no serial marker', () => {
    expect(splitDeviceName('INV-1001')).toEqual({ prefix: 'INV-1001', seq: '', suffix: '' })
  })
})

describe('station colour tone', () => {
  it('is stable for the same station', () => {
    expect(stationTone('PV-01')).toBe(stationTone('PV-01'))
  })

  it('gives the four business stations distinct colours', () => {
    const tones = ['PV-01', 'PV-02', 'PV-03', 'PV-04'].map(stationTone)
    expect(new Set(tones).size).toBe(4)
  })

  it('falls back to the neutral dot when the station is unknown', () => {
    expect(stationTone('')).toBe('t0')
  })
})
