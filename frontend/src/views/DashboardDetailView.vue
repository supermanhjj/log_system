<script setup>
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft, Edit, Share, Refresh, Plus, Close, Check, Rank, Document,
  DataLine, PieChart, Histogram, TrendCharts, Grid, Delete, CopyDocument,
} from '@element-plus/icons-vue'
import * as echarts from 'echarts'
import { useAppStore } from '../stores/app'
import {
  getDashboard, updateDashboard, getDashboardMeta, queryWidgets,
} from '../api/dashboards'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

// —— 模板内纯函数 ——
function formatVal(v) { return v === undefined || v === null ? '—' : Number(v).toLocaleString('zh-CN') }
function filterText(flt) {
  if (!flt || !Object.keys(flt).length) return '全部'
  const parts = []
  if (flt.level?.length) parts.push(flt.level.join('/'))
  if (flt.source) parts.push(`source=${flt.source}`)
  if (flt.host) parts.push(`host=${flt.host}`)
  return parts.join(' · ') || '全部'
}
function fmtTime(s) { return s ? String(s).replace('T', ' ').slice(0, 19) : '' }

const dashId = Number(route.params.id)
const loading = ref(false)
const saving = ref(false)
const error = ref(false)
const editMode = ref(route.query.edit === '1')

const dashboard = reactive({
  id: dashId, name: '', owner: '', description: '',
  status: 'running', defaultRange: '近 24 小时', widgets: [],
})
const meta = reactive({ chartTypes: [], dimensions: [], ranges: [] })
const rangeLabel = ref('近 24 小时')
const dataMap = reactive({})          // widgetId -> 聚合结果
const updatedAt = ref(null)

// —— 字典辅助 ——
const chartTypeIcon = {
  kpi: DataLine, line: TrendCharts, area: TrendCharts, bar: Histogram,
  hbar: Rank, pie: PieChart, donut: PieChart, table: Document,
}
const dimsForType = (type) => meta.chartTypes.find((t) => t.value === type)?.dims || []
const dimLabel = (value) => meta.dimensions.find((d) => d.value === value)?.label || value
const typeLabel = (value) => meta.chartTypes.find((t) => t.value === typeSafe(value))?.label || value
function typeSafe(v) { return v }

// —— 取数 ——
async function loadMeta() {
  try { Object.assign(meta, await getDashboardMeta()) } catch { /* 字典失败不阻塞 */ }
}

async function loadDashboard() {
  loading.value = true
  error.value = false
  try {
    const d = await getDashboard(dashId)
    Object.assign(dashboard, d)
    rangeLabel.value = d.defaultRange || '近 24 小时'
    await loadChartData()
  } catch {
    error.value = true
    ElMessage.error('看板加载失败')
  } finally {
    loading.value = false
  }
}

let querySeq = 0
async function loadChartData(force = false) {
  if (!dashboard.widgets?.length) return
  const seq = ++querySeq
  try {
    const data = await queryWidgets({
      range_in: rangeLabel.value,
      tenant: app.tenant,
      widgets: dashboard.widgets,
      force,
    })
    if (seq !== querySeq) return
    for (const r of data.results) {
      if (r.id) dataMap[r.id] = r
    }
    updatedAt.value = data.updatedAt
    if (data.errors?.length) ElMessage.warning(`${data.errors.length} 个图表取数降级`)
    await nextTick()
    renderAll()
  } catch {
    ElMessage.error('图表数据加载失败')
  }
}

function onRefresh() { loadChartData(true); ElMessage.success('看板数据已刷新') }
function onRangeChange() { loadChartData() }

// —— ECharts 渲染 ——
const chartEls = new Map()
const chartInstances = new Map()
function setChartEl(id, el) {
  if (el) {
    chartEls.set(id, el)
    // 编辑态/查看态是两套 v-if DOM，切换时 Vue 会传入新节点。
    // 用实例实际绑定的 DOM 判断：若已绑到旧节点，销毁以便 renderAll 重新 init。
    const inst = chartInstances.get(id)
    if (inst && !inst.isDisposed?.() && inst.getDom() !== el) {
      inst.dispose()
      chartInstances.delete(id)
    }
  } else chartEls.delete(id)
}
function disposeAll() {
  for (const inst of chartInstances.values()) inst.dispose()
  chartInstances.clear()
}
function baseGrid() { return { left: 46, right: 14, top: 26, bottom: 28 } }
function axisBase() {
  return { axisLabel: { color: '#999', fontSize: 11 }, axisLine: { lineStyle: { color: '#BFBFBF' } } }
}

function optionFor(w, d) {
  if (!d) return null
  const isArea = w.type === 'area'
  if (w.type === 'line' || w.type === 'area') {
    const pts = d.points || []
    return {
      grid: baseGrid(),
      tooltip: { trigger: 'axis', appendToBody: true },
      xAxis: { type: 'category', data: pts.map((p) => p.t), boundaryGap: false, ...axisBase() },
      yAxis: { type: 'value', axisLabel: { color: '#999', fontSize: 11 }, splitLine: { lineStyle: { color: '#E7ECF2' } } },
      series: [{
        type: 'line', smooth: true, showSymbol: false, data: pts.map((p) => p.v),
        lineStyle: { color: '#03857E', width: 2 }, itemStyle: { color: '#03857E' },
        ...(isArea ? { areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(3,133,126,0.28)' }, { offset: 1, color: 'rgba(3,133,126,0.02)' },
        ]) } } : {}),
      }],
    }
  }
  if (w.type === 'bar') {
    const items = d.items || []
    return {
      grid: baseGrid(),
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, appendToBody: true },
      xAxis: { type: 'category', data: items.map((i) => i.name), ...axisBase(), axisLabel: { color: '#999', fontSize: 11, interval: 0, rotate: items.length > 6 ? 30 : 0 } },
      yAxis: { type: 'value', axisLabel: { color: '#999', fontSize: 11 }, splitLine: { lineStyle: { color: '#E7ECF2' } } },
      series: [{
        type: 'bar', barMaxWidth: 26,
        data: items.map((i) => ({ value: i.value, itemStyle: { color: i.color, borderRadius: [3, 3, 0, 0] } })),
      }],
    }
  }
  if (w.type === 'hbar') {
    const items = [...(d.items || [])].reverse()
    return {
      grid: { left: 96, right: 30, top: 12, bottom: 20 },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, appendToBody: true },
      xAxis: { type: 'value', axisLabel: { color: '#999', fontSize: 11 }, splitLine: { lineStyle: { color: '#E7ECF2' } } },
      yAxis: { type: 'category', data: items.map((i) => i.name), ...axisBase(), axisLabel: { color: '#666', fontSize: 11 } },
      series: [{
        type: 'bar', barMaxWidth: 18,
        data: items.map((i) => ({ value: i.value, itemStyle: { color: i.color, borderRadius: [0, 3, 3, 0] } })),
        label: { show: true, position: 'right', color: '#666', fontSize: 11 },
      }],
    }
  }
  if (w.type === 'pie' || w.type === 'donut') {
    const items = d.items || []
    return {
      tooltip: { trigger: 'item', appendToBody: true, formatter: '{b}：{c}（{d}%）' },
      legend: { orient: 'vertical', right: 4, top: 'middle', textStyle: { fontSize: 11, color: '#666' } },
      series: [{
        type: 'pie',
        radius: w.type === 'donut' ? ['48%', '70%'] : '62%',
        center: ['38%', '52%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        data: items.map((i) => ({ name: i.name, value: i.value, itemStyle: { color: i.color } })),
      }],
    }
  }
  return null
}

function renderAll() {
  for (const w of dashboard.widgets) {
    if (w.type === 'table' || w.type === 'kpi') continue
    const el = chartEls.get(w.id)
    if (!el) continue
    let inst = chartInstances.get(w.id)
    if (!inst || inst.isDisposed?.() || inst.getDom() !== el) {
      if (inst && !inst.isDisposed?.()) inst.dispose()
      inst = echarts.init(el)
      chartInstances.set(w.id, inst)
    }
    const opt = optionFor(w, dataMap[w.id])
    if (opt) inst.setOption(opt, true)
  }
}

function resizeAll() { for (const inst of chartInstances.values()) inst.resize() }
window.addEventListener('resize', resizeAll)

// —— 编辑：拖拽 ——
const dragType = ref('')        // 从组件库拖入的图表类型
const dragWidgetId = ref(null)  // 画布内被拖动排序的 widget
function onPaletteDragStart(e, type) {
  dragType.value = type
  dragWidgetId.value = null
  e.dataTransfer.effectAllowed = 'copy'
  e.dataTransfer.setData('text/plain', `new:${type}`)
}
function onWidgetDragStart(e, w) {
  dragType.value = ''
  dragWidgetId.value = w.id
  e.dataTransfer.effectAllowed = 'move'
  e.dataTransfer.setData('text/plain', `move:${w.id}`)
}
function onDragOver(e) { e.preventDefault(); e.dataTransfer.dropEffect = dragType.value ? 'copy' : 'move' }
function makeWidget(type) {
  const firstDim = dimsForType(type)[0] || 'none'
  return {
    id: `w${Date.now()}${Math.floor(Math.random() * 100)}`,
    type, title: defaultTitle(type),
    dimension: firstDim, filter: {}, limit: 10,
    metric: 'count', span: type === 'kpi' ? 1 : (type === 'table' ? 4 : 2),
  }
}
function defaultTitle(type) {
  return { kpi: '新指标卡', line: '新趋势图', area: '新面积图', bar: '新柱状图',
    hbar: '新排行', pie: '新占比图', donut: '新环形图', table: '日志明细表' }[type] || '新图表'
}
function onCanvasDrop(e) {
  e.preventDefault()
  if (dragType.value) {
    const w = makeWidget(dragType.value)
    dashboard.widgets.push(w)
    selectWidget(w)
    ElMessage.success(`已添加${typeLabel(w.type)}`)
  }
  dragType.value = ''
  dragWidgetId.value = null
}
function onWidgetDrop(e, target) {
  e.preventDefault()
  e.stopPropagation()
  if (dragWidgetId.value && dragWidgetId.value !== target.id) {
    const from = dashboard.widgets.findIndex((w) => w.id === dragWidgetId.value)
    const to = dashboard.widgets.findIndex((w) => w.id === target.id)
    if (from > -1 && to > -1) {
      const [moved] = dashboard.widgets.splice(from, 1)
      dashboard.widgets.splice(to, 0, moved)
    }
  } else if (dragType.value) {
    const w = makeWidget(dragType.value)
    const to = dashboard.widgets.findIndex((x) => x.id === target.id)
    dashboard.widgets.splice(to, 0, w)
    selectWidget(w)
    dragType.value = ''
  }
  dragWidgetId.value = null
  scheduleRecompute()
}
function onDragEnd() { dragType.value = ''; dragWidgetId.value = null }

// —— 编辑：选中与配置 ——
const selectedId = ref(null)
const selected = computed(() => dashboard.widgets.find((w) => w.id === selectedId.value) || null)
function selectWidget(w) { selectedId.value = w.id }
function removeWidget(w) {
  const i = dashboard.widgets.findIndex((x) => x.id === w.id)
  if (i > -1) dashboard.widgets.splice(i, 1)
  if (selectedId.value === w.id) selectedId.value = null
  chartEls.delete(w.id)
  const inst = chartInstances.get(w.id)
  if (inst) { inst.dispose(); chartInstances.delete(w.id) }
  scheduleRecompute()
}
function onTypeChange(w) {
  const allowed = dimsForType(w.type)
  if (!allowed.includes(w.dimension)) w.dimension = allowed[0]
  if (w.type === 'table') w.dimension = 'records'
  w.span = w.type === 'kpi' ? 1 : (w.type === 'table' ? 4 : (w.span === 1 ? 2 : w.span))
  scheduleRecompute()
}
function onDimChange(w) { scheduleRecompute() }
function onFilterChange(w) { scheduleRecompute() }

let recomputeTimer = null
function scheduleRecompute() {
  clearTimeout(recomputeTimer)
  recomputeTimer = setTimeout(() => loadChartData(), 250)
}

// —— 筛选态映射（编辑面板）——
const levelOptions = ['FATAL', 'ERROR', 'WARN', 'INFO', 'DEBUG', 'NOTICE', 'TRACE']
const metricOptions = [
  { value: 'count', label: '日志条数' },
  { value: 'distinct_host', label: '去重主机数' },
  { value: 'distinct_source', label: '去重来源数' },
  { value: 'error_rate', label: '错误占比(%)' },
]

// —— 保存 / 分享 / 返回 ——
async function save() {
  saving.value = true
  try {
    await updateDashboard(dashId, {
      name: dashboard.name, owner: dashboard.owner,
      description: dashboard.description, status: dashboard.status,
      defaultRange: rangeLabel.value, widgets: dashboard.widgets,
    })
    ElMessage.success('看板配置已保存')
    editMode.value = false
    router.replace(`/dashboards/${dashId}`)
    await nextTick(); renderAll()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}
function enterEdit() { editMode.value = true; nextTick(() => renderAll()) }
function cancelEdit() {
  editMode.value = false
  // 同步清掉 URL 上的 edit=1，否则刷新后 route.query.edit 仍为 '1'，又回到编辑态。
  if (route.query.edit) router.replace(`/dashboards/${dashId}`)
  // 取消丢弃本地改动，从后端重载配置与数据；loadChartData 内会 renderAll 重绘。
  loadDashboard()
}
async function share() {
  try {
    await navigator.clipboard.writeText(window.location.href)
    ElMessage.success('分享链接已复制')
  } catch {
    ElMessage.info(`分享链接：${window.location.href}`)
  }
}
function goBack() { router.push('/dashboards') }

// 配置变化（非拖拽）也轻量重算：仅监听影响取数的字段
watch(() => JSON.stringify(dashboard.widgets.map((w) => [w.id, w.dimension, w.filter, w.limit, w.metric])),
  () => { if (editMode.value) scheduleRecompute() })

onMounted(async () => {
  await loadMeta()
  await loadDashboard()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeAll)
  clearTimeout(recomputeTimer)
  disposeAll()
})
</script>

<template>
  <div class="page" v-loading="loading">
    <!-- 页头 -->
    <div class="page-head">
      <div class="breadcrumb">
        <span>日志分析</span><span class="sep">/</span>
        <a @click="goBack">仪表盘</a><span class="sep">/</span>
        <span class="cur">{{ dashboard.name || '看板详情' }}</span>
      </div>
      <div class="head-bar">
        <div>
          <h1 class="title">{{ dashboard.name }}</h1>
          <p class="desc">负责人 {{ dashboard.owner }} · 共 {{ dashboard.widgets.length }} 个图表</p>
        </div>
        <div class="head-actions">
          <template v-if="!editMode">
            <el-select v-model="rangeLabel" style="width: 130px" @change="onRangeChange">
              <el-option v-for="r in (meta.ranges?.length ? meta.ranges : ['近 1 小时','近 24 小时','近 7 天'])" :key="r" :label="r" :value="r" />
            </el-select>
            <el-button :icon="ArrowLeft" @click="goBack">返回</el-button>
            <el-button :icon="Edit" @click="enterEdit">编辑</el-button>
            <el-button :icon="Share" @click="share">分享</el-button>
            <el-button type="primary" :icon="Refresh" @click="onRefresh">刷新</el-button>
          </template>
          <template v-else>
            <el-tag type="info" effect="plain" class="editing-tag">编辑模式</el-tag>
            <el-button :icon="Close" @click="cancelEdit">取消</el-button>
            <el-button type="primary" :icon="Check" :loading="saving" @click="save">保存</el-button>
          </template>
        </div>
      </div>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon title="看板加载失败" description="请稍后重试或返回列表" />

    <!-- ===== 查看模式 ===== -->
    <template v-if="!editMode">
      <div v-if="!dashboard.widgets.length" class="empty-box">
        <el-empty description="该看板暂无图表，点击「编辑」添加">
          <el-button type="primary" @click="enterEdit">编辑看板</el-button>
        </el-empty>
      </div>
      <div class="widget-grid">
        <div
          v-for="w in dashboard.widgets" :key="w.id"
          class="widget" :class="`widget--span${w.span || 2}`"
        >
          <!-- 指标卡 -->
          <div v-if="w.type === 'kpi'" class="kpi-widget">
            <div class="kpi-label">{{ w.title }}</div>
            <div class="kpi-value">{{ formatVal(dataMap[w.id]?.value) }}<span class="kpi-unit">{{ dataMap[w.id]?.unit || '条' }}</span></div>
            <div class="kpi-foot">{{ dimLabel(w.dimension) }} · {{ filterText(w.filter) }}</div>
          </div>
          <!-- 明细表 -->
          <div v-else-if="w.type === 'table'" class="chart-widget">
            <div class="chart-title">{{ w.title }}</div>
            <el-table :data="dataMap[w.id]?.rows || []" size="small" stripe class="detail-table">
              <el-table-column prop="time" label="时间" width="170" />
              <el-table-column prop="level" label="级别" width="90">
                <template #default="{ row }">
                  <span class="level-pill" :class="`lv-${String(row.level).toLowerCase()}`">{{ row.level }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="source" label="来源" width="140" />
              <el-table-column prop="host" label="主机" width="140" />
              <el-table-column prop="message" label="内容" min-width="260" show-overflow-tooltip />
            </el-table>
          </div>
          <!-- ECharts 图表 -->
          <div v-else class="chart-widget">
            <div class="chart-title">{{ w.title }}</div>
            <div :ref="(el) => setChartEl(w.id, el)" class="chart-canvas"></div>
            <div class="chart-foot">
              <span>维度：{{ dimLabel(w.dimension) }}</span>
              <span v-if="w.filter && Object.keys(w.filter).length">筛选：{{ filterText(w.filter) }}</span>
            </div>
          </div>
        </div>
      </div>
      <div class="board-foot" v-if="updatedAt">
        数据截至 {{ fmtTime(updatedAt) }} · 每 60 秒可手动刷新 · 数据来自 OpenSearch 实时聚合
      </div>
    </template>

    <!-- ===== 编辑模式：三栏 ===== -->
    <template v-else>
      <div class="editor">
        <!-- 左：组件库 -->
        <aside class="palette">
          <div class="panel-head">图表组件</div>
          <p class="palette-hint">拖拽到右侧画布添加图表</p>
          <div
            v-for="t in meta.chartTypes" :key="t.value"
            class="palette-item" draggable="true"
            @dragstart="onPaletteDragStart($event, t.value)" @dragend="onDragEnd"
          >
            <el-icon><component :is="chartTypeIcon[t.value] || Grid" /></el-icon>
            <span>{{ t.label }}</span>
            <span class="palette-dim">{{ t.dims.slice(0, 2).map(dimLabel).join('/') }}{{ t.dims.length > 2 ? '…' : '' }}</span>
          </div>
        </aside>

        <!-- 中：画布 -->
        <section class="canvas" @dragover="onDragOver" @drop="onCanvasDrop">
          <div class="canvas-head">
            <span>看板画布</span>
            <span class="hint">{{ dashboard.widgets.length }} 个组件 · 拖拽排序 · 右侧配置宽度</span>
          </div>
          <div class="widget-grid edit-grid">
            <div
              v-for="w in dashboard.widgets" :key="w.id"
              class="widget edit-widget" :class="[
                `widget--span${w.span || 2}`,
                { 'is-selected': selectedId === w.id },
              ]"
              draggable="true"
              @dragstart="onWidgetDragStart($event, w)" @dragend="onDragEnd"
              @dragover="onDragOver" @drop="onWidgetDrop($event, w)"
              @click="selectWidget(w)"
            >
              <div class="edit-widget__bar">
                <span class="edit-widget__type">{{ typeLabel(w.type) }} · {{ w.title }}</span>
                <span class="edit-widget__ops">
                  <el-button link size="small" @click.stop="removeWidget(w)"><el-icon><Delete /></el-icon></el-button>
                </span>
              </div>
              <!-- 编辑态内依然渲染真实数据预览 -->
              <div v-if="w.type === 'kpi'" class="kpi-widget kpi-widget--preview">
                <div class="kpi-label">{{ w.title }}</div>
                <div class="kpi-value">{{ formatVal(dataMap[w.id]?.value) }}<span class="kpi-unit">{{ dataMap[w.id]?.unit || '条' }}</span></div>
              </div>
              <div v-else-if="w.type === 'table'" class="chart-widget preview">
                <div class="chart-title">{{ w.title }}（预览 {{ dataMap[w.id]?.rows?.length || 0 }} 行）</div>
              </div>
              <div v-else class="chart-widget preview">
                <div :ref="(el) => setChartEl(w.id, el)" class="chart-canvas chart-canvas--preview"></div>
              </div>
            </div>
          </div>
          <div v-if="!dashboard.widgets.length" class="canvas-empty">
            <el-icon><Plus /></el-icon>
            <span>从左侧拖入图表组件开始配置</span>
          </div>
        </section>

        <!-- 右：配置面板 -->
        <aside class="config">
          <div class="panel-head">组件配置</div>
          <template v-if="selected">
            <el-form :model="selected" label-width="76px" size="small" class="config-form">
              <el-form-item label="标题">
                <el-input v-model="selected.title" maxlength="20" />
              </el-form-item>
              <el-form-item label="图表类型">
                <el-select v-model="selected.type" @change="onTypeChange(selected)">
                  <el-option v-for="t in meta.chartTypes" :key="t.value" :label="t.label" :value="t.value" />
                </el-select>
              </el-form-item>
              <el-form-item label="展示维度">
                <el-select v-model="selected.dimension" :disabled="selected.type === 'table'" @change="onDimChange(selected)">
                  <el-option v-for="dv in dimsForType(selected.type)" :key="dv" :label="dimLabel(dv)" :value="dv" />
                </el-select>
              </el-form-item>
              <el-form-item v-if="selected.type !== 'table'" label="统计指标">
                <el-select v-model="selected.metric" @change="onFilterChange(selected)">
                  <el-option v-for="m in metricOptions" :key="m.value" :label="m.label" :value="m.value" />
                </el-select>
              </el-form-item>
              <el-form-item label="组件宽度">
                <el-radio-group v-model="selected.span">
                  <el-radio-button :value="1">1/4</el-radio-button>
                  <el-radio-button :value="2">1/2</el-radio-button>
                  <el-radio-button :value="4">整行</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="级别筛选">
                <el-select v-model="selected.filter.level" multiple collapse-tags collapse-tags-tooltip clearable placeholder="全部级别" @change="onFilterChange(selected)">
                  <el-option v-for="lv in levelOptions" :key="lv" :label="lv" :value="lv" />
                </el-select>
              </el-form-item>
              <el-form-item label="来源服务">
                <el-input v-model="selected.filter.source" placeholder="精确匹配，留空全部" clearable @change="onFilterChange(selected)" />
              </el-form-item>
              <el-form-item label="主机">
                <el-input v-model="selected.filter.host" placeholder="精确匹配，留空全部" clearable @change="onFilterChange(selected)" />
              </el-form-item>
              <el-form-item label="取数条数">
                <el-input-number v-model="selected.limit" :min="1" :max="50" size="small" @change="onFilterChange(selected)" />
              </el-form-item>
            </el-form>
            <div class="config-tip">
              <el-icon><CopyDocument /></el-icon>
              <span>维度决定 X 轴/分组，指标决定数值口径；保存后看板按此配置实时取数。</span>
            </div>
          </template>
          <el-empty v-else description="点击画布中的图表进行配置" :image-size="60" />
        </aside>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.breadcrumb { font-size: 12px; color: var(--color-text-3); }
.breadcrumb .sep { margin: 0 6px; }
.breadcrumb a { color: var(--color-link); cursor: pointer; }
.breadcrumb .cur { color: var(--color-text-2); }
.head-bar { display: flex; justify-content: space-between; align-items: flex-end; margin-top: 8px; gap: 12px; }
.title { font-size: 18px; font-weight: 600; color: var(--color-text-1); margin: 0; }
.desc { font-size: 13px; color: var(--color-text-3); margin: 4px 0 0; }
.head-actions { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
.editing-tag { font-size: 12px; }
.empty-box { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 24px; }

/* —— 图表网格 —— */
.widget-grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
.widget { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; min-width: 0; overflow: hidden; }
.widget--span1 { grid-column: span 3; }
.widget--span2 { grid-column: span 6; }
.widget--span4 { grid-column: span 12; }

.kpi-widget { padding: 16px 18px; height: 100%; }
.kpi-label { font-size: 13px; color: var(--color-text-2); }
.kpi-value { margin-top: 10px; font-size: 28px; font-weight: 700; color: var(--color-text-1); font-variant-numeric: tabular-nums; }
.kpi-unit { font-size: 13px; color: var(--color-text-3); margin-left: 3px; font-weight: 400; }
.kpi-foot { margin-top: 8px; font-size: 12px; color: var(--color-text-3); }

.chart-widget { padding: 12px 16px 14px; height: 100%; }
.chart-title { font-size: 14px; font-weight: 600; color: var(--color-text-1); margin-bottom: 8px; }
.chart-canvas { width: 100%; height: 220px; }
.chart-canvas--preview { height: 180px; }
.chart-foot { display: flex; gap: 16px; margin-top: 8px; font-size: 12px; color: var(--color-text-3); }
.detail-table { width: 100%; }
.level-pill { font-size: 11px; padding: 1px 7px; border-radius: 9px; background: #E8F3F1; color: #258C89; }
.level-pill.lv-error, .level-pill.lv-fatal { background: #F8BABA; color: #E81A1A; }
.level-pill.lv-warn { background: #FFDFB8; color: #FE9311; }
.board-foot { font-size: 12px; color: var(--color-text-3); text-align: right; padding: 0 2px; }

/* —— 编辑三栏 —— */
.editor { display: grid; grid-template-columns: 210px minmax(0, 1fr) 280px; gap: 12px; align-items: start; }
.palette, .config { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 12px; min-height: 480px; }
.panel-head { font-size: 14px; font-weight: 600; color: var(--color-text-1); padding-bottom: 10px; border-bottom: 1px solid var(--color-border-1); }
.palette-hint { font-size: 12px; color: var(--color-text-3); margin: 10px 0; }
.palette-item {
  display: flex; align-items: center; gap: 8px; padding: 9px 10px; margin-bottom: 8px;
  border: 1px dashed var(--color-border-2); border-radius: 4px; cursor: grab;
  font-size: 13px; background: #FAFBFC; color: var(--color-text-2);
}
.palette-item:hover { border-color: var(--color-primary); color: var(--color-primary); background: #F1FAF9; }
.palette-item:active { cursor: grabbing; }
.palette-dim { margin-left: auto; font-size: 10px; color: var(--color-text-4); }

.canvas { background: transparent; min-height: 480px; }
.canvas-head { display: flex; justify-content: space-between; align-items: center; background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 10px 14px; margin-bottom: 12px; font-size: 14px; font-weight: 600; }
.canvas-head .hint { font-size: 12px; font-weight: 400; color: var(--color-text-3); }
.canvas-empty {
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px;
  min-height: 360px; border: 2px dashed var(--color-border-2); border-radius: 4px;
  color: var(--color-text-3); font-size: 13px; background: #FAFBFC;
}
.edit-grid { align-content: start; }
.edit-widget { cursor: grab; border: 2px solid transparent; position: relative; }
.edit-widget:hover { border-color: var(--color-primary-light-5); }
.edit-widget.is-selected { border-color: var(--color-primary); box-shadow: 0 0 0 2px rgba(3,133,126,0.12); }
.edit-widget__bar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 10px; background: #F1FAF9; border-bottom: 1px solid var(--color-border-1);
  font-size: 12px; color: var(--color-text-2);
}
.edit-widget__type { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.edit-widget__ops { display: flex; }
.preview { pointer-events: none; }

.config-form { margin-top: 14px; }
.config-form :deep(.el-form-item) { margin-bottom: 12px; }
.config-form :deep(.el-form-item__label) { font-size: 12px; }
.config-tip { display: flex; gap: 6px; margin-top: 8px; padding: 10px; background: #F1FAF9; border-radius: 4px; font-size: 11px; color: var(--color-text-3); line-height: 1.6; }
.config-tip .el-icon { flex-shrink: 0; margin-top: 2px; color: var(--color-primary); }

@media (max-width: 1300px) {
  .editor { grid-template-columns: 170px minmax(0, 1fr) 250px; }
}
</style>
