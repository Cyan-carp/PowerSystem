// @vitest-environment ./tests/vue-environment.ts
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, effectScope, h, nextTick } from 'vue'
import { useLatestRequest } from '../src/lib/useLatestRequest'

const { api, route } = vi.hoisted(() => ({
  api: Object.fromEntries(['summary', 'overview', 'alarms', 'availability', 'timeliness', 'stations', 'devices', 'history', 'exportReport'].map(name => [name, vi.fn()])),
  route: { query: {} as Record<string, string> },
}))
vi.mock('../src/lib/api', () => ({ api }))
vi.mock('vue-router', async () => {
  const { h } = await import('vue')
  return {
    useRoute: () => route,
    useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
    RouterLink: { setup: (_: unknown, { slots }: any) => () => h('a', slots.default?.()) },
  }
})
vi.mock('../src/components/PowerChart.vue', () => ({ default: { render: () => null } }))
// CardMenu's document listeners are unrelated to request scheduling.
vi.mock('../src/components/CardMenu.vue', () => ({ default: { render: () => null } }))

import OverviewView from '../src/views/OverviewView.vue'
import ReportsView from '../src/views/ReportsView.vue'

function deferred<T = any>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

// Vue's public renderer runs the actual page templates and lifecycle without a
// browser dependency. Tests control API completion order instead of wall time.
type Node = { text: string; type: string; children: Node[]; parent: Node | null; props: Record<string, any> }
const node = (type: string, text = ''): Node => Object.assign({ type, text, children: [], parent: null, props: {} }, { addEventListener() {}, getRootNode: () => ({ activeElement: null }), options: [], tagName: type.toUpperCase() })
const renderer = createRenderer<Node, Node>({
  createElement: type => node(type), createText: text => node('text', text), createComment: text => node('comment', text),
  setText: (el, text) => { el.text = text },
  setElementText: (el, text) => { el.text = text; el.children = [] },
  parentNode: el => el.parent,
  nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
  insert: (el, parent, anchor) => {
    if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1)
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, el)
    el.parent = parent
  },
  remove: el => { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1); el.parent = null },
  patchProp: (el, key, _old, value) => { el.props[key] = value },
})
const text = (el: Node): string => (el.type === 'comment' ? '' : el.text) + el.children.map(text).join('')
const find = (el: Node, predicate: (el: Node) => boolean): Node | undefined => predicate(el) ? el : el.children.map(child => find(child, predicate)).find(Boolean)
const flush = async () => { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
const cleanups: (() => void)[] = []
function mount(component: any) {
  const root = node('root')
  const app = renderer.createApp(component)
  app.mount(root)
  cleanups.push(() => app.unmount())
  return root
}
async function click(root: Node, label: string) {
  const button = find(root, el => el.type === 'button' && text(el).trim() === label)
  expect(button, `button: ${label}`).toBeDefined()
  button!.props.onClick()
  await flush()
}
const stat = { count: 0, mean_ms: null, median_ms: null, p90_ms: null }
const timely = { rule_version: 'timeliness-v1', result: { ack_timeliness: stat, recover_timeliness: stat, telemetry_recover_timeliness: stat, prediction_recover_timeliness: stat, pending_ack: 7, pending_recover: 2, recovery_reasons: {}, event_count: 9, invalid_lifecycle: 0 } }
const available = { rule_version: 'availability-v1', stations: [], devices: [] }

beforeEach(() => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.resetAllMocks()
  route.query = {}
  api.summary.mockResolvedValue({ device_total: 143, online: 140, active_alarms: 3, current_power_kw: 1234.5, today_energy_kwh: 100, health_score_percent: 90 })
  api.overview.mockResolvedValue({ stations: [{ station_code: 'ST-TEST', station_name: '独立加载场站', registered_device_count: 1, expected_device_count: 1, online: 1 }] })
  api.alarms.mockResolvedValue({ list: [] })
  api.devices.mockResolvedValue({ list: [], total: 0 })
  api.stations.mockResolvedValue({ list: [{ station_code: 'ST-TEST', station_name: '独立加载场站' }] })
  api.timeliness.mockResolvedValue(timely)
  api.availability.mockResolvedValue(available)
})
afterEach(() => cleanups.splice(0).forEach(cleanup => cleanup()))

describe('page loading isolation', () => {
  it('renders overview metrics, stations and history while availability is still pending', async () => {
    api.availability.mockReturnValue(deferred().promise)
    api.devices.mockResolvedValue({ list: [{ id: 1 }], total: 1 })
    api.history.mockResolvedValue({ points: [[1, 12]] })
    const root = mount(OverviewView)
    await flush()
    expect(text(root)).toContain('1,234.5')
    expect(text(root)).toContain('独立加载场站')
    expect(text(root)).toContain('正在统计近 7 天数据质量')
    expect(api.history).toHaveBeenCalled()
    expect(text(root)).toContain('1 个采样点')
  })

  it('keeps successful overview sections visible when availability fails', async () => {
    api.availability.mockRejectedValue(new Error('统计服务暂不可用'))
    const root = mount(OverviewView)
    await flush()
    expect(text(root)).toContain('1,234.5')
    expect(text(root)).toContain('独立加载场站')
    expect(text(root)).toContain('数据质量未取到：统计服务暂不可用')
    expect(text(root)).not.toContain('npm run mock')
  })

  it('keeps the previous overview visible during refresh and after a refresh failure', async () => {
    const root = mount(OverviewView)
    await flush()
    const slow = deferred()
    api.summary.mockReturnValueOnce(slow.promise)
    await click(root, '刷新')
    expect(text(root)).toContain('1,234.5')
    slow.reject(new Error('刷新失败'))
    await flush()
    expect(text(root)).toContain('1,234.5')
    expect(text(root)).toContain('总览指标未取到：刷新失败')
  })

  it('queries only the initial report tab and renders it despite station lookup failure', async () => {
    route.query = { tab: 'timeliness' }
    api.stations.mockRejectedValue(new Error('台账暂不可用'))
    const root = mount(ReportsView)
    await flush()
    expect(api.availability).not.toHaveBeenCalled()
    expect(api.timeliness).toHaveBeenCalledTimes(1)
    expect(text(root)).toContain('时效分布')
    expect(text(root)).toContain('事件 9 条')
    expect(text(root)).toContain('场站列表未取到')
  })

  it('switches away from a slow report without waiting, aborts it, and ignores its late response', async () => {
    const slow = deferred()
    api.availability.mockReturnValue(slow.promise)
    const root = mount(ReportsView)
    await flush()
    expect(text(root)).toContain('正在统计可用率')
    const signal = api.availability.mock.calls[0][1] as AbortSignal
    await click(root, '告警及时性')
    expect(signal.aborted).toBe(true)
    expect(text(root)).toContain('时效分布')
    slow.resolve({ ...available, rule_version: 'obsolete-report' })
    await flush()
    expect(text(root)).not.toContain('obsolete-report')
    expect(text(root)).toContain('timeliness-v1')
  })

  it('can replace a slow query and prevents the older result from overwriting it', async () => {
    const slow = deferred()
    api.availability.mockReturnValueOnce(slow.promise).mockResolvedValueOnce({ ...available, rule_version: 'new-window' })
    const root = mount(ReportsView)
    await flush()
    await click(root, '查询')
    expect(api.availability.mock.calls[0][1].aborted).toBe(true)
    slow.resolve({ ...available, rule_version: 'old-window' })
    await flush()
    expect(text(root)).toContain('new-window')
    expect(text(root)).not.toContain('old-window')
  })

  it('shows a failed report as an error instead of empty or zero statistics', async () => {
    api.availability.mockRejectedValue(new Error('telemetry unavailable'))
    const root = mount(ReportsView)
    await flush()
    expect(text(root)).toContain('可用率报表未取到')
    expect(text(root)).not.toContain('整体可用率')
    expect(text(root)).not.toContain('暂无可用率数据')
    await click(root, '告警及时性')
    expect(text(root)).toContain('时效分布')
    expect(text(root)).not.toContain('可用率报表未取到')
  })
})

it('ignores both success and failure after leaving a request scope', async () => {
  const scope = effectScope()
  const request = scope.run(() => useLatestRequest<number>())!
  const slow = deferred<number>()
  let signal!: AbortSignal
  const run = request.run(value => { signal = value; return slow.promise })
  scope.stop()
  expect(signal.aborted).toBe(true)
  slow.reject(new Error('late error'))
  await run
  expect(request.data.value).toBeNull()
  expect(request.error.value).toBe('')
  expect(request.pending.value).toBe(false)
})
