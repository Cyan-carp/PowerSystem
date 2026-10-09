<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api'
import { errorMessage } from '../lib/format'
import type { Device } from '../types'

const router = useRouter()
const rows = ref<Device[]>([])
const page = ref(1)
const total = ref(0)
const groupInput = ref('')
const group = ref('')
const loading = ref(false)
const error = ref('')
const pageSize = 20

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await api.devices(page.value, pageSize, group.value)
    rows.value = result.list || []
    total.value = result.total
  } catch (cause) { error.value = errorMessage(cause); rows.value = [] }
  finally { loading.value = false }
}
function search(): void { page.value = 1; group.value = groupInput.value.trim(); void load() }
function reset(): void { groupInput.value = ''; group.value = ''; page.value = 1; void load() }
onMounted(() => { void load() })
</script>

<template>
  <section class="page-head"><div><span class="section-kicker">设备台账</span><h1>设备管理</h1><p>查看已登记设备和历史遥测记录。</p></div><div class="head-stat">设备总数 <strong>{{ total }}</strong></div></section>
  <section class="surface">
    <div class="toolbar"><div class="toolbar-title">设备列表 <span>按设备分组精确筛选</span></div><div class="toolbar-actions"><el-input v-model="groupInput" placeholder="输入完整分组名称" clearable @keyup.enter="search" /><el-button type="primary" @click="search">筛选</el-button><el-button @click="reset">重置</el-button></div></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="content-alert" />
    <el-table v-loading="loading" :data="rows" stripe empty-text="暂无符合条件的设备" class="data-table" @row-click="(row: Device) => router.push(`/devices/${row.id}`)">
      <el-table-column label="设备编号" prop="device_code" min-width="150"><template #default="{ row }"><strong class="code-cell">{{ row.device_code }}</strong></template></el-table-column>
      <el-table-column label="设备名称" prop="name" min-width="180" />
      <el-table-column label="类型" prop="dev_type" min-width="120" />
      <el-table-column label="仿真参考型号" min-width="215"><template #default="{ row }">{{ row.is_simulated ? `${row.reference_vendor} ${row.reference_model}` : '—' }}</template></el-table-column>
      <el-table-column label="场站" prop="station_code" min-width="130" />
      <el-table-column label="分组" prop="group_name" min-width="130"><template #default="{ row }">{{ row.group_name || '未分组' }}</template></el-table-column>
      <el-table-column label="操作" width="115"><template #default="{ row }"><el-button link type="primary" @click.stop="router.push(`/devices/${row.id}`)">查看详情 →</el-button></template></el-table-column>
    </el-table>
    <div class="table-footer"><span>共 {{ total }} 台设备</span><el-pagination v-model:current-page="page" :page-size="pageSize" :total="total" layout="prev, pager, next" @current-change="load" /></div>
  </section>
</template>
