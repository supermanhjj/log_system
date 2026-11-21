<script setup>
import { ref, reactive, watch, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Download, Upload, Setting } from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import {
  AGENT_STATUS_OPTIONS, listAgents, getAgent, updateAgent, sendCommand,
  setAgentStatus, listUpgradeTasks, createUpgradeTask, cancelUpgradeTask, installConfig,
  installPackage, listCollectorCredentials, revokeCollectorCredential,
} from '../api/agents'

const app = useAppStore()

const loading = ref(false)
const error = ref(false)
const filters = reactive({ keyword: '', version: '', status: '', region: '' })
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const selection = ref([])
const stats = ref({})
const versions = ref([])
const regions = ref([])
const timeoutDefault = ref(180)

// 升级任务
const activeTask = ref(null)

// 详情
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)

// 编辑
const editVisible = ref(false)
const editSaving = ref(false)
const editHost = ref('')
const editIpAuto = ref('')
// 版本 / 操作系统随心跳自动更新，只读展示；采集范围、字段角色映射、状态覆盖
// 已从表单下线（统一走自动探测 / 全量口径），保存时显式置空以回到默认行为。
const editForm = reactive({
  ip: '', version: '', region: '', os: '',
  heartbeat_timeout_sec: 180, tags: [], note: '',
})

// 批量升级
const upgradeVisible = ref(false)
const upgradeSaving = ref(false)
const upgradeForm = reactive({ target_version: '', concurrency: 1, skip_offline: true, note: '' })

// 接入新主机：生成接入包（可同时签发凭证、登记台账）
const installVisible = ref(false)
const installSaving = ref(false)
const installResult = ref(null)
const gatewayHost = window.location.hostname || 'localhost'
const gatewayPort = Number(window.location.port || (window.location.protocol === 'https:' ? 443 : 80))
const installForm = reactive({
  variant: 'host',
  hostname: '',
  app_name: '',
  address: gatewayHost,
  port: gatewayPort,
  credential: 'create',
  existingKey: '',
  register_host: true,
  cluster_id: 'default',
  multiline: 'ruilog-java',
})

function openInstall(hostname = '') {
  installForm.hostname = hostname
  installResult.value = null
  installVisible.value = true
}

async function onInstall() {
  const hostname = installForm.hostname.trim()
  if (!hostname) return ElMessage.warning('请先填目标主机名')
  installSaving.value = true
  try {
    installResult.value = await installPackage({
      hostname,
      variant: installForm.variant,
      app_name: installForm.app_name.trim(),
      ingest_host: installForm.address.trim(),
      ingest_port: installForm.port,
      agent_host: installForm.address.trim(),
      agent_port: installForm.port,
      cluster_id: installForm.cluster_id.trim() || 'default',
      multiline: installForm.multiline,
      credential: installForm.credential,
      collector_key: installForm.credential === 'existing' ? installForm.existingKey.trim() : '',
      register_host: installForm.variant === 'host' ? installForm.register_host : false,
    })
  } catch (e) {
    ElMessage.error('生成失败：' + (e.response?.data?.detail || e.message))
  } finally {
    installSaving.value = false
  }
}

async function copyCollectorKey() {
  try {
    await navigator.clipboard.writeText(installResult.value.key)
    ElMessage.success('凭证已复制')
  } catch { ElMessage.warning('复制失败，请手动选中复制') }
}

function ledgerText(v) {
  return { ok: '已自动登记', skipped: '未登记', failed: '登记失败', not_admin: '无权限登记' }[v] || v || '—'
}

// 凭证台账
const credVisible = ref(false)
const credLoading = ref(false)
const credError = ref('')
const creds = ref([])

async function openCreds() {
  credVisible.value = true
  await loadCreds()
}
async function loadCreds() {
  credLoading.value = true
  credError.value = ''
  try {
    const d = await listCollectorCredentials()
    creds.value = d.items || []
  } catch (e) {
    credError.value = e.response?.data?.detail || e.message
    creds.value = []
  } finally { credLoading.value = false }
}
async function revokeCred(row) {
  await ElMessageBox.confirm(`撤销凭证 ${row.code}？使用它的采集器会立即上报失败（401）。`, '撤销凭证',
    { type: 'warning' })
  try {
    await revokeCollectorCredential(row.code)
    ElMessage.success('已撤销')
    loadCreds()
  } catch (e) { ElMessage.error('撤销失败：' + (e.response?.data?.detail || e.message)) }
}

// 兼容旧入口：只下载单文件 conf（排障用）
const legacyVisible = ref(false)
const legacyHost = ref('new-host')
async function onLegacyInstall() {
  try {
    const name = await installConfig(legacyHost.value.trim() || 'new-host', app.tenant)
    ElMessage.success(`已下载单文件配置：${name}`)
    legacyVisible.value = false
  } catch (e) { ElMessage.error('下载失败：' + (e.response?.data?.detail || e.message)) }
}

function fmt(n) { return (n ?? 0).toLocaleString('zh-CN') }
function statusTagType(s) {
  return { 运行中: 'success', 告警中: 'warning', 离线: 'info', 维护中: 'info', 异常: 'danger' }[s] || 'info'
}

// ——— 取数 ———
async function loadTasks() {
  try {
    const d = await listUpgradeTasks(5)
    activeTask.value = d.active
  } catch (e) { console.warn('upgrade tasks load failed', e) }
}
async function load(showTip = false) {
  loading.value = true
  error.value = false
  try {
    const d = await listAgents({
      keyword: filters.keyword, version: filters.version, status: filters.status,
      region: filters.region, page: page.value, pageSize: pageSize.value,
    })
    tableData.value = d.items
    total.value = d.total
    stats.value = d.stats || {}
    versions.value = d.versions || []
    regions.value = d.regions || []
    timeoutDefault.value = d.timeoutDefault || 180
    if (showTip) ElMessage.success(`已刷新，共 ${d.total} 台 Agent`)
  } catch (e) {
    error.value = true
    ElMessage.error('Agent 加载失败：' + (e.response?.data?.detail || e.message))
  } finally { loading.value = false }
}
function onSearch() { page.value = 1; load(false) }
function onReset() {
  Object.assign(filters, { keyword: '', version: '', status: '', region: '' })
  page.value = 1; load(false)
}
function onRefresh() { load(true); loadTasks() }
function onPageChange(p) { page.value = p; load(false) }
function onSizeChange(s) { pageSize.value = s; page.value = 1; load(false) }
watch(() => app.tenant, () => { page.value = 1; load(false) })

// ——— 详情 ———
async function openDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try { detail.value = await getAgent(row.hostname) }
  catch (e) { ElMessage.error('详情加载失败：' + (e.response?.data?.detail || e.message)) }
  finally { detailLoading.value = false }
}

const IP_SOURCE_TIP = {
  '人工登记': '人工登记（不会被心跳覆盖）',
  'Agent 自报': '采集端心跳上报，随心跳更新',
  '服务端观测': '服务端接收心跳时观测到的来源地址，随心跳自动更新',
}
function ipTip(row) {
  const base = IP_SOURCE_TIP[row.ipSource] || ''
  return row.observedIp && row.ip !== row.observedIp
    ? `${base}｜当前自动观测值 ${row.observedIp}` : base
}

// ——— 编辑 ———
function openEdit(row) {
  editHost.value = row.hostname
  // 自动观测值不预填，避免用户只是打开配置再保存就把它固化成人工登记值
  editIpAuto.value = (row.ipSource === '服务端观测' || row.ipSource === 'Agent 自报') ? row.ip : ''
  Object.assign(editForm, {
    ip: editIpAuto.value ? '' : (row.ip === '—' ? '' : row.ip),
    version: row.version === '—' ? '' : row.version,
    region: row.region === '—' ? '' : row.region,
    os: row.os || '',
    heartbeat_timeout_sec: row.heartbeatTimeoutSec || timeoutDefault.value,
    tags: [...(row.tags || [])], note: row.note || '',
  })
  editVisible.value = true
}
async function saveEdit() {
  editSaving.value = true
  try {
    await updateAgent(editHost.value, {
      ip: editForm.ip || null, version: editForm.version || null,
      region: editForm.region || null, os: editForm.os || null,
      heartbeat_timeout_sec: Number(editForm.heartbeat_timeout_sec),
      tags: editForm.tags, note: editForm.note || null,
      // 以下三项已从表单下线：显式置空，回到「按心跳自动判定状态 / 统计覆盖全部日志 /
      // 字段角色自动探测」的默认行为，避免历史配置在页面上不可见却仍然生效。
      status_override: null, host_values: [], source_keys: [], field_map: {},
      config: {}, extra: {},
    })
    ElMessage.success('Agent 元数据已更新')
    editVisible.value = false
    load(false)
  } catch (e) { ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message)) }
  finally { editSaving.value = false }
}

// ——— 指令 ———
async function onRestart(row) {
  try {
    const r = await sendCommand(row.hostname, 'restart', { reason: '页面手动下发' }, '页面重启指令')
    ElMessage.success(`重启指令已入队（#${r.commandId}），将在下一次心跳下发`)
    if (detailVisible.value && detail.value?.agent?.hostname === row.hostname) openDetail(row)
  } catch (e) { ElMessage.error('下发失败：' + (e.response?.data?.detail || e.message)) }
}
async function onMaintain(row) {
  const next = row.statusOverride ? '' : '维护中'
  try {
    await setAgentStatus(row.hostname, next)
    ElMessage.success(next ? '已置为维护中' : '已恢复正常状态判定')
    load(false)
  } catch (e) { ElMessage.error('操作失败：' + (e.response?.data?.detail || e.message)) }
}
async function onBatchRestart() {
  if (!selection.value.length) { ElMessage.warning('请先选择 Agent'); return }
  await ElMessageBox.confirm(`向 ${selection.value.length} 台 Agent 下发重启指令？`, '批量重启', { type: 'warning' })
  for (const a of selection.value) {
    try { await sendCommand(a.hostname, 'restart', { reason: '批量重启' }, '批量指令') }
    catch (_) { /* 逐台失败由下方统一提示 */ }
  }
  ElMessage.success('批量指令已下发')
  load(false)
}
function openUpgrade() {
  upgradeForm.target_version = ''
  upgradeForm.concurrency = 1
  upgradeForm.skip_offline = true
  upgradeForm.note = ''
  upgradeVisible.value = true
}
async function submitUpgrade() {
  if (!upgradeForm.target_version.trim()) { ElMessage.warning('请填写目标版本'); return }
  upgradeSaving.value = true
  try {
    const r = await createUpgradeTask({
      hostnames: selection.value.map((x) => x.hostname),
      target_version: upgradeForm.target_version.trim(),
      concurrency: upgradeForm.concurrency,
      skip_offline: upgradeForm.skip_offline,
      created_by: '黄金金', note: upgradeForm.note || null,
    })
    ElMessage.success(`升级任务 #${r.taskId} 已创建，目标 ${r.targets} 台${r.skipped?.length ? `，跳过离线 ${r.skipped.length} 台` : ''}`)
    upgradeVisible.value = false
    loadTasks(); load(false)
  } catch (e) { ElMessage.error('创建失败：' + (e.response?.data?.detail || e.message)) }
  finally { upgradeSaving.value = false }
}
async function onCancelTask() {
  if (!activeTask.value) return
  await ElMessageBox.confirm(`确认取消升级任务 #${activeTask.value.id}？未确认的指令会标记为失败。`, '取消升级', { type: 'warning' })
  try {
    await cancelUpgradeTask(activeTask.value.id)
    ElMessage.success('任务已取消')
    loadTasks()
  } catch (e) { ElMessage.error('操作失败：' + (e.response?.data?.detail || e.message)) }
}
let timer = null
onMounted(() => {
  load(false); loadTasks()
  timer = setInterval(() => {
    if (!detailVisible.value && !editVisible.value && !upgradeVisible.value && !installVisible.value) {
      load(false); loadTasks()
    }
  }, 30000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page" v-loading="loading">
    <!-- 页头 -->
    <div class="page-head">
      <div class="breadcrumb"><span>数据接入</span><span class="sep">/</span><span class="cur">Agent 管理</span></div>
      <div class="head-bar">
        <div>
          <h1 class="title">Agent 管理</h1>
          <p class="desc">采集端安装后心跳自动注册；超过 {{ timeoutDefault / 60 }} 分钟无心跳标记离线</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Download" type="primary" plain @click="openInstall()">接入新主机</el-button>
          <el-button :icon="Setting" @click="openCreds">凭证台账</el-button>
          <el-button :icon="Upload" disabled title="后续版本开放">批量升级</el-button>
          <el-button :icon="Refresh" @click="onRefresh">刷新</el-button>
        </div>
      </div>
    </div>

    <!-- 升级任务（真实进度） -->
    <el-card v-if="activeTask" shadow="never" class="task-card">
      <div class="task-head">
        <div class="task-title">Agent 升级任务 #{{ activeTask.id }} → {{ activeTask.targetVersion }}</div>
        <div class="task-meta">
          <el-tag :type="activeTask.status === 'running' ? 'success' : 'info'" size="small" effect="light">{{ activeTask.status }}</el-tag>
          <span>{{ activeTask.createdAt }} · 并发 {{ activeTask.concurrency }} · 目标 {{ activeTask.total }} 台</span>
          <el-button link size="small" type="danger" @click="onCancelTask">取消任务</el-button>
        </div>
      </div>
      <div class="progress-row">
        <span>指令下发</span>
        <el-progress :percentage="activeTask.dispatchPct" :stroke-width="10" style="flex:1" />
        <span class="progress-num">{{ activeTask.delivered }} / {{ activeTask.total }}</span>
      </div>
      <div class="progress-row">
        <span>Agent 确认</span>
        <el-progress :percentage="activeTask.ackPct" :stroke-width="10" status="warning" style="flex:1" />
        <span class="progress-num">{{ activeTask.acked }} / {{ activeTask.total }}（失败 {{ activeTask.failed }}）</span>
      </div>
      <div class="task-note">
        当前采集端为原生 Fluent Bit dummy 心跳（单向 HTTP 上报），不消费响应体也不回传 ack，
        因此「Agent 确认」进度会停在真实值 0。指令已真实入库并在心跳响应中下发；接入随包管控脚本
        调用 <code>POST /commands/{id}/ack</code> 后该进度会自动推进。
      </div>
      <div class="cmd-chips">
        <el-tag v-for="c in activeTask.commands" :key="c.id" size="small"
          :type="c.status === 'acked' ? 'success' : c.status === 'failed' ? 'danger' : 'info'" effect="plain">
          {{ c.hostname }} · {{ c.status }}
        </el-tag>
      </div>
    </el-card>

    <!-- 状态说明条 -->
    <div class="info-bar">
      <el-tag type="success" size="small" effect="light">在线 {{ stats.online || 0 }}</el-tag>
      <el-tag type="warning" size="small" effect="light">告警 {{ stats.warning || 0 }}</el-tag>
      <el-tag type="info" size="small" effect="light">离线 {{ stats.offline || 0 }}</el-tag>
      <el-tag size="small" effect="light">维护 {{ stats.maintain || 0 }}</el-tag>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon title="Agent 加载失败" style="margin-bottom:12px">
      <template #default>
        <div class="alert-body"><span>数据加载失败，请稍后重试</span>
          <el-button size="small" type="danger" plain @click="load(false)">重试</el-button></div>
      </template>
    </el-alert>

    <!-- 筛选 -->
    <div class="filter-card">
      <div class="filter-row">
        <div class="filter-item"><label>主机名 / IP</label>
          <el-input v-model="filters.keyword" placeholder="请输入主机名或 IP" clearable @keyup.enter="onSearch" />
        </div>
        <div class="filter-item"><label>Agent 版本</label>
          <el-select v-model="filters.version" placeholder="全部版本" clearable>
            <el-option v-for="v in versions" :key="v" :label="v" :value="v" />
          </el-select>
        </div>
        <div class="filter-item"><label>运行状态</label>
          <el-select v-model="filters.status" placeholder="全部状态" clearable>
            <el-option v-for="s in AGENT_STATUS_OPTIONS" :key="s" :label="s" :value="s" />
          </el-select>
        </div>
        <div class="filter-item"><label>所属区域</label>
          <el-select v-model="filters.region" placeholder="全部区域" clearable filterable allow-create>
            <el-option v-for="r in regions" :key="r" :label="r" :value="r" />
          </el-select>
        </div>
        <div class="filter-actions">
          <el-button type="primary" @click="onSearch">查询</el-button>
          <el-button @click="onReset">重置</el-button>
        </div>
      </div>
    </div>

    <!-- 列表 -->
    <el-card shadow="never" class="table-card">
      <div class="batch-bar">
        <span>已选 <b>{{ selection.length }}</b> 项</span>
        <div>
          <el-button link size="small" disabled title="后续版本开放">批量升级</el-button>
          <el-button link size="small" disabled title="后续版本开放" @click="onBatchRestart">批量重启</el-button>
        </div>
      </div>
      <el-table :data="tableData" stripe @selection-change="(v) => (selection = v)" row-key="hostname">
        <el-table-column type="selection" width="46" />
        <el-table-column label="主机名" min-width="200">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="name-main" @click="openDetail(row)">{{ row.hostname }}</span>
            </div>
            <div class="tag-row" v-if="row.tags?.length">
              <el-tag v-for="t in row.tags" :key="t" size="small" effect="plain">{{ t }}</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="IP 地址" width="140">
          <template #default="{ row }">
            <el-tooltip v-if="row.ipSource" :content="ipTip(row)" placement="top">
              <span>{{ row.ip }}</span>
            </el-tooltip>
            <span v-else class="muted">{{ row.ip }}</span>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="110">
          <template #default="{ row }"><span>{{ row.version }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tooltip :content="row.statusReason" placement="top">
              <el-tag :type="statusTagType(row.status)" size="small" effect="light">{{ row.status }}</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="最近心跳" width="165">
          <template #default="{ row }">
            <span :class="{ muted: !row.lastHeartbeat }">{{ row.lastHeartbeat || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="采集速率" width="130" align="right" header-align="right">
          <template #default="{ row }">
            <el-tooltip :content="`${row.rateNote}｜近 5min ${fmt(row.last5min)} 条 · 近 24h ${fmt(row.last24h)} 条｜范围：${row.rateScope}`" placement="top">
              <span class="num">{{ row.rate }} <i>条/秒</i></span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="区域" width="90">
          <template #default="{ row }"><span class="muted">{{ row.region }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right" align="center" header-align="center">
          <template #default="{ row }">
            <div class="op-actions">
              <el-button text size="small" @click="openDetail(row)">详情</el-button>
              <el-button text size="small" :icon="Setting" @click="openEdit(row)">配置</el-button>
              <!-- 重启 / 维护：占位按钮，后续版本接入指令队列的消费端后再放开 -->
              <el-button text size="small" disabled title="后续版本开放" @click="onRestart(row)">重启</el-button>
              <el-button text size="small" disabled title="后续版本开放" @click="onMaintain(row)">维护</el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-box">
            <p>暂无 Agent，采集端安装并上报心跳后会自动注册</p>
            <el-button size="small" type="primary" @click="openInstall()">接入新主机</el-button>
          </div>
        </template>
      </el-table>
      <div class="pager">
        <span>共 {{ total }} 台 Agent</span>
        <div class="pager-right">
          <div class="page-size">
            <span>每页</span>
            <el-select v-model="pageSize" size="small" @change="onSizeChange">
              <el-option v-for="s in [20, 50, 100]" :key="s" :label="s" :value="s" />
            </el-select>
            <span>条</span>
          </div>
          <el-pagination background layout="prev, pager, next" :total="total"
            :current-page="page" :page-size="pageSize"
            @current-change="onPageChange" />
        </div>
      </div>
    </el-card>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" title="Agent 详情" size="620px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="detail-head">
            <div>
              <div class="detail-title">{{ detail.agent.hostname }}</div>
              <div class="detail-sub" :title="detail.agent.ipSource ? `IP 来源：${detail.agent.ipSource}` : ''">{{ detail.agent.ip }} · {{ detail.agent.version }} · {{ detail.agent.region }}</div>
            </div>
            <el-tag :type="statusTagType(detail.agent.status)" effect="light">{{ detail.agent.status }}</el-tag>
          </div>
          <el-alert type="info" :closable="false" :title="detail.agent.statusReason" style="margin-bottom:14px" />

          <div class="metric-grid">
            <div class="metric"><span>累计心跳</span><b>{{ fmt(detail.heartbeat.count) }}</b><i>次</i></div>
            <div class="metric"><span>心跳间隔</span><b>{{ detail.agent.avgIntervalSec ?? '—' }}</b><i>秒</i></div>
            <div class="metric"><span>采集速率</span><b>{{ detail.logStats.rate }}</b><i>条/秒</i></div>
            <div class="metric"><span>近 24h 日志</span><b>{{ fmt(detail.logStats.last24h) }}</b><i>条</i></div>
          </div>

          <div class="section">
            <div class="section-title">心跳信息</div>
            <div class="kv"><span>最近心跳（服务端）</span><b>{{ detail.agent.lastHeartbeat || '—' }}</b></div>
            <div class="kv"><span>客户端上报 ts</span><b>{{ detail.heartbeat.clientTs || '—' }}</b></div>
            <div class="kv"><span>Redis 状态</span><b>{{ detail.heartbeat.redis ? '已写入' : '不可用（降级用 PG last_seen）' }}</b></div>
            <div class="kv"><span>超时阈值 / 首次注册</span><b>{{ detail.agent.timeoutSec }} 秒 · {{ detail.agent.firstSeen }}</b></div>
            <div class="kv"><span>近期心跳间隔</span><b>{{ (detail.heartbeat.intervals || []).join('s, ') || '—' }}{{ detail.heartbeat.intervals?.length ? 's' : '' }}</b></div>
          </div>

          <div class="section">
            <div class="section-title">采集覆盖</div>
            <div class="kv"><span>统计范围</span><b>{{ detail.logStats.scope }}</b></div>
            <div class="kv"><span>字段角色</span><b>{{ detail.roles.time || '—' }} / {{ detail.roles.source || '—' }} / {{ detail.roles.host || '—' }}</b></div>
            <div class="two-col">
              <div>
                <div class="sub-title">主机分布（近 24h）</div>
                <div v-for="(v, k) in detail.logStats.hosts" :key="k" class="kv"><span>{{ k }}</span><b>{{ fmt(v) }}</b></div>
                <div v-if="!Object.keys(detail.logStats.hosts || {}).length" class="muted">无数据</div>
              </div>
              <div>
                <div class="sub-title">来源分布（近 24h）</div>
                <div v-for="(v, k) in detail.logStats.sources" :key="k" class="kv"><span>{{ k }}</span><b>{{ fmt(v) }}</b></div>
                <div v-if="!Object.keys(detail.logStats.sources || {}).length" class="muted">无数据</div>
              </div>
            </div>
          </div>

          <div class="section">
            <div class="section-title">标签与备注</div>
            <div class="kv"><span>标签</span><b>{{ detail.agent.tags?.join('、') || '—' }}</b></div>
            <div class="kv"><span>备注</span><b>{{ detail.agent.note || '—' }}</b></div>
          </div>

          <div class="section">
            <div class="section-title">指令历史</div>
            <div v-for="c in detail.commands" :key="c.id" class="cmd-line">
              <el-tag size="small" :type="c.status === 'acked' ? 'success' : c.status === 'failed' ? 'danger' : 'info'" effect="plain">{{ c.status }}</el-tag>
              <span>#{{ c.id }} {{ c.type }}</span><span class="muted">{{ c.createdAt }}</span>
            </div>
            <div v-if="!detail.commands.length" class="muted">暂无指令</div>
          </div>
        </template>
      </div>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <el-button :icon="Setting" @click="openEdit(detail.agent)">编辑配置</el-button>
        <el-button type="primary" disabled title="后续版本开放" @click="onRestart(detail.agent)">下发重启指令</el-button>
      </template>
    </el-drawer>

    <!-- 编辑配置 -->
    <el-dialog v-model="editVisible" :title="`编辑 Agent · ${editHost}`" width="600px">
      <el-form label-width="140px">
        <el-form-item label="IP 地址"><el-input v-model="editForm.ip" :placeholder="editIpAuto ? `留空自动使用当前观测值 ${editIpAuto}` : '可手工登记，留空则自动获取'" /></el-form-item>
        <el-form-item label="版本">
          <el-input :model-value="editForm.version || '—'" disabled />
          <span class="form-hint-block">由心跳自动上报，仅查看</span>
        </el-form-item>
        <el-form-item label="所属区域">
          <el-input v-model="editForm.region" clearable maxlength="32"
            placeholder="手工填写，如 华北 / 华东 / 自建机房-A" />
        </el-form-item>
        <el-form-item label="操作系统">
          <el-input :model-value="editForm.os || '—'" disabled />
          <span class="form-hint-block">由心跳自动上报，仅查看</span>
        </el-form-item>
        <el-form-item label="心跳超时(秒)"><el-input-number v-model="editForm.heartbeat_timeout_sec" :min="30" :max="86400" style="width:180px" />
          <span class="form-hint">默认 {{ timeoutDefault }} 秒</span></el-form-item>
        <el-form-item label="标签"><el-select v-model="editForm.tags" multiple filterable allow-create default-first-option style="width:100%" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="editForm.note" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 批量升级 -->
    <el-dialog v-model="upgradeVisible" title="批量升级 Agent" width="520px">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        title="服务端按滚动方式逐台生成升级指令，随心跳下发；离线主机默认跳过。" />
      <el-form label-width="120px">
        <el-form-item label="目标版本" required><el-input v-model="upgradeForm.target_version" placeholder="如 5.2.0 或自定义版本标识" /></el-form-item>
        <el-form-item label="滚动并发"><el-input-number v-model="upgradeForm.concurrency" :min="1" :max="50" /></el-form-item>
        <el-form-item label="跳过离线"><el-switch v-model="upgradeForm.skip_offline" /></el-form-item>
        <el-form-item label="范围"><span class="form-hint">{{ selection.length ? `已选 ${selection.length} 台：${selection.map(x => x.hostname).join(', ')}` : '未选择＝全部在线 Agent' }}</span></el-form-item>
        <el-form-item label="备注"><el-input v-model="upgradeForm.note" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="upgradeVisible = false">取消</el-button>
        <el-button type="primary" :loading="upgradeSaving" @click="submitUpgrade">开始升级</el-button>
      </template>
    </el-dialog>

    <!-- 接入新主机 -->
    <el-dialog v-model="installVisible" title="接入新主机" width="640px" :close-on-click-modal="false">
      <template v-if="!installResult">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom:14px"
          title="按参数生成可直接落地的接入包（tar.gz）：Fluent Bit 主配置 + parsers.conf + 安装脚本 + 采集器凭证，不是占位样例。" />

        <el-form label-width="96px" label-position="left">
          <el-form-item label="接入形态">
            <el-radio-group v-model="installForm.variant">
              <el-radio-button value="host">业务主机（集群外）</el-radio-button>
              <el-radio-button value="container">集群内容器</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="主机名" required>
            <el-input v-model="installForm.hostname" placeholder="如 host-10-24-8-21 / order-node-1" />
            <div class="form-tip">主机形态填机器名；容器形态填 K8s 节点名（用于 NODE_NAME 与归属）</div>
          </el-form-item>
          <el-form-item label="应用名">
            <el-input v-model="installForm.app_name" placeholder="留空则用主机名" />
            <div class="form-tip">只写入 app 字段做检索分组，不影响租户归属</div>
          </el-form-item>
          <el-form-item label="平台地址">
            <div class="inline-row">
              <el-input v-model="installForm.address" placeholder="平台对外地址或 IP" style="flex:1" />
              <el-input-number v-model="installForm.port" :min="1" :max="65535" controls-position="right" style="width:120px" />
            </div>
            <div class="form-tip">已按当前浏览器地址预填；集群外主机必须能访问该地址下的 /heartbeat 与 /ingest/</div>
          </el-form-item>
          <template v-if="installForm.variant === 'container'">
            <el-form-item label="集群 ID">
              <el-input v-model="installForm.cluster_id" placeholder="default" />
            </el-form-item>
            <el-form-item label="多行合并">
              <el-select v-model="installForm.multiline" style="width:100%">
                <el-option label="Java 堆栈（ruilog-java）" value="ruilog-java" />
                <el-option label="Go 堆栈（ruilog-go）" value="ruilog-go" />
                <el-option label="Python traceback（ruilog-python）" value="ruilog-python" />
                <el-option label="不合并（一行一条）" value="none" />
              </el-select>
            </el-form-item>
          </template>
          <el-form-item label="采集器凭证">
            <el-radio-group v-model="installForm.credential">
              <el-radio value="create">生成新凭证（推荐）</el-radio>
              <el-radio value="existing">用已有凭证</el-radio>
              <el-radio value="none">先不填</el-radio>
            </el-radio-group>
            <el-input v-if="installForm.credential === 'existing'" v-model="installForm.existingKey"
              placeholder="rk_xxx" show-password style="margin-top:8px" />
            <div class="form-tip">服务端只存 hash，明文仅在生成时返回一次；不填则包内为占位符，采集器会 401</div>
          </el-form-item>
          <el-form-item v-if="installForm.variant === 'host'" label="主机台账">
            <el-checkbox v-model="installForm.register_host">自动登记到 hosts 台账（需要平台管理员）</el-checkbox>
            <div class="form-tip">不登记则日志文件名必须是 &lt;租户&gt;.log，否则入口无法判定归属而拒收</div>
          </el-form-item>
        </el-form>
      </template>

      <template v-else>
        <el-result icon="success" title="接入包已生成" :sub-title="installResult.name" style="padding:10px 0" />
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="接入形态">
            {{ installResult.variant === 'host' ? '业务主机（集群外）' : '集群内容器' }}
          </el-descriptions-item>
          <el-descriptions-item label="凭证编号">{{ installResult.code || '未签发' }}</el-descriptions-item>
          <el-descriptions-item label="主机台账">{{ ledgerText(installResult.ledger) }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="installResult.key" class="key-box">
          <div class="key-head">
            <span>凭证明文（只显示这一次，关闭后无法找回）</span>
            <el-button size="small" type="primary" link @click="copyCollectorKey">复制</el-button>
          </div>
          <code class="key-value">{{ installResult.key }}</code>
        </div>
        <el-alert v-if="installResult.ledger === 'failed' || installResult.ledger === 'not_admin'"
          type="warning" :closable="false" show-icon style="margin-top:12px"
          :title="installResult.ledger === 'not_admin'
            ? '当前账号无台账权限，请在数据源页由管理员登记该主机，否则日志会被拒收'
            : '台账登记失败，请在数据源页补登记该主机，否则日志会被拒收'" />
      </template>

      <template #footer>
        <el-button link size="small" style="float:left" @click="legacyVisible = true">排障：单文件 .conf</el-button>
        <el-button v-if="!installResult" @click="installVisible = false">取消</el-button>
        <el-button v-if="!installResult" type="primary" :loading="installSaving" :icon="Download" @click="onInstall">生成并下载</el-button>
        <template v-else>
          <el-button @click="installVisible = false">完成</el-button>
          <el-button type="primary" @click="openInstall()">再接一台</el-button>
        </template>
      </template>
    </el-dialog>

    <!-- 凭证台账 -->
    <el-dialog v-model="credVisible" title="采集器凭证台账" width="640px">
      <el-alert type="info" :closable="false" show-icon style="margin-bottom:12px"
        title="只展示凭证编号与使用情况，明文不落库、无法查看；撤销后该采集器立即上报失败。" />
      <div v-if="credError" class="cred-error">{{ credError }}</div>
      <el-table v-else :data="creds" v-loading="credLoading" size="small">
        <el-table-column prop="code" label="凭证编号" min-width="200" />
        <el-table-column prop="tenantAllow" label="允许租户" min-width="100" />
        <el-table-column prop="lastSeen" label="最近使用" min-width="130">
          <template #default="{ row }">{{ row.lastSeen || '未使用' }}</template>
        </el-table-column>
        <el-table-column prop="createdAt" label="签发时间" min-width="130" />
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button text size="small" type="danger" @click="revokeCred(row)">撤销</el-button>
          </template>
        </el-table-column>
        <template #empty><div class="empty-box"><p>暂无已签发凭证</p></div></template>
      </el-table>
      <template #footer>
        <el-button @click="credVisible = false">关闭</el-button>
        <el-button :icon="Refresh" @click="loadCreds">刷新</el-button>
      </template>
    </el-dialog>

    <!-- 旧入口：单文件 conf（排障用） -->
    <el-dialog v-model="legacyVisible" title="下载单文件 Fluent Bit 配置" width="480px">
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom:12px"
        title="单文件不含 parsers.conf 与凭证，目标机仍需手工补齐；正式接入请用「接入新主机」。" />
      <el-form label-width="90px">
        <el-form-item label="主机名"><el-input v-model="legacyHost" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="legacyVisible = false">取消</el-button>
        <el-button @click="onLegacyInstall">下载 .conf</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.page-head { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.breadcrumb { font-size: 12px; color: var(--color-text-3); }
.breadcrumb .sep { margin: 0 6px; }
.breadcrumb .cur { color: var(--color-text-2); }
.head-bar { display: flex; justify-content: space-between; align-items: flex-end; margin-top: 8px; gap: 12px; }
.title { font-size: 18px; font-weight: 600; color: var(--color-text-1); margin: 0; }
.desc { font-size: 13px; color: var(--color-text-3); margin: 4px 0 0; }
.head-actions { display: flex; gap: 8px; flex-shrink: 0; }

.task-card :deep(.el-card__body) { padding: 12px 16px 14px; }
.task-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.task-title { font-size: 14px; font-weight: 600; }
.task-meta { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--color-text-3); }
.progress-row { display: flex; align-items: center; gap: 12px; font-size: 13px; margin: 6px 0; }
.progress-row > span:first-child { width: 72px; color: var(--color-text-2); }
.progress-num { width: 150px; text-align: right; font-variant-numeric: tabular-nums; color: var(--color-text-2); }
.task-note { font-size: 12px; line-height: 1.6; color: var(--color-text-3); margin-top: 8px; }
.task-note code { background: #F0F2F5; padding: 1px 4px; border-radius: 3px; }
.cmd-chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }

.info-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 10px 16px; }
.alert-body { display: flex; justify-content: space-between; align-items: center; }

.filter-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.filter-row { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; }
.filter-item { display: flex; flex-direction: column; gap: 6px; }
.filter-item label { font-size: 12px; color: var(--color-text-2); }
.filter-item :deep(.el-input), .filter-item :deep(.el-select) { width: 180px; }
.filter-actions { display: flex; gap: 8px; margin-left: auto; }

.table-card :deep(.el-card__body) { padding: 12px 14px 14px; }
.batch-bar { display: flex; align-items: center; gap: 16px; padding: 2px 4px 10px; font-size: 13px; color: var(--color-text-2); }
.name-cell { display: flex; flex-direction: column; gap: 2px; }
.name-main { color: #03857E; font-weight: 600; cursor: pointer; }
.name-main:hover { text-decoration: underline; }
.tag-row { display: flex; gap: 4px; margin-top: 4px; flex-wrap: wrap; }
.num { font-variant-numeric: tabular-nums; font-weight: 600; }
.num i { font-style: normal; font-weight: 400; color: var(--color-text-3); font-size: 12px; }
.muted { color: var(--color-text-3); }
.op-actions { display: flex; align-items: center; justify-content: center; gap: 2px; flex-wrap: nowrap; }
.op-actions .el-button { margin: 0 !important; }
.pager { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; font-size: 13px; color: var(--color-text-3); }
.empty-box { padding: 22px 0; color: var(--color-text-3); }

:deep(.el-table__header th.el-table-column--selection),
:deep(.el-table__body td.el-table-column--selection) { background-color: #fff !important; }
:deep(.el-table__header th.el-table-column--selection) { background-color: #E6FCF8 !important; }
:deep(.el-table__row--striped td.el-table-column--selection) { background-color: #FAFAFA !important; }
:deep(.el-table__row:hover > td.el-table-column--selection) { background-color: #E8F3F1 !important; }

.detail-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
.detail-title { font-size: 17px; font-weight: 600; }
.detail-sub { font-size: 12px; color: var(--color-text-3); margin-top: 4px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 16px; }
.metric { background: #F7FAFA; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 10px; }
.metric span { display: block; font-size: 12px; color: var(--color-text-3); }
.metric b { font-size: 19px; color: #03857E; font-variant-numeric: tabular-nums; }
.metric i { font-style: normal; font-size: 12px; color: var(--color-text-3); margin-left: 3px; }
.section { border-top: 1px solid var(--color-border-1); padding-top: 12px; margin-top: 12px; }
.section-title { font-size: 14px; font-weight: 600; margin-bottom: 10px; }
.sub-title { font-size: 13px; color: var(--color-text-2); margin-bottom: 6px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 8px; }
.kv { display: flex; justify-content: space-between; font-size: 13px; padding: 3px 0; color: var(--color-text-2); gap: 10px; }
.kv b { font-weight: 500; color: var(--color-text-1); word-break: break-all; text-align: right; }
.cmd-line { display: flex; align-items: center; gap: 10px; font-size: 13px; padding: 4px 0; }
.form-hint { font-size: 12px; color: var(--color-text-3); margin-left: 10px; }
.form-hint-block { display: block; font-size: 12px; color: var(--color-text-3); line-height: 1.6; margin-top: 2px; }
.form-tip { font-size: 12px; color: var(--color-text-3); line-height: 1.6; margin-top: 2px; }
.inline-row { display: flex; gap: 8px; width: 100%; align-items: center; }
.key-box { margin-top: 14px; border: 1px solid #FAE3B5; background: #FFFBF2; border-radius: 4px; padding: 10px 12px; }
.key-head { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #8A6A1F; margin-bottom: 6px; }
.key-value { display: block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: #7A4E00; word-break: break-all; background: #fff; border: 1px solid #FAE3B5; border-radius: 3px; padding: 6px 8px; }
.cred-error { padding: 16px 0; color: var(--color-text-3); font-size: 13px; }
</style>
