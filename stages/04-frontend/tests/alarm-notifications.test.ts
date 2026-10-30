// @vitest-environment ./tests/vue-environment.ts
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, nextTick } from 'vue'

const { api } = vi.hoisted(() => ({ api: { ticket: vi.fn(), alarms: vi.fn() } }))
vi.mock('../src/lib/api', () => ({ api }))
vi.mock('vue-router', async () => {
  const { h } = await import('vue')
  return { RouterLink: { props: ['to'], setup: (props: any, { slots }: any) => () => h('a', { href: props.to.path + '?id=' + props.to.query.id }, slots.default?.()) } }
})
import { alarmNotices, dismissAlarmNotice } from '../src/lib/alarm-notices'
import { connectRealtime, closeRealtime, shell } from '../src/lib/store'
import AlarmNoticeHost from '../src/components/AlarmNoticeHost.vue'

class Socket {
  static instances: Socket[] = []
  onopen?: () => void
  onclose?: () => void
  onmessage?: (event: { data: string }) => void
  onerror?: () => void
  constructor(readonly url: string) { Socket.instances.push(this) }
  close() { this.onclose?.() }
  send(type: string, data: unknown) { this.onmessage?.({ data: JSON.stringify({ type, data }) }) }
}
const flush = async () => { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
const alarm = (id = 71, extra = {}) => ({ id, device_id: 42, level: 'urgent', metric: 'temperature', value: 82, threshold: 80, status: 'unhandled', triggered_at: new Date().toISOString(), ...extra })
let notify: ReturnType<typeof vi.fn>
let reset: ReturnType<typeof vi.fn>
async function connect() {
  connectRealtime(); await flush()
  const ws = Socket.instances.at(-1)!
  ws.onopen?.(); await flush(); return ws
}
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-30T08:00:00Z'))
  notify = vi.fn().mockResolvedValue({ submitted: true }); reset = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('window', { setTimeout, clearTimeout, powerSystemDesktop: { getCapabilities: vi.fn().mockResolvedValue({ protocol: 'powersystem-desktop/v1', features: { notifications: true } }), notifyAlarm: notify, resetNotificationSession: reset } })
  vi.stubGlobal('location', { protocol: 'https:', host: 'fixture.test' }); vi.stubGlobal('WebSocket', Socket)
  closeRealtime(); Socket.instances = []
  api.ticket.mockReset().mockResolvedValue({ ticket: 'fixture-ticket' })
  api.alarms.mockReset().mockResolvedValue({ list: [] })
})
afterEach(() => { closeRealtime(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('live alarm prompts', () => {
  it('routes a real WS frame into the visible popup and the existing desktop bridge exactly once', async () => {
    const ws = await connect()
    ws.send('alarm_created', alarm()); ws.send('alarm_triggered', alarm()); await flush()
    expect(ws.url).toContain('wss://fixture.test/ws/realtime?ticket=')
    expect(alarmNotices.map(x => x.id)).toEqual([71])
    expect(notify).toHaveBeenCalledTimes(1)
    expect(notify.mock.calls[0][0]).toMatchObject({ alarmId: 71, level: 'urgent' })
    type Node = { type: string; text: string; children: Node[]; parent: Node | null; props: Record<string, any> }
    const node = (type: string, text = ''): Node => ({ type, text, children: [], parent: null, props: {} })
    const renderer = createRenderer<Node, Node>({
      createElement: type => node(type), createText: value => node('text', value), createComment: value => node('comment', value),
      setText: (el, value) => { el.text = value }, setElementText: (el, value) => { el.text = value; el.children = [] },
      parentNode: el => el.parent, nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
      insert: (el, parent, anchor) => { const i = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(i < 0 ? parent.children.length : i, 0, el); el.parent = parent },
      remove: el => { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1) }, patchProp: (el, key, _old, value) => { el.props[key] = value },
    })
    const root = node('root'), app = renderer.createApp(AlarmNoticeHost)
    app.mount(root); await flush()
    const text = (el: Node): string => el.text + el.children.map(text).join('')
    const links = (el: Node): Node[] => (el.type === 'a' ? [el] : []).concat(el.children.flatMap(links))
    expect(text(root)).toContain('告警'); expect(text(root)).toContain('温度'); expect(text(root)).toContain('82')
    expect(links(root)[0]?.props.href).toBe('/alarms?id=71')
    dismissAlarmNotice(71); await flush(); expect(text(root)).not.toContain('查看并处理告警')
    ws.send('alarm_created', alarm()); await flush(); expect(alarmNotices).toHaveLength(0)
    app.unmount()
  })

  it('catches up only fresh unhandled alarms and deduplicates reconnects', async () => {
    api.alarms.mockResolvedValue({ list: [alarm(72), alarm(70, { triggered_at: '2026-10-29T00:00:00Z' }), alarm(69, { status: 'recovered' })] })
    const ws = await connect(); ws.send('alarm_created', alarm(72)); await flush()
    expect(alarmNotices.map(x => x.id)).toEqual([72])
    ws.close(); await vi.advanceTimersByTimeAsync(5000); await flush()
    Socket.instances.at(-1)!.onopen?.(); await flush()
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('refreshes revisions without prompting for ack, recovery, archive, malformed or stale frames', async () => {
    const ws = await connect(), revision = shell.alarmRevision
    for (const type of ['alarm_acked', 'alarm_recovered', 'alarm_archived']) ws.send(type, alarm())
    ws.send('alarm_created', alarm(1, { level: 'unknown' }))
    ws.send('alarm_created', alarm(2, { triggered_at: '2000-01-01T00:00:00Z' }))
    ws.send('alarm_created', { id: 3 }); await flush()
    expect(shell.alarmRevision).toBe(revision + 6)
    expect(alarmNotices).toHaveLength(0); expect(notify).not.toHaveBeenCalled()
  })

  it('keeps a bounded visible stack while retaining dedup for dismissed notices', async () => {
    const ws = await connect()
    for (let id = 1; id <= 8; id++) ws.send('alarm_created', alarm(id))
    await flush(); expect(alarmNotices.map(x => x.id)).toEqual([8, 7, 6, 5, 4])
    ws.send('alarm_created', alarm(1)); await flush(); expect(notify).toHaveBeenCalledTimes(8)
  })

  it('clears prompts on logout and ignores old sockets without reconnecting', async () => {
    const ws = await connect(); ws.send('alarm_created', alarm()); await flush()
    closeRealtime(); ws.send('alarm_created', alarm(73)); await vi.advanceTimersByTimeAsync(10000)
    expect(alarmNotices).toHaveLength(0); expect(shell.live).toBe(false); expect(Socket.instances).toHaveLength(1)
    await connect(); Socket.instances.at(-1)!.send('alarm_created', alarm()); await flush()
    expect(notify).toHaveBeenCalledTimes(2)
  })

  it('does not create a socket when its ticket arrives after logout', async () => {
    let done!: (value: { ticket: string }) => void
    api.ticket.mockReturnValue(new Promise(resolve => { done = resolve }))
    connectRealtime(); closeRealtime(); done({ ticket: 'obsolete' }); await flush()
    expect(Socket.instances).toHaveLength(0)
  })

  it('ignores an old catch-up response after logout', async () => {
    let done!: (value: { list: unknown[] }) => void
    api.alarms.mockReturnValue(new Promise(resolve => { done = resolve }))
    await connect(); closeRealtime(); done({ list: [alarm()] }); await flush()
    expect(alarmNotices).toHaveLength(0); expect(notify).not.toHaveBeenCalled()
  })

  it('keeps the page popup when desktop notification delivery fails', async () => {
    const ws = await connect(); notify.mockRejectedValueOnce(new Error('fixture unavailable'))
    ws.send('alarm_created', alarm()); await flush()
    expect(alarmNotices).toHaveLength(1)
  })
})
