<script setup lang="ts">
/**
 * 登录页装饰：浅色能量走廊。
 *
 * 全部为线稿（白色低透明度）叠加在深蓝渐变面板上，只作背景氛围，
 * 不承载业务信息，也不拦截鼠标事件。纯 SVG，无第三方依赖。
 */
</script>

<template>
  <svg class="grid-scene" viewBox="0 0 760 900" preserveAspectRatio="xMidYMax slice" aria-hidden="true">
    <defs>
      <radialGradient id="glowA" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stop-color="#4d94ff" stop-opacity=".38" />
        <stop offset="100%" stop-color="#4d94ff" stop-opacity="0" />
      </radialGradient>
      <radialGradient id="glowB" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stop-color="#7fb8ff" stop-opacity=".22" />
        <stop offset="100%" stop-color="#7fb8ff" stop-opacity="0" />
      </radialGradient>
      <linearGradient id="wire" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color="#9ec8ff" stop-opacity=".05" />
        <stop offset="50%" stop-color="#cfe4ff" stop-opacity=".5" />
        <stop offset="100%" stop-color="#9ec8ff" stop-opacity=".05" />
      </linearGradient>

      <g id="pylon" fill="none" stroke="currentColor" stroke-linecap="round">
        <path d="M50 300 22 12M50 300 78 12" stroke-width="2.4" />
        <path d="M28 250H72M26 210H74M24 170H76M22 130H78M20 90H80M18 50H82" stroke-width="1.2" opacity=".8" />
        <path d="M28 250 76 210M28 210 74 170M26 170 78 130M24 130 80 90M22 90 82 50" stroke-width="1" opacity=".55" />
        <path d="M2 52H98M10 92H90M20 132H80" stroke-width="1.8" />
        <path d="M50 12 44 2M50 12 56 2" stroke-width="1.2" opacity=".7" />
      </g>
    </defs>

    <circle cx="150" cy="180" r="260" fill="url(#glowA)" />
    <circle cx="640" cy="640" r="300" fill="url(#glowB)" />

    <!-- 远景线路 -->
    <g stroke="rgba(158, 200, 255, .16)" stroke-width="1.1" fill="none">
      <path d="M-20 205C120 232 210 232 330 214" />
      <path d="M-20 224C120 251 210 251 330 233" />
      <path d="M330 214C470 232 610 232 780 208" />
      <path d="M330 233C470 251 610 251 780 227" />
    </g>

    <!-- 主塔与副塔 -->
    <g color="rgba(207, 228, 255, .42)">
      <use href="#pylon" transform="translate(262 300) scale(2.05)" />
      <use href="#pylon" transform="translate(566 402) scale(1.28)" opacity=".7" />
      <use href="#pylon" transform="translate(64 452) scale(.86)" opacity=".45" />
    </g>

    <!-- 近景导线：中间一条带冷光的主线 -->
    <g fill="none" stroke-linecap="round">
      <path d="M-40 452C110 486 250 486 364 458C470 434 600 456 780 424" stroke="rgba(158, 200, 255, .22)" stroke-width="1.6" />
      <path d="M-40 470C110 504 250 504 364 476C470 452 600 474 780 442" stroke="rgba(158, 200, 255, .18)" stroke-width="1.4" />
      <path d="M-40 461C110 495 250 495 364 467C470 443 600 465 780 433" stroke="url(#wire)" stroke-width="2.6" />
    </g>

    <!-- 数据节点 -->
    <g fill="#cfe4ff">
      <circle cx="364" cy="458" r="3.4" opacity=".85" />
      <circle cx="364" cy="458" r="9" opacity=".18" />
      <circle cx="112" cy="483" r="2.4" opacity=".55" />
      <circle cx="612" cy="452" r="2.4" opacity=".55" />
    </g>
  </svg>
</template>

<style scoped>
.grid-scene {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  color: rgba(207, 228, 255, .42);
  opacity: .9;
}
@media (max-width: 1024px) {
  .grid-scene { display: none; }
}
</style>
