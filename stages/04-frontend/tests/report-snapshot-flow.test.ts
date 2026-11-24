// @vitest-environment ./tests/vue-environment.ts
/**
 * 首页 → 统计报表的快照交接，以及「改条件后两个页签不串扰」。
 *
 * 这两件事都只能靠组件级测试证明：单测快照模块证明的是「谁能读」，
 * 而这里要证明的是「报表页真的没有多发一次请求」和「另一页签的旧结果真的被作废」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick } from 'vue'

const { api, route } = vi.hoisted(() => ({
  api: Object.fromEntries(
    ['summary', 'overview', 'alarms', 'availability', 'timeliness', 'stations', 'devices', 'history', 'exportReport'].map(
      name => [name, vi.fn()],
    ),
  ),
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
vi.mock('../src/components/CardMenu.vue', () => ({ default: { render: () => null } }))

import OverviewView from '../src/views/OverviewView.vue'
import ReportsView from '../src/views/ReportsView.vue'
import { authToken, currentUser, sessionFingerprint } from '../src/lib/auth'
import { clearReportState } from '../src/lib/report-state'
import { putSnapshot, takeSnapshot } from '../src/lib/report-snapshot'

type Node = { text: string; type: string; children: Node[]; parent: Node | null; props: Record<string, any> }
const node = (type: string, text = ''): Node =>
  Object.assign(
    { type, text, children: [], parent: null, props: {} },
    { addEventListener() {}, getRootNode: () => ({ activeElement: null }), options: [], tagName: type.toUpperCase() },
  )
const renderer = createRenderer<Node, Node>({
  createElement: type => node(type),
  createText: text => node('text', text),
  createComment: text => node('comment', text),
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
const find = (el: Node, predicate: (el: Node) => boolean): Node | undefined =>
  predicate(el) ? el : el.children.map(child => find(child, predicate)).find(Boolean)
const flush = async () => { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
const cleanups: (() => void)[] = []
/** 拿到 app 句柄，用来在用例里模拟「离开页面再回来」 */
function mountTracked(component: any) {
  const root = node('root')
  const app = renderer.createApp(component)
  app.mount(root)
  let live = true
  const unmount = () => {
    if (!live) return
    live = false
    app.unmount()
  }
  cleanups.push(unmount)
  return { root, unmount }
}
function mount(component: any) {
  return mountTracked(component).root
}
async function click(root: Node, label: string) {
  const button = find(root, el => el.type === 'button' && text(el).trim() === label)
  expect(button, `button: ${label}`).toBeDefined()
  button!.props.onClick()
  await flush()
}

const stat = { count: 0, mean_ms: null, median_ms: null, p90_ms: null }
const timely = {
  rule_version: 'timeliness-v1',
  computed_at: '2026-11-24T06:36:18Z',
  start: '2026-11-17T00:00:00Z',
  end: '2026-11-24T00:00:00Z',
  scope: 'all',
  result: {
    ack_timeliness: stat,
    recover_timeliness: stat,
    telemetry_recover_timeliness: stat,
    prediction_recover_timeliness: stat,
    pending_ack: 7,
    pending_recover: 2,
    managed_recover: 0,
    unknown_recover: 0,
    event_count: 9,
    invalid_lifecycle: 0,
    recovery_reasons: {},
  },
}
const available = {
  rule_version: 'availability-v1',
  computed_at: '2026-11-24T06:36:17Z',
  start: '2026-11-17T00:00:00Z',
  end: '2026-11-24T00:00:00Z',
  statistics_updated_at: '2026-11-24T06:36:00Z',
  aggregation_state: 'ready',
  devices: [],
  stations: [],
}

beforeEach(() => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.resetAllMocks()
  route.query = {}
  /* 快照与页面留存都是模块级单槽，用例之间要真清空 */
  takeSnapshot()
  clearReportState()
  authToken.value = ''
  currentUser.value = null
  api.summary.mockResolvedValue({ device_total: 143, online: 140, active_alarms: 3, current_power_kw: 1234.5, today_energy_kwh: 100, health_score_percent: 90 })
  api.overview.mockResolvedValue({ stations: [] })
  api.alarms.mockResolvedValue({ list: [] })
  api.devices.mockResolvedValue({ list: [], total: 0 })
  api.stations.mockResolvedValue({ list: [] })
  api.timeliness.mockResolvedValue(timely)
  api.availability.mockResolvedValue(available)
})
afterEach(() => cleanups.splice(0).forEach(cleanup => cleanup()))

describe('首页 → 报表的快照交接', () => {
  it('首页算完报表后进报表页，不新增任何报表请求', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }

    mount(OverviewView)
    await flush()
    expect(api.availability).toHaveBeenCalledTimes(1)

    const root = mount(ReportsView)
    await flush()

    /* 关键断言：报表页一次都没有再请求 */
    expect(api.availability).toHaveBeenCalledTimes(1)
    /* 而且真的用上了那份数据，不是显示空表 */
    expect(text(root)).toContain('availability-v1')
    expect(text(root)).toContain('生成于')
  })

  it('快照的时间区间原样回填，不会标成「此刻往前 7 天」', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }

    const old = { ...available, start: '2026-11-01T00:00:00Z', end: '2026-11-08T00:00:00Z' }
    putSnapshot(old, sessionFingerprint())

    const root = mount(ReportsView)
    await flush()
    expect(api.availability).not.toHaveBeenCalled()

    /* 点查询：提交的参数必须是快照自己的区间，而不是「此刻往前 7 天」。
       比较时间值而不是字符串：datetime-local 只到分钟，重排版会把 Z 后缀写成 .000Z。 */
    await click(root, '查询')
    const params = api.availability.mock.calls[0][0]
    expect(Date.parse(params.start)).toBe(Date.parse('2026-11-01T00:00:00Z'))
    expect(Date.parse(params.end)).toBe(Date.parse('2026-11-08T00:00:00Z'))
  })

  it('带秒的快照区间回填会截到分钟（datetime-local 的精度上限，已知边界)', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }

    putSnapshot(
      { ...available, start: '2026-11-01T00:13:37Z', end: '2026-11-08T00:13:37Z' },
      sessionFingerprint(),
    )
    const root = mount(ReportsView)
    await flush()
    await click(root, '查询')

    const params = api.availability.mock.calls[0][0]
    /* 秒被输入框吃掉：窗口起点从 :37 变成 :00，跨度不变。报表按小时汇总，秒级边界不影响口径 */
    expect(Date.parse(params.start)).toBe(Date.parse('2026-11-01T00:13:00Z'))
    expect(Date.parse(params.end)).toBe(Date.parse('2026-11-08T00:13:00Z'))
    expect(Date.parse(params.end) - Date.parse(params.start)).toBe(7 * 86_400_000)
  })

  it('没有快照时照常查询，不依赖首页', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }

    mount(ReportsView)
    await flush()

    expect(api.availability).toHaveBeenCalledTimes(1)
  })

  it('未登录不写快照：首页那份结果不会被下一个会话读到', async () => {
    /* 没有 authToken，首页即使挂了也不该留下东西 */
    mount(OverviewView)
    await flush()

    authToken.value = 'token-next'
    currentUser.value = { id: 2, username: 'u2', real_name: '', role: 'operator' }
    mount(ReportsView)
    await flush()

    expect(api.availability).toHaveBeenCalledTimes(2)
  })

  it('URL 直接带 tab=timeliness 且命中快照时，当前页签照样要查', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }
    putSnapshot(available, sessionFingerprint())
    route.query = { tab: 'timeliness' }

    const root = mount(ReportsView)
    await flush()

    /* 快照只顶了可用率那一半，及时性必须自己发一次 */
    expect(api.timeliness).toHaveBeenCalledTimes(1)
    expect(api.availability).not.toHaveBeenCalled()
    expect(text(root)).toContain('时效分布')
  })
})

describe('报表页文案', () => {
  it('表格底部不再放口径小字，生成时间独立成块', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }

    const root = mount(ReportsView)
    await flush()

    expect(text(root)).toContain('生成于')
    /* 解释性小字按所有者要求从表格底部删掉；口径说明留在页尾可展开的「口径说明」里 */
    expect(text(root)).not.toContain('台·时 = 设备数 × 小时')
    /* 「如需最新请刷新」这类提示也一并去掉：要不要新数据只看用户按不按刷新 */
    expect(text(root)).not.toContain('快照数据')
  })
})

describe('切出去再回来不重算', () => {
  it('离开页面再回来沿用上次的结果：一个报表请求都不发，窗口不被推到此刻', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }

    const first = mountTracked(ReportsView)
    await flush()
    expect(api.availability).toHaveBeenCalledTimes(1)
    const windowBefore = (text(first.root).match(/统计窗口[^·]+/)?.[0] ?? '').trim()
    expect(windowBefore).not.toBe('')
    first.unmount()

    const second = mountTracked(ReportsView)
    await flush()

    /* 关键断言：回来不发请求，窗口与生成时间都还是离开时那一份 */
    expect(api.availability).toHaveBeenCalledTimes(1)
    expect(text(second.root)).toContain(windowBefore)
    expect(text(second.root)).toContain('生成于')
  })

  it('在两个页签之间来回切，不重发任何请求', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }

    const root = mount(ReportsView)
    await flush()
    expect(api.availability).toHaveBeenCalledTimes(1)

    await click(root, '告警及时性')
    expect(api.timeliness).toHaveBeenCalledTimes(1)

    /* 来回切三轮：两边都有结果，谁都不该再问后端 */
    await click(root, '可用率')
    await click(root, '告警及时性')
    await click(root, '可用率')
    expect(api.availability).toHaveBeenCalledTimes(1)
    expect(api.timeliness).toHaveBeenCalledTimes(1)
  })

  it('页签还在加载时切走，请求继续跑完；切回来直接显示，不重发', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }

    let resolveTimeliness: (value: unknown) => void = () => {}
    api.timeliness.mockReturnValue(new Promise((resolve) => { resolveTimeliness = resolve }))

    const root = mount(ReportsView)
    await flush()
    await click(root, '告警及时性')
    expect(api.timeliness).toHaveBeenCalledTimes(1)

    /* 还没回来就切回可用率，再切过去：不许重发第二次 */
    await click(root, '可用率')
    await click(root, '告警及时性')
    expect(api.timeliness).toHaveBeenCalledTimes(1)
    expect(text(root)).toContain('正在加载告警及时性')

    /* 那次请求自己跑完，结果照样填进来 */
    resolveTimeliness(timely)
    await flush()
    expect(text(root)).toContain('时效分布')
  })

  it('换人 / 登出后留存失效：上个会话的报表不会给下一个人看', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }
    const first = mountTracked(ReportsView)
    await flush()
    expect(api.availability).toHaveBeenCalledTimes(1)
    first.unmount()

    authToken.value = 'token-b'
    currentUser.value = { id: 2, username: 'u2', real_name: '', role: 'operator' }
    mount(ReportsView)
    await flush()

    /* 归属不符，留存按「没有」处理，必须重新查 */
    expect(api.availability).toHaveBeenCalledTimes(2)
  })
})

describe('改条件后两个页签不串扰', () => {
  it('提交新查询会作废另一页签的旧结果，切回去必须重新取', async () => {
    authToken.value = 'token-a'
    currentUser.value = { id: 1, username: 'u1', real_name: '', role: 'operator' }

    const root = mount(ReportsView)
    await flush()
    expect(api.availability).toHaveBeenCalledTimes(1)

    /* 加载第二个页签 */
    await click(root, '告警及时性')
    expect(api.timeliness).toHaveBeenCalledTimes(1)

    /* 切回来：已有结果，不该再请求 */
    await click(root, '可用率')
    expect(api.availability).toHaveBeenCalledTimes(1)

    /* 提交一次新查询：两个页签的结果一起作废 */
    await click(root, '查询')
    expect(api.availability).toHaveBeenCalledTimes(2)

    /* 再切到及时性：旧结果已被清掉，必须重取，不能拿上一组条件的 */
    await click(root, '告警及时性')
    expect(api.timeliness).toHaveBeenCalledTimes(2)
  })
})
