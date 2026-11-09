<script setup lang="ts">
/**
 * 关于：平台是什么、数据从哪来、备案与版本标识。
 * 备案号仍来自服务端注入的那段 HTML（见 lib/site.ts），这里只负责展示。
 */
import { computed } from 'vue'
import Icon from '../components/Icon.vue'
import { buildTag, icpNotice } from '../lib/site'

const tag = buildTag()
const icp = icpNotice
const href = computed(() => icp.value?.href || 'https://beian.miit.gov.cn/')
</script>

<template>
  <div class="page-head">
    <h1>关于</h1>
  </div>

  <section class="card about-card">
    <div class="about-logo">
      <Icon name="home" :size="26" :stroke="2" />
    </div>
    <div class="about-title">
      <h2>源网智联</h2>
      <p>新能源设备智能运维平台 · 云边协同</p>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h3 class="card-title">这个平台做什么</h3></div>
    <div class="card-body about-body">
      <p>把场站遥测、设备台账、告警处置、故障预测与模型质量放在同一处：值班看总览与告警，工程师查设备与曲线，管理员盯模型与报表。</p>
      <p>同一套页面同时供电脑浏览器与手机使用：窄屏下左侧图标轨收成底部四格导航，其余入口进「更多」面板。</p>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h3 class="card-title">数据与边界</h3></div>
    <div class="card-body about-body">
      <ul>
        <li>全平台 143 台设备均为<strong>合成数据</strong>，故障由模拟器注入，不代表真实场站运行状况。</li>
        <li>预测与健康分只作趋势参考，不替代现场测量与专业判断。</li>
        <li>告警解读中模型不可用时会退回模板并明确标注来源，不伪造「AI 已分析」。</li>
      </ul>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h3 class="card-title">版本与备案</h3></div>
    <div class="card-body about-body">
      <dl class="about-kv">
        <dt>前端构建</dt>
        <dd class="num">{{ tag || '—' }}</dd>
        <dt>会话</dt>
        <dd>JWT · 8 小时有效，退出即清本地登录态</dd>
        <dt>备案</dt>
        <dd>
          <a v-if="icp" class="about-icp" :href="href" target="_blank" rel="noopener noreferrer" v-html="icp.html"></a>
          <span v-else class="muted">当前入口未登记备案信息</span>
        </dd>
      </dl>
      <p class="about-note">
        备案信息由域名入口在页面返回时注入，IP 直连入口不显示；如需核对，请以工业和信息化部备案系统为准。
      </p>
    </div>
  </section>
</template>

<style scoped>
.about-card {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px 20px;
}

.about-logo {
  width: 46px;
  height: 46px;
  flex: 0 0 46px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  background: var(--brand-soft);
  color: var(--brand);
}

.about-title h2 {
  font-size: 16px;
  font-weight: 650;
  letter-spacing: -0.01em;
  color: var(--ink);
}

.about-title p {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--ink-3);
}

.about-body {
  display: flex;
  flex-direction: column;
  gap: 9px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--ink-2);
}

.about-body ul {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--ink-2);
}

.about-kv {
  display: grid;
  grid-template-columns: 84px minmax(0, 1fr);
  gap: 8px 12px;
  margin: 0;
  font-size: 12.5px;
}

.about-kv dt {
  color: var(--ink-3);
}

.about-kv dd {
  margin: 0;
  color: var(--ink);
  word-break: break-all;
}

.about-icp {
  color: var(--brand);
  font-weight: 600;
}

.about-icp:hover {
  text-decoration: underline;
}

.about-note {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: var(--r-md);
  background: var(--card-soft);
  border: 1px solid var(--line-soft);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--ink-3);
}

.muted {
  color: var(--ink-3);
}

@media (max-width: 760px) {
  .about-kv {
    grid-template-columns: 68px minmax(0, 1fr);
  }
}
</style>
