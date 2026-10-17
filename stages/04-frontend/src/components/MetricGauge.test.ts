import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, ref } from 'vue'
import * as Vue from 'vue'
import type { Component } from 'vue'
import * as echarts from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { GaugeChart } from 'echarts/charts'
import { CanvasRenderer, SVGRenderer } from 'echarts/renderers'
import { compileScript, parse } from 'vue/compiler-sfc'
import ts from 'typescript'
import componentSource from './MetricGauge.vue?raw'

const charts: EChartsType[] = []
const observers: { observe: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn>; notify: () => void }[] = []

// Exercise the actual component and ECharts gauge updates without a browser or
// a canvas dependency. Only the rendering surface is replaced by SVG SSR.
echarts.use([SVGRenderer])
const modules: Record<string, unknown> = {
  vue: Vue,
  'echarts/charts': { GaugeChart },
  'echarts/renderers': { CanvasRenderer },
  'echarts/core': { ...echarts, init: () => {
    const chart = echarts.init(null, undefined, { renderer: 'svg', ssr: true, width: 400, height: 168 })
    vi.spyOn(chart, 'resize')
    vi.spyOn(chart, 'dispose')
    charts.push(chart)
    return chart
  } },
}
// Compile the client render function as well as setup: Vitest's default node
// transform otherwise supplies only the component's server render function.
const descriptor = parse(componentSource).descriptor
const script = compileScript(descriptor, { id: 'metric-gauge-regression', inlineTemplate: true })
const compiled = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const componentModule = { exports: {} as { default: Component } }
new Function('require', 'module', 'exports', compiled)((id: string) => {
  if (!(id in modules)) throw new Error(`Unexpected component import: ${id}`)
  return modules[id]
}, componentModule, componentModule.exports)
const MetricGauge = componentModule.exports.default

interface HostNode {
  children: HostNode[]
  parent: HostNode | null
  props: Record<string, unknown>
  text: string
}
const node = (text = ''): HostNode => ({ children: [], parent: null, props: {}, text })
const renderer = createRenderer<HostNode, HostNode>({
  createElement: () => node(), createText: node, createComment: node,
  setText: (el, text) => { el.text = text },
  setElementText: (el, text) => { el.text = text },
  parentNode: el => el.parent, nextSibling: () => null,
  patchProp: (el, key, _previous, value) => { el.props[key] = value },
  insert: (el, parent) => { el.parent = parent; parent.children.push(el) },
  remove: el => { if (el.parent) el.parent.children = el.parent.children.filter(item => item !== el) },
})

const unmounts: (() => void)[] = []
function mountGauge(initial: number | null) {
  vi.stubGlobal('ResizeObserver', class {
    constructor(notify: () => void) {
      const observer = { observe: vi.fn(), disconnect: vi.fn(), notify }
      observers.push(observer)
      return observer
    }
  })
  const value = ref(initial)
  const root = node()
  const app = renderer.createApp({ render: () => h(MetricGauge, { value: value.value, name: '当前功率', unit: 'kW' }) })
  app.mount(root)
  const unmount = () => app.unmount()
  unmounts.push(unmount)
  return { value, root, chart: charts[charts.length - 1], unmount }
}

afterEach(() => {
  unmounts.splice(0).forEach(unmount => unmount())
  charts.splice(0).forEach(chart => { if (!chart.isDisposed()) chart.dispose() })
  observers.splice(0)
  vi.unstubAllGlobals()
})

describe('MetricGauge lifecycle', () => {
  it('recovers from an initial empty value and keeps zero distinct from missing data', async () => {
    const { value, root, chart } = mountGauge(null)
    expect(root.children[0].props['aria-label']).toContain('暂无数据')
    for (const reading of [235.6, null, 0, 240, Number.NaN, 125]) {
      value.value = reading
      await nextTick()
      const missing = reading === null || !Number.isFinite(reading)
      expect(root.children[0].props['aria-label']).toBe(missing ? '当前功率，当前 暂无数据' : `当前功率，当前 ${reading!.toFixed(1)}kW`)
      const option = chart.getOption() as { series: { data: { value: number }[]; detail: { formatter: (value: number) => string } }[] }
      expect(option.series[0].data[0].value).toBe(missing ? 0 : reading)
      expect(option.series[0].detail.formatter(option.series[0].data[0].value)).toBe(missing ? '—' : reading!.toFixed(1))
      expect(() => chart.renderToSVGString()).not.toThrow()
    }
  })

  it('resizes and releases the observer and chart on unmount', () => {
    const { chart, unmount } = mountGauge(10)
    const observer = observers[0]
    expect(observer.observe).toHaveBeenCalledOnce()
    observer.notify()
    expect(chart.resize).toHaveBeenCalledOnce()
    unmount()
    unmounts.pop()
    expect(observer.disconnect).toHaveBeenCalledOnce()
    expect(chart.dispose).toHaveBeenCalledOnce()
    expect(chart.isDisposed()).toBe(true)
  })
})
