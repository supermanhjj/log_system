<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Download, Upload, Setting } from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import {
  AGENT_STATUS_OPTIONS, listAgents, getAgent, updateAgent, sendCommand,
  setAgentStatus, listUpgradeTasks, createUpgradeTask, cancelUpgradeTask, installConfig,
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
const fields = ref({})
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
const editForm = reactive({
  ip: '', version: '', region: '', os: '', status_override: '',
  heartbeat_timeout_sec: 180, host_values: [], source_keys: [],
  fieldMap: { time: '', source: '', host: '' }, tags: [], note: '',
})

// 批量升级
const upgradeVisible = ref(false)
const upgradeSaving = ref(false)
const upgradeForm = reactive({ target_version: '', concurrency: 1, skip_offline: true, note: '' })

// 下载接入配置
const installVisible = ref(false)
const installHost = ref('new-host')

const allHostValues = computed(() => [...new Set(
  tableData.value.flatMap((a) => Object.keys(a.coverageHosts || {})),
)].sort())
const allSourceKeys = computed(() => [...new Set(
  tableData.value.flatMap((a) => Object.keys(a.coverageSources || {})),
)].sort())
const fieldNames = computed(() => Object.keys(fields.value).sort())

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
    fields.value = d.fields || {}
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
  const fm = row.fieldMap || {}
  Object.assign(editForm, {
    ip: editIpAuto.value ? '' : (row.ip === '—' ? '' : row.ip),
    version: row.version === '—' ? '' : row.version,
    region: row.region === '—' ? '' : row.region,
    os: row.os || '', status_override: row.statusOverride || '',
    heartbeat_timeout_sec: row.heartbeatTimeoutSec || timeoutDefault.value,
    host_values: [...(row.hostValues || [])],
    source_keys: [...(row.sourceKeys || [])],
    fieldMap: { time: fm.time || '', source: fm.source || '', host: fm.host || '' },
    tags: [...(row.tags || [])], note: row.note || '',
  })
  editVisible.value = true
}
async function saveEdit() {
  editSaving.value = true
  try {
    const fm = {}
    for (const k of ['time', 'source', 'host']) if (editForm.fieldMap[k]) fm[k] = editForm.fieldMap[k]
    await updateAgent(editHost.value, {
      ip: editForm.ip || null, version: editForm.version || null,
      region: editForm.region || null, os: editForm.os || null,
      status_override: editForm.status_override || null,
      heartbeat_timeout_sec: Number(editForm.heartbeat_timeout_sec),
      host_values: editForm.host_values, source_keys: editForm.source_keys,
      field_map: fm, tags: editForm.tags, note: editForm.note || null,
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
async function onInstall() {
  try {
    const name = await installConfig(installHost.value.trim() || 'new-host', app.tenant)
    ElMessage.success(`接入配置已下载：${name}`)
    installVisible.value = false
  } catch (e) { ElMessage.error('下载失败：' + (e.response?.data?.detail || e.message)) }
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
          <el-button :icon="Download" @click="installVisible = true">下载接入配置</el-button>
          <el-button :icon="Upload" @click="openUpgrade">批量升级</el-button>
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
          <el-button link type="primary" size="small" @click="openUpgrade">批量升级</el-button>
          <el-button link size="small" @click="onBatchRestart">批量重启</el-button>
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
              <el-button text size="small" :disabled="row.status === '离线'" @click="onRestart(row)">重启</el-button>
              <el-button text size="small" @click="onMaintain(row)">{{ row.statusOverride ? '结束维护' : '维护' }}</el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-box">
            <p>暂无 Agent，采集端安装并上报心跳后会自动注册</p>
            <el-button size="small" type="primary" @click="installVisible = true">下载接入配置</el-button>
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
            <div class="section-title">采集配置</div>
            <div class="kv"><span>负责 host 值</span><b>{{ detail.agent.hostValues?.join(', ') || '全部（不过滤）' }}</b></div>
            <div class="kv"><span>负责来源</span><b>{{ detail.agent.sourceKeys?.join(', ') || '全部（不过滤）' }}</b></div>
            <div class="kv"><span>字段角色覆盖</span><b>{{ Object.keys(detail.agent.fieldMap || {}).length ? JSON.stringify(detail.agent.fieldMap) : '自动探测' }}</b></div>
            <div class="kv"><span>状态覆盖 / 备注</span><b>{{ detail.agent.statusOverride || '无' }} · {{ detail.agent.note || '—' }}</b></div>
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
        <el-button type="primary" @click="onRestart(detail.agent)">下发重启指令</el-button>
      </template>
    </el-drawer>

    <!-- 编辑配置 -->
    <el-dialog v-model="editVisible" :title="`编辑 Agent · ${editHost}`" width="600px">
      <el-form label-width="140px">
        <el-form-item label="IP 地址"><el-input v-model="editForm.ip" :placeholder="editIpAuto ? `留空自动使用当前观测值 ${editIpAuto}` : '可手工登记，留空则自动获取'" /></el-form-item>
        <el-form-item label="版本"><el-input v-model="editForm.version" placeholder="由心跳上报，可修正" /></el-form-item>
        <el-form-item label="所属区域">
          <el-select v-model="editForm.region" clearable filterable allow-create style="width:100%" placeholder="可输入自定义区域">
            <el-option v-for="r in ['华北', '华东', '华南', '华中', '西南', '西北', '东北']" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
        <el-form-item label="操作系统"><el-input v-model="editForm.os" /></el-form-item>
        <el-form-item label="心跳超时(秒)"><el-input-number v-model="editForm.heartbeat_timeout_sec" :min="30" :max="86400" style="width:180px" />
          <span class="form-hint">默认 {{ timeoutDefault }} 秒</span></el-form-item>
        <el-form-item label="状态覆盖">
          <el-select v-model="editForm.status_override" clearable placeholder="按心跳自动判定" style="width:100%">
            <el-option label="维护中" value="维护中" />
          </el-select>
        </el-form-item>
        <el-divider content-position="left">采集范围（用于反推速率，留空＝该采集端覆盖全部日志）</el-divider>
        <el-form-item label="负责 host 值">
          <el-select v-model="editForm.host_values" multiple filterable allow-create style="width:100%" placeholder="从真实日志 host 中选，或自定义">
            <el-option v-for="h in allHostValues" :key="h" :label="h" :value="h" />
          </el-select>
        </el-form-item>
        <el-form-item label="负责来源">
          <el-select v-model="editForm.source_keys" multiple filterable allow-create style="width:100%" placeholder="从真实日志来源中选，或自定义">
            <el-option v-for="s in allSourceKeys" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-divider content-position="left">字段角色映射（留空＝按真实 mapping 自动探测）</el-divider>
        <el-form-item v-for="(label, key) in { time: '时间字段', source: '来源字段', host: '主机字段' }" :key="key" :label="label">
          <el-select v-model="editForm.fieldMap[key]" clearable filterable style="width:100%" :placeholder="`自动探测`">
            <el-option v-for="f in fieldNames" :key="f" :label="`${f} (${fields[f]})`" :value="f" />
          </el-select>
        </el-form-item>
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

    <!-- 下载接入配置 -->
    <el-dialog v-model="installVisible" title="下载采集端接入配置" width="480px">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        title="按当前环境实时生成可直接落盘的 Fluent Bit 配置：日志采集 + 租户路由 + 心跳上报，不是占位安装包。" />
      <el-form label-width="100px">
        <el-form-item label="目标主机名"><el-input v-model="installHost" placeholder="如 host-10-24-8-21" /></el-form-item>
        <el-form-item label="目标租户"><span class="form-hint">{{ app.tenant }}</span></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="installVisible = false">取消</el-button>
        <el-button type="primary" :icon="Download" @click="onInstall">下载 .conf</el-button>
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
</style>
