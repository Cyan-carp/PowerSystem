<script setup lang="ts">
/**
 * 登录页背景：黄昏逆光下的输电走廊。
 *
 * 关键在于"背景比前景亮"：
 *   · 天空是从深蓝到灰蓝的渐层，地平线附近最亮；
 *   · 城市是地平线上一条几乎纯黑的锯齿剪影；
 *   · 铁塔是**透空**的——只有构件（塔腿、横材、交叉斜材、横担）是黑的，
 *     天空从格子里漏出来，所以它不是一块黑楔子，也不是蓝色线稿；
 *   · 导线同样是深色剪影，一道冷白光段沿着中间那根从左边塔头爬到右边塔头。
 *
 * 机位在地面附近：塔顶压在画面上沿、塔身一直落到画框下方，只看得见塔头与
 * 一段塔身——这是"站在塔下抬头看"的视角。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const n1 = (value: number): string => value.toFixed(1)

/* ---------------------------------------------------------------- 视口尺寸 */

const host = ref<HTMLDivElement | null>(null)
const size = ref({ w: 1440, h: 900 })
let observer: ResizeObserver | null = null

onMounted(() => {
  const box = host.value?.getBoundingClientRect()
  if (box && box.width > 0 && box.height > 0) {
    size.value = { w: Math.round(box.width), h: Math.round(box.height) }
  }
  observer = new ResizeObserver((entries) => {
    const rect = entries[0]?.contentRect
    if (rect && rect.width > 0 && rect.height > 0) {
      size.value = { w: Math.round(rect.width), h: Math.round(rect.height) }
    }
  })
  if (host.value) observer.observe(host.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

/* ---------------------------------------------------------------- 工具 */

function makeRandom(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let t = Math.imul(state ^ (state >>> 15), 1 | state)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

type Pt = [number, number]

function bezierLength(p0: Pt, control: Pt, p1: Pt): number {
  let length = 0
  let prev = p0
  for (let step = 1; step <= 40; step++) {
    const t = step / 40
    const m = 1 - t
    const x = m * m * p0[0] + 2 * m * t * control[0] + t * t * p1[0]
    const y = m * m * p0[1] + 2 * m * t * control[1] + t * t * p1[1]
    length += Math.hypot(x - prev[0], y - prev[1])
    prev = [x, y]
  }
  return length
}

/* ---------------------------------------------------------------- 铁塔 */

interface Pylon {
  /** 塔腿（最粗） */
  legs: string
  /** 横材与交叉斜材 */
  lattice: string
  /** 横担桁架 */
  arms: string
  /** 挂点：每一层左右各一个 */
  attach: Array<[Pt, Pt]>
}

/**
 * 透空铁塔：塔身由两条斜腿 + 每档横材 + X 形交叉斜材组成，
 * 三层横担各做成一片小桁架（上下弦 + 竖杆 + 斜杆），端部挂绝缘子串。
 * 构件粗细按塔高取，塔越高越粗，透视才成立。
 */
function buildPylon(
  cx: number,
  baseY: number,
  topY: number,
  armRatios: number[],
  armHalf: number,
): Pylon {
  const H = baseY - topY
  const halfBase = H * 0.076
  const halfTop = H * 0.017
  const halfAt = (y: number): number => {
    const t = Math.max(0, Math.min(1, (baseY - y) / H))
    return halfBase + (halfTop - halfBase) * Math.pow(t, 0.85)
  }

  /** 每档高度：越靠塔顶越密，跟真塔一样 */
  const belts: number[] = []
  let y = topY
  let step = H * 0.042
  while (y < baseY) {
    belts.push(y)
    y += step
    step *= 1.14
  }
  belts.push(baseY)

  const legs = `M${n1(cx - halfAt(baseY))} ${n1(baseY)}L${n1(cx - halfTop)} ${n1(topY)}` +
    `M${n1(cx + halfAt(baseY))} ${n1(baseY)}L${n1(cx + halfTop)} ${n1(topY)}`

  let lattice = ''
  for (let i = 0; i < belts.length - 1; i++) {
    const y1 = belts[i]
    const y2 = belts[i + 1]
    const w1 = halfAt(y1)
    const w2 = halfAt(y2)
    lattice += `M${n1(cx - w2)} ${n1(y2)}H${n1(cx + w2)}`
    lattice += `M${n1(cx - w1)} ${n1(y1)}L${n1(cx + w2)} ${n1(y2)}`
    lattice += `M${n1(cx + w1)} ${n1(y1)}L${n1(cx - w2)} ${n1(y2)}`
  }

  let arms = ''
  const attach: Array<[Pt, Pt]> = []
  const armDepth = Math.max(6, H * 0.014)
  for (const ratio of armRatios) {
    const y0 = topY + H * ratio
    const half = H * armHalf
    for (const sign of [-1, 1]) {
      const root = cx + sign * halfAt(y0) * 0.92
      const tip = cx + sign * half
      const x0 = Math.min(root, tip)
      const x1 = Math.max(root, tip)
      arms += `M${n1(x0)} ${n1(y0)}H${n1(x1)}`
      arms += `M${n1(x0)} ${n1(y0 + armDepth)}H${n1(x1)}`
      for (const t of [0.3, 0.58, 0.84]) {
        const px = x0 + (x1 - x0) * t
        arms += `M${n1(px)} ${n1(y0)}V${n1(y0 + armDepth)}`
      }
      for (const t of [0, 0.3, 0.58, 0.84]) {
        const ax = x0 + (x1 - x0) * t
        const bx = Math.min(x0 + (x1 - x0) * (t + 0.28), x1)
        arms += `M${n1(ax)} ${n1(y0 + armDepth)}L${n1(bx)} ${n1(y0)}`
      }
      arms += `M${n1(tip)} ${n1(y0 + armDepth)}v${n1(H * 0.016)}`
    }
    attach.push([[cx - half, y0 + armDepth], [cx + half, y0 + armDepth]])
  }

  arms += `M${n1(cx - halfTop)} ${n1(topY)}L${n1(cx)} ${n1(topY - H * 0.032)}L${n1(cx + halfTop)} ${n1(topY)}`

  return { legs, lattice, arms, attach }
}

/* ---------------------------------------------------------------- 场景 */

/** 一轮光流：CYCLE 秒一个循环，行进占 TRAVEL，其余时间留白。
 *  改这两个值时必须同步 <style> 里 `pg-travel` / `pg-flash` 关键帧的百分比。 */
const CYCLE = 6.4
const TRAVEL = 0.55

const scene = computed(() => {
  const w = size.value.w
  const h = size.value.h
  const horizon = Math.round(h * 0.60)
  const compact = w < 900

  /* 城市：地平线上一条连续的街区剪影。高度用两条正弦叠加，避免出现一排锯齿，
     再单独补几座高一点的楼——天际线才有起伏而不是锯齿。 */
  const rand = makeRandom(20261024)
  const city: string[] = [`M-4 ${horizon}V${horizon - 6}H${w + 4}V${horizon}`]
  let x = -10
  while (x < w + 10) {
    const step = 12 + rand() * 22
    const centre = Math.abs(x - w / 2) / (w / 2)
    const wave = Math.sin(x / 96) * 7 + Math.sin(x / 41 + 1.7) * 5
    const hgt = Math.max(3, 9 + centre * 15 + wave + (rand() - 0.5) * 4)
    city.push(`M${n1(x)} ${horizon}V${n1(horizon - hgt)}H${n1(x + step)}V${horizon}`)
    x += step
  }
  for (let i = 0; i < 7; i++) {
    const bx = 40 + rand() * (w - 100)
    const bw = 15 + rand() * 24
    const bh = 24 + rand() * 34
    city.push(`M${n1(bx)} ${horizon}V${n1(horizon - bh)}H${n1(bx + bw)}V${horizon}`)
  }

  const lights: Array<{ x: number; y: number; o: number }> = []
  for (let i = 0; i < 44; i++) {
    lights.push({ x: rand() * w, y: horizon - rand() * 22, o: 0.25 + rand() * 0.45 })
  }

  /* 两座塔：宽屏时塔顶压在画面上沿、塔身落到画框外（站在塔下抬头看）；
     窄屏放不下这么大的塔，改成两座站在地平线上的中景塔。 */
  const left = compact
    ? buildPylon(w * 0.03, horizon, horizon - h * 0.5, [0.07, 0.13, 0.19], 0.17)
    : buildPylon(w * 0.075, h * 1.18, h * 0.06, [0.045, 0.08, 0.115], 0.19)
  const right = compact
    ? buildPylon(w * 0.97, horizon, horizon - h * 0.53, [0.07, 0.13, 0.19], 0.17)
    : buildPylon(w * 0.93, h * 1.26, h * 0.015, [0.04, 0.075, 0.11], 0.165)

  /* 远景小塔：站在地平线上，只露塔头 */
  const far = [
    buildPylon(w * 0.29, horizon + 4, horizon - h * 0.05, [0.02, 0.05], 0.34),
    buildPylon(w * 0.695, horizon + 4, horizon - h * 0.042, [0.02, 0.05], 0.34),
  ]

  /* 导线：左塔右臂 → 右塔左臂 */
  const wires = left.attach.map((pair, level) => {
    const p0 = pair[1]
    const p1 = right.attach[level][0]
    const sag = Math.abs(p1[0] - p0[0]) * 0.035
    const control: Pt = [(p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2 + sag * 2]
    return {
      d: `M${n1(p0[0])} ${n1(p0[1])}Q${n1(control[0])} ${n1(control[1])} ${n1(p1[0])} ${n1(p1[1])}`,
      length: bezierLength(p0, control, p1),
      tip: p1,
    }
  })

  const live = wires[1]
  // 光段长度按档距取 18%，短档里也不能超过三分之一，否则整根线都白了
  const dash = Math.min(live.length * 0.34, Math.max(80, live.length * 0.18))
  const flow = {
    d: live.d,
    style: {
      strokeDasharray: `${dash} ${live.length}`,
      '--pg-from': `${dash + live.length}`,
      '--pg-to': `${dash * 2}`,
    },
  }

  return {
    horizon,
    city: city.join(''),
    lights,
    left,
    right,
    far,
    wires,
    flow,
    flash: { x: live.tip[0], y: live.tip[1], delay: TRAVEL * CYCLE * 0.93 },
  }
})
</script>

<template>
  <div ref="host" class="pg-scene" aria-hidden="true" :style="{ '--pg-cycle': `${CYCLE}s` }">
    <svg class="pg-canvas" :viewBox="`0 0 ${size.w} ${size.h}`" :width="size.w" :height="size.h">
      <defs>
        <linearGradient id="pg-sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#0d1a2b" />
          <stop offset="42%" stop-color="#152b45" />
          <stop offset="74%" stop-color="#20405f" />
          <stop offset="100%" stop-color="#2e5279" />
        </linearGradient>
        <linearGradient id="pg-ground" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#0d1d31" />
          <stop offset="10%" stop-color="#081120" />
          <stop offset="32%" stop-color="#05090f" />
          <stop offset="100%" stop-color="#04070c" />
        </linearGradient>
        <radialGradient id="pg-glow" cx="50%" cy="100%" r="66%">
          <stop offset="0%" stop-color="rgba(150, 190, 235, .34)" />
          <stop offset="52%" stop-color="rgba(90, 130, 180, .12)" />
          <stop offset="100%" stop-color="rgba(30, 50, 80, 0)" />
        </radialGradient>
        <radialGradient id="pg-flash" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="rgba(236, 246, 255, .95)" />
          <stop offset="100%" stop-color="rgba(150, 200, 255, 0)" />
        </radialGradient>
      </defs>

      <rect x="0" y="0" :width="size.w" :height="scene.horizon" fill="url(#pg-sky)" />
      <rect x="0" :y="scene.horizon - size.h * 0.30" :width="size.w" :height="size.h * 0.30" fill="url(#pg-glow)" />

      <path class="pg-city" :d="scene.city" />
      <circle
        v-for="(light, index) in scene.lights"
        :key="`light-${index}`"
        class="pg-light"
        :cx="n1(light.x)"
        :cy="n1(light.y)"
        r="1"
        :style="{ opacity: light.o }"
      />

      <rect x="0" :y="scene.horizon" :width="size.w" :height="size.h - scene.horizon" fill="url(#pg-ground)" />

      <g class="pg-far">
        <template v-for="(tower, index) in scene.far" :key="`far-${index}`">
          <path :d="tower.legs" />
          <path :d="tower.lattice" />
          <path :d="tower.arms" />
        </template>
      </g>

      <path v-for="(wire, index) in scene.wires" :key="`wire-${index}`" class="pg-wire" :d="wire.d" />

      <g class="pg-tower">
        <template v-for="(tower, index) in [scene.left, scene.right]" :key="`tower-${index}`">
          <path class="pg-legs" :d="tower.legs" />
          <path class="pg-lattice" :d="tower.lattice" />
          <path class="pg-arms" :d="tower.arms" />
        </template>
      </g>

      <path class="pg-flow" :d="scene.flow.d" :style="scene.flow.style" />
      <circle
        class="pg-flash"
        :cx="n1(scene.flash.x)"
        :cy="n1(scene.flash.y)"
        r="12"
        :style="{ animationDelay: `${scene.flash.delay}s` }"
      />
    </svg>
  </div>
</template>

<style scoped>
.pg-scene {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
}
.pg-canvas { display: block; width: 100%; height: 100%; }

.pg-city { fill: #03060b; }
.pg-light { fill: #bcd6f2; }

/* 铁塔：构件是黑的，天空从格子里漏出来 */
.pg-tower path { fill: none; stroke: #03060b; stroke-linecap: round; stroke-linejoin: round; }
.pg-legs { stroke-width: 4.4; }
.pg-lattice { stroke-width: 2.6; }
.pg-arms { stroke-width: 3.2; }
.pg-far path { fill: none; stroke: rgba(4, 8, 14, .82); stroke-linecap: round; stroke-width: 1.4; }

/* 导线：同样是剪影，比塔细 */
.pg-wire { fill: none; stroke: rgba(4, 8, 14, .78); stroke-width: 1.7; }

/* 电流：一道冷白光段，走到右边塔头即熄 */
.pg-flow {
  fill: none;
  stroke: #f4faff;
  stroke-width: 3.2;
  stroke-linecap: round;
  stroke-dashoffset: var(--pg-to, 0);
  animation: pg-travel var(--pg-cycle, 6.4s) cubic-bezier(.44, .03, .56, .97) infinite;
}

.pg-flash {
  fill: url(#pg-flash);
  opacity: 0;
  transform-box: fill-box;
  transform-origin: center;
  animation: pg-flash var(--pg-cycle, 6.4s) ease-out infinite;
}

@keyframes pg-travel {
  0% { stroke-dashoffset: var(--pg-from, 0); opacity: 0; }
  5% { opacity: 1; }
  55% { stroke-dashoffset: var(--pg-to, 0); opacity: 1; }
  60% { stroke-dashoffset: var(--pg-to, 0); opacity: 0; }
  100% { stroke-dashoffset: var(--pg-to, 0); opacity: 0; }
}

@keyframes pg-flash {
  0%, 51% { opacity: 0; transform: scale(.4); }
  57% { opacity: .9; transform: scale(1.25); }
  70% { opacity: 0; transform: scale(2.2); }
  100% { opacity: 0; transform: scale(2.2); }
}

@media (prefers-reduced-motion: reduce) {
  .pg-flow { animation: none; opacity: .35; stroke-dashoffset: var(--pg-to, 0); }
  .pg-flash { animation: none; opacity: 0; }
}
</style>
