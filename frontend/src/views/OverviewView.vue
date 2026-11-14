<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { Refresh, Download } from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import { getOverview, RANGE_OPTIONS } from '../api/overview'

const app = useAppStore()

const rangeLabel = ref('今日')
const loading = ref(false)
const error = ref(false)
const updatedAt = ref(new Date())

// 概览数据模型（结构与后端 /overview 对齐）
const overview = reactive({
  stats: {},
  writeTrend: { unit: '万条', points: [] },
  alarmDist: [],
  latency: { unit: 'ms', target: 800, points: [] },
  sources: [],
  recentAlarms: [],
})

const updatedAtText = computed(() => fmt(updatedAt.value))
const descText = computed(() => `数据截至 ${updatedAtText.value}，每 60 秒自动刷新`)
const alarmTotal = computed(() => overview.alarmDist.reduce((s, x) => s + x.value, 0))
// 时间维度短文本：用于卡片脚注，使“近24小时”等写死文案随所选维度变化
const rangeShort = computed(() => {
  const m = {
    '今日': '今日',
    '近 1 小时': '近1小时',
    '近 24 小时': '近24小时',
    '近 7 天': '近7天',
    '近 30 天': '近30天',
  }
  return m[rangeLabel.value] || rangeLabel.value
})

function fmt(d) {
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function levelTagType(level) {
  if (level === '严重') return 'danger'
  if (level === '警告') return 'warning'
  if (level === '提示') return 'info'
  return 'info'
}

// ——— 取数 ———
async function loadData(showTip = false, force = false) {
  loading.value = true
  error.value = false
  try {
    const data = await getOverview(rangeLabel.value, app.tenant, force)
    overview.stats = data.stats
    overview.writeTrend = data.writeTrend
    overview.alarmDist = data.alarmDist
    overview.latency = data.latency
    overview.sources = data.sources
    overview.recentAlarms = data.recentAlarms
    updatedAt.value = new Date(data.updatedAt || Date.now())
    if (showTip) ElMessage.success('指标已刷新')
  } catch (e) {
    error.value = true
    ElMessage.error('指标加载失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

function onRangeChange() {
  loadData(false)
}

function onRefresh() {
  loadData(true, true)
}

function onExport() {
  ElMessage.success('运行报表已导出，可在下载中心查看')
}

// ——— 图表实例 ———
const trendEl = ref(null)
const donutEl = ref(null)
const latencyEl = ref(null)
let trendChart = null
let donutChart = null
let latencyChart = null

function renderTrend() {
  if (!trendChart) return
  const pts = overview.writeTrend.points
  trendChart.setOption({
    grid: { left: 44, right: 16, top: 28, bottom: 28 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'line' } },
    xAxis: {
      type: 'category',
      data: pts.map((p) => p.t),
      axisLabel: { color: '#999', fontSize: 11 },
      axisLine: { lineStyle: { color: '#BFBFBF' } },
      boundaryGap: false,
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#999', fontSize: 11 },
      splitLine: { lineStyle: { color: '#E7ECF2' } },
    },
    series: [
      {
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: pts.map((p) => p.v),
        lineStyle: { color: '#03857E', width: 2 },
        itemStyle: { color: '#03857E' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(3,133,126,0.28)' },
            { offset: 1, color: 'rgba(3,133,126,0.02)' },
          ]),
        },
      },
    ],
  })
}

function renderDonut() {
  if (!donutChart) return
  donutChart.setOption({
    tooltip: {
      trigger: 'item',
      // 渲染到 body 顶层，脱离卡片/图表容器的裁剪与遮挡（环形图左侧扇区悬浮时气泡不再被卡片左缘挡住）
      appendToBody: true,
      formatter: '{b}：{c} 条（{d}%）',
      // 优先显示在鼠标右侧（卡片右侧空间大，符合实际布局）；右侧不足再翻到左侧，并约束在视口内
      position: (point, _params, _dom, _rect, size) => {
        const tw = (size && size.contentSize && size.contentSize[0]) || 150
        const th = (size && size.contentSize && size.contentSize[1]) || 60
        let x = point[0] + 12
        if (x + tw > window.innerWidth - 8) x = point[0] - tw - 12
        let y = point[1] - th / 2
        y = Math.max(8, Math.min(y, window.innerHeight - th - 8))
        return [x, y]
      },
    },
    legend: { show: false },
    series: [
      {
        type: 'pie',
        radius: ['56%', '74%'],
        center: ['50%', '50%'],
        avoidLabelOverlap: false,
        label: { show: false },
        labelLine: { show: false },
        data: overview.alarmDist.map((it) => ({
          name: it.name,
          value: it.value,
          itemStyle: { color: it.color },
        })),
      },
    ],
  })
}

function renderLatency() {
  if (!latencyChart) return
  const pts = overview.latency.points
  const target = overview.latency.target
  const maxV = Math.max(...pts.map((p) => p.v), 1)
  // 真实检索耗时在演示数据量下可能远小于 800ms 目标；仅当目标线相对数据有意义时才绘制，
  // 避免坐标轴被目标线拉高导致柱体不可见。
  const includeTarget = target <= maxV * 3
  const yMax = includeTarget
    ? Math.max(Math.ceil(maxV * 1.3), target * 1.1)
    : Math.ceil(maxV * 1.3)
  latencyChart.setOption({
    grid: { left: 44, right: 16, top: 24, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (p) => {
        const d = p[0]?.data || {}
        if (!d.count) return `${p[0].axisValue}<br/>该时段无检索记录`
        return `${p[0].axisValue}<br/>P95 耗时：${d.value} ms · ${d.count} 次检索`
      },
    },
    xAxis: {
      type: 'category',
      data: pts.map((p) => p.t),
      axisLabel: { color: '#999', fontSize: 11 },
      axisLine: { lineStyle: { color: '#BFBFBF' } },
    },
    yAxis: {
      type: 'value',
      max: yMax,
      axisLabel: { color: '#999', fontSize: 11 },
      splitLine: { lineStyle: { color: '#E7ECF2' } },
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 18,
        data: pts.map((p) => ({
          value: p.v,
          count: p.count || 0,
          itemStyle: {
            color: p.v > target ? '#EA720F' : '#03857E',
            borderRadius: [2, 2, 0, 0],
          },
        })),
        markLine: includeTarget
          ? {
              silent: true,
              symbol: 'none',
              data: [{ yAxis: target }],
              lineStyle: { color: '#EA720F', type: 'dashed' },
              label: {
                formatter: `目标 ${target}ms`,
                color: '#EA720F',
                fontSize: 11,
                position: 'end',
              },
            }
          : undefined,
      },
    ],
  })
}

function renderAll() {
  renderTrend()
  renderDonut()
  renderLatency()
}

function resizeAll() {
  trendChart && trendChart.resize()
  donutChart && donutChart.resize()
  latencyChart && latencyChart.resize()
}

// ——— 自动刷新（每 60 秒）———
let timer = null
function startAutoRefresh() {
  stopAutoRefresh()
  timer = setInterval(() => loadData(false), 60000)
}
function stopAutoRefresh() {
  if (timer) clearInterval(timer)
  timer = null
}

watch(() => overview.writeTrend, renderTrend, { deep: true })
watch(() => overview.alarmDist, renderDonut, { deep: true })
watch(() => overview.latency, renderLatency, { deep: true })
watch(
  () => app.tenant,
  () => loadData(false),
)

onMounted(async () => {
  await nextTick()
  trendChart = echarts.init(trendEl.value)
  donutChart = echarts.init(donutEl.value)
  latencyChart = echarts.init(latencyEl.value)
  window.addEventListener('resize', resizeAll)
  await loadData(false)
  renderAll()
  startAutoRefresh()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeAll)
  stopAutoRefresh()
  trendChart && trendChart.dispose()
  donutChart && donutChart.dispose()
  latencyChart && latencyChart.dispose()
})
</script>

<template>
  <div class="page" v-loading="loading">
    <!-- 页头 -->
    <div class="page-head">
      <div class="breadcrumb">
        <span>概览</span><span class="sep">/</span><span class="cur">运行总览</span>
      </div>
      <div class="head-bar">
        <div>
          <h1 class="title">运行总览</h1>
          <p class="desc">{{ descText }}</p>
        </div>
        <div class="head-actions">
          <el-select v-model="rangeLabel" size="default" style="width: 130px" @change="onRangeChange">
            <el-option v-for="r in RANGE_OPTIONS" :key="r" :label="r" :value="r" />
          </el-select>
          <el-button :icon="Refresh" @click="onRefresh">刷新</el-button>
          <el-button :icon="Download" @click="onExport">导出报表</el-button>
        </div>
      </div>
    </div>

    <!-- 错误态 -->
    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      title="指标加载失败"
      description="数据加载失败，请稍后重试"
      style="margin-bottom: 12px"
    >
      <template #default>
        <div class="alert-body">
          <span>数据加载失败，请稍后重试</span>
          <el-button size="small" type="danger" plain @click="loadData(false)">重试</el-button>
        </div>
      </template>
    </el-alert>

    <!-- 核心指标 -->
    <section class="stat-grid" aria-label="核心指标">
      <div v-for="(s, key) in overview.stats" :key="key" class="stat-card">
        <div class="stat-head">
          <span class="stat-label">{{ s.label }}</span>
          <el-badge v-if="s.badge !== undefined" :value="s.badge" type="danger" class="stat-badge" />
          <span
            v-else-if="s.status"
            class="status-tag"
            :class="`status-tag--${s.status}`"
          ><i class="dot"></i>{{ s.statusText }}</span>
        </div>
        <div class="stat-value">
          <span class="num">{{ s.value }}</span>
          <span class="unit" v-if="s.total !== undefined">{{ ` / ${s.total} ${s.unit}` }}</span>
          <span class="unit" v-else-if="s.unit">{{ s.unit }}</span>
        </div>
        <div class="stat-desc">
          <span v-if="s.trend === 'up'" class="trend trend--up">{{ s.trendText }}</span>
          <span v-else-if="s.trend === 'down'" class="trend trend--down">{{ s.trendText }}</span>
          <span class="sub">{{ s.sub }}</span>
        </div>
      </div>
    </section>

    <!-- 趋势与分布 -->
    <section class="grid-2" aria-label="趋势与分布">
      <el-card shadow="never" class="card">
        <template #header>
          <div class="card-head">
            <span class="card-title">日志写入趋势</span>
            <span class="hint">单位：{{ overview.writeTrend.unit }} · {{ rangeShort }}</span>
          </div>
        </template>
        <div ref="trendEl" class="chart"></div>
        <div class="chart-foot">
          <span>数据来源：日志检索 · 全量数据源</span>
        </div>
      </el-card>

      <el-card shadow="never" class="card">
        <template #header>
          <div class="card-head">
            <span class="card-title">告警级别分布</span>
            <router-link to="/alarms/events" class="lnk">查看告警事件</router-link>
          </div>
        </template>
        <div class="donut-wrap">
          <div class="donut-box">
            <div ref="donutEl" class="donut-chart"></div>
            <div class="donut-center">
              <div class="donut-center__value">{{ alarmTotal }}</div>
              <div class="donut-center__label">活动告警</div>
            </div>
          </div>
          <ul class="donut-legend">
            <li v-for="it in overview.alarmDist" :key="it.raw || it.name">
              <span class="lg-dot" :style="{ background: it.color }"></span>
              <span class="lg-name">{{ it.name }}</span>
              <span class="lg-val">{{ it.value }} 条 · {{ Math.round((it.value / Math.max(alarmTotal, 1)) * 100) }}%</span>
            </li>
          </ul>
        </div>
        <div class="chart-foot">
          <span>数据来源：告警中心 · {{ rangeShort }}</span>
          <span>注：同一告警 30 分钟内合并计一次</span>
        </div>
      </el-card>
    </section>

    <!-- 检索耗时与数据源健康 -->
    <section class="grid-2" aria-label="检索耗时与数据源健康">
      <el-card shadow="never" class="card">
        <template #header>
          <div class="card-head">
            <span class="card-title">检索耗时分布</span>
            <span class="hint">单位：{{ overview.latency.unit }}</span>
          </div>
        </template>
        <div ref="latencyEl" class="chart"></div>
        <div class="chart-foot">
          <span>数据来源：检索网关 · {{ rangeShort }}</span>
        </div>
      </el-card>

      <el-card shadow="never" class="card">
        <template #header>
          <div class="card-head">
            <span class="card-title">数据源健康度</span>
            <router-link to="/data-sources" class="lnk">查看数据源</router-link>
          </div>
        </template>
        <div class="health-list">
          <div v-for="src in overview.sources" :key="src.name" class="health-item">
            <div class="health-row">
              <span class="health-name">{{ src.name }}</span>
              <span v-if="src.rate" class="hint">{{ src.rate }}</span>
              <span v-else class="status-tag" :class="`status-tag--${src.status}`">
                <i class="dot"></i>{{ src.tag }}
              </span>
            </div>
            <div class="health-bar">
              <div
                class="health-bar__fill"
                :class="`health-bar__fill--${src.status}`"
                :style="{ width: src.health + '%' }"
              ></div>
            </div>
          </div>
        </div>
        <p class="hint health-note">
          健康度 = 最近 1 小时实际接收量 / 近 7 天同时段均值，低于 70% 触发告警。
        </p>
      </el-card>
    </section>

    <!-- 最近告警 -->
    <el-card shadow="never" class="card" aria-label="最近告警">
      <template #header>
        <div class="card-head">
          <span class="card-title">最近告警</span>
          <router-link to="/alarms/events" class="lnk">查看全部告警</router-link>
        </div>
      </template>
      <el-table :data="overview.recentAlarms" stripe size="small">
        <el-table-column prop="name" label="告警名称" min-width="220" show-overflow-tooltip />
        <el-table-column label="级别" width="100">
          <template #default="{ row }">
            <el-tag :type="levelTagType(row.level)" size="small" effect="light">{{ row.level }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="告警来源" width="160" />
        <el-table-column prop="occurTime" label="发生时间" width="180" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.status === '告警中'" type="warning" size="small" effect="light">
              <i class="dot dot--warning"></i>{{ row.status }}
            </el-tag>
            <span v-else class="muted">{{ row.status }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="handler" label="处理人" width="110" />
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }

.page-head { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.breadcrumb { font-size: 12px; color: var(--color-text-3); }
.breadcrumb .sep { margin: 0 6px; }
.breadcrumb .cur { color: var(--color-text-2); }
.head-bar { display: flex; justify-content: space-between; align-items: flex-end; margin-top: 8px; }
.title { font-size: 18px; font-weight: 600; color: var(--color-text-1); margin: 0; }
.desc { font-size: 13px; color: var(--color-text-3); margin: 4px 0 0; }
.head-actions { display: flex; gap: 8px; }

.alert-body { display: flex; justify-content: space-between; align-items: center; }

/* 核心指标 */
.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 16px 18px; }
.stat-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.stat-label { font-size: 13px; color: var(--color-text-2); }
.stat-badge { :deep(.el-badge__content) { background: #E53935; } }
.status-tag { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; padding: 2px 8px; border-radius: 10px; }
.status-tag .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.status-tag--normal { background: var(--tag-normal-bg); color: var(--tag-normal-text); }
.status-tag--normal .dot { background: var(--tag-normal-text); }
.status-tag--warning { background: var(--tag-warning-bg); color: var(--tag-warning-text); }
.status-tag--warning .dot { background: var(--tag-warning-text); }

.stat-value { margin-top: 12px; color: var(--color-text-1); }
.stat-value .num { font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat-value .unit { font-size: 13px; color: var(--color-text-3); margin-left: 2px; }
.stat-desc { margin-top: 8px; font-size: 12px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.trend { font-weight: 600; font-variant-numeric: tabular-nums; }
.trend--up { color: #E53935; }
.trend--down { color: #005E53; }
.sub { color: var(--color-text-3); }

/* 卡片网格 */
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
/* ECharts tooltip 渲染在卡片内部，el-card 默认 overflow:hidden 会裁掉边缘气泡；放开后完整显示 */
.card { overflow: visible; }
.card :deep(.el-card__header) { padding: 12px 16px; border-bottom: 1px solid var(--color-border-1); }
.card :deep(.el-card__body) { padding: 12px 16px 14px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.card-title { font-size: 14px; font-weight: 600; color: var(--color-text-1); }
.hint { font-size: 12px; color: var(--color-text-3); }
.lnk { font-size: 12px; color: var(--color-link); cursor: pointer; }
.chart { width: 100%; height: 200px; }
.chart-foot { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-top: 8px; font-size: 12px; color: var(--color-text-3); }

/* 告警级别分布 环形图 */
.donut-wrap { display: flex; align-items: center; gap: 16px; }
.donut-box { position: relative; width: 200px; height: 200px; flex-shrink: 0; }
.donut-chart { width: 200px; height: 200px; }
.donut-center { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; pointer-events: none; }
.donut-center__value { font-size: 26px; font-weight: 700; color: var(--color-text-1); font-variant-numeric: tabular-nums; }
.donut-center__label { font-size: 12px; color: var(--color-text-3); margin-top: 2px; }
.donut-legend { list-style: none; margin: 0; padding: 0; flex: 1; }
.donut-legend li { display: flex; align-items: center; gap: 8px; padding: 8px 0; font-size: 13px; color: var(--color-text-2); border-bottom: 1px dashed var(--color-border-1); }
.donut-legend li:last-child { border-bottom: 0; }
.lg-dot { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.lg-name { flex: 1; }
.lg-val { color: var(--color-text-3); font-variant-numeric: tabular-nums; }

/* 数据源健康度 */
.health-list { display: flex; flex-direction: column; gap: 14px; }
.health-row { display: flex; justify-content: space-between; align-items: center; font-size: 13px; }
.health-name { font-weight: 600; color: var(--color-text-1); }
.health-bar { margin-top: 8px; height: 6px; background: #EEF1F4; border-radius: 3px; overflow: hidden; }
.health-bar__fill { height: 100%; border-radius: 3px; background: #03857E; }
.health-bar__fill--normal { background: #03857E; }
.health-bar__fill--warning { background: #EA720F; }
.health-bar__fill--fault { background: #E53935; }
.health-note { margin: 14px 0 0; line-height: 1.6; }

.muted { color: var(--color-text-3); }
.dot--warning { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--tag-warning-text); margin-right: 4px; vertical-align: middle; }

@media (max-width: 1100px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
  .grid-2 { grid-template-columns: 1fr; }
}
</style>
