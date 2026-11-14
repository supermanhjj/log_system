<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Download, Close } from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import {
  listEvents,
  closeEvent,
  closeEvents,
  getRelatedQuery,
  REAL_SOURCES,
  LEVELS,
  STATUS_OPTIONS,
} from '../api/alarms'

const app = useAppStore()
const router = useRouter()

const loading = ref(false)
const error = ref(false)
const updatedAt = ref(new Date())

// 筛选条件（均映射到真实后端字段，不依赖原型样例数据）
const filters = reactive({
  keyword: '',
  level: '',
  status: '',
  source: '',
  date: '',
})

const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
function onPageSizeChange() { page.value = 1; loadEvents() }
const selectedRows = ref([])

const drawerVisible = ref(false)
const currentEvent = ref(null)

const updatedAtText = computed(() => fmt(updatedAt.value))
const descText = computed(() => `数据每 60 秒自动刷新，最新更新 ${updatedAtText.value}`)
const sourceOptions = computed(() => [
  { value: '', label: '全部来源' },
  { value: '全部数据源', label: '全部数据源' },
  ...REAL_SOURCES.map((s) => ({ value: s, label: s })),
])

function fmt(d) {
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec || 0))
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  if (d > 0) return `${d} 天 ${h} 小时`
  if (h > 0) return `${h} 小时 ${m} 分`
  if (m > 0) return `${m} 分 ${s} 秒`
  return `${s} 秒`
}

function levelTagType(level) {
  if (level === '严重') return 'danger'
  if (level === '警告') return 'warning'
  return 'info'
}
function statusTagType(status) {
  if (status === '告警中') return 'warning'
  if (status === '已恢复') return 'success'
  return 'info'
}

// ——— 取数 ———
async function loadEvents(showTip = false) {
  loading.value = true
  error.value = false
  try {
    const data = await listEvents({
      tenant: app.tenant,
      keyword: filters.keyword,
      level: filters.level,
      status: filters.status,
      source: filters.source,
      date: filters.date,
      page: page.value,
      pageSize: pageSize.value,
    })
    tableData.value = data.items
    total.value = data.total
    updatedAt.value = new Date()
    if (showTip) ElMessage.success('告警事件已刷新')
  } catch (e) {
    error.value = true
    ElMessage.error('告警事件加载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

function onSearch() {
  page.value = 1
  loadEvents(false)
}
function onReset() {
  filters.keyword = ''
  filters.level = ''
  filters.status = ''
  filters.source = ''
  filters.date = ''
  page.value = 1
  loadEvents(false)
}
function onRefresh() {
  loadEvents(true)
}
function onSelectionChange(rows) {
  selectedRows.value = rows
}

// ——— 详情抽屉 ———
function openDetail(row) {
  currentEvent.value = row
  drawerVisible.value = true
}
// ——— 查看关联日志 ———
// 检索语句由后端「分析当前规则配置 + 索引真实字段」自适应生成，前端不写死任何模板；
// 用户仍可在检索页看到并编辑该 PPL。接口异常时降级为仅按索引取日志的最小语句。
async function viewRelatedLog() {
  const ev = currentEvent.value
  if (!ev) return
  let q
  try {
    q = await getRelatedQuery(ev.id, app.tenant)
  } catch (e) {
    ElMessage.warning('关联查询分析失败，已使用基础检索：' + (e.response?.data?.detail || e.message))
    const idx = app.tenant && app.tenant !== 'all' ? `logs-${app.tenant}` : 'logs-*'
    q = {
      pql: `source=${idx} | head 100`,
      from: ev.firstSeenTs || Date.now() - 3600e3,
      to: Date.now(),
    }
  }
  router.push({
    path: '/search',
    query: {
      ppl: q.pql,
      from: String(q.from),
      to: String(q.to),
      note: q.note || '',
      fromAlarm: '1',
    },
  })
}

// ——— 关闭 / 维护 ———
async function doClose(row, reason) {
  try {
    await closeEvent(row.id, reason, app.userName || '黄金金')
    ElMessage.success('告警已关闭，恢复通知已发送')
    loadEvents(false)
  } catch (e) {
    ElMessage.error('关闭失败：' + (e.response?.data?.detail || e.message))
  }
}
function onClose(row) {
  if (row.status === '已恢复') {
    ElMessage.info('该告警已恢复，无需重复关闭')
    return
  }
  ElMessageBox.prompt('请输入关闭原因（将记录到操作审计）', '关闭告警', {
    confirmButtonText: '确认关闭',
    cancelButtonText: '取消',
    inputValue: '已处理',
    inputValidator: (v) => (v && v.trim() ? true : '关闭原因不可为空'),
  })
    .then(({ value }) => doClose(row, value.trim()))
    .catch(() => {})
}

// ——— 批量关闭 ———
async function onBatchClose() {
  if (!selectedRows.value.length) {
    ElMessage.warning('请先在列表勾选告警')
    return
  }
  const ids = selectedRows.value.map((r) => r.id)
  ElMessageBox.prompt(`将关闭所选 ${ids.length} 条告警并停止重复通知（已恢复项自动跳过）`, '批量关闭告警', {
    confirmButtonText: '关闭告警',
    cancelButtonText: '取消',
    inputValue: '批量处理',
    inputValidator: (v) => (v && v.trim() ? true : '原因不可为空'),
  })
    .then(({ value }) => closeEvents(ids, value.trim(), app.userName || '黄金金'))
    .then((res) => {
      ElMessage.success(`成功关闭 ${res.closed} 条，跳过 ${res.skipped} 条已恢复`)
      loadEvents(false)
    })
    .catch(() => {})
}

// ——— 导出 ———
function exportCSV(rows, filename) {
  const header = ['告警名称', '级别', '告警来源', '首次发生', '持续时长', '命中次数', '状态', '处理人']
  const lines = rows.map((r) =>
    [
      r.name,
      r.level,
      r.source,
      r.firstSeen,
      fmtDuration(r.durationSec),
      r.count,
      r.status,
      r.handler || '',
    ]
      .map((c) => `"${(c ?? '').toString().replace(/"/g, '""')}"`)
      .join(','),
  )
  const csv = '﻿' + [header.join(','), ...lines].join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}
async function onExport() {
  try {
    if (selectedRows.value.length) {
      exportCSV(selectedRows.value, `alarm-events-${Date.now()}.csv`)
      ElMessage.success(`已导出 ${selectedRows.value.length} 条`)
      return
    }
    // 未勾选则导出当前筛选条件下的全量
    const data = await listEvents({
      tenant: app.tenant,
      keyword: filters.keyword,
      level: filters.level,
      status: filters.status,
      source: filters.source,
      date: filters.date,
      page: 1,
      pageSize: 100000,
    })
    if (!data.items.length) {
      ElMessage.warning('当前筛选无数据可导出')
      return
    }
    exportCSV(data.items, `alarm-events-${Date.now()}.csv`)
    ElMessage.success(`已导出 ${data.items.length} 条`)
  } catch (e) {
    ElMessage.error('导出失败：' + (e.response?.data?.detail || e.message))
  }
}

// ——— 自动刷新 ———
let timer = null
function startAutoRefresh() {
  stopAutoRefresh()
  timer = setInterval(() => loadEvents(false), 60000)
}
function stopAutoRefresh() {
  if (timer) clearInterval(timer)
  timer = null
}

watch(
  () => app.tenant,
  () => {
    page.value = 1
    loadEvents(false)
  },
)

onMounted(() => {
  loadEvents(false)
  startAutoRefresh()
})
onBeforeUnmount(() => stopAutoRefresh())
</script>

<template>
  <div class="page" v-loading="loading">
    <!-- 页头 -->
    <div class="page-head">
      <div class="breadcrumb">
        <span>告警中心</span><span class="sep">/</span><span class="cur">告警事件</span>
      </div>
      <div class="head-bar">
        <div>
          <h1 class="title">告警事件</h1>
          <p class="desc">{{ descText }}</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" @click="onRefresh">刷新</el-button>
          <el-button :icon="Download" @click="onExport">导出</el-button>
          <el-button :icon="Close" @click="onBatchClose">批量关闭</el-button>
        </div>
      </div>
    </div>

    <!-- 错误态 -->
    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      title="告警事件加载失败"
      style="margin-bottom: 12px"
    >
      <template #default>
        <div class="alert-body">
          <span>数据加载失败，请稍后重试</span>
          <el-button size="small" type="danger" plain @click="loadEvents(false)">重试</el-button>
        </div>
      </template>
    </el-alert>

    <!-- 筛选 -->
    <div class="filter-card">
      <div class="filter-row">
        <div class="filter-item">
          <label>关键词</label>
          <el-input v-model="filters.keyword" placeholder="告警名称 / 来源" clearable @keyup.enter="onSearch" />
        </div>
        <div class="filter-item">
          <label>告警级别</label>
          <el-select v-model="filters.level" placeholder="全部级别" clearable>
            <el-option v-for="l in LEVELS" :key="l" :label="l" :value="l" />
          </el-select>
        </div>
        <div class="filter-item">
          <label>处理状态</label>
          <el-select v-model="filters.status" placeholder="全部状态" clearable>
            <el-option v-for="s in STATUS_OPTIONS" :key="s" :label="s" :value="s" />
          </el-select>
        </div>
        <div class="filter-item">
          <label>告警来源</label>
          <el-select v-model="filters.source" placeholder="全部来源" clearable>
            <el-option v-for="o in sourceOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </div>
        <div class="filter-item">
          <label>发生时间</label>
          <el-date-picker
            v-model="filters.date"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="选择日期"
            style="width: 160px"
          />
        </div>
        <div class="filter-actions">
          <el-button type="primary" @click="onSearch">查询</el-button>
          <el-button @click="onReset">重置</el-button>
        </div>
      </div>
    </div>

    <!-- 批量操作条 -->
    <div v-if="selectedRows.length" class="batch-bar">
      <span>已选 <strong>{{ selectedRows.length }}</strong> 项</span>
      <span class="batch-actions">
        <el-button text size="small" :icon="Close" @click="onBatchClose">批量关闭</el-button>
        <el-button text size="small" :icon="Download" @click="onExport">导出所选</el-button>
      </span>
    </div>

    <!-- 列表 -->
    <el-card shadow="never" class="table-card">
      <el-table
        :data="tableData"
        stripe
        height="460"
        @selection-change="onSelectionChange"
        @row-dblclick="openDetail"
      >
        <el-table-column type="selection" width="44" align="center" header-align="center" />
        <el-table-column prop="name" label="告警名称" min-width="200" show-overflow-tooltip align="left" header-align="left" />
        <el-table-column label="级别" width="90" align="center" header-align="center">
          <template #default="{ row }">
            <el-tag :type="levelTagType(row.level)" size="small" effect="light">{{ row.level }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="告警来源" min-width="150" show-overflow-tooltip align="left" header-align="left" />
        <el-table-column prop="firstSeen" label="首次发生" width="170" align="left" header-align="left" />
        <el-table-column label="持续时长" width="110" align="left" header-align="left">
          <template #default="{ row }">{{ fmtDuration(row.durationSec) }}</template>
        </el-table-column>
        <el-table-column label="命中次数" width="90" align="center" header-align="center">
          <template #default="{ row }">
            <span class="count">{{ row.count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110" align="center" header-align="center">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small" effect="light">
              <i class="dot" :class="`dot--${row.status}`"></i>{{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right" align="center" header-align="center">
          <template #default="{ row }">
            <div class="op-actions">
              <el-button text size="small" @click="openDetail(row)">详情</el-button>
              <el-button
                v-if="row.status !== '已恢复'"
                text
                size="small"
                type="danger"
                @click="onClose(row)"
              >关闭</el-button>
              <span v-if="row.status === '已恢复'" class="muted">已关闭</span>
            </div>
          </template>
        </el-table-column>
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
            @current-change="loadEvents"
          />
        </div>
      </div>
    </el-card>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawerVisible" title="告警详情" size="480px">
      <template v-if="currentEvent">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="告警名称">{{ currentEvent.name }}</el-descriptions-item>
          <el-descriptions-item label="级别">
            <el-tag :type="levelTagType(currentEvent.level)" size="small" effect="light">{{ currentEvent.level }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="告警来源">{{ currentEvent.source }}</el-descriptions-item>
          <el-descriptions-item label="首次发生">{{ currentEvent.firstSeen }}</el-descriptions-item>
          <el-descriptions-item label="持续时长">{{ fmtDuration(currentEvent.durationSec) }}</el-descriptions-item>
          <el-descriptions-item label="命中次数">{{ currentEvent.count }} 条（近 {{ currentEvent.windowMinutes }} 分钟）</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusTagType(currentEvent.status)" size="small" effect="light">{{ currentEvent.status }}</el-tag>
            <span v-if="currentEvent.closeReason" class="muted"> · {{ currentEvent.closeReason }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="currentEvent.handler" label="处理人">{{ currentEvent.handler }}</el-descriptions-item>
        </el-descriptions>

        <div v-if="currentEvent.matchedSources && currentEvent.matchedSources.length" class="mini-block">
          <div class="mini-title">命中来源分布</div>
          <el-tag v-for="s in currentEvent.matchedSources" :key="s" size="small" effect="plain" class="mini-tag">{{ s }}</el-tag>
        </div>
        <div v-if="currentEvent.matchedHosts && currentEvent.matchedHosts.length" class="mini-block">
          <div class="mini-title">命中主机分布</div>
          <el-tag v-for="h in currentEvent.matchedHosts" :key="h" size="small" effect="plain" class="mini-tag">{{ h }}</el-tag>
        </div>

        <div class="raw-section">
          <div class="raw-title">最近一条原始日志</div>
          <pre class="raw-block">{{ currentEvent.sample?.raw || '（无原始日志）' }}</pre>
        </div>
      </template>

      <template #footer>
        <el-button @click="drawerVisible = false">关闭</el-button>
        <el-button @click="viewRelatedLog">查看关联日志</el-button>
        <el-button
          v-if="currentEvent && currentEvent.status !== '已恢复'"
          type="primary"
          @click="onClose(currentEvent)"
        >关闭告警</el-button>
      </template>
    </el-drawer>
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

.filter-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.filter-row { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; }
.filter-item { display: flex; flex-direction: column; gap: 6px; }
.filter-item label { font-size: 12px; color: var(--color-text-2); }
.filter-item :deep(.el-input), .filter-item :deep(.el-select) { width: 170px; }
.filter-actions { display: flex; gap: 8px; margin-left: auto; }

.batch-bar { display: flex; justify-content: space-between; align-items: center; background: var(--el-color-primary-light-9); border: 1px solid var(--el-color-primary-light-7); border-radius: 4px; padding: 6px 12px; font-size: 13px; color: var(--color-text-2); }
.batch-actions { display: flex; gap: 4px; }

.table-card :deep(.el-card__body) { padding: 12px 14px 14px; }
.op-actions { display: flex; align-items: center; justify-content: center; gap: 2px; flex-wrap: nowrap; }
.op-actions .el-button { margin: 0 !important; }

/* 选择列背景与数据行保持一致：消除全选/取消后灰底残留，表头也与其它列对齐 */
/* 注意：本主题表头底色为 #E6FCF8（见 global.css），不能用 --el-table-header-bg-color（该变量为 #fff） */
.table-card :deep(.el-table__header th.el-table-column--selection) {
  background-color: #E6FCF8 !important;
  color: #258C89 !important;
}
.table-card :deep(.el-table__row td.el-table-column--selection) {
  background-color: var(--el-table-tr-bg-color, #fff) !important;
}
.table-card :deep(.el-table__row--striped td.el-table-column--selection) {
  background-color: var(--el-fill-color-lighter, #fafafa) !important;
}
.table-card :deep(.el-table__row:hover td.el-table-column--selection) {
  background-color: #E8F3F1 !important;
}
.table-card :deep(.el-table__row.is-selected td.el-table-column--selection),
.table-card :deep(.el-table__row.current-row td.el-table-column--selection) {
  background-color: var(--el-fill-color-lighter, #f5f7fa) !important;
}
.count { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--color-text-1); }
.pager { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; }
.total { font-size: 13px; color: var(--color-text-3); }
.muted { color: var(--color-text-3); }

.dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
.dot--告警中 { background: var(--tag-warning-text); }
.dot--已恢复 { background: var(--color-success); }
.dot--维护中 { background: var(--color-link); }

.mini-block { margin-top: 16px; }
.mini-title { font-size: 13px; font-weight: 600; color: var(--color-text-1); margin-bottom: 8px; }
.mini-tag { margin: 0 6px 6px 0; }

.raw-section { margin-top: 16px; }
.raw-title { font-size: 13px; font-weight: 600; color: var(--color-text-1); margin-bottom: 8px; }
.raw-block { background: #f5f7fa; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 12px; font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size: 12px; line-height: 1.6; color: #1f2d3d; white-space: pre-wrap; word-break: break-all; margin: 0; }
</style>
