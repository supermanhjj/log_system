<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus, Document, Delete } from '@element-plus/icons-vue'
import { currentTenant } from '../api/http'
import {
  listRules,
  createRule,
  updateRule,
  deleteRule,
  toggleRule,
  getRule,
  listTemplates,
  applyTemplate,
  REAL_SOURCES,
  LEVELS,
  NOTIFY_OPTIONS,
  conditionText,
  notifyLabels,
} from '../api/alarms'

const router = useRouter()

const loading = ref(false)
const error = ref(false)
const activeTab = ref('rules')

const filters = reactive({ keyword: '', level: '', enabled: '', owner: '' })
const tableData = ref([])
const total = ref(0)

const templateLoading = ref(false)
const templateError = ref(false)
const templates = ref([])
const templateTotal = ref(0)
const templateFilters = reactive({ keyword: '', category: '' })

const dialogVisible = ref(false)
const dialogMode = ref('create') // create | edit
const saving = ref(false)
const formRef = ref(null)
const form = reactive({
  id: null,
  name: '',
  matchSource: '',
  matchLogClass: '',
  matchSourceType: '',
  matchLevel: '',
  matchStatusGte: null,
  keyword: '',
  windowMinutes: 5,
  threshold: 1,
  level: '警告',
  notify: ['sms'],
  enabled: true,
  owner: '黄金金',
})
// 日志类型 / 来源类型：与 ingest-service log_schema 枚举一致，规则按类型圈定范围
const LOG_CLASS_OPTIONS = [
  { value: 'system', label: '系统日志' },
  { value: 'middleware', label: '中间件日志' },
  { value: 'database', label: '数据库日志' },
  { value: 'business', label: '业务日志' },
  { value: 'network', label: '网络日志' },
  { value: 'container', label: '容器日志' },
]
// 来源类型按日志类型联动，避免选出「系统日志 + mysql-slow」这种不存在的组合
const SOURCE_TYPE_BY_CLASS = {
  system: ['syslog', 'auth', 'kern', 'cron', 'user'],
  middleware: ['nginx-access', 'nginx-error', 'redis'],
  database: ['mysql-error', 'mysql-slow', 'postgres'],
  business: ['app'],
}
const sourceTypeOptions = computed(() => {
  if (form.matchLogClass) return SOURCE_TYPE_BY_CLASS[form.matchLogClass] || []
  return Object.values(SOURCE_TYPE_BY_CLASS).flat()
})
const rules = {
  name: [{ required: true, message: '请输入规则名称', trigger: 'blur' }],
  level: [{ required: true, message: '请选择告警级别', trigger: 'change' }],
}

const sourceOptions = computed(() => [
  { value: '', label: '全部数据源' },
  ...REAL_SOURCES.map((s) => ({ value: s, label: s })),
])
const levelOptions = computed(() => [
  { value: '', label: '不限' },
  { value: 'ERROR', label: 'ERROR' },
  { value: 'WARN', label: 'WARN' },
  { value: 'INFO', label: 'INFO' },
])
const windowOptions = [5, 10, 30, 60]
const TEMPLATE_CATEGORIES = ['系统日志', '中间件日志', '数据库日志', '业务日志', '通用']

// 企业微信渠道已下线：历史规则如果还带 wecom，打开编辑时清掉；
// 如果清完没有任何渠道，默认回到短信，避免保存成“无通知”。
function sanitizeNotify(notify) {
  const allowed = NOTIFY_OPTIONS.map((o) => o.value)
  const kept = (notify || []).filter((n) => allowed.includes(n))
  return kept.length ? kept : ['sms']
}

function levelTagType(level) {
  if (level === '严重') return 'danger'
  if (level === '警告') return 'warning'
  return 'info'
}
function enableTagType(enabled) {
  return enabled ? 'success' : 'info'
}

// ——— 取数 ———
async function loadRules(showTip = false) {
  loading.value = true
  error.value = false
  try {
    const data = await listRules({
      keyword: filters.keyword,
      level: filters.level,
      enabled: filters.enabled,
      owner: filters.owner,
    })
    tableData.value = data.items
    total.value = data.total
    if (showTip) ElMessage.success('规则列表已刷新')
  } catch (e) {
    error.value = true
    ElMessage.error('规则加载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}
function onSearch() {
  loadRules(false)
}
function onReset() {
  filters.keyword = ''
  filters.level = ''
  filters.enabled = ''
  filters.owner = ''
  loadRules(false)
}
function onRefresh() {
  loadRules(true)
}
function goEvents() {
  router.push('/alarms/events')
}

// ——— 模板库 ———
async function loadTemplates(showTip = false) {
  templateLoading.value = true
  templateError.value = false
  try {
    const data = await listTemplates({
      keyword: templateFilters.keyword,
      category: templateFilters.category,
    })
    templates.value = data.items
    templateTotal.value = data.total
    if (showTip) ElMessage.success('模板列表已刷新')
  } catch (e) {
    templateError.value = true
    ElMessage.error('模板加载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    templateLoading.value = false
  }
}
function onTemplateSearch() {
  loadTemplates(false)
}
function onTemplateReset() {
  templateFilters.keyword = ''
  templateFilters.category = ''
  loadTemplates(false)
}
async function onApplyTemplate(row) {
  if (currentTenant() === 'all') {
    ElMessage.warning('请先切换到具体租户，再拉取模板')
    return
  }
  try {
    const resp = await applyTemplate(row.id)
    if (resp.existing) {
      ElMessage.info('当前租户已有同名规则，已打开编辑')
    } else {
      ElMessage.success('已拉取到当前租户，可继续调整配置')
    }
    activeTab.value = 'rules'
    await loadRules(false)
    const detail = await getRule(resp.id)
    openEdit(detail)
  } catch (e) {
    ElMessage.error('拉取模板失败：' + (e.response?.data?.detail || e.message))
  }
}

// ——— 弹窗 ———
function openCreate() {
  if (currentTenant() === 'all') {
    ElMessage.warning('请先切换到具体租户，再新建规则')
    return
  }
  dialogMode.value = 'create'
  Object.assign(form, {
    id: null,
    name: '',
    matchSource: '',
    matchLogClass: '',
    matchSourceType: '',
    matchLevel: '',
    matchStatusGte: null,
    keyword: '',
    windowMinutes: 5,
    threshold: 1,
    level: '警告',
    notify: ['sms'],
    enabled: true,
    owner: '黄金金',
  })
  dialogVisible.value = true
}
function openEdit(row) {
  dialogMode.value = 'edit'
  Object.assign(form, {
    id: row.id,
    name: row.name,
    matchSource: row.matchSource || '',
    matchLogClass: row.matchLogClass || '',
    matchSourceType: row.matchSourceType || '',
    matchLevel: row.matchLevel || '',
    matchStatusGte: row.matchStatusGte,
    keyword: row.keyword || '',
    windowMinutes: row.windowMinutes,
    threshold: row.threshold,
    level: row.level,
    notify: sanitizeNotify(row.notify),
    enabled: row.enabled,
    owner: row.owner || '黄金金',
  })
  dialogVisible.value = true
}

async function onSubmit() {
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    saving.value = true
    const payload = {
      name: form.name.trim(),
      match_source: form.matchSource || null,
      match_log_class: form.matchLogClass || null,
      match_source_type: form.matchSourceType || null,
      match_level: form.matchLevel || null,
      match_status_gte: form.matchStatusGte != null && form.matchStatusGte !== '' ? Number(form.matchStatusGte) : null,
      keyword: form.keyword.trim() || null,
      window_minutes: Number(form.windowMinutes),
      threshold: Number(form.threshold),
      level: form.level,
      notify: sanitizeNotify(form.notify),
      enabled: form.enabled,
      owner: form.owner.trim() || 'system',
    }
    try {
      if (dialogMode.value === 'create') {
        await createRule(payload)
        ElMessage.success('规则已保存，实时判定 10 秒内生效')
      } else {
        await updateRule(form.id, payload)
        ElMessage.success('规则已更新')
      }
      dialogVisible.value = false
      loadRules(false)
    } catch (e) {
      ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
    } finally {
      saving.value = false
    }
  })
}

// ——— 启用开关 / 删除 ———
async function onToggle(row, val) {
  const prev = row.enabled
  // 乐观更新：先把界面绑定的数据态改成新值，避免开关靠组件动画翻过去后回弹
  row.enabled = val
  try {
    const resp = await toggleRule(row.id, val)
    // 以服务端返回为准，确保界面与持久化状态完全一致
    row.enabled = resp.enabled
    ElMessage.success(resp.enabled ? '规则已启用' : '规则已停用')
  } catch (e) {
    // 失败回滚到原值
    row.enabled = prev
    ElMessage.error('操作失败：' + (e.response?.data?.detail || e.message))
  }
}
function onDelete(row) {
  ElMessageBox.confirm(
    `确认删除规则「${row.name}」？历史告警将保留 30 天。`,
    '删除告警规则',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  )
    .then(async () => {
      await deleteRule(row.id)
      ElMessage.success('规则已删除')
      loadRules(false)
    })
    .catch(() => {})
}

onMounted(() => {
  loadRules(false)
  loadTemplates(false)
})
</script>

<template>
  <div class="page" v-loading="loading">
    <!-- 页头 -->
    <div class="page-head">
      <div class="breadcrumb">
        <span>告警中心</span><span class="sep">/</span><span class="cur">告警规则</span>
      </div>
      <div class="head-bar">
        <div>
          <h1 class="title">{{ activeTab === 'rules' ? '告警规则' : '规则模板' }}</h1>
          <p class="desc">
            {{ activeTab === 'rules' ? '规则基于检索条件对日志实时判定，保存后即时生效' : '平台共享的规则定义，拉取后成为当前租户规则' }}
          </p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" @click="activeTab === 'rules' ? onRefresh() : loadTemplates(true)">刷新</el-button>
          <el-button v-if="activeTab === 'rules'" type="primary" :icon="Plus" @click="openCreate">新建规则</el-button>
          <el-button :icon="Document" @click="goEvents">查看告警事件</el-button>
        </div>
      </div>
    </div>

    <div class="view-switch">
      <el-radio-group v-model="activeTab" size="small">
        <el-radio-button value="rules">告警规则</el-radio-button>
        <el-radio-button value="templates">规则模板</el-radio-button>
      </el-radio-group>
    </div>

    <!-- 错误态 -->
    <el-alert
      v-if="error && activeTab === 'rules'"
      type="error"
      :closable="false"
      show-icon
      title="规则加载失败"
      style="margin-bottom: 12px"
    >
      <template #default>
        <div class="alert-body">
          <span>数据加载失败，请稍后重试</span>
          <el-button size="small" type="danger" plain @click="loadRules(false)">重试</el-button>
        </div>
      </template>
    </el-alert>

    <!-- 筛选 -->
    <div v-if="activeTab === 'rules'" class="filter-card">
      <div class="filter-row">
        <div class="filter-item">
          <label>规则名称</label>
          <el-input v-model="filters.keyword" placeholder="请输入规则名称" clearable @keyup.enter="onSearch" />
        </div>
        <div class="filter-item">
          <label>告警级别</label>
          <el-select v-model="filters.level" placeholder="全部级别" clearable>
            <el-option v-for="l in LEVELS" :key="l" :label="l" :value="l" />
          </el-select>
        </div>
        <div class="filter-item">
          <label>启用状态</label>
          <el-select v-model="filters.enabled" placeholder="全部状态" clearable>
            <el-option label="运行中" value="true" />
            <el-option label="已停用" value="false" />
          </el-select>
        </div>
        <div class="filter-item">
          <label>更新人</label>
          <el-input v-model="filters.owner" placeholder="请输入姓名" clearable @keyup.enter="onSearch" />
        </div>
        <div class="filter-actions">
          <el-button type="primary" @click="onSearch">查询</el-button>
          <el-button @click="onReset">重置</el-button>
        </div>
      </div>
    </div>

    <!-- 列表 -->
    <el-card v-if="activeTab === 'rules'" shadow="never" class="table-card">
      <el-table :data="tableData" stripe>
        <el-table-column label="规则名称" min-width="180" show-overflow-tooltip align="left" header-align="left">
          <template #default="{ row }">
            <span>{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="作用范围" width="170" align="left" header-align="left">
          <template #default="{ row }">
            <el-tag v-if="row.matchLogClass" size="small" effect="plain">
              {{ LOG_CLASS_OPTIONS.find((o) => o.value === row.matchLogClass)?.label || row.matchLogClass }}
            </el-tag>
            <span v-if="row.matchSourceType" class="mono-scope">{{ row.matchSourceType }}</span>
            <span v-if="!row.matchLogClass" class="hint-scope">{{ row.matchSource || '全部数据源' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="判定条件" min-width="280" show-overflow-tooltip align="left" header-align="left">
          <template #default="{ row }">
            <span class="cond" :title="conditionText(row)">{{ conditionText(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="级别" width="90" align="center" header-align="center">
          <template #default="{ row }">
            <el-tag :type="levelTagType(row.level)" size="small" effect="light">{{ row.level }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="通知方式" width="170" align="left" header-align="left">
          <template #default="{ row }">
            <span class="notify">{{ notifyLabels(row.notify) || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90" align="center" header-align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              @change="(v) => onToggle(row, v)"
              aria-label="启用开关"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right" align="center" header-align="center">
          <template #default="{ row }">
            <div class="op-actions">
              <el-button text size="small" @click="openEdit(row)">编辑</el-button>
              <el-button text size="small" type="danger" :icon="Delete" @click="onDelete(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="total-line">共 {{ total }} 条规则</div>
    </el-card>

    <!-- 规则模板：平台共享定义，租户拉取后才生成自己的规则 -->
    <template v-if="activeTab === 'templates'">
      <el-alert
        v-if="templateError"
        type="error"
        :closable="false"
        show-icon
        title="模板加载失败"
        style="margin-bottom: 12px"
      >
        <template #default>
          <div class="alert-body">
            <span>数据加载失败，请稍后重试</span>
            <el-button size="small" type="danger" plain @click="loadTemplates(false)">重试</el-button>
          </div>
        </template>
      </el-alert>

      <div class="filter-card">
        <div class="filter-row">
          <div class="filter-item">
            <label>模板名称</label>
            <el-input v-model="templateFilters.keyword" placeholder="名称或说明" clearable @keyup.enter="onTemplateSearch" />
          </div>
          <div class="filter-item">
            <label>分类</label>
            <el-select v-model="templateFilters.category" placeholder="全部分类" clearable>
              <el-option v-for="c in TEMPLATE_CATEGORIES" :key="c" :label="c" :value="c" />
            </el-select>
          </div>
          <div class="filter-actions">
            <el-button type="primary" @click="onTemplateSearch">查询</el-button>
            <el-button @click="onTemplateReset">重置</el-button>
          </div>
        </div>
      </div>

      <el-card v-loading="templateLoading" shadow="never" class="table-card">
        <el-table :data="templates" stripe>
          <el-table-column label="模板名称" min-width="220" show-overflow-tooltip align="left" header-align="left">
            <template #default="{ row }">
              <div class="tpl-name">{{ row.name }}</div>
              <div v-if="row.description" class="tpl-desc">{{ row.description }}</div>
            </template>
          </el-table-column>
          <el-table-column label="分类" width="110" align="left" header-align="left">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.category }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="作用范围" width="170" align="left" header-align="left">
            <template #default="{ row }">
              <el-tag v-if="row.matchLogClass" size="small" effect="plain">
                {{ LOG_CLASS_OPTIONS.find((o) => o.value === row.matchLogClass)?.label || row.matchLogClass }}
              </el-tag>
              <span v-if="row.matchSourceType" class="mono-scope">{{ row.matchSourceType }}</span>
              <span v-if="!row.matchLogClass" class="hint-scope">{{ row.matchSource || '全部数据源' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="判定条件" min-width="260" show-overflow-tooltip align="left" header-align="left">
            <template #default="{ row }">
              <span class="cond" :title="conditionText(row)">{{ conditionText(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="级别" width="90" align="center" header-align="center">
            <template #default="{ row }">
              <el-tag :type="levelTagType(row.level)" size="small" effect="light">{{ row.level }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="通知方式" width="150" align="left" header-align="left">
            <template #default="{ row }">
              <span class="notify">{{ notifyLabels(row.notify) || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="150" fixed="right" align="center" header-align="center">
            <template #default="{ row }">
              <el-button text type="primary" size="small" @click="onApplyTemplate(row)">拉取到当前租户</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="total-line">共 {{ templateTotal }} 个模板</div>
      </el-card>
    </template>

    <!-- 新建 / 编辑 弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新建告警规则' : '编辑告警规则'"
      width="560px"
      @closed="formRef?.clearValidate?.()"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="108px">
        <el-form-item label="规则名称" prop="name">
          <el-input v-model="form.name" placeholder="例如：订单服务错误率突增" />
        </el-form-item>
        <el-form-item label="日志类型">
          <el-select
            v-model="form.matchLogClass"
            clearable
            placeholder="全部类型"
            style="width: 100%"
            @change="() => (form.matchSourceType = '')"
          >
            <el-option v-for="o in LOG_CLASS_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="来源类型">
          <el-select v-model="form.matchSourceType" clearable placeholder="全部来源类型" style="width: 100%">
            <el-option v-for="st in sourceTypeOptions" :key="st" :label="st" :value="st" />
          </el-select>
        </el-form-item>
        <el-form-item label="关联数据源">
          <el-select v-model="form.matchSource" placeholder="选择数据源" style="width: 100%">
            <el-option v-for="o in sourceOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="日志级别">
          <el-select v-model="form.matchLevel" style="width: 100%">
            <el-option v-for="o in levelOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="状态码 ≥">
          <el-input v-model="form.matchStatusGte" placeholder="可选，如 500" style="width: 100%">
            <template #prepend>status</template>
          </el-input>
        </el-form-item>
        <el-form-item label="关键字">
          <el-input v-model="form.keyword" placeholder="可选，匹配 message / 原始日志" style="width: 100%" />
        </el-form-item>
        <el-form-item label="触发窗口">
          <el-select v-model="form.windowMinutes" style="width: 160px">
            <el-option v-for="w in windowOptions" :key="w" :label="`${w} 分钟`" :value="w" />
          </el-select>
          <span class="hint">窗口内累计判定</span>
        </el-form-item>
        <el-form-item label="触发阈值">
          <el-input-number v-model="form.threshold" :min="1" :max="99999" style="width: 160px" />
          <span class="hint">窗口内命中 ≥ 该条数即触发</span>
        </el-form-item>
        <el-form-item label="告警级别" prop="level">
          <el-select v-model="form.level" style="width: 160px">
            <el-option v-for="l in LEVELS" :key="l" :label="l" :value="l" />
          </el-select>
        </el-form-item>
        <el-form-item label="通知方式">
          <el-checkbox-group v-model="form.notify">
            <el-checkbox v-for="o in NOTIFY_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="更新人">
          <el-input v-model="form.owner" placeholder="更新人" style="width: 200px" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
          <span class="hint">保存后立即生效，可在列表中随时停用</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSubmit">保存</el-button>
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
.head-bar { display: flex; justify-content: space-between; align-items: flex-end; margin-top: 8px; }
.title { font-size: 18px; font-weight: 600; color: var(--color-text-1); margin: 0; }
.desc { font-size: 13px; color: var(--color-text-3); margin: 4px 0 0; }
.head-actions { display: flex; gap: 8px; }
.alert-body { display: flex; justify-content: space-between; align-items: center; }
.view-switch { display: flex; }
.view-switch :deep(.el-radio-group) { background: #fff; padding: 4px; border: 1px solid var(--color-border-1); border-radius: 4px; }

.filter-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px; }
.filter-row { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; }
.filter-item { display: flex; flex-direction: column; gap: 6px; }
.filter-item label { font-size: 12px; color: var(--color-text-2); }
.filter-item :deep(.el-input), .filter-item :deep(.el-select) { width: 170px; }
.filter-actions { display: flex; gap: 8px; margin-left: auto; }

.table-card :deep(.el-card__body) { padding: 12px 14px 14px; }
.op-actions { display: flex; align-items: center; justify-content: center; gap: 2px; flex-wrap: nowrap; }
.op-actions .el-button { margin: 0 !important; }
.cond { color: var(--color-text-2); font-size: 13px; }
.tpl-name { color: var(--color-text-1); font-size: 13px; }
.tpl-desc { color: var(--color-text-3); font-size: 12px; margin-top: 2px; }
/* 作用范围列：类型标签 + 来源类型机器码并列，未按类型圈定时回落到数据源名 */
.mono-scope { margin-left: 6px; font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 12px; color: var(--color-text-3); }
.hint-scope { color: var(--color-text-3); font-size: 13px; }
.notify { color: var(--color-text-2); font-size: 13px; }
.hint { font-size: 12px; color: var(--color-text-3); margin-left: 10px; }
.total-line { font-size: 13px; color: var(--color-text-3); margin-top: 12px; }
</style>
