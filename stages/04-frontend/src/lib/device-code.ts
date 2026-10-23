const TONE_COUNT = 5

export interface DeviceCodeParts { station: string; kind: string; seq: string }
export interface DeviceNameParts { prefix: string; seq: string; suffix: string }

/**
 * Device codes share a long constant prefix inside one station — only the
 * trailing serial differs (`PV-01-INV-01` … `PV-01-INV-48`). Splitting the code
 * lets the view dim the constant part and emphasise the serial, so rows can be
 * told apart while scanning instead of reading every character.
 */
export function splitDeviceCode(code: string): DeviceCodeParts {
  const match = /^(?:(.+)-)?([A-Za-z]+)-(\d+)$/.exec(code.trim())
  if (!match) return { station: '', kind: '', seq: '' }
  return { station: match[1] ?? '', kind: match[2], seq: match[3] }
}

/** Device names repeat the station name and end in `<serial>号…`. */
export function splitDeviceName(name: string): DeviceNameParts {
  const match = /^(.*?)(\d+)号(.*)$/.exec(name.trim())
  if (!match) return { prefix: name, seq: '', suffix: '' }
  return { prefix: match[1], seq: match[2], suffix: match[3] }
}

/**
 * Stable per-station colour: the same station always renders the same dot, and
 * new stations get a colour without a hand-maintained mapping. The tone only
 * carries the station; the serial stays readable without it.
 */
export function stationTone(station: string): string {
  if (!station) return 't0'
  let hash = 0
  for (let index = 0; index < station.length; index += 1) {
    hash = (hash * 33 + station.charCodeAt(index)) >>> 0
  }
  return `t${(hash % TONE_COUNT) + 1}`
}
