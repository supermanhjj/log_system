<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus, Download, Setting, Connection } from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import {
  INGEST_TYPES, STATUS_OPTIONS, ROLE_LABELS,
  getMeta, listSources, getSource, createSource, updateSource, deleteSource,
  batchStatus, testConnectivity, exportConfig, getSettings, putSettings,
} from '../api/data'

const app = useAppStore()

const loading = ref(false)
const error = ref(false)
const filters = reactive({ keyword: '', type: '', status: '', region: '', registered: '' })
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const selection = ref([])
const meta = ref({ fields: {}, roles: {}, aggregatableFields: [], settings: {}, capabilities: {} })
const discoveredCount = ref(0)
const registeredCount = ref(0)
const updatedAt = ref('')
const discovery = ref({})

// 详情
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const detailRow = ref(null)

// 新建 / 编辑
const formVisible = ref(false)
const formMode = ref('create')
const saving = ref(false)
const testing = ref(false)
const testResult = ref(null)
const formRef = ref(null)

// 接入设置
const settingsVisible = ref(false)
const settingsSaving = ref(false)
const settingsForm = reactive({})

function emptyForm() {
  return {
    sourceKey: '', displayName: '', ingestType: 'Agent', endpoint: '',
    region: '', encoding: 'UTF-8', parserRule: '', sampleRate: 100,
    targetIndex: '', tenant: app.tenant === 'all' ? 'default' : app.tenant,
    tags: [], note: '', statusOverride: '',
    silenceWarnMin: null, silenceOfflineMin: null,
    fieldMap: { source: '', time: '', level: '', host: '', tenant: '', message: '' },
    extraText: '{}',
  }
}
const form = reactive(emptyForm())

const formRules = {
  sourceKey: [{ required: true, message: '请输入来源标识', trigger: 'blur' }],
  ingestType: [{ required: true, message: '请选择接入类型', trigger: 'change' }],
}

const roleOptions = computed(() => Object.keys(meta.value.fields || {}).sort())
const regionOptions = computed(() => [...new Set(tableData.value.map((x) => x.region).filter((r) => r && r !== '—'))])
const typeOptions = computed(() => ['自动发现', ...INGEST_TYPES])
const caps = computed(() => meta.value.capabilities || {})

function fmt(n) {
  return (n ?? 0).toLocaleString('zh-CN')
}
function statusTagType(s) {
  return { 运行中: 'success', 告警中: 'warning', 异常: 'danger', 离线: 'info', 维护中: 'info', 待接入: 'info' }[s] || 'info'
}
function healthColor(v) {
  if (v == null) return '#909399'
  if (v < 70) return '#E53935'
  if (v < 85) return '#EA720F'
  return '#03857E'
}

// ——— 取数 ———
async function loadMeta() {
  try {
    meta.value = await getMeta(app.tenant)
  } catch (e) {
    // meta 失败不阻断列表，字段下拉会为空并提示
    console.warn('meta load failed', e)
  }
}
async function load(showTip = false) {
  loading.value = true
  error.value = false
  try {
    const d = await listSources({
      tenant: app.tenant, keyword: filters.keyword, type: filters.type,
      status: filters.status, region: filters.region, registered: filters.registered,
      page: page.value, pageSize: pageSize.value,
    })
    tableData.value = d.items
    total.value = d.total
    discoveredCount.value = d.discoveredCount
    registeredCount.value = d.registeredCount
    updatedAt.value = d.updatedAt
    discovery.value = d.discovery || {}
    if (d.fields && Object.keys(d.fields).length) meta.value.fields = d.fields
    if (d.roles) meta.value.roles = d.roles
    if (showTip) ElMessage.success(`已刷新，共 ${d.total} 个数据源`)
  } catch (e) {
    error.value = true
    ElMessage.error('数据源加载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}
function onSearch() { page.value = 1; load(false) }
function onReset() {
  Object.assign(filters, { keyword: '', type: '', status: '', region: '', registered: '' })
  page.value = 1
  load(false)
}
function onRefresh() { loadMeta(); load(true) }
function onPageChange(p) { page.value = p; load(false) }
function onSizeChange(s) { pageSize.value = s; page.value = 1; load(false) }

watch(() => app.tenant, () => { page.value = 1; loadMeta(); load(false) })

// ——— 详情 ———
async function openDetail(row) {
  detailRow.value = row
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getSource(row.sourceKey, app.tenant)
  } catch (e) {
    ElMessage.error('详情加载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    detailLoading.value = false
  }
}

// ——— 新建 / 编辑 ———
function openCreate(row) {
  formMode.value = 'create'
  Object.assign(form, emptyForm())
  if (row) { form.sourceKey = row.sourceKey; form.displayName = row.displayName || '' }
  testResult.value = null
  formVisible.value = true
  formRef.value?.clearValidate?.()
}
function openEdit(row) {
  formMode.value = 'edit'
  const fm = row.fieldMap || {}
  Object.assign(form, {
    sourceKey: row.sourceKey,
    displayName: row.displayName || '',
    ingestType: INGEST_TYPES.includes(row.ingestType) ? row.ingestType : 'Agent',
    endpoint: row.endpoint || '', region: row.region === '—' ? '' : (row.region || ''),
    encoding: row.encoding || 'UTF-8', parserRule: row.parserRule || '',
    sampleRate: row.sampleRate ?? 100, targetIndex: row.targetIndex || '',
    tenant: row.tenant || (app.tenant === 'all' ? 'default' : app.tenant),
    tags: [...(row.tags || [])], note: row.note || '',
    statusOverride: row.statusOverride || '',
    silenceWarnMin: row.silenceWarnMin, silenceOfflineMin: row.silenceOfflineMin,
    fieldMap: { source: fm.source || '', time: fm.time || '', level: fm.level || '',
                host: fm.host || '', tenant: fm.tenant || '', message: fm.message || '' },
    extraText: JSON.stringify(row.extra || {}, null, 2),
  })
  testResult.value = null
  formVisible.value = true
  formRef.value?.clearValidate?.()
}

function toPayload() {
  let extra = {}
  try { extra = JSON.parse(form.extraText || '{}') } catch (_) { extra = {} }
  const fieldMap = {}
  for (const k of Object.keys(ROLE_LABELS)) {
    if (form.fieldMap[k]) fieldMap[k] = form.fieldMap[k]
  }
  return {
    tenant: form.tenant,
    source_key: form.sourceKey.trim(),
    display_name: form.displayName.trim() || null,
    ingest_type: form.ingestType,
    endpoint: form.endpoint.trim() || null,
    region: form.region || null,
    encoding: form.encoding,
    parser_rule: form.parserRule || null,
    sample_rate: Number(form.sampleRate),
    target_index: form.targetIndex.trim() || null,
    field_map: fieldMap,
    tags: form.tags,
    extra,
    status_override: form.statusOverride || null,
    silence_warn_min: form.silenceWarnMin || null,
    silence_offline_min: form.silenceOfflineMin || null,
    owner: '黄金金',
    note: form.note || null,
  }
}
function checkExtra() {
  try { JSON.parse(form.extraText || '{}'); return true }
  catch (_) { ElMessage.error('扩展属性不是合法 JSON'); return false }
}
async function onSubmit() {
  if (!checkExtra()) return
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    saving.value = true
    try {
      if (formMode.value === 'create') await createSource(toPayload())
      else await updateSource(form.sourceKey, toPayload())
      ElMessage.success(formMode.value === 'create' ? '接入配置已登记' : '接入配置已更新')
      formVisible.value = false
      loadMeta(); load(false)
    } catch (e) {
      ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
    } finally {
      saving.value = false
    }
  })
}
async function onTest() {
  testing.value = true
  testResult.value = null
  try {
    const p = toPayload()
    testResult.value = await testConnectivity({
      ingest_type: p.ingest_type, endpoint: p.endpoint || '',
      source_key: p.source_key, tenant: p.tenant, target_index: p.target_index || '',
    })
  } catch (e) {
    testResult.value = { ok: false, detail: e.response?.data?.detail || e.message }
  } finally {
    testing.value = false
  }
}
function onDelete(row) {
  ElMessageBox.confirm(
    `确认移除「${row.name}」的登记配置？已写入 OpenSearch 的日志不会被删除，该来源仍会以「自动发现」形态出现在列表中。`,
    '移除登记配置', { type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消' },
  ).then(async () => {
    try {
      await deleteSource(row.sourceKey, app.tenant)
      ElMessage.success('登记配置已移除')
      load(false)
    } catch (e) { ElMessage.error('操作失败：' + (e.response?.data?.detail || e.message)) }
  }).catch(() => {})
}

// ——— 批量 ———
async function onBatch(status) {
  const keys = selection.value.map((x) => x.sourceKey)
  if (!keys.length) { ElMessage.warning('请先选择数据源'); return }
  try {
    const r = await batchStatus(keys, status, app.tenant)
    ElMessage.success(`已更新 ${r.changed} 个数据源的状态为「${r.status}」`)
    load(false)
  } catch (e) { ElMessage.error('批量操作失败：' + (e.response?.data?.detail || e.message)) }
}
async function onExport() {
  try {
    const name = await exportConfig(app.tenant)
    ElMessage.success(`配置已导出：${name}`)
  } catch (e) { ElMessage.error('导出失败：' + (e.response?.data?.detail || e.message)) }
}

// ——— 接入设置 ———
async function openSettings() {
  try {
    const d = await getSettings()
    Object.assign(settingsForm, d.settings)
  } catch (e) {
    ElMessage.error('设置加载失败：' + (e.response?.data?.detail || e.message))
    return
  }
  settingsVisible.value = true
}
async function saveSettings() {
  settingsSaving.value = true
  try {
    await putSettings({ ...settingsForm })
    ElMessage.success('接入设置已保存，字段角色与阈值立即生效')
    settingsVisible.value = false
    loadMeta(); load(false)
  } catch (e) {
    ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
  } finally { settingsSaving.value = false }
}

// 趋势柱图（纯 CSS，保持轻量）
const trendMax = computed(() => Math.max(1, ...(detail.value?.trend || []).map((x) => x.v)))

let timer = null
onMounted(() => {
  loadMeta(); load(false)
  timer = setInterval(() => {
    if (!formVisible.value && !detailVisible.value && !settingsVisible.value) load(false)
  }, 30000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page" v-loading="loading">
    <!-- 页头 -->
    <div class="page-head">
      <div class="breadcrumb"><span>数据接入</span><span class="sep">/</span><span class="cur">数据源</span></div>
      <div class="head-bar">
        <h1 class="title">数据源</h1>
        <div class="head-actions">
          <el-button :icon="Setting" @click="openSettings">接入设置</el-button>
          <el-button :icon="Refresh" @click="onRefresh">刷新</el-button>
          <el-button :icon="Download" @click="onExport">导出配置</el-button>
          <el-button type="primary" :icon="Plus" @click="openCreate()">新建数据源</el-button>
        </div>
      </div>
    </div>

    <!-- 真实环境说明条 -->
    <div class="info-bar">
      <div class="info-line">
        <span class="info-strong">自动发现</span>
        <span>当前索引 <code>{{ discovery.index || 'logs-' + app.tenant }}</code> 中发现 <b>{{ discoveredCount + registeredCount }}</b> 个日志来源，其中 <b>{{ discoveredCount }}</b> 个尚未登记配置；已登记 <b>{{ registeredCount }}</b> 个。</span>
      </div>
      <div class="cap-line">
        <span>组件连通：</span>
        <el-tag v-for="(v, k) in caps" :key="k" :type="v ? 'success' : 'danger'" size="small" effect="plain">
          {{ { opensearch: 'OpenSearch', postgres: 'PostgreSQL', kafka: 'Kafka', agentService: '管控服务' }[k] || k }} {{ v ? '可达' : '不可达' }}
        </el-tag>
        <span class="info-note">更新于 {{ updatedAt || '—' }} · 每 30 秒自动刷新</span>
      </div>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon title="数据加载失败" style="margin-bottom:12px">
      <template #default>
        <div class="alert-body">
          <span>{{ discovery.error || '数据加载失败，请稍后重试' }}</span>
          <el-button size="small" type="danger" plain @click="load(false)">重试</el-button>
        </div>
      </template>
    </el-alert>

    <!-- 筛选 -->
    <div class="filter-card">
      <div class="filter-row">
        <div class="filter-item"><label>名称 / 来源标识</label>
          <el-input v-model="filters.keyword" placeholder="支持名称、标识、标签" clearable @keyup.enter="onSearch" />
        </div>
        <div class="filter-item"><label>接入类型</label>
          <el-select v-model="filters.type" placeholder="全部类型" clearable>
            <el-option v-for="t in typeOptions" :key="t" :label="t" :value="t" />
          </el-select>
        </div>
        <div class="filter-item"><label>运行状态</label>
          <el-select v-model="filters.status" placeholder="全部状态" clearable>
            <el-option v-for="s in STATUS_OPTIONS" :key="s" :label="s" :value="s" />
          </el-select>
        </div>
        <div class="filter-item"><label>所属区域</label>
          <el-select v-model="filters.region" placeholder="全部区域" clearable filterable allow-create>
            <el-option v-for="r in regionOptions" :key="r" :label="r" :value="r" />
          </el-select>
        </div>
        <div class="filter-item"><label>登记状态</label>
          <el-select v-model="filters.registered" placeholder="全部" clearable style="width:150px">
            <el-option label="已登记" value="true" />
            <el-option label="仅自动发现" value="false" />
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
          <el-button link type="primary" size="small" @click="onBatch('')">批量启用</el-button>
          <el-button link size="small" @click="onBatch('已停用')">批量停用</el-button>
          <el-button link size="small" @click="onBatch('维护中')">置为维护中</el-button>
        </div>
      </div>
      <el-table :data="tableData" stripe @selection-change="(v) => (selection = v)" row-key="key">
        <el-table-column type="selection" width="46" />
        <el-table-column label="数据源名称" min-width="220">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="name-main" @click="openDetail(row)">{{ row.name }}</span>
              <span class="name-sub">{{ row.sourceKey }}</span>
            </div>
            <div class="tag-row" v-if="row.tags?.length">
              <el-tag v-for="t in row.tags" :key="t" size="small" effect="plain">{{ t }}</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="接入类型" width="120">
          <template #default="{ row }">
            <el-tag :type="row.registered ? 'primary' : 'info'" size="small" effect="light">{{ row.ingestType }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tooltip :content="row.statusReason" placement="top">
              <el-tag :type="statusTagType(row.status)" size="small" effect="light">{{ row.status }}</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="最近接收" width="165">
          <template #default="{ row }">
            <span :class="{ muted: !row.lastReceive }">{{ row.lastReceive || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="日增量" width="130" align="right" header-align="right">
          <template #default="{ row }">
            <el-tooltip :content="`今日 ${fmt(row.todayCount)} 条 · 近 24h ${fmt(row.last24hCount)} 条 · 近 30d ${fmt(row.docCount30d)} 条`" placement="top">
              <span class="num">{{ fmt(row.todayCount) }} <i>条</i></span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="健康度" width="100" align="center" header-align="center">
          <template #default="{ row }">
            <span v-if="row.health == null" class="muted">—</span>
            <span v-else :style="{ color: healthColor(row.health), fontWeight: 600 }">{{ row.health }}%</span>
          </template>
        </el-table-column>
        <el-table-column label="区域" width="90">
          <template #default="{ row }"><span class="muted">{{ row.region }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="170" fixed="right" align="center" header-align="center">
          <template #default="{ row }">
            <div class="op-actions">
              <el-button text size="small" @click="openDetail(row)">详情</el-button>
              <el-button text size="small" @click="openEdit(row)">{{ row.registered ? '编辑' : '登记' }}</el-button>
              <el-button v-if="row.registered" text size="small" type="danger" @click="onDelete(row)">移除</el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-box">
            <p>当前条件下没有数据源</p>
            <el-button size="small" type="primary" @click="openCreate()">登记接入配置</el-button>
          </div>
        </template>
      </el-table>
      <div class="pager">
        <span>共 {{ total }} 个数据源</span>
        <div class="pager-right">
          <div class="page-size">
            <span>每页</span>
            <el-select v-model="pageSize" size="small" @change="onSizeChange">
              <el-option v-for="s in [20, 50, 100]" :key="s" :label="s" :value="s" />
            </el-select>
            <span>条</span>
          </div>
          <el-pagination
            background layout="prev, pager, next"
            :total="total" :current-page="page" :page-size="pageSize"
            @current-change="onPageChange"
          />
        </div>
      </div>
    </el-card>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" title="数据源详情" size="620px">
      <div v-loading="detailLoading">
      <template v-if="detail">
        <div class="detail-head">
          <div>
            <div class="detail-title">{{ detail.config.name }}</div>
            <div class="detail-sub">{{ detail.config.sourceKey }} · {{ detail.config.ingestType }} · {{ detail.config.region }}</div>
          </div>
          <el-tag :type="statusTagType(detail.config.status)" effect="light">{{ detail.config.status }}</el-tag>
        </div>
        <el-alert type="info" :closable="false" :title="detail.config.statusReason" style="margin-bottom:14px" />

        <div class="metric-grid">
          <div class="metric"><span>今日写入</span><b>{{ fmt(detail.config.todayCount) }}</b><i>条</i></div>
          <div class="metric"><span>近 24 小时</span><b>{{ fmt(detail.config.last24hCount) }}</b><i>条</i></div>
          <div class="metric"><span>近 1 小时</span><b>{{ fmt(detail.config.last1hCount) }}</b><i>条</i></div>
          <div class="metric"><span>错误率</span><b>{{ detail.config.errorRate }}</b><i>%</i></div>
        </div>

        <div class="section">
          <div class="section-title">近 24 小时写入趋势 <span class="section-note">{{ detail.note }}</span></div>
          <div class="trend" v-if="detail.trend.length">
            <el-tooltip v-for="(p, i) in detail.trend" :key="i" :content="`${p.t} · ${p.v} 条`" placement="top">
              <div class="trend-col"><div class="trend-bar" :style="{ height: Math.max(2, p.v / trendMax * 46) + 'px' }" /></div>
            </el-tooltip>
          </div>
          <div v-else class="muted">该来源近 24 小时无日志</div>
        </div>

        <div class="section two-col">
          <div>
            <div class="section-title">级别分布</div>
            <div v-for="(v, k) in detail.config.levels" :key="k" class="kv"><span>{{ k }}</span><b>{{ fmt(v) }}</b></div>
            <div v-if="!Object.keys(detail.config.levels || {}).length" class="muted">无数据</div>
          </div>
          <div>
            <div class="section-title">主机分布</div>
            <div v-for="(v, k) in detail.config.hosts" :key="k" class="kv"><span>{{ k }}</span><b>{{ fmt(v) }}</b></div>
            <div v-if="!Object.keys(detail.config.hosts || {}).length" class="muted">无数据</div>
          </div>
        </div>

        <div class="section">
          <div class="section-title">字段角色映射 <span class="section-note">按索引真实 mapping 探测，可在编辑中覆盖</span></div>
          <div class="role-grid">
            <div v-for="(r, k) in detail.roles" :key="k" class="role-item">
              <span>{{ ROLE_LABELS[k] }}</span>
              <b>{{ r.field || '未探测到' }}</b>
              <el-tag size="small" :type="r.from === 'manual' ? 'warning' : r.from === 'auto' ? 'success' : 'info'" effect="plain">
                {{ { manual: '人工指定', auto: '自动探测', missing: '缺失' }[r.from] }}
              </el-tag>
            </div>
          </div>
        </div>

        <div class="section" v-if="Object.keys(detail.fieldCoverage || {}).length">
          <div class="section-title">字段覆盖率 <span class="section-note">该来源近 24h 日志中非空占比</span></div>
          <div class="cov-grid">
            <div v-for="(v, k) in detail.fieldCoverage" :key="k" class="cov-item">
              <div class="cov-head"><span>{{ k }}</span><b>{{ v }}%</b></div>
              <div class="cov-track"><div class="cov-fill" :style="{ width: v + '%' }" /></div>
            </div>
          </div>
        </div>

        <div class="section">
          <div class="section-title">样例原始日志</div>
          <div v-for="(s, i) in detail.samples" :key="i" class="raw-line">{{ s.raw }}</div>
          <div v-if="!detail.samples.length" class="muted">暂无样例</div>
        </div>

        <div class="section">
          <div class="section-title">接入配置</div>
          <div class="kv"><span>接收地址</span><b>{{ detail.config.endpoint || '—' }}</b></div>
          <div class="kv"><span>目标索引</span><b>{{ detail.config.targetIndex || detail.index }}</b></div>
          <div class="kv"><span>解析规则</span><b>{{ detail.config.parserRule || '—' }}</b></div>
          <div class="kv"><span>字符编码 / 采样率</span><b>{{ detail.config.encoding || '—' }} / {{ detail.config.sampleRate ?? 100 }}%</b></div>
          <div class="kv"><span>静默阈值</span><b>{{ detail.config.silenceWarnMin || meta.settings?.silence_warn_min || 5 }} / {{ detail.config.silenceOfflineMin || meta.settings?.silence_offline_min || 60 }} 分钟</b></div>
          <div class="kv"><span>登记人 / 时间</span><b>{{ detail.config.owner || '—' }} · {{ detail.config.updatedAt || '—' }}</b></div>
        </div>
      </template>
      </div>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <el-button :icon="Connection" @click="openEdit(detailRow)">编辑配置</el-button>
        <el-button type="primary" @click="openDetail(detailRow)">重新取数</el-button>
      </template>
    </el-drawer>

    <!-- 新建 / 编辑抽屉 -->
    <el-drawer v-model="formVisible" :title="formMode === 'create' ? '登记 / 新建数据源' : '编辑接入配置'" size="560px">
      <el-form ref="formRef" :model="form" :rules="formRules" label-width="118px">
        <el-divider content-position="left">基础信息</el-divider>
        <el-form-item label="来源标识" prop="sourceKey">
          <el-input v-model="form.sourceKey" :disabled="formMode === 'edit'" placeholder="与真实日志的来源字段值一致，如 order-service" />
          <div class="form-hint">该值用于匹配 OpenSearch 日志；保存后可在「自动发现」中核对是否已有数据</div>
        </el-form-item>
        <el-form-item label="显示名称"><el-input v-model="form.displayName" placeholder="留空则显示来源标识" /></el-form-item>
        <el-form-item label="接入类型" prop="ingestType">
          <el-select v-model="form.ingestType" style="width:100%">
            <el-option v-for="t in INGEST_TYPES" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="接收地址">
          <el-input v-model="form.endpoint" :placeholder="form.ingestType === 'Kafka' ? 'kafka:9092 或 topic 所在 broker' : form.ingestType === 'HTTP' ? 'http://host:port/logs' : 'host:port'" />
          <div class="form-hint">Syslog / HTTP / Kafka 会做真实 TCP / HTTP / broker 探测；Agent 类型按来源标识校验真实日志</div>
        </el-form-item>
        <el-form-item label="目标租户">
          <el-select v-model="form.tenant" style="width:100%">
            <el-option v-for="t in ['default', 'acme', 'gp', 'all']" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="目标索引"><el-input v-model="form.targetIndex" placeholder="留空按租户推导 logs-&lt;tenant&gt;" /></el-form-item>
        <el-form-item label="所属区域">
          <el-select v-model="form.region" clearable filterable allow-create style="width:100%" placeholder="可输入自定义区域">
            <el-option v-for="r in ['华北', '华东', '华南', '华中', '西南', '西北', '东北']" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
        <el-form-item label="标签">
          <el-select v-model="form.tags" multiple filterable allow-create default-first-option style="width:100%" placeholder="回车创建新标签" />
        </el-form-item>

        <el-divider content-position="left">解析与采样</el-divider>
        <el-form-item label="解析规则"><el-input v-model="form.parserRule" placeholder="如 Nginx 默认 / JSON 结构化 / 正则提取" /></el-form-item>
        <el-form-item label="字符编码">
          <el-select v-model="form.encoding" style="width:100%">
            <el-option v-for="e in ['UTF-8', 'GBK', 'GB18030', 'ISO-8859-1']" :key="e" :label="e" :value="e" />
          </el-select>
        </el-form-item>
        <el-form-item label="采样率">
          <el-input-number v-model="form.sampleRate" :min="0" :max="100" style="width:160px" />
          <span class="form-hint">%，100 表示全量；平台侧登记值，实际采集由 Fluent Bit 配置决定</span>
        </el-form-item>

        <el-divider content-position="left">状态与阈值</el-divider>
        <el-form-item label="状态覆盖">
          <el-select v-model="form.statusOverride" clearable placeholder="按真实数据自动判定" style="width:100%">
            <el-option label="维护中" value="维护中" />
            <el-option label="已停用" value="已停用" />
          </el-select>
        </el-form-item>
        <el-form-item label="静默告警(分)"><el-input-number v-model="form.silenceWarnMin" :min="1" :max="1440" placeholder="留空用全局" style="width:160px" /></el-form-item>
        <el-form-item label="静默异常(分)"><el-input-number v-model="form.silenceOfflineMin" :min="1" :max="10080" placeholder="留空用全局" style="width:160px" /></el-form-item>

        <el-divider content-position="left">字段角色映射</el-divider>
        <div class="form-hint block">上线后日志字段命名各不相同，这里可指定各角色对应的真实字段；留空则按索引 mapping 自动探测。当前索引共有 {{ Object.keys(meta.fields || {}).length }} 个字段。</div>
        <el-form-item v-for="(label, key) in ROLE_LABELS" :key="key" :label="label">
          <el-select v-model="form.fieldMap[key]" clearable filterable :placeholder="`自动探测：${meta.roles?.[key]?.field || '未探测到'}`" style="width:100%">
            <el-option v-for="f in roleOptions" :key="f" :label="`${f} (${meta.fields[f]})`" :value="f" />
          </el-select>
        </el-form-item>

        <el-divider content-position="left">扩展</el-divider>
        <el-form-item label="扩展属性"><el-input v-model="form.extraText" type="textarea" :rows="3" placeholder="任意 JSON，如 {&quot;ownerDept&quot;:&quot;运行处&quot;}" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="form.note" type="textarea" :rows="2" /></el-form-item>

        <div v-if="testResult" class="test-result" :class="{ ok: testResult.ok, bad: !testResult.ok }">
          <div class="test-title">{{ testResult.ok ? '连通性正常' : '连通性未通过' }} <span v-if="testResult.latencyMs">· {{ testResult.latencyMs }} ms</span></div>
          <div class="test-detail">{{ testResult.detail }}</div>
          <div v-for="(v, k) in testResult.evidence || {}" :key="k" class="test-evi">{{ k }}：{{ Array.isArray(v) ? v.join(', ') || '（无）' : v }}</div>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="formVisible = false">取消</el-button>
        <el-button :icon="Connection" :loading="testing" @click="onTest">测试连通性</el-button>
        <el-button type="primary" :loading="saving" @click="onSubmit">保存</el-button>
      </template>
    </el-drawer>

    <!-- 接入设置 -->
    <el-dialog v-model="settingsVisible" title="接入设置（全局）" width="600px">
      <el-alert type="info" :closable="false" style="margin-bottom:14px"
        title="字段角色决定平台如何识别日志。留空＝按索引真实 mapping 自动探测，适配任意日志 schema。" />
      <el-form label-width="140px">
        <el-form-item v-for="(label, key) in ROLE_LABELS" :key="key" :label="label">
          <el-select v-model="settingsForm[key + '_field']" clearable filterable placeholder="自动探测" style="width:100%">
            <el-option v-for="f in roleOptions" :key="f" :label="`${f} (${meta.fields[f]})`" :value="f" />
          </el-select>
        </el-form-item>
        <el-form-item label="静默告警(分钟)"><el-input-number v-model="settingsForm.silence_warn_min" :min="1" :max="1440" /></el-form-item>
        <el-form-item label="静默异常(分钟)"><el-input-number v-model="settingsForm.silence_offline_min" :min="1" :max="10080" /></el-form-item>
        <el-form-item label="自动发现天数"><el-input-number v-model="settingsForm.discovery_days" :min="1" :max="365" /></el-form-item>
        <el-form-item label="自动发现"><el-switch v-model="settingsForm.discovery_enabled" /><span class="form-hint">关闭后列表仅显示已登记配置</span></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="settingsVisible = false">取消</el-button>
        <el-button type="primary" :loading="settingsSaving" @click="saveSettings">保存设置</el-button>
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
.head-bar { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; gap: 12px; }
.title { font-size: 18px; font-weight: 600; color: var(--color-text-1); margin: 0; }
.head-actions { display: flex; gap: 8px; flex-shrink: 0; }

.info-bar { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 10px 16px; font-size: 13px; color: var(--color-text-2); }
.info-line, .cap-line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.cap-line { margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--color-border-1); }
.info-strong { color: #03857E; font-weight: 600; }
.info-note { color: var(--color-text-3); font-size: 12px; margin-left: auto; }
.info-bar code { background: #F0F2F5; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
.alert-body { display: flex; justify-content: space-between; align-items: center; }

.filter-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.filter-row { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; }
.filter-item { display: flex; flex-direction: column; gap: 6px; }
.filter-item label { font-size: 12px; color: var(--color-text-2); }
.filter-item :deep(.el-input), .filter-item :deep(.el-select) { width: 170px; }
.filter-actions { display: flex; gap: 8px; margin-left: auto; }

.table-card :deep(.el-card__body) { padding: 12px 14px 14px; }
.batch-bar { display: flex; align-items: center; gap: 16px; padding: 2px 4px 10px; font-size: 13px; color: var(--color-text-2); }
.name-cell { display: flex; flex-direction: column; gap: 2px; }
.name-main { color: #03857E; font-weight: 600; cursor: pointer; }
.name-main:hover { text-decoration: underline; }
.name-sub { font-size: 12px; color: var(--color-text-3); }
.tag-row { display: flex; gap: 4px; margin-top: 4px; flex-wrap: wrap; }
.num { font-variant-numeric: tabular-nums; font-weight: 600; }
.num i { font-style: normal; font-weight: 400; color: var(--color-text-3); font-size: 12px; }
.muted { color: var(--color-text-3); }
.op-actions { display: flex; align-items: center; justify-content: center; gap: 2px; flex-wrap: nowrap; }
.op-actions .el-button { margin: 0 !important; }
.pager { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; font-size: 13px; color: var(--color-text-3); }
.empty-box { padding: 22px 0; color: var(--color-text-3); }

/* 选择列与数据列同底（stripe / hover / 选中态） */
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
.section-note { font-size: 12px; font-weight: 400; color: var(--color-text-3); margin-left: 8px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.kv { display: flex; justify-content: space-between; font-size: 13px; padding: 3px 0; color: var(--color-text-2); }
.kv b { font-weight: 500; color: var(--color-text-1); word-break: break-all; text-align: right; }
.trend { display: flex; align-items: flex-end; gap: 2px; height: 56px; }
.trend-col { flex: 1; height: 100%; display: flex; align-items: flex-end; }
.trend-bar { width: 100%; background: #84C5C1; border-radius: 2px 2px 0 0; min-height: 2px; }
.trend-col:hover .trend-bar { background: #03857E; }
.role-grid, .cov-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.role-item { display: flex; align-items: center; gap: 6px; font-size: 12px; background: #F7FAFA; padding: 6px 8px; border-radius: 3px; }
.role-item span { color: var(--color-text-3); min-width: 62px; }
.role-item b { flex: 1; font-weight: 600; word-break: break-all; }
.cov-item { font-size: 12px; }
.cov-head { display: flex; justify-content: space-between; margin-bottom: 3px; }
.cov-track { height: 5px; background: #E8F3F1; border-radius: 3px; overflow: hidden; }
.cov-fill { height: 100%; background: #03857E; }
.raw-line { font-family: Menlo, Consolas, monospace; font-size: 12px; background: #F7FAFA; padding: 6px 8px; border-radius: 3px; margin-bottom: 4px; word-break: break-all; }

.form-hint { font-size: 12px; color: var(--color-text-3); line-height: 1.5; }
.form-hint.block { margin: 0 0 12px 118px; }
:deep(.el-form-item) .form-hint { margin-left: 0; }
.test-result { margin: 12px 0 0; padding: 10px 12px; border-radius: 4px; font-size: 13px; }
.test-result.ok { background: #E8F7F3; border: 1px solid #B9E0DE; }
.test-result.bad { background: #FDECEC; border: 1px solid #F5C6C6; }
.test-title { font-weight: 600; }
.test-detail { margin-top: 4px; }
.test-evi { font-size: 12px; color: var(--color-text-3); margin-top: 2px; word-break: break-all; }
</style>
