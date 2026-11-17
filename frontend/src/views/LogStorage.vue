<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as echarts from 'echarts'
import { Refresh } from '@element-plus/icons-vue'
import {
  getCapacity,
  getStorageConfig,
  putStorageConfig,
  listIndexes,
  cleanIndex,
  listTenantQuotas,
  putTenantQuota,
  STORAGE_RANGE_OPTIONS,
} from '../api/storage'
import { useAppStore } from '../stores/app'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const app = useAppStore()

const me = computed(() => auth.user || {})
const canUpdate = computed(() => auth.has('storage', 'update'))
const canClean = computed(() => auth.has('storage', 'clean'))
const isPlatformAdmin = computed(() => auth.user?.roleKey === 'platform_admin')

// 查看范围：平台管理员默认看全部租户（否则「日志空间管理」只能看到自己那一个索引），
// 其他角色锁定本租户。注意磁盘为各租户共用，单租户视角下已用与可用不同源，页面上如实说明。
const scopeMode = ref('tenant')
const scopeTenant = computed(() => (scopeMode.value === 'all' ? 'all' : app.tenant))
const scopeLabel = computed(() => (scopeMode.value === 'all'
  ? '全部租户'
  : (me.value.tenantName || app.tenant)))
let scopeReady = false

const TABS = [
  { key: 'capacity', label: '容量概览' },
  { key: 'config', label: '存储配置' },
  { key: 'index', label: '索引管理' },
  { key: 'quota', label: '租户配额' },
]
const visibleTabs = computed(() => TABS.filter(() => auth.has('storage', 'view')))
const activeTab = ref('capacity')
watch(visibleTabs, (list) => {
  if (list.length && !list.some((t) => t.key === activeTab.value)) activeTab.value = list[0].key
}, { immediate: true })

function errText(e, fallback) {
  const status = e.response?.status
  if (status === 401) {
    auth.clear()
    router.replace({ path: '/login', query: { redirect: '/settings/storage' } })
    return '登录态已失效，请重新登录'
  }
  if (status === 403) return e.response?.data?.detail || '当前角色无此操作权限'
  return e.response?.data?.detail || e.message || fallback
}

// ---- 容量概览 ----
const capLoading = ref(false)
const capError = ref('')
const capacity = reactive({
  total: 0, used: 0, available: 0, occupancy: '0', unit: 'GB',
  totalText: '—', usedText: '—', availableText: '—',
  logText: '—', docs: 0, indexCount: 0, basis: 'physical', quotaGB: 0,
  physical: { totalText: '—', availableText: '—' },
  trend: { unit: 'GB', points: [], source: 'estimated', note: '' },
  updatedAt: '', note: '',
})
const rangeLabel = ref('近 7 天')

const trendSourceText = computed(() => (
  capacity.trend.source === 'snapshot' ? '实测快照' : '按真实日志量推算'
))

async function loadCapacity() {
  capLoading.value = true
  capError.value = ''
  try {
    const res = await getCapacity(rangeLabel.value, scopeTenant.value)
    Object.assign(capacity, res)
    renderTrend()
  } catch (e) {
    capError.value = errText(e, '容量数据加载失败')
  } finally {
    capLoading.value = false
  }
}

// ---- 存储配置 ----
const cfgLoading = ref(false)
const cfgSaving = ref(false)
const cfgError = ref('')
const cfgUpdatedAt = ref('')
const cfgForm = reactive({ retainDays: 30, rollThreshold: 80, hotColdStrategy: 'hot7d', quotaGB: 0 })
const cfgStrategies = ref([])

async function loadConfig() {
  cfgLoading.value = true
  cfgError.value = ''
  try {
    const res = await getStorageConfig()
    cfgForm.retainDays = res.retainDays
    cfgForm.rollThreshold = res.rollThreshold
    cfgForm.hotColdStrategy = res.hotColdStrategy
    cfgForm.quotaGB = res.quotaGB
    cfgUpdatedAt.value = res.updatedAt || ''
    cfgStrategies.value = res.strategies || []
  } catch (e) {
    cfgError.value = errText(e, '存储配置加载失败')
  } finally {
    cfgLoading.value = false
  }
}

async function saveConfig() {
  cfgSaving.value = true
  try {
    const res = await putStorageConfig({ ...cfgForm })
    ElMessage.success(res.message || '存储配置已保存')
    if (res.config) cfgUpdatedAt.value = res.config.updatedAt || ''
    loadCapacity()
  } catch (e) {
    ElMessage.error(errText(e, '保存失败'))
  } finally {
    cfgSaving.value = false
  }
}

// ---- 索引管理 ----
const idxLoading = ref(false)
const idxError = ref('')
const idxItems = ref([])
const idxTotal = ref(0)

const STATUS_TEXT = { healthy: '健康', warning: '告警', cold: '冷数据' }

async function loadIndexes() {
  idxLoading.value = true
  idxError.value = ''
  try {
    const res = await listIndexes(scopeTenant.value)
    idxItems.value = res.items || []
    idxTotal.value = res.total || 0
  } catch (e) {
    idxError.value = errText(e, '索引列表加载失败')
    idxItems.value = []
    idxTotal.value = 0
  } finally {
    idxLoading.value = false
  }
}

function statusTagType(s) {
  if (s === 'healthy') return 'success'
  if (s === 'warning') return 'warning'
  return 'info'
}

async function doClean(row) {
  try {
    await ElMessageBox.confirm(
      `将删除「${row.name}」中早于保留期（${cfgForm.retainDays} 天）的日志文档，`
      + '索引本身保留。清理不可恢复，确定继续？',
      '清理过期日志',
      { type: 'warning', confirmButtonText: '确认清理', cancelButtonText: '取消' },
    )
  } catch { return }
  try {
    const res = await cleanIndex(row.name, 'purge')
    if (res.deleted) ElMessage.success(res.message)
    else ElMessage.info(res.message)
    await Promise.all([loadIndexes(), loadCapacity()])
  } catch (e) {
    ElMessage.error(errText(e, '清理失败'))
  }
}

async function doDrop(row) {
  try {
    await ElMessageBox.confirm(
      `将删除整个索引「${row.name}」（${row.sizeText} / ${row.docs} 条文档），`
      + '该索引的数据将无法恢复。仅冷索引允许删除，确定继续？',
      '删除索引',
      { type: 'error', confirmButtonText: '删除索引', cancelButtonText: '取消' },
    )
  } catch { return }
  try {
    const res = await cleanIndex(row.name, 'drop')
    ElMessage.success(res.message)
    await Promise.all([loadIndexes(), loadCapacity()])
  } catch (e) {
    ElMessage.error(errText(e, '删除失败'))
  }
}

// ---- 租户配额 ----
const quotaLoading = ref(false)
const quotaError = ref('')
const quotaItems = ref([])

async function loadQuotas() {
  quotaLoading.value = true
  quotaError.value = ''
  try {
    const res = await listTenantQuotas()
    quotaItems.value = res.items || []
  } catch (e) {
    quotaError.value = errText(e, '租户配额加载失败')
  } finally {
    quotaLoading.value = false
  }
}

async function saveQuota(row) {
  try {
    const res = await putTenantQuota(row.tenant, Number(row.quotaGB) || 0)
    ElMessage.success(res.message || '配额已更新')
    await Promise.all([loadQuotas(), loadCapacity()])
  } catch (e) {
    ElMessage.error(errText(e, '更新失败'))
  }
}

function quotaPercent(row) {
  if (!row.quotaGB) return 0
  return Math.min(100, Math.round((row.usedBytes / (row.quotaGB * 1024 ** 3)) * 100))
}

// ---- 图表 ----
const trendEl = ref(null)
let trendChart = null

function renderTrend() {
  if (!trendChart) return
  const pts = capacity.trend.points || []
  trendChart.setOption({
    grid: { left: 52, right: 16, top: 24, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v) => `${v} ${capacity.trend.unit}`,
    },
    xAxis: { type: 'category', data: pts.map((p) => p.t), axisLabel: { color: '#999', fontSize: 11 } },
    yAxis: {
      type: 'value',
      name: capacity.trend.unit,
      nameTextStyle: { color: '#999', fontSize: 11 },
      axisLabel: { color: '#999', fontSize: 11 },
      splitLine: { lineStyle: { color: '#E7ECF2' } },
    },
    series: [{
      type: 'line',
      smooth: true,
      data: pts.map((p) => p.v),
      lineStyle: { color: '#03857E', width: 2 },
      areaStyle: { color: 'rgba(3,133,126,0.15)' },
    }],
  }, true)
}

function onTabChange(key) {
  if (key === 'capacity') loadCapacity()
  if (key === 'config' && !cfgStrategies.value.length) loadConfig()
  if (key === 'index' && !idxItems.value.length) loadIndexes()
  if (key === 'quota' && !quotaItems.value.length) loadQuotas()
}

function reloadActive() {
  if (activeTab.value === 'capacity') loadCapacity()
  else if (activeTab.value === 'config') loadConfig()
  else if (activeTab.value === 'index') loadIndexes()
  else loadQuotas()
}

// Tab 切换会把容量区块的 DOM 销毁重建，图表实例必须跟着重新绑定，否则切回来看不到曲线
function initChart() {
  nextTick(() => {
    if (!trendEl.value) return
    if (trendChart && trendChart.getDom() === trendEl.value) {
      renderTrend()
      return
    }
    if (trendChart) {
      trendChart.dispose()
      trendChart = null
    }
    trendChart = echarts.init(trendEl.value)
    renderTrend()
  })
}

function onResize() {
  if (trendChart) trendChart.resize()
}

onMounted(async () => {
  if (!auth.has('storage', 'view')) return
  scopeMode.value = isPlatformAdmin.value ? 'all' : 'tenant'
  await Promise.all([loadCapacity(), loadConfig()])
  scopeReady = true
  initChart()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (trendChart) {
    trendChart.dispose()
    trendChart = null
  }
})

watch(activeTab, (key) => {
  if (key === 'capacity') initChart()
})

watch(scopeMode, () => {
  if (!scopeReady) return
  loadCapacity()
  if (activeTab.value === 'index') loadIndexes()
})

watch(() => app.tenant, () => {
  if (!scopeReady) return
  if (activeTab.value === 'capacity') loadCapacity()
  if (activeTab.value === 'index') loadIndexes()
  if (activeTab.value === 'quota') loadQuotas()
})

watch(rangeLabel, () => loadCapacity())
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div class="breadcrumb"><span>平台管理</span><span class="sep">/</span><span class="cur">日志空间管理</span></div>
      <div class="head-bar">
        <div>
          <h1 class="title">日志空间管理</h1>
          <p class="desc">容量概览、存储配置、索引管理与租户配额</p>
        </div>
        <div class="head-actions">
          <el-select v-if="isPlatformAdmin" v-model="scopeMode" size="small" style="width:128px">
            <el-option label="全部租户" value="all" />
            <el-option label="本租户" value="tenant" />
          </el-select>
          <el-button :icon="Refresh" @click="reloadActive">刷新</el-button>
        </div>
      </div>
    </div>

    <section class="card">
      <div class="card-head">
        <el-tabs v-model="activeTab" class="tabs" @tab-change="onTabChange">
          <el-tab-pane v-for="t in visibleTabs" :key="t.key" :label="t.label" :name="t.key" />
        </el-tabs>
        <span class="tenant-hint">当前租户：{{ me.tenantName || app.tenant }} · 当前角色：{{ me.roleName || '—' }}</span>
      </div>

      <!-- 容量概览 -->
      <div v-if="activeTab === 'capacity'">
        <el-alert v-if="capError" type="error" :closable="false" show-icon :title="capError" class="block-alert" />
        <div v-loading="capLoading" class="cap-body">
          <div class="stat-grid">
            <div class="stat-card">
              <div class="stat-label">总容量</div>
              <div class="stat-value"><span class="num">{{ capacity.totalText }}</span></div>
            </div>
            <div class="stat-card">
              <div class="stat-label">日志已用</div>
              <div class="stat-value"><span class="num">{{ capacity.usedText }}</span></div>
            </div>
            <div class="stat-card">
              <div class="stat-label">可用</div>
              <div class="stat-value"><span class="num">{{ capacity.availableText }}</span></div>
            </div>
            <div class="stat-card">
              <div class="stat-label">占用率</div>
              <div class="stat-value"><span class="num">{{ capacity.occupancy }}</span><span class="unit">%</span></div>
              <el-progress
                :percentage="Math.min(100, Number(capacity.occupancy) || 0)"
                :stroke-width="8"
                :show-text="false"
                style="margin-top:8px"
              />
            </div>
          </div>
          <div class="meta-row">
            <span>范围 <b>{{ scopeLabel }}</b></span>
            <span>索引 <b>{{ capacity.indexCount }}</b> 个</span>
            <span>文档 <b>{{ capacity.docs.toLocaleString() }}</b> 条</span>
            <span>物理卷 <b>{{ capacity.physical.totalText }}</b>（可用 {{ capacity.physical.availableText }}）</span>
            <span>口径 <b>{{ capacity.basis === 'quota' ? `配额 ${capacity.quotaGB} GB` : '物理卷' }}</b></span>
            <span>更新于 {{ capacity.updatedAt || '—' }}</span>
          </div>

          <div class="chart-card">
            <div class="chart-head">
              <span class="card-title">
                日志占用趋势
                <el-tag size="small" effect="plain" :type="capacity.trend.source === 'snapshot' ? 'success' : 'info'">
                  {{ trendSourceText }}
                </el-tag>
              </span>
              <el-select v-model="rangeLabel" size="small" style="width:120px">
                <el-option v-for="r in STORAGE_RANGE_OPTIONS" :key="r" :label="r" :value="r" />
              </el-select>
            </div>
            <div v-show="capacity.trend.points.length" ref="trendEl" class="chart"></div>
            <div v-if="!capacity.trend.points.length" class="empty-box">{{ capacity.trend.note || '暂无趋势数据' }}</div>
            <p v-else class="cap-note">{{ capacity.trend.note }}</p>
          </div>
        </div>
      </div>

      <!-- 存储配置 -->
      <div v-else-if="activeTab === 'config'">
        <el-alert v-if="cfgError" type="error" :closable="false" show-icon :title="cfgError" class="block-alert" />
        <div v-loading="cfgLoading" class="param-body">
          <div class="param-row">
            <label class="param-label">保留天数</label>
            <div class="param-control">
              <el-input-number v-model="cfgForm.retainDays" :min="1" :max="3650" :disabled="!canUpdate" />
              <span class="param-note">早于该天数的日志可被清理，日志生命周期的唯一设置项</span>
            </div>
          </div>
          <div class="param-row">
            <label class="param-label">滚动阈值</label>
            <div class="param-control">
              <el-slider v-model="cfgForm.rollThreshold" :min="50" :max="95" :disabled="!canUpdate" style="width:240px" />
              <span class="param-note">{{ cfgForm.rollThreshold }}%（单索引体积超过配额的该比例时告警）</span>
            </div>
          </div>
          <div class="param-row">
            <label class="param-label">热/冷分级</label>
            <div class="param-control">
              <el-select v-model="cfgForm.hotColdStrategy" style="width:280px" :disabled="!canUpdate">
                <el-option v-for="s in cfgStrategies" :key="s.value" :label="s.label" :value="s.value" />
              </el-select>
              <span class="param-note">超过热窗口的索引标记为冷数据，可整体删除</span>
            </div>
          </div>
          <div class="param-row">
            <label class="param-label">日志配额</label>
            <div class="param-control">
              <el-input-number v-model="cfgForm.quotaGB" :min="0" :step="100" :disabled="!canUpdate" />
              <span class="param-note">GB，0 表示不限制（按物理卷剩余空间计算可用容量）</span>
            </div>
          </div>
          <div class="param-row">
            <span class="param-label"></span>
            <div class="param-control">
              <el-button type="primary" :loading="cfgSaving" :disabled="!canUpdate" @click="saveConfig">保存</el-button>
              <el-button @click="loadConfig">重置</el-button>
              <span class="param-note">最近更新：{{ cfgUpdatedAt || '—' }}</span>
            </div>
          </div>
          <p v-if="!canUpdate" class="param-note">当前角色只有查看权限，无法修改存储配置。</p>
        </div>
      </div>

      <!-- 索引管理 -->
      <div v-else-if="activeTab === 'index'">
        <el-alert v-if="idxError" type="error" :closable="false" show-icon :title="idxError" class="block-alert" />
        <div v-loading="idxLoading" style="padding:14px 18px">
          <el-table :data="idxItems" stripe>
            <el-table-column prop="name" label="索引名称" min-width="150" />
            <el-table-column prop="sizeText" label="大小" width="110" />
            <el-table-column prop="docs" label="文档数" width="120">
              <template #default="{ row }">{{ row.docs.toLocaleString() }}</template>
            </el-table-column>
            <el-table-column prop="ageDays" label="已存在" width="100">
              <template #default="{ row }">{{ row.ageDays }} 天</template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTagType(row.status)" effect="plain">
                  {{ STATUS_TEXT[row.status] || row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="reason" label="判定依据" min-width="240" show-overflow-tooltip />
            <el-table-column label="操作" width="190" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small" type="primary" plain
                  :disabled="!canClean"
                  @click="doClean(row)"
                >清理过期</el-button>
                <el-button
                  v-if="row.status === 'cold'"
                  size="small" type="danger" plain
                  :disabled="!canClean"
                  @click="doDrop(row)"
                >删除索引</el-button>
              </template>
            </el-table-column>
            <template #empty><div class="empty-box">未发现 logs-* 索引</div></template>
          </el-table>
          <p class="note">
            共 {{ idxTotal }} 个索引 · 状态判据：超出保留期 → 告警；超出热窗口 → 冷数据；health 非 green 或体积超阈值 → 告警
            <span v-if="!canClean"> · 当前角色仅有查看权限，清理按钮不可用</span>
          </p>
        </div>
      </div>

      <!-- 租户配额 -->
      <div v-else>
        <el-alert v-if="quotaError" type="error" :closable="false" show-icon :title="quotaError" class="block-alert" />
        <div v-loading="quotaLoading" style="padding:14px 18px">
          <el-table :data="quotaItems" stripe>
            <el-table-column prop="tenantName" label="租户" min-width="130" />
            <el-table-column prop="tenant" label="编码" width="110" />
            <el-table-column label="配额(GB)" width="170">
              <template #default="{ row }">
                <el-input-number v-model="row.quotaGB" :min="0" :step="100" size="small" :disabled="!canUpdate" />
              </template>
            </el-table-column>
            <el-table-column label="已用" width="110">
              <template #default="{ row }">{{ row.usedText }}</template>
            </el-table-column>
            <el-table-column prop="docs" label="文档数" width="110">
              <template #default="{ row }">{{ row.docs.toLocaleString() }}</template>
            </el-table-column>
            <el-table-column label="使用率" width="160">
              <template #default="{ row }">
                <el-progress v-if="row.quotaGB > 0" :percentage="quotaPercent(row)" :stroke-width="8" />
                <span v-else class="param-note">不限制</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100">
              <template #default="{ row }">
                <el-button size="small" type="primary" :disabled="!canUpdate" @click="saveQuota(row)">保存</el-button>
              </template>
            </el-table-column>
          </el-table>
          <p class="note">配额为 0 表示不限制；已用容量来自该租户索引的真实 store.size。</p>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.breadcrumb { font-size: 12px; color: var(--color-text-3); }
.breadcrumb .sep { margin: 0 6px; }
.breadcrumb .cur { color: var(--color-text-2); }
.head-bar { display: flex; justify-content: space-between; align-items: flex-start; margin-top: 8px; gap: 12px; }
.title { font-size: 18px; font-weight: 600; color: var(--color-text-1); margin: 0; }
.desc { margin: 4px 0 0; font-size: 12px; color: var(--color-text-3); }
.head-actions { display: flex; gap: 8px; flex-shrink: 0; }

.card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 0 18px; border-bottom: 1px solid var(--color-border-1); }
.tabs { flex: 1; }
.tabs :deep(.el-tabs__header) { margin: 0; }
.tabs :deep(.el-tabs__nav-wrap::after) { display: none; }
.tenant-hint { font-size: 12px; color: var(--color-text-3); flex-shrink: 0; }

.block-alert { margin: 12px 18px; width: auto; }
.cap-body { padding: 18px; }
.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 16px 18px; }
.stat-label { font-size: 13px; color: var(--color-text-2); }
.stat-value { margin-top: 8px; color: var(--color-text-1); }
.stat-value .num { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat-value .unit { font-size: 13px; color: var(--color-text-3); margin-left: 2px; }

.cap-note { margin: 10px 0 0; font-size: 12px; color: var(--color-text-3); line-height: 1.6; }
.meta-row { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 8px; font-size: 12px; color: var(--color-text-3); }
.meta-row b { color: var(--color-text-2); font-weight: 600; }

.chart-card { margin-top: 14px; background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 16px; }
.chart-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 12px; }
.card-title { font-size: 14px; font-weight: 600; color: var(--color-text-1); display: inline-flex; align-items: center; gap: 8px; }
.chart { width: 100%; height: 220px; }

.param-body { padding: 18px; max-width: 960px; }
.param-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; }
.param-label { width: 96px; flex-shrink: 0; text-align: right; font-size: 13px; color: var(--color-text-2); }
.param-control { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.param-note { font-size: 12px; color: var(--color-text-3); }

.note { margin: 0; padding: 10px 0 0; font-size: 12px; color: var(--color-text-3); }
.empty-box { padding: 22px 0; color: var(--color-text-3); font-size: 13px; text-align: center; }

@media (max-width: 1100px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
