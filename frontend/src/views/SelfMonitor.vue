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
const activeTab = ref('health')
const health = ref(null)
const topology = ref(null)
const collectors = ref([])
const loading = ref(false)
const diagnosing = ref(false)
const diagnosis = ref(null)
const selectedHost = ref('')

const canDiagnose = computed(() => auth.has('monitor', 'diagnose'))
const tenantLabel = computed(() => app.tenants.find((t) => t.value === app.tenant)?.label || app.tenant)
const healthGroups = computed(() => {
  if (!health.value) return []
  const groups = {}
  health.value.items.forEach((it) => {
    ;(groups[it.group] = groups[it.group] || []).push(it)
  })
  return Object.entries(groups).map(([name, items]) => ({ name, items }))
})
const statusMeta = {
  ok: { text: '正常', color: '#1f9d55', bg: '#e8f7ee' },
  warn: { text: '警告', color: '#b7791f', bg: '#fdf5e6' },
  fail: { text: '异常', color: '#c53030', bg: '#fdecec' },
  unknown: { text: '未知', color: '#718096', bg: '#eef2f7' },
  skip: { text: '跳过', color: '#718096', bg: '#eef2f7' },
}
const groupStatus = (items) => {
  const rank = { fail: 3, warn: 2, unknown: 1, ok: 0 }
  return items.reduce((s, it) => (rank[it.status] > rank[s] ? it.status : s), 'ok')
}

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

async function loadCollectors() {
  try {
    const data = await listMonitorCollectors(app.tenant)
    collectors.value = data.items || []
    if (!collectors.value.some((c) => c.hostname === selectedHost.value)) {
      selectedHost.value = collectors.value[0]?.hostname || ''
    }
  } catch (e) {
    collectors.value = []
  }
}

async function loadAll() {
  loading.value = true
  diagnosis.value = null
  try {
    await Promise.all([loadHealth(), loadTopology(), loadCollectors()])
  } finally {
    loading.value = false
  }
}

async function runDiagnose() {
  if (!canDiagnose.value) return
  diagnosing.value = true
  try {
    diagnosis.value = await runMonitorDiagnose({
      tenant: app.tenant,
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
  const H = 440
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
      const y = items.length === 1 ? H / 2 : 66 + (i * (H - 108)) / (items.length - 1)
      const r = n.layer === 'web' ? 25 : (n.layer === 'storage' ? 21 : (n.layer === 'collector' ? 15 : 18))
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

watch(() => app.tenant, loadAll)
onMounted(loadAll)
</script>

<template>
  <div class="self-monitor">
    <header class="page-head">
      <div>
        <h2>自监测管理</h2>
        <p class="sub">组件健康 · 采集链路 · 全链路拓扑 <span class="tenant">当前：{{ tenantLabel }}</span></p>
      </div>
      <div class="head-actions">
        <el-button :icon="Refresh" :loading="loading" @click="loadAll">刷新</el-button>
      </div>
    </header>

    <div v-if="health" class="kpi-row">
      <div class="kpi">
        <span class="kpi-label">组件健康</span>
        <div class="kpi-value">
          <b :class="health.status">{{ health.summary.ok }}/{{ health.summary.total }}</b>
          <span class="kpi-note">正常</span>
        </div>
        <div class="kpi-foot">异常 {{ health.summary.fail }} · 警告 {{ health.summary.warn }}</div>
      </div>
      <div class="kpi">
        <span class="kpi-label">采集器在线</span>
        <div class="kpi-value">
          <b>{{ topology?.summary.online || 0 }}/{{ topology?.summary.collectors || 0 }}</b>
          <span class="kpi-note">台</span>
        </div>
        <div class="kpi-foot">离线 {{ topology?.summary.offline || 0 }} 台</div>
      </div>
      <div class="kpi">
        <span class="kpi-label">近 15 分钟写入</span>
        <div class="kpi-value"><b>{{ (topology?.summary.recent15m || 0).toLocaleString() }}</b><span class="kpi-note">条</span></div>
        <div class="kpi-foot">按 {{ app.tenant === 'all' ? '全部租户' : tenantLabel }} 聚合</div>
      </div>
      <div class="kpi">
        <span class="kpi-label">链路节点</span>
        <div class="kpi-value"><b>{{ topology?.summary.nodes || 0 }}</b><span class="kpi-note">个</span></div>
        <div class="kpi-foot">连接 {{ topology?.summary.edges || 0 }} 条</div>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="monitor-tabs">
      <el-tab-pane label="组件健康总览" name="health">
        <div v-loading="loading" class="health-wrap">
          <section v-for="g in healthGroups" :key="g.name" class="health-group">
            <div class="group-head">
              <span class="dot" :class="groupStatus(g.items)" />
              <h3>{{ g.name }}</h3>
              <span class="group-count">{{ g.items.filter((i) => i.status === 'ok').length }}/{{ g.items.length }}</span>
            </div>
            <div class="health-list">
              <div v-for="it in g.items" :key="it.id" class="health-row" :class="it.status">
                <span class="state-dot" :class="it.status" />
                <div class="health-main">
                  <div class="health-title">
                    <b>{{ it.name }}</b>
                    <el-tag size="small" :style="{ color: statusMeta[it.status]?.color, background: statusMeta[it.status]?.bg, borderColor: 'transparent' }">
                      {{ it.statusText }}
                    </el-tag>
                    <span v-if="it.latencyMs" class="latency">{{ it.latencyMs }} ms</span>
                  </div>
                  <div class="health-detail">{{ it.detail }}</div>
                </div>
              </div>
            </div>
          </section>
          <el-empty v-if="!healthGroups.length" description="暂无组件健康数据" />
        </div>
        <div class="foot-note">更新于 {{ health?.updatedAt || '—' }}</div>
      </el-tab-pane>

      <el-tab-pane label="采集链路一键诊断" name="diagnose">
        <div class="diag-toolbar">
          <div class="diag-target">
            <span class="label">诊断采集器</span>
            <el-select v-model="selectedHost" clearable placeholder="自动选择首台采集器" style="width: 240px" :disabled="!collectors.length">
              <el-option v-for="c in collectors" :key="c.hostname" :label="`${c.hostname}（${c.tenant}）`" :value="c.hostname" />
            </el-select>
          </div>
          <el-button type="primary" :icon="Link" :loading="diagnosing" :disabled="!canDiagnose" @click="runDiagnose">
            开始一键诊断
          </el-button>
          <span v-if="!canDiagnose" class="no-perm">当前角色无诊断权限</span>
        </div>
        <div v-if="diagnosing" class="diag-running">
          <span class="pulse" /> 正在按真实链路逐段探测，请稍候…
        </div>
        <div v-else-if="diagnosis" class="diag-result">
          <div class="diag-summary" :class="diagnosis.overall">
            <div class="overall">
              <span class="state-dot" :class="diagnosis.overall" />
              <b>{{ statusMeta[diagnosis.overall]?.text || diagnosis.overall }}</b>
              <span class="muted">耗时 {{ diagnosis.durationMs }} ms</span>
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
                  <el-tag size="small" :style="{ color: statusMeta[c.status]?.color, background: statusMeta[c.status]?.bg, borderColor: 'transparent' }">
                    {{ c.statusText }}
                  </el-tag>
                </div>
                <div class="step-detail">{{ c.detail }}</div>
                <div v-if="c.status !== 'ok' && c.action" class="step-action">处理建议：{{ c.action }}</div>
              </div>
            </div>
          </div>
        </div>
        <el-empty v-else description="尚未执行诊断" />
      </el-tab-pane>

      <el-tab-pane label="全链路拓扑" name="topology">
        <div class="topo-head">
          <div class="legend">
            <span><i class="legend-dot ok" />正常</span>
            <span><i class="legend-dot warn" />警告</span>
            <span><i class="legend-dot fail" />异常</span>
            <span><i class="legend-dot unknown" />未知</span>
          </div>
          <span class="muted">节点颜色代表健康状态，连线颜色代表边界状态</span>
        </div>
        <div v-if="topoLayout" class="topo-scroll">
          <svg class="topo-svg" :viewBox="`0 0 ${topoLayout.W} ${topoLayout.H}`" preserveAspectRatio="xMidYMid meet">
            <defs>
              <marker id="topo-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#7d97ae" opacity="0.8" />
              </marker>
            </defs>
            <g class="topo-guides">
              <line v-for="g in topoLayout.guides" :key="g.title" :x1="g.x" :x2="g.x" y1="48" :y2="topoLayout.H - 18" />
              <text v-for="g in topoLayout.guides" :key="`t-${g.title}`" :x="g.x" y="28" text-anchor="middle">{{ g.title }}</text>
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
                <text :y="n.r + 15" text-anchor="middle">{{ n.short }}</text>
                <title>{{ n.title }}</title>
              </g>
            </g>
          </svg>
        </div>
        <el-empty v-else description="拓扑数据加载中" />
        <div class="foot-note">更新于 {{ topology?.updatedAt || '—' }}</div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.self-monitor { padding: 18px 20px 28px; color: #1f3550; }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
.page-head h2 { margin: 0; font-size: 22px; font-weight: 600; letter-spacing: .5px; }
.page-head .sub { margin: 6px 0 0; color: #7086a0; font-size: 13px; }
.tenant { margin-left: 10px; color: #2c7bb6; }
.kpi-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 14px; }
.kpi { border: 1px solid #e2ebf3; border-radius: 10px; padding: 13px 15px; background: linear-gradient(180deg, #fbfdff, #f5f9fc); }
.kpi-label { color: #6e849b; font-size: 12px; }
.kpi-value { display: flex; align-items: baseline; gap: 5px; margin: 3px 0 2px; }
.kpi-value b { font-size: 25px; color: #143b63; font-variant-numeric: tabular-nums; }
.kpi-value b.warn { color: #b7791f; }
.kpi-value b.fail { color: #c53030; }
.kpi-note { font-size: 12px; color: #8094aa; }
.kpi-foot { font-size: 12px; color: #8b9daf; }
.monitor-tabs :deep(.el-tabs__header) { margin-bottom: 10px; }
.health-wrap { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 14px; }
.health-group { border: 1px solid #e4edf5; border-radius: 10px; background: #fff; overflow: hidden; }
.group-head { display: flex; align-items: center; gap: 8px; padding: 11px 14px; background: #f5f9fc; border-bottom: 1px solid #e8f0f7; }
.group-head h3 { margin: 0; font-size: 14px; font-weight: 600; }
.group-count { margin-left: auto; font-size: 12px; color: #7f93a8; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #a0aec0; }
.dot.ok { background: #1f9d55; }
.dot.warn { background: #d69e2e; }
.dot.fail { background: #c53030; }
.health-list { padding: 4px 0; }
.health-row { display: flex; gap: 9px; padding: 10px 14px; border-bottom: 1px dashed #eef3f8; }
.health-row:last-child { border-bottom: 0; }
.state-dot { flex: 0 0 auto; width: 7px; height: 7px; border-radius: 50%; margin-top: 7px; background: #a0aec0; }
.state-dot.ok { background: #1f9d55; }
.state-dot.warn { background: #d69e2e; }
.state-dot.fail { background: #c53030; }
.state-dot.unknown, .state-dot.skip { background: #a0aec0; }
.health-main { flex: 1; min-width: 0; }
.health-title { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.health-title b { font-size: 13px; }
.latency { font-size: 11px; color: #8a9db2; font-variant-numeric: tabular-nums; }
.health-detail { margin-top: 4px; font-size: 12px; color: #647b92; line-height: 1.5; word-break: break-all; }
.foot-note { margin-top: 12px; font-size: 11px; color: #94a6b8; }
.diag-toolbar { display: flex; align-items: center; gap: 14px; padding: 12px 14px; border: 1px solid #e4edf5; border-radius: 10px; background: #f8fbfd; }
.diag-target { display: flex; align-items: center; gap: 9px; }
.diag-target .label { font-size: 13px; color: #5f778f; }
.no-perm { font-size: 12px; color: #a06a1f; }
.diag-running { display: flex; align-items: center; gap: 8px; padding: 26px 6px; color: #53718e; font-size: 13px; }
.pulse { width: 9px; height: 9px; border-radius: 50%; background: #2c7bb6; animation: pulse 1.2s infinite; }
@keyframes pulse { 0%, 100% { opacity: .35; transform: scale(.85); } 50% { opacity: 1; transform: scale(1.15); } }
.diag-result { margin-top: 12px; }
.diag-summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 15px; border-radius: 10px; border: 1px solid #e4edf5; background: #f8fbfd; }
.diag-summary.warn { border-color: #f3dfb9; background: #fffaf0; }
.diag-summary.fail { border-color: #f3c7c7; background: #fff6f6; }
.overall { display: flex; align-items: center; gap: 8px; }
.overall .state-dot { margin-top: 0; width: 9px; height: 9px; }
.muted { color: #8a9db2; font-size: 12px; }
.chips { display: flex; gap: 8px; }
.chip { font-size: 12px; padding: 3px 9px; border-radius: 12px; }
.chip.ok { color: #1f7a49; background: #e8f7ee; }
.chip.warn { color: #95651b; background: #fdf5e6; }
.chip.fail { color: #a52a2a; background: #fdecec; }
.steps { margin-top: 12px; display: flex; flex-direction: column; gap: 8px; }
.step { display: flex; gap: 10px; padding: 11px 14px; border: 1px solid #e7eef5; border-radius: 9px; background: #fff; }
.step.fail { border-left: 3px solid #c53030; }
.step.warn { border-left: 3px solid #d69e2e; }
.step.ok { border-left: 3px solid #1f9d55; }
.step-index { width: 20px; height: 20px; border-radius: 50%; background: #eef4f9; color: #6f8499; font-size: 11px; display: flex; align-items: center; justify-content: center; flex: 0 0 auto; }
.step-body { flex: 1; min-width: 0; }
.step-title { display: flex; align-items: center; gap: 8px; }
.step-detail { margin-top: 4px; font-size: 12px; color: #5f778f; line-height: 1.6; word-break: break-all; }
.step-action { margin-top: 4px; font-size: 12px; color: #94651c; }
.topo-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 4px; flex-wrap: wrap; }
.legend { display: flex; gap: 14px; font-size: 12px; color: #5d778f; }
.legend span { display: flex; align-items: center; gap: 5px; }
.legend-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.legend-dot.ok { background: #1f9d55; }
.legend-dot.warn { background: #d69e2e; }
.legend-dot.fail { background: #c53030; }
.legend-dot.unknown { background: #a0aec0; }
.topo-scroll { width: 100%; overflow-x: auto; border: 1px solid #e4edf5; border-radius: 10px; background: radial-gradient(circle at 20% 15%, #fbfdff, #f3f8fc 70%); }
.topo-svg { display: block; width: 100%; height: 440px; }
.topo-guides line { stroke: #e8f0f7; stroke-width: 1; stroke-dasharray: 4 6; }
.topo-guides text { fill: #8aa0b5; font-size: 11px; }
.edge { stroke-width: 1.5; opacity: .58; transition: opacity .15s ease, stroke-width .15s ease; }
.edge:hover { opacity: 1; stroke-width: 3; }
.node circle { stroke: #ffffff; stroke-width: 2.5; filter: drop-shadow(0 3px 4px rgba(15, 42, 74, .18)); cursor: default; }
.node:hover circle { stroke: #0b3b66; stroke-width: 3; }
.node text { fill: #25435f; font-size: 10px; pointer-events: none; }
@media (max-width: 1100px) {
  .kpi-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .topo-svg { height: 460px; }
}
</style>
