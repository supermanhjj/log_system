<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Link } from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import { useAuthStore } from '../stores/auth'
import {
  getMonitorHealth,
  getMonitorTopology,
  listMonitorCollectors,
  runMonitorDiagnose,
} from '../api/monitor'

const app = useAppStore()
const auth = useAuthStore()
const health = ref(null)
const topology = ref(null)
const collectors = ref([])
const loading = ref(false)
const diagnosing = ref(false)
const diagnosis = ref(null)
const selectedHost = ref('')
const diagVisible = ref(false)
const diagTenant = ref('')

const canDiagnose = computed(() => auth.has('monitor', 'diagnose'))
const tenantLabel = computed(() => app.tenants.find((t) => t.value === app.tenant)?.label || app.tenant)
const realTenants = computed(() => app.tenants.filter((t) => t.value !== 'all'))
const healthGroups = computed(() => {
  if (!health.value) return []
  const groups = {}
  health.value.items.forEach((it) => {
    ;(groups[it.group] = groups[it.group] || []).push(it)
  })
  return Object.entries(groups).map(([name, items]) => ({ name, items }))
})
const statusMeta = {
  ok: { text: '正常', color: '#1f9d55', bg: '#e8f7ee', type: 'success' },
  warn: { text: '警告', color: '#b7791f', bg: '#fdf5e6', type: 'warning' },
  fail: { text: '异常', color: '#c53030', bg: '#fdecec', type: 'danger' },
  unknown: { text: '未知', color: '#718096', bg: '#eef2f7', type: 'info' },
  skip: { text: '跳过', color: '#718096', bg: '#eef2f7', type: 'info' },
}
const tagType = (status) => statusMeta[status]?.type || 'info'
const groupStatus = (items) => {
  const rank = { fail: 3, warn: 2, unknown: 1, ok: 0 }
  return items.reduce((s, it) => (rank[it.status] > rank[s] ? it.status : s), 'ok')
}
const healthText = computed(() => {
  if (!health.value) return '加载中'
  const s = health.value.summary
  return `正常 ${s.ok}/${s.total} · 警告 ${s.warn} · 异常 ${s.fail}`
})

async function loadHealth() {
  try {
    health.value = await getMonitorHealth()
  } catch (e) {
    ElMessage.error(`组件健康加载失败：${e.response?.data?.detail || e.message}`)
  }
}

async function loadTopology() {
  try {
    topology.value = await getMonitorTopology(app.tenant)
  } catch (e) {
    ElMessage.error(`全链路状态加载失败：${e.response?.data?.detail || e.message}`)
  }
}

async function loadAll() {
  loading.value = true
  try {
    await Promise.all([loadHealth(), loadTopology()])
  } finally {
    loading.value = false
  }
}

async function loadDiagnoseCollectors() {
  if (!diagTenant.value) {
    collectors.value = []
    selectedHost.value = ''
    return
  }
  try {
    const data = await listMonitorCollectors(diagTenant.value)
    collectors.value = data.items || []
    if (!collectors.value.some((c) => c.hostname === selectedHost.value)) {
      selectedHost.value = collectors.value[0]?.hostname || ''
    }
  } catch {
    collectors.value = []
    selectedHost.value = ''
  }
}

async function runDiagnose() {
  if (!canDiagnose.value) return
  if (!diagTenant.value) {
    ElMessage.warning('请选择诊断租户')
    return
  }
  diagnosing.value = true
  try {
    diagnosis.value = await runMonitorDiagnose({
      tenant: diagTenant.value,
      hostname: selectedHost.value || null,
    })
    if (diagnosis.value.overall !== 'ok') {
      ElMessage.warning('诊断完成，存在需要处理的环节')
    }
  } catch (e) {
    ElMessage.error(`诊断失败：${e.response?.data?.detail || e.message}`)
  } finally {
    diagnosing.value = false
  }
}

async function openDiagnose() {
  if (!canDiagnose.value) {
    ElMessage.warning('当前角色无诊断权限')
    return
  }
  let target = app.tenant
  if (target === 'all') {
    try {
      const data = await listMonitorCollectors('all')
      target = (data.items || []).find((c) => c.tenant)?.tenant || realTenants.value[0]?.value || ''
    } catch {
      target = realTenants.value[0]?.value || ''
    }
  }
  diagTenant.value = target
  diagnosis.value = null
  diagVisible.value = true
  await loadDiagnoseCollectors()
  await runDiagnose()
}

watch(diagTenant, async () => {
  diagnosis.value = null
  await loadDiagnoseCollectors()
})
watch(() => app.tenant, loadAll)
onMounted(loadAll)

const topoLayer = {
  collector: 0,
  gateway: 1,
  control: 1,
  buffer: 2,
  compute: 3,
  storage: 4,
  tenant: 5,
  index: 6,
  query: 7,
  platform: 7,
  web: 8,
}
const topoLayerTitle = ['采集器', '接入/管控', '缓冲', '计算', '存储', '租户', '索引', '查询/平台', 'Web 展示']
const topoColor = { ok: '#1f9d55', warn: '#d69e2e', fail: '#c53030', unknown: '#a0aec0' }

const topoLayout = computed(() => {
  if (!topology.value) return null
  const W = 760
  const H = 420
  const left = 36
  const right = 700
  const layers = {}
  topology.value.nodes.forEach((n) => {
    const idx = topoLayer[n.layer] ?? 9
    ;(layers[idx] = layers[idx] || []).push(n)
  })
  const nodes = []
  Object.entries(layers).forEach(([idxText, items]) => {
    const idx = Number(idxText)
    const x = left + idx * ((right - left) / 8)
    items.sort((a, b) => a.label.localeCompare(b.label, 'zh-CN'))
    items.forEach((n, i) => {
      const y = items.length === 1 ? H / 2 : 62 + (i * (H - 96)) / (items.length - 1)
      const r = n.layer === 'web' ? 24 : (n.layer === 'storage' ? 20 : (n.layer === 'collector' ? 14 : 17))
      nodes.push({
        ...n, x, y, r,
        short: n.label.length > 9 ? n.label.slice(0, 8) + '…' : n.label,
        color: topoColor[n.status] || topoColor.unknown,
        title: `${n.label} · ${statusMeta[n.status]?.text || n.statusText}${Object.entries(n.meta || {})
          .filter(([, v]) => v !== null && v !== undefined && v !== '')
          .map(([k, v]) => ` · ${k}: ${v}`).join('')}`,
      })
    })
  })
  const nodeMap = Object.fromEntries(nodes.map((n) => [n.id, n]))
  const edges = topology.value.edges.map((e) => {
    const from = nodeMap[e.source]
    const to = nodeMap[e.target]
    if (!from || !to) return null
    const dx = to.x - from.x
    const dy = to.y - from.y
    const len = Math.hypot(dx, dy) || 1
    return {
      ...e,
      x1: from.x + (dx / len) * from.r,
      y1: from.y + (dy / len) * from.r,
      x2: to.x - (dx / len) * (to.r + 3),
      y2: to.y - (dy / len) * (to.r + 3),
      color: topoColor[e.status] || topoColor.unknown,
      title: `${from.label} → ${to.label} · ${e.label || ''}${e.metric ? ' · ' + e.metric : ''}`,
    }
  }).filter(Boolean)
  return {
    W, H, nodes, edges,
    guides: topoLayerTitle.map((title, idx) => ({ title, x: left + idx * ((right - left) / 8) })),
  }
})
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-head">
      <div class="breadcrumb">
        <span>概览</span><span class="sep">/</span><span class="cur">自监测总览</span>
      </div>
      <div class="head-bar">
        <div>
          <h1 class="title">自监测总览</h1>
          <p class="desc">全链路状态与组件健康 · 当前：{{ tenantLabel }} · 更新于 {{ topology?.updatedAt || '—' }}</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" :loading="loading" @click="loadAll">刷新</el-button>
          <el-button type="primary" :icon="Link" :disabled="!canDiagnose" @click="openDiagnose">
            采集链路一键诊断
          </el-button>
        </div>
      </div>
    </div>

    <el-card shadow="never" class="card">
      <template #header>
        <div class="card-head">
          <span class="card-title">全链路状态</span>
          <div class="card-meta">
            <span>采集器 <b>{{ topology?.summary.online || 0 }}/{{ topology?.summary.collectors || 0 }}</b></span>
            <span>租户 <b>{{ topology?.summary.tenants || 0 }}</b></span>
            <span>近 15 分钟 <b>{{ (topology?.summary.recent15m || 0).toLocaleString() }}</b> 条</span>
          </div>
        </div>
      </template>
      <div class="topo-head">
        <div class="legend">
          <span><i class="legend-dot ok" />正常</span>
          <span><i class="legend-dot warn" />警告</span>
          <span><i class="legend-dot fail" />异常</span>
          <span><i class="legend-dot unknown" />未知</span>
        </div>
        <span class="hint">节点颜色代表健康状态，连线颜色代表边界状态</span>
      </div>
      <div v-if="topoLayout" class="topo-scroll">
        <svg class="topo-svg" :viewBox="`0 0 ${topoLayout.W} ${topoLayout.H}`" preserveAspectRatio="xMidYMid meet">
          <defs>
            <marker id="topo-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#7d97ae" opacity="0.8" />
            </marker>
          </defs>
          <g class="topo-guides">
            <line v-for="g in topoLayout.guides" :key="g.title" :x1="g.x" :x2="g.x" y1="44" :y2="topoLayout.H - 14" />
            <text v-for="g in topoLayout.guides" :key="`t-${g.title}`" :x="g.x" y="26" text-anchor="middle">{{ g.title }}</text>
          </g>
          <g class="topo-edges">
            <line v-for="(e, i) in topoLayout.edges" :key="`${e.source}-${e.target}-${i}`"
                  :x1="e.x1" :y1="e.y1" :x2="e.x2" :y2="e.y2"
                  :stroke="e.color" marker-end="url(#topo-arrow)" class="edge">
              <title>{{ e.title }}</title>
            </line>
          </g>
          <g class="topo-nodes">
            <g v-for="n in topoLayout.nodes" :key="n.id" :transform="`translate(${n.x}, ${n.y})`" class="node">
              <circle :r="n.r" :fill="n.color" />
              <text :y="n.r + 14" text-anchor="middle">{{ n.short }}</text>
              <title>{{ n.title }}</title>
            </g>
          </g>
        </svg>
      </div>
      <el-empty v-else description="拓扑数据加载中" :image-size="60" />
      <div class="chart-foot">
        <span>链路：采集器 → 接入/管控 → 缓冲 → 计算 → 存储 → 租户/索引 → 查询/平台 → Web</span>
        <span>{{ tenantLabel }}</span>
      </div>
    </el-card>

    <el-card shadow="never" class="card">
      <template #header>
        <div class="card-head">
          <span class="card-title">组件健康总览</span>
          <div class="card-meta">
            <span v-if="health" class="status-tag" :class="`status-tag--${health.status}`">
              <i class="dot" />{{ statusMeta[health.status]?.text || health.status }}
            </span>
            <span class="hint">{{ healthText }}</span>
          </div>
        </div>
      </template>
      <div v-if="healthGroups.length" class="health-grid">
        <div v-for="g in healthGroups" :key="g.name" class="health-group">
          <div class="group-head">
            <span class="status-dot" :class="groupStatus(g.items)" />
            <b>{{ g.name }}</b>
            <span class="group-count">{{ g.items.filter((i) => i.status === 'ok').length }}/{{ g.items.length }}</span>
          </div>
          <div class="health-rows">
            <div v-for="it in g.items" :key="it.id" class="health-item">
              <div class="health-main">
                <div class="health-name">
                  <span class="state-dot" :class="it.status" />{{ it.name }}
                </div>
                <div class="health-detail">{{ it.detail }}</div>
              </div>
              <div class="health-right">
                <el-tag size="small" effect="light" :type="tagType(it.status)">{{ it.statusText }}</el-tag>
                <span v-if="it.latencyMs" class="latency">{{ it.latencyMs }} ms</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <el-empty v-else description="暂无组件健康数据" :image-size="60" />
      <div class="chart-foot">
        <span>组件探活为进程/端口级，不等价于业务功能自检</span>
        <span>{{ health?.updatedAt || '—' }}</span>
      </div>
    </el-card>
  </div>

  <el-dialog v-model="diagVisible" title="采集链路一键诊断" width="780px" top="7vh" destroy-on-close>
    <div class="diag-toolbar">
      <div class="diag-field">
        <label>诊断租户</label>
        <el-select v-model="diagTenant" style="width: 170px">
          <el-option v-for="t in realTenants" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>
      </div>
      <div class="diag-field">
        <label>诊断采集器</label>
        <el-select v-model="selectedHost" clearable placeholder="自动选择首台采集器" style="width: 220px" :disabled="!collectors.length">
          <el-option v-for="c in collectors" :key="c.hostname" :label="`${c.hostname}（${c.tenant}）`" :value="c.hostname" />
        </el-select>
      </div>
      <el-button type="primary" :icon="Link" :loading="diagnosing" @click="runDiagnose">重新诊断</el-button>
    </div>

    <div v-if="diagnosing" class="diag-running">
      <span class="pulse" /> 正在按真实链路逐段探测，请稍候…
    </div>
    <div v-else-if="diagnosis" class="diag-result">
      <div class="diag-summary" :class="diagnosis.overall">
        <div class="overall">
          <span class="state-dot" :class="diagnosis.overall" />
          <b>{{ statusMeta[diagnosis.overall]?.text || diagnosis.overall }}</b>
          <span class="muted">耗时 {{ diagnosis.durationMs }} ms · {{ diagnosis.hostname || '未指定采集器' }}</span>
        </div>
        <div class="chips">
          <span class="chip ok">正常 {{ diagnosis.summary.ok }}</span>
          <span class="chip warn">警告 {{ diagnosis.summary.warn }}</span>
          <span class="chip fail">异常 {{ diagnosis.summary.fail }}</span>
        </div>
      </div>
      <div class="steps">
        <div v-for="(c, i) in diagnosis.checks" :key="c.key" class="step" :class="c.status">
          <div class="step-index">{{ i + 1 }}</div>
          <div class="step-body">
            <div class="step-title">
              <b>{{ c.title }}</b>
              <el-tag size="small" effect="light" :type="tagType(c.status)">{{ c.statusText }}</el-tag>
            </div>
            <div class="step-detail">{{ c.detail }}</div>
            <div v-if="c.status !== 'ok' && c.action" class="step-action">处理建议：{{ c.action }}</div>
          </div>
        </div>
      </div>
    </div>
    <el-empty v-else description="尚未执行诊断" :image-size="60" />

    <template #footer>
      <el-button @click="diagVisible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; width: calc(100vw - 282px); max-width: 100%; min-width: 0; }
.page-head { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.breadcrumb { font-size: 12px; color: var(--color-text-3); }
.breadcrumb .sep { margin: 0 6px; }
.breadcrumb .cur { color: var(--color-text-2); }
.head-bar { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-top: 8px; }
.title { font-size: 18px; font-weight: 600; color: var(--color-text-1); margin: 0; }
.desc { font-size: 13px; color: var(--color-text-3); margin: 4px 0 0; }
.head-actions { display: flex; gap: 8px; }
.card { overflow: visible; }
.card :deep(.el-card__header) { padding: 12px 16px; border-bottom: 1px solid var(--color-border-1); }
.card :deep(.el-card__body) { padding: 12px 16px 14px; }
.card-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.card-title { font-size: 14px; font-weight: 600; color: var(--color-text-1); }
.card-meta { display: flex; align-items: center; gap: 14px; font-size: 12px; color: var(--color-text-3); flex-wrap: wrap; }
.card-meta b { color: var(--color-text-1); font-variant-numeric: tabular-nums; }
.hint { font-size: 12px; color: var(--color-text-3); }
.topo-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; flex-wrap: wrap; }
.legend { display: flex; gap: 14px; font-size: 12px; color: var(--color-text-2); }
.legend span { display: flex; align-items: center; gap: 5px; }
.legend-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.legend-dot.ok { background: #1f9d55; }
.legend-dot.warn { background: #d69e2e; }
.legend-dot.fail { background: #c53030; }
.legend-dot.unknown { background: #a0aec0; }
.topo-scroll { width: 100%; background: #F7F9FB; border: 1px solid var(--color-border-1); border-radius: 4px; overflow: hidden; }
.topo-svg { display: block; width: 100%; height: 360px; }
.topo-guides line { stroke: #E5EDF5; stroke-width: 1; stroke-dasharray: 4 6; }
.topo-guides text { fill: #8AA0B5; font-size: 11px; }
.edge { stroke-width: 1.5; opacity: .58; transition: opacity .15s ease, stroke-width .15s ease; }
.edge:hover { opacity: 1; stroke-width: 3; }
.node circle { stroke: #fff; stroke-width: 2.5; filter: drop-shadow(0 3px 4px rgba(15, 42, 74, .18)); }
.node:hover circle { stroke: #0B3B66; stroke-width: 3; }
.node text { fill: #25435F; font-size: 10px; pointer-events: none; }
.chart-foot { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-top: 8px; font-size: 12px; color: var(--color-text-3); }
.status-tag { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; padding: 2px 8px; border-radius: 10px; }
.status-tag .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.status-tag--ok { background: #E8F7EE; color: #1F7A49; }
.status-tag--ok .dot { background: #1F7A49; }
.status-tag--warn { background: #FDF5E6; color: #95651B; }
.status-tag--warn .dot { background: #95651B; }
.status-tag--fail { background: #FDECEC; color: #A52A2A; }
.status-tag--fail .dot { background: #A52A2A; }
.health-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.health-group { border: 1px solid var(--color-border-1); border-radius: 4px; overflow: hidden; background: #fff; }
.group-head { display: flex; align-items: center; gap: 8px; padding: 9px 12px; background: #F7F9FB; border-bottom: 1px solid var(--color-border-1); font-size: 13px; color: var(--color-text-1); }
.group-head b { font-weight: 600; }
.group-count { margin-left: auto; font-size: 12px; color: var(--color-text-3); font-variant-numeric: tabular-nums; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: #a0aec0; }
.status-dot.ok { background: #1f9d55; }
.status-dot.warn { background: #d69e2e; }
.status-dot.fail { background: #c53030; }
.status-dot.unknown { background: #a0aec0; }
.health-rows { padding: 2px 0; }
.health-item { display: flex; align-items: flex-start; gap: 10px; padding: 8px 12px; border-bottom: 1px dashed var(--color-border-1); }
.health-item:last-child { border-bottom: 0; }
.health-main { flex: 1; min-width: 0; }
.health-name { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 500; color: var(--color-text-1); }
.state-dot { flex: 0 0 auto; width: 6px; height: 6px; border-radius: 50%; background: #a0aec0; }
.state-dot.ok { background: #1f9d55; }
.state-dot.warn { background: #d69e2e; }
.state-dot.fail { background: #c53030; }
.state-dot.unknown, .state-dot.skip { background: #a0aec0; }
.health-detail { margin-top: 3px; font-size: 12px; color: var(--color-text-3); line-height: 1.45; word-break: break-all; }
.health-right { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; flex: 0 0 auto; }
.latency { font-size: 11px; color: var(--color-text-3); font-variant-numeric: tabular-nums; }
.diag-toolbar { display: flex; align-items: flex-end; gap: 14px; padding-bottom: 12px; border-bottom: 1px solid var(--color-border-1); margin-bottom: 12px; flex-wrap: wrap; }
.diag-field { display: flex; flex-direction: column; gap: 6px; }
.diag-field label { font-size: 12px; color: var(--color-text-3); }
.diag-running { display: flex; align-items: center; gap: 8px; padding: 30px 6px; color: #53718e; font-size: 13px; }
.pulse { width: 9px; height: 9px; border-radius: 50%; background: #2c7bb6; animation: pulse 1.2s infinite; }
@keyframes pulse { 0%, 100% { opacity: .35; transform: scale(.85); } 50% { opacity: 1; transform: scale(1.15); } }
.diag-summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; border-radius: 4px; border: 1px solid var(--color-border-1); background: #F7F9FB; }
.diag-summary.warn { border-color: #F3DFB9; background: #FFFAF0; }
.diag-summary.fail { border-color: #F3C7C7; background: #FFF6F6; }
.overall { display: flex; align-items: center; gap: 8px; }
.overall .state-dot { width: 9px; height: 9px; }
.muted { color: var(--color-text-3); font-size: 12px; }
.chips { display: flex; gap: 8px; }
.chip { font-size: 12px; padding: 3px 9px; border-radius: 12px; }
.chip.ok { color: #1f7a49; background: #e8f7ee; }
.chip.warn { color: #95651b; background: #fdf5e6; }
.chip.fail { color: #a52a2a; background: #fdecec; }
.steps { margin-top: 12px; display: flex; flex-direction: column; gap: 8px; max-height: 55vh; overflow-y: auto; }
.step { display: flex; gap: 10px; padding: 10px 12px; border: 1px solid var(--color-border-1); border-left: 3px solid #1f9d55; border-radius: 4px; background: #fff; }
.step.warn { border-left-color: #d69e2e; }
.step.fail { border-left-color: #c53030; }
.step.skip { border-left-color: #a0aec0; }
.step-index { width: 20px; height: 20px; border-radius: 50%; background: #EEF4F9; color: #6F8499; font-size: 11px; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; }
.step-body { flex: 1; min-width: 0; }
.step-title { display: flex; align-items: center; gap: 8px; }
.step-detail { margin-top: 4px; font-size: 12px; color: var(--color-text-2); line-height: 1.6; word-break: break-all; }
.step-action { margin-top: 4px; font-size: 12px; color: #94651c; }
@media (max-width: 1100px) {
  .page-head { padding: 12px 14px; }
  .head-bar { align-items: flex-start; }
  .card-meta { gap: 8px; }
  .topo-svg { height: 300px; }
}
</style>
