<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { Search, RefreshLeft, Download, DocumentCopy } from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import { searchQuery, searchHistogram, searchPql, searchContext } from '../api/search'

const app = useAppStore()
const route = useRoute()

const QUICK = [
  { key: '1h', label: '近 1 小时', ms: 3600e3, interval: '1m' },
  { key: '1d', label: '近 1 天', ms: 86400e3, interval: '1h' },
  { key: '1w', label: '近 1 周', ms: 7 * 86400e3, interval: '1d' },
  { key: '1m', label: '近 1 月', ms: 30 * 86400e3, interval: '1d' },
  { key: '1y', label: '近 1 年', ms: 365 * 86400e3, interval: '1M' },
]
const quickKey = ref('1d')
// 自定义时间范围：开始/结束各一个独立日期时间选择器
const customStart = ref(null)
const customEnd = ref(null)
const customRange = computed(() => (customStart.value && customEnd.value ? [customStart.value, customEnd.value] : null))
const usingCustom = computed(() => quickKey.value === 'custom' && !!customRange.value)
const customRangeLabel = computed(() => {
  if (!usingCustom.value) return ''
  const f = (d) => {
    const dt = new Date(d)
    const p = (n) => String(n).padStart(2, '0')
    return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}`
  }
  return `${f(customRange.value[0])} ~ ${f(customRange.value[1])}`
})

function quickDef() {
  return QUICK.find((x) => x.key === quickKey.value) || QUICK[1]
}
// 自定义区间按跨度自动选直方图粒度
function computeIntervalForSpan(ms) {
  const H = 3600e3, D = 86400e3
  if (ms <= 2 * H) return '5m'
  if (ms <= 12 * H) return '15m'
  if (ms <= 2 * D) return '1h'
  if (ms <= 7 * D) return '6h'
  return '1d'
}
const effectiveInterval = computed(() => {
  if (usingCustom.value) {
    const span = new Date(customRange.value[1]).getTime() - new Date(customRange.value[0]).getTime()
    return computeIntervalForSpan(span)
  }
  return quickDef().interval || '1h'
})
// x 轴时间格式：time=HH:MM，date=MM-DD，year=YYYY-MM
const timeFormatKey = computed(() => {
  if (usingCustom.value) {
    const span = new Date(customRange.value[1]).getTime() - new Date(customRange.value[0]).getTime()
    const D = 86400e3
    if (span > 60 * D) return 'year'
    if (span > 2 * D) return 'date'
    return 'time'
  }
  const k = quickKey.value
  if (k === '1y') return 'year'
  if (k === '1w' || k === '1m') return 'date'
  return 'time'
})

const splText = ref('')
const loading = ref(false)
const page = ref(1)
const pageSize = ref(20)
function onPageSizeChange() { page.value = 1; doSearch() }

const tableData = ref([])
const total = ref(0)
const took = ref(0)
const selectedRows = ref([])

const histogramBuckets = ref([])
const chartEl = ref(null)
let chart = null
const zoomRange = ref(null)

const FIELDS = [
  { key: 'time', label: '时间', fixed: true },
  { key: 'level', label: '级别' },
  { key: 'source', label: '来源' },
  { key: 'host', label: '主机' },
  { key: 'traceId', label: 'traceId' },
  { key: 'status', label: '状态' },
  { key: 'tenant_id', label: '租户' },
  { key: 'message', label: '内容', fixed: true },
]
const visibleFields = ref(['time', 'level', 'source', 'host', 'message'])
const isVisible = (k) => visibleFields.value.includes(k)
const toggleField = (k) => {
  const f = FIELDS.find((x) => x.key === k)
  if (!f || f.fixed) return
  const i = visibleFields.value.indexOf(k)
  if (i >= 0) visibleFields.value.splice(i, 1)
  else visibleFields.value.push(k)
}
const fieldCoverage = (k) => {
  if (!tableData.value.length) return 0
  return tableData.value.filter((r) => r[k] !== undefined && r[k] !== null && r[k] !== '').length
}

const drawerVisible = ref(false)
const currentRow = ref(null)

// 上下文日志（原始日志上/下各 N 条）
const ctxBefore = ref([])
const ctxAfter = ref([])
const ctxLoading = ref(false)
const rawLog = computed(() => {
  const r = currentRow.value
  if (!r) return ''
  // 优先用采集端保留的原始整行；缺省（老数据无 raw）再回退合成
  return r.raw || `${r.time} ${r.level} ${r.source} [${r.host}] ${r.message}`
})
// 上下文条目同样取原始整行
function ctxLine(c) {
  return c.raw || `${c.time} ${c.level} ${c.source} [${c.host}] ${c.message}`
}

// PQL（管道语法）结果
const pqlResult = ref(null)
const pqlCols = ref([])
const pqlRows = ref([])
const pqlLoading = ref(false)

// 是否为 PPL 模式：输入含管道符 | 即走 OpenSearch /_plugins/_pql
const isPql = computed(() => /\|/.test(splText.value))

const CHIPS = [
  'level:ERROR',
  'status>=500',
  "source=logs-default | where level='ERROR'",
  'source=logs-default | stats count() by level',
]

function intervalMs(iv) {
  const m = { m: 60000, h: 3600000, d: 86400000, M: 30 * 86400000 }
  const u = iv.slice(-1)
  const n = parseInt(iv.slice(0, -1) || '1', 10)
  return (m[u] || m.h) * n
}

function buildTimeRange() {
  if (zoomRange.value) return zoomRange.value
  if (usingCustom.value) {
    const [from, to] = customRange.value
    return { from: new Date(from).toISOString(), to: new Date(to).toISOString() }
  }
  const q = quickDef()
  const to = new Date()
  const from = new Date(to.getTime() - q.ms)
  return { from: from.toISOString(), to: to.toISOString() }
}

function parseSpl(raw) {
  const text = (raw || '').trim()
  const filters = {}
  if (!text) return { filters }
  let m
  if ((m = text.match(/\blevel\s*:\s*(DEBUG|INFO|WARN|ERROR|FATAL)/i))) filters.level = m[1].toUpperCase()
  if ((m = text.match(/\bsource\s*:\s*([\w.\-]+)/i))) filters.source = m[1]
  if ((m = text.match(/\bhost\s*:\s*([\w.\-]+)/i))) filters.host = m[1]
  if ((m = text.match(/\btraceId\s*:\s*([\w]+)/i))) filters.traceId = m[1]
  if ((m = text.match(/\bstatus\s*(>=|<=|>|<|=)\s*(\d+)/i))) {
    const op = m[1]
    const v = parseInt(m[2], 10)
    if (op === '>=') filters.statusGte = v
    else if (op === '<=') filters.statusLte = v
    else if (op === '>') filters.statusGte = v + 1
    else if (op === '<') filters.statusLte = v - 1
    else filters.status = v
  }
  let kw = text
    .replace(/\b(level|source|host|traceId|status)\s*[:><=]+\s*[\w.\-]+/gi, ' ')
    .replace(/\b(AND|OR)\b/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  if (kw) filters.keyword = kw
  return { filters }
}

function buildPayload() {
  return {
    tenant: app.tenant,
    timeRange: buildTimeRange(),
    filters: parseSpl(splText.value).filters,
    page: page.value,
    pageSize: pageSize.value,
  }
}

async function runQuery() {
  loading.value = true
  try {
    const payload = buildPayload()
    const [q, h] = await Promise.all([
      searchQuery(payload),
      searchHistogram({ ...payload, interval: effectiveInterval.value }),
    ])
    tableData.value = q.hits
    total.value = q.total
    took.value = q.took_ms
    histogramBuckets.value = h.buckets
  } catch (e) {
    ElMessage.error('检索失败：' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

async function runPql() {
  pqlLoading.value = true
  pqlResult.value = null
  try {
    const res = await searchPql({ pql: splText.value, tenant: app.tenant })
    pqlResult.value = res
    pqlCols.value = (res.schema || []).map((s) => s.name)
    pqlRows.value = (res.datarows || []).map((r) => {
      const o = {}
      ;(res.schema || []).forEach((s, i) => {
        o[s.name] = r[i]
      })
      return o
    })
  } catch (e) {
    ElMessage.error('PQL 执行失败：' + (e.response?.data?.detail || e.message))
  } finally {
    pqlLoading.value = false
  }
}

async function doSearch() {
  if (isPql.value) await runPql()
  else await runQuery()
}

function onChip(text) {
  splText.value = text
  doSearch()
}

function resetAll() {
  splText.value = ''
  zoomRange.value = null
  pqlResult.value = null
  page.value = 1
  doSearch()
}

function onChartClick(params) {
  const b = histogramBuckets.value[params?.dataIndex]
  if (!b) return
  const toMs = new Date(b.time).getTime() + intervalMs(effectiveInterval.value)
  zoomRange.value = { from: b.time, to: new Date(toMs).toISOString() }
  page.value = 1
  doSearch()
}
function onQuickChange() {
  if (quickKey.value !== 'custom') {
    customStart.value = null
    customEnd.value = null
  }
  page.value = 1
  doSearch()
}
function onCustomChange() {
  if (customStart.value && customEnd.value) {
    quickKey.value = 'custom'
  } else {
    quickKey.value = '1d'
  }
  page.value = 1
  doSearch()
}
function clearCustomRange() {
  customStart.value = null
  customEnd.value = null
  quickKey.value = '1d'
  page.value = 1
  doSearch()
}
function resetZoom() {
  if (!zoomRange.value) return
  zoomRange.value = null
  doSearch()
}

function levelTagType(level) {
  if (level === 'ERROR' || level === 'FATAL') return 'danger'
  if (level === 'WARN') return 'warning'
  if (level === 'INFO' || level === 'DEBUG') return 'success'
  return 'info'
}

function openDetail(row) {
  currentRow.value = row
  drawerVisible.value = true
  loadContext(row)
  // 若"更多上下文"框正开着，随锚点切换同步重载
  if (moreVisible.value) openMore()
}
async function loadContext(row) {
  ctxBefore.value = []
  ctxAfter.value = []
  if (!row || !row.ts) return
  ctxLoading.value = true
  try {
    const res = await searchContext({
      tenant: app.tenant,
      time: row.ts,
      source: row.source,
      before: 2,
      after: 2,
    })
    ctxBefore.value = res.before || []
    ctxAfter.value = res.after || []
  } catch (e) {
    ElMessage.error('上下文加载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    ctxLoading.value = false
  }
}
// 点击上下文中的某条日志，切换为当前详情并重新加载其上下文
function openContext(row) {
  openDetail(row)
}

// ---- 更多上下文日志框：锚点高亮 + 滚动到边缘按需追加 ----
// /context 以锚点时间为界取前 before / 后 after 条，扩大条数即可幂等地"加载更多"，
// 无需游标，也不会出现同毫秒日志的重复或遗漏。
const MORE_PAGE = 20
const MORE_MAX = 200
const moreVisible = ref(false)
const moreBody = ref(null)
const moreLines = ref([]) // 时间正序：[更早 ...] → 锚点 → [更晚 ...]
const moreLoading = ref(false)
const moreBeforeCount = ref(MORE_PAGE)
const moreAfterCount = ref(MORE_PAGE)
const moreBeforeEnd = ref(false)
const moreAfterEnd = ref(false)
// 默认取全局时间流（跨来源），排查问题时能看到同一时刻其它服务的日志
const moreSameSource = ref(false)

function moreLine(c) {
  return c.raw || `${c.time} ${c.level} ${c.source} [${c.host}] ${c.message}`
}

async function fetchMoreLines() {
  const row = currentRow.value
  if (!row || !row.ts) return
  const req = {
    tenant: app.tenant,
    time: row.ts,
    before: moreBeforeCount.value,
    after: moreAfterCount.value,
  }
  if (moreSameSource.value && row.source) req.source = row.source
  const res = await searchContext(req)
  const before = res.before || []
  const after = res.after || []
  // 返回不足请求条数 → 该方向已无更多日志
  moreBeforeEnd.value = before.length < moreBeforeCount.value || moreBeforeCount.value >= MORE_MAX
  moreAfterEnd.value = after.length < moreAfterCount.value || moreAfterCount.value >= MORE_MAX
  moreLines.value = [
    ...before.map((c) => ({ ...c, _anchor: false })),
    { ...row, raw: rawLog.value, _anchor: true },
    ...after.map((c) => ({ ...c, _anchor: false })),
  ]
}

function scrollToAnchor() {
  const el = moreBody.value
  const a = el?.querySelector('.more-line--anchor')
  if (!el || !a) return
  const top = a.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop
  el.scrollTop = top - el.clientHeight / 2 + a.clientHeight / 2
}

async function openMore() {
  if (!currentRow.value?.ts) return
  moreBeforeCount.value = MORE_PAGE
  moreAfterCount.value = MORE_PAGE
  moreBeforeEnd.value = false
  moreAfterEnd.value = false
  moreLines.value = []
  moreVisible.value = true
  moreLoading.value = true
  try {
    await fetchMoreLines()
    await nextTick()
    scrollToAnchor()
  } catch (e) {
    ElMessage.error('更多上下文加载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    moreLoading.value = false
  }
}

// 向上滚到顶 / 向下滚到底各追加一页；before 段插在顶部，需补偿新增高度才能停住不跳
async function loadMore(dir) {
  if (moreLoading.value) return
  if (dir === 'before' && (moreBeforeEnd.value || moreBeforeCount.value >= MORE_MAX)) return
  if (dir === 'after' && (moreAfterEnd.value || moreAfterCount.value >= MORE_MAX)) return
  const el = moreBody.value
  if (!el) return
  const oldH = el.scrollHeight
  const oldTop = el.scrollTop
  moreLoading.value = true
  if (dir === 'before') moreBeforeCount.value += MORE_PAGE
  else moreAfterCount.value += MORE_PAGE
  try {
    await fetchMoreLines()
    await nextTick()
    el.scrollTop = dir === 'before' ? oldTop + (el.scrollHeight - oldH) : oldTop
  } catch (e) {
    ElMessage.error('加载更多失败：' + (e.response?.data?.detail || e.message))
  } finally {
    moreLoading.value = false
  }
}

function onMoreScroll(e) {
  const el = e.target
  if (moreLoading.value) return
  if (el.scrollTop <= 8) loadMore('before')
  else if (el.scrollTop + el.clientHeight >= el.scrollHeight - 8) loadMore('after')
}

async function onSameSourceChange() {
  moreBeforeCount.value = MORE_PAGE
  moreAfterCount.value = MORE_PAGE
  moreBeforeEnd.value = false
  moreAfterEnd.value = false
  moreLoading.value = true
  try {
    await fetchMoreLines()
    await nextTick()
    scrollToAnchor()
  } catch (e) {
    ElMessage.error('切换失败：' + (e.response?.data?.detail || e.message))
  } finally {
    moreLoading.value = false
  }
}

async function copyTraceId() {
  try {
    await navigator.clipboard.writeText(currentRow.value?.traceId || '')
    ElMessage.success('已复制 traceId')
  } catch {
    ElMessage.error('复制失败')
  }
}

function onSelectionChange(rows) {
  selectedRows.value = rows
}
function exportSelected() {
  if (!selectedRows.value.length) {
    ElMessage.warning('请先勾选日志')
    return
  }
  const header = ['time', 'level', 'source', 'host', 'traceId', 'status', 'message']
  const lines = selectedRows.value.map((r) => header.map((h) => `"${(r[h] ?? '').toString().replace(/"/g, '""')}"`).join(','))
  const csv = [header.join(','), ...lines].join('\n')
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = 'logs-export.csv'
  a.click()
  URL.revokeObjectURL(a.href)
  ElMessage.success(`已导出 ${selectedRows.value.length} 条`)
}
function copyStatement() {
  const stmt = splText.value || '（无检索条件）'
  navigator.clipboard?.writeText(stmt)
  ElMessage.success('已复制检索语句')
}
function onExport() {
  ElMessage.info('导出结果（上限 5,000 条）规划中')
}

function renderChart() {
  if (!chart) return
  const buckets = histogramBuckets.value
  // x 轴标签按时间范围自适应：时/天→HH:MM，周/月→MM-DD，年→YYYY-MM
  const fmt = (t) => {
    if (timeFormatKey.value === 'year') return (t || '').slice(0, 7)
    if (timeFormatKey.value === 'date') return (t || '').slice(5, 10)
    return (t || '').slice(11, 16)
  }
  chart.setOption({
    grid: { left: 44, right: 16, top: 16, bottom: 28 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: {
      type: 'category',
      data: buckets.map((b) => fmt(b.time)),
      axisLabel: { color: '#999', fontSize: 11, interval: 'auto' },
      axisLine: { lineStyle: { color: '#BFBFBF' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#999', fontSize: 11 },
      splitLine: { lineStyle: { color: '#E7ECF2' } },
    },
    series: [
      {
        type: 'bar',
        data: buckets.map((b) => b.count),
        itemStyle: { color: '#03857E', borderRadius: [2, 2, 0, 0] },
        barMaxWidth: 18,
      },
    ],
  })
}
function resizeChart() {
  chart && chart.resize()
}

watch(histogramBuckets, () => renderChart())
watch(isPql, (now) => {
  // 从 PQL 模式退回检索模式时，卡片由 display:none 转回显示，容器尺寸可能未更新，resize 兜底
  if (!now) nextTick(resizeChart)
})
watch(
  () => app.tenant,
  () => {
    page.value = 1
    doSearch()
  },
)

// 从告警事件「查看关联日志」跳转而来：回填 PPL 语句与时间范围并自动检索。
// PPL 由后端按「规则配置 + 索引真实字段」自适应生成，note 为其分析/降级说明。
const relatedNote = ref('')
function applyIncomingQuery() {
  const rq = route.query
  if (!rq.ppl) {
    relatedNote.value = ''
    return false
  }
  splText.value = String(rq.ppl)
  relatedNote.value = rq.note ? String(rq.note) : ''
  if (rq.from || rq.to) {
    customStart.value = rq.from ? new Date(Number(rq.from)) : null
    customEnd.value = rq.to ? new Date(Number(rq.to)) : null
    quickKey.value = 'custom'
  }
  return true
}

onMounted(async () => {
  // 先尝试应用告警事件传入的关联查询，再初始化图表并检索
  applyIncomingQuery()
  await nextTick()
  chart = echarts.init(chartEl.value)
  chart.on('click', onChartClick)
  window.addEventListener('resize', resizeChart)
  doSearch()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeChart)
  chart && chart.dispose()
})

// 已在检索页时再次从告警事件跳转（query.ppl 变化）也能自动回填并检索
watch(
  () => route.query.ppl,
  () => {
    if (applyIncomingQuery()) doSearch()
  },
)
</script>

<template>
  <div class="page">
    <!-- 页头 -->
    <div class="page-head">
      <div class="breadcrumb">
        <span>日志分析</span><span class="sep">/</span><span class="cur">日志检索</span>
      </div>
      <div class="head-bar">
        <div>
          <h1 class="title">日志检索</h1>
          <p class="desc">支持 PPL 管道语法与关键字检索，命中后可按字段下钻</p>
        </div>
        <div class="head-actions">
          <span class="time-label">时间范围</span>
          <el-date-picker
            v-model="customStart"
            type="datetime"
            placeholder="开始时间"
            format="YYYY-MM-DD HH:mm"
            size="default"
            class="custom-dt"
            @change="onCustomChange"
          />
          <span class="range-sep">至</span>
          <el-date-picker
            v-model="customEnd"
            type="datetime"
            placeholder="结束时间"
            format="YYYY-MM-DD HH:mm"
            size="default"
            class="custom-dt"
            @change="onCustomChange"
          />
          <el-button v-if="usingCustom" text size="small" @click="clearCustomRange">清除</el-button>
          <el-button :icon="Download" @click="onExport">导出结果</el-button>
        </div>
      </div>
    </div>

    <!-- 检索栏 -->
    <div class="query-card">
      <div class="query-row">
        <el-select v-model="quickKey" size="default" style="width: 130px" @change="onQuickChange">
          <el-option v-for="q in QUICK" :key="q.key" :label="q.label" :value="q.key" />
          <el-option label="自定义" value="custom" />
        </el-select>
        <el-input
          v-model="splText"
          size="default"
          class="spl-input"
          placeholder="输入 PPL 或关键字，例如 source=logs-default | where level='ERROR'"
          @keyup.enter="doSearch"
        >
          <template #prepend><span class="mono">检索</span></template>
        </el-input>
        <el-button type="primary" :icon="Search" :loading="loading || pqlLoading" @click="doSearch">查询</el-button>
        <el-button :icon="RefreshLeft" @click="resetAll">重置</el-button>
      </div>
      <div class="chips-row">
        <span class="chips-label">常用语法</span>
        <el-button v-for="c in CHIPS" :key="c" text size="small" class="chip" @click="onChip(c)">{{ c }}</el-button>
        <span class="hint">含 <code>|</code> 走 PPL 管道分析，否则按关键字/过滤检索；按 Enter 执行</span>
      </div>
    </div>

    <!-- 直方图（仅关键字/过滤检索模式；用 v-show 保持 ECharts 实例存活，避免 PQL 模式切回后空白） -->
    <el-card v-show="!isPql" shadow="never" class="hist-card">
      <template #header>
        <div class="card-head">
          <span class="card-title">日志分布直方图</span>
          <span class="hint">
            命中 {{ total }} 条 · 耗时 {{ took }} ms<template v-if="usingCustom"> · 自定义区间（{{ customRangeLabel }}）</template> · 点击柱体缩小到该时段<template v-if="zoomRange"> ·
            <a class="lnk" @click="resetZoom">重置时段</a></template>
          </span>
        </div>
      </template>
      <div ref="chartEl" class="chart"></div>
    </el-card>

    <!-- 主区：字段侧栏 + 结果 -->
    <div class="main-layout">
      <aside v-if="!isPql" class="fields">
        <div class="fields-head">
          <span class="fields-title">字段</span>
          <span class="hint">已选 {{ visibleFields.length }} / {{ FIELDS.length }}</span>
        </div>
        <div class="fields-body">
          <div class="fields-group">已选字段</div>
          <div v-for="f in FIELDS.filter((x) => x.fixed || isVisible(x.key))" :key="f.key" class="field-item">
            <el-checkbox :model-value="isVisible(f.key)" :disabled="f.fixed" @change="toggleField(f.key)">{{ f.label }}</el-checkbox>
            <span class="field-count">{{ fieldCoverage(f.key) }}</span>
          </div>
          <template v-if="FIELDS.some((x) => !x.fixed && !isVisible(x.key))">
            <div class="fields-group">可选字段</div>
            <div v-for="f in FIELDS.filter((x) => !x.fixed && !isVisible(x.key))" :key="f.key" class="field-item">
              <el-checkbox :model-value="isVisible(f.key)" :disabled="f.fixed" @change="toggleField(f.key)">{{ f.label }}</el-checkbox>
              <span class="field-count">{{ fieldCoverage(f.key) }}</span>
            </div>
          </template>
        </div>
      </aside>

      <div class="results">
        <!-- PQL 管道分析模式 -->
        <template v-if="isPql">
          <div v-if="relatedNote" class="related-note">
            <span class="related-note-label">关联检索</span>
            <span>{{ relatedNote }}</span>
          </div>
          <div class="batch-bar">
            <span><el-tag size="small" type="warning" effect="plain">PQL 分析模式</el-tag> 管道语法结果</span>
          </div>
          <el-table v-if="pqlResult" :data="pqlRows" size="small" stripe max-height="480" v-loading="pqlLoading">
            <el-table-column v-for="c in pqlCols" :key="c" :prop="c" :label="c" show-overflow-tooltip />
          </el-table>
          <el-empty v-else description="输入含 | 的 PPL 语句后执行，例如 source=logs-default | stats count() by level" />
        </template>

        <!-- 关键字/过滤检索模式 -->
        <template v-else>
          <div class="batch-bar" v-if="selectedRows.length">
            <span>已选 <strong>{{ selectedRows.length }}</strong> 项</span>
            <span class="batch-actions">
              <el-button text size="small" :icon="Download" @click="exportSelected">导出所选</el-button>
              <el-button text size="small" :icon="DocumentCopy" @click="copyStatement">复制语句</el-button>
            </span>
          </div>

          <el-table
            :data="tableData"
            v-loading="loading"
            height="420"
            stripe
            @row-click="openDetail"
            @selection-change="onSelectionChange"
            style="cursor: pointer"
          >
            <el-table-column type="selection" width="44" />
            <el-table-column v-if="isVisible('time')" prop="time" label="时间" width="180" />
            <el-table-column v-if="isVisible('level')" label="级别" width="92">
              <template #default="{ row }">
                <el-tag :type="levelTagType(row.level)" size="small" effect="light">{{ row.level }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column v-if="isVisible('source')" prop="source" label="来源" width="150" />
            <el-table-column v-if="isVisible('host')" prop="host" label="主机" width="140" />
            <el-table-column v-if="isVisible('traceId')" prop="traceId" label="traceId" width="170" />
            <el-table-column v-if="isVisible('status')" prop="status" label="状态" width="80" />
            <el-table-column v-if="isVisible('tenant_id')" prop="tenant_id" label="租户" width="100" />
            <el-table-column v-if="isVisible('message')" prop="message" label="内容" show-overflow-tooltip />
          </el-table>

          <div class="pager">
            <span class="total">共 {{ total }} 条</span>
            <div class="pager-right">
              <div class="page-size">
                <span>每页</span>
                <el-select v-model="pageSize" size="small" @change="onPageSizeChange">
                  <el-option v-for="s in [20, 50, 100]" :key="s" :label="s" :value="s" />
                </el-select>
                <span>条</span>
              </div>
              <el-pagination
                v-model:current-page="page"
                :total="total" :page-size="pageSize"
                layout="prev, pager, next"
                background
                @current-change="doSearch"
              />
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawerVisible" title="日志详情" size="480px">
      <el-descriptions v-if="currentRow" :column="1" border>
        <el-descriptions-item label="时间">{{ currentRow.time }}</el-descriptions-item>
        <el-descriptions-item label="级别">
          <el-tag :type="levelTagType(currentRow.level)" size="small" effect="light">{{ currentRow.level }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="来源">{{ currentRow.source }}</el-descriptions-item>
        <el-descriptions-item label="主机">{{ currentRow.host }}</el-descriptions-item>
        <el-descriptions-item label="traceId">{{ currentRow.traceId }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ currentRow.status }}</el-descriptions-item>
        <el-descriptions-item label="租户">{{ currentRow.tenant_id }}</el-descriptions-item>
        <el-descriptions-item label="日志内容">{{ currentRow.message }}</el-descriptions-item>
      </el-descriptions>

      <!-- 上下文：前 2 条（原始日志上方） -->
      <div class="ctx-section">
        <div class="ctx-title-row">
          <div class="ctx-title">上下文日志（前 2 条）</div>
          <el-button link type="primary" size="small" @click="openMore">更多 ›</el-button>
        </div>
        <div v-if="ctxLoading" class="ctx-loading">加载中…</div>
        <template v-else>
          <div v-if="!ctxBefore.length" class="ctx-empty">无更早日志</div>
          <div
            v-for="(c, i) in ctxBefore"
            :key="'b' + i"
            class="ctx-line"
            @click="openContext(c)"
          >{{ ctxLine(c) }}</div>
        </template>
      </div>

      <!-- 原始日志 -->
      <div class="raw-section">
        <div class="raw-title">原始日志</div>
        <pre class="raw-block">{{ rawLog }}</pre>
      </div>

      <!-- 上下文：后 2 条（原始日志下方） -->
      <div class="ctx-section">
        <div class="ctx-title">上下文日志（后 2 条）</div>
        <div v-if="ctxLoading" class="ctx-loading">加载中…</div>
        <template v-else>
          <div v-if="!ctxAfter.length" class="ctx-empty">无更晚日志</div>
          <div
            v-for="(c, i) in ctxAfter"
            :key="'a' + i"
            class="ctx-line"
            @click="openContext(c)"
          >{{ ctxLine(c) }}</div>
        </template>
      </div>

      <template #footer>
        <el-button @click="drawerVisible = false">关闭</el-button>
        <el-button type="primary" @click="copyTraceId">复制 traceId</el-button>
      </template>
    </el-drawer>

    <!-- 更多上下文日志框：当前日志高亮，向上/向下滚动按需加载更多 -->
    <el-dialog
      v-model="moreVisible"
      title="更多上下文日志"
      width="900px"
      :append-to-body="true"
      :close-on-click-modal="false"
      @opened="scrollToAnchor"
    >
      <div class="more-toolbar">
        <el-checkbox v-model="moreSameSource" @change="onSameSourceChange">
          仅同来源（{{ currentRow && currentRow.source || '-' }}）
        </el-checkbox>
        <div class="more-toolbar-right">
          <span class="more-tip">高亮行为当前日志 · 向上/向下滚动加载更多</span>
          <el-button size="small" @click="scrollToAnchor">回到当前日志</el-button>
        </div>
      </div>
      <div ref="moreBody" class="more-body" @scroll="onMoreScroll">
        <div v-if="moreBeforeEnd" class="more-edge">
          — {{ moreBeforeCount >= MORE_MAX ? '已到加载上限' : '已到最早日志' }} —
        </div>
        <div v-else class="more-edge">{{ moreLoading ? '加载中…' : '继续向上滚动，加载更早日志' }}</div>
        <div
          v-for="(c, i) in moreLines"
          :key="(c.ts || i) + '-' + i"
          :class="['more-line', { 'more-line--anchor': c._anchor }]"
        >
          <span v-if="c._anchor" class="more-anchor-tag">当前日志</span><span class="more-line-text">{{ moreLine(c) }}</span>
        </div>
        <div v-if="moreAfterEnd" class="more-edge">
          — {{ moreAfterCount >= MORE_MAX ? '已到加载上限' : '已到最新日志' }} —
        </div>
        <div v-else class="more-edge">{{ moreLoading ? '加载中…' : '继续向下滚动，加载更晚日志' }}</div>
      </div>
    </el-dialog>
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
.head-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.time-label { font-size: 13px; color: var(--color-text-2); white-space: nowrap; }
.custom-dt { width: 200px; }
.range-sep { color: var(--color-text-3); font-size: 13px; padding: 0 2px; }

/* 详情抽屉：上下文 + 原始日志 */
.ctx-section { margin-top: 16px; }
.ctx-title-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.ctx-title { font-size: 13px; font-weight: 600; color: var(--color-text-1); margin-bottom: 8px; }
.ctx-title-row .ctx-title { margin-bottom: 0; }
.ctx-loading, .ctx-empty { font-size: 12px; color: var(--color-text-3); padding: 6px 0; }
.ctx-line { font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size: 12px; line-height: 1.6; color: var(--color-text-1); background: #f5f7fa; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 8px 10px; margin-bottom: 6px; cursor: pointer; white-space: pre-wrap; word-break: break-all; transition: background .15s; }
.ctx-line:hover { background: var(--el-color-primary-light-9); border-color: var(--el-color-primary-light-7); }
.raw-section { margin-top: 16px; }
.raw-title { font-size: 13px; font-weight: 600; color: var(--color-text-1); margin-bottom: 8px; }
.raw-block { background: #f5f7fa; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 12px; font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size: 12px; line-height: 1.6; color: #1f2d3d; white-space: pre-wrap; word-break: break-all; margin: 0; }

/* 更多上下文日志框 */
.more-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.more-toolbar-right { display: flex; align-items: center; gap: 10px; }
.more-tip { font-size: 12px; color: var(--color-text-3); }
.more-body { height: 58vh; overflow-y: auto; background: #fafbfc; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 8px; }
.more-edge { text-align: center; font-size: 12px; color: var(--color-text-3); padding: 8px 0; }
.more-line { position: relative; font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size: 12px; line-height: 1.65; color: var(--color-text-1); white-space: pre-wrap; word-break: break-all; background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 7px 10px; margin-bottom: 6px; }
.more-line--anchor { background: #E6FCF8; border-color: #03857E; box-shadow: inset 3px 0 0 #03857E; padding-left: 14px; }
.more-anchor-tag { display: inline-block; font-size: 11px; color: #fff; background: #03857E; border-radius: 3px; padding: 0 6px; margin-right: 8px; }

.query-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.query-row { display: flex; align-items: center; gap: 10px; }
.spl-input { flex: 1; }
.spl-input :deep(.el-input-group__prepend) { background: #f7f9fa; }
.mono { font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size: 12px; }
.chips-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.chips-label { font-size: 13px; color: var(--color-text-2); }
.chip { font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; color: var(--color-primary); }
.hint { font-size: 12px; color: var(--color-text-3); }
.hint code { font-family: 'SFMono-Regular', Consolas, monospace; background: #f0f3f5; padding: 0 4px; border-radius: 3px; }

.hist-card :deep(.el-card__body) { padding: 8px 16px 12px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.card-title { font-size: 14px; font-weight: 600; color: var(--color-text-1); }
.chart { width: 100%; height: 160px; }
.lnk { color: var(--color-link); cursor: pointer; }

.main-layout { display: flex; gap: 12px; align-items: stretch; }
.fields { width: 200px; flex-shrink: 0; background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; align-self: flex-start; }
.fields-head { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; border-bottom: 1px solid var(--color-border-1); }
.fields-title { font-size: 14px; font-weight: 600; }
.fields-body { padding: 6px 0 10px; max-height: 520px; overflow: auto; }
.fields-group { font-size: 12px; color: var(--color-text-3); padding: 8px 14px 4px; }
.field-item { display: flex; justify-content: space-between; align-items: center; padding: 6px 14px; cursor: pointer; }
.field-item:hover { background: var(--el-color-primary-light-9); }
.field-check { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--color-text-2); }
.field-count { font-size: 12px; color: var(--color-text-3); font-variant-numeric: tabular-nums; }

.results { flex: 1; min-width: 0; background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 12px 14px; }
.related-note { display: flex; align-items: flex-start; gap: 8px; background: var(--el-color-primary-light-9); border: 1px solid var(--el-color-primary-light-7); border-radius: 4px; padding: 8px 12px; margin-bottom: 10px; font-size: 12px; line-height: 1.6; color: var(--color-text-2); }
.related-note-label { flex-shrink: 0; font-weight: 600; color: var(--color-primary); }
.batch-bar { display: flex; justify-content: space-between; align-items: center; background: var(--el-color-primary-light-9); border: 1px solid var(--el-color-primary-light-7); border-radius: 4px; padding: 6px 12px; margin-bottom: 10px; font-size: 13px; color: var(--color-text-2); }
.batch-actions { display: flex; gap: 4px; }
.pager { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; }
.total { font-size: 13px; color: var(--color-text-3); }
</style>
