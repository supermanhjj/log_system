<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus, Document, Delete } from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import {
  listRules,
  createRule,
  updateRule,
  deleteRule,
  toggleRule,
  REAL_SOURCES,
  LEVELS,
  NOTIFY_OPTIONS,
  conditionText,
  notifyLabels,
} from '../api/alarms'

const app = useAppStore()
const router = useRouter()

const loading = ref(false)
const error = ref(false)

const filters = reactive({ keyword: '', level: '', enabled: '', owner: '' })
const tableData = ref([])
const total = ref(0)

const dialogVisible = ref(false)
const dialogMode = ref('create') // create | edit
const saving = ref(false)
const formRef = ref(null)
const form = reactive({
  id: null,
  name: '',
  matchSource: '',
  matchLevel: '',
  matchStatusGte: null,
  keyword: '',
  windowMinutes: 5,
  threshold: 1,
  level: '警告',
  notify: ['wecom'],
  enabled: true,
  owner: '黄金金',
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

// ——— 弹窗 ———
function openCreate() {
  dialogMode.value = 'create'
  Object.assign(form, {
    id: null,
    name: '',
    matchSource: '',
    matchLevel: '',
    matchStatusGte: null,
    keyword: '',
    windowMinutes: 5,
    threshold: 1,
    level: '警告',
    notify: ['wecom'],
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
    matchLevel: row.matchLevel || '',
    matchStatusGte: row.matchStatusGte,
    keyword: row.keyword || '',
    windowMinutes: row.windowMinutes,
    threshold: row.threshold,
    level: row.level,
    notify: [...row.notify],
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
      match_level: form.matchLevel || null,
      match_status_gte: form.matchStatusGte != null && form.matchStatusGte !== '' ? Number(form.matchStatusGte) : null,
      keyword: form.keyword.trim() || null,
      window_minutes: Number(form.windowMinutes),
      threshold: Number(form.threshold),
      level: form.level,
      notify: form.notify,
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

onMounted(() => loadRules(false))
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
          <h1 class="title">告警规则</h1>
          <p class="desc">规则基于检索条件对日志实时判定，保存后即时生效</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" @click="onRefresh">刷新</el-button>
          <el-button type="primary" :icon="Plus" @click="openCreate">新建规则</el-button>
          <el-button :icon="Document" @click="goEvents">查看告警事件</el-button>
        </div>
      </div>
    </div>

    <!-- 错误态 -->
    <el-alert
      v-if="error"
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
    <div class="filter-card">
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
    <el-card shadow="never" class="table-card">
      <el-table :data="tableData" stripe>
        <el-table-column prop="name" label="规则名称" min-width="180" show-overflow-tooltip align="left" header-align="left" />
        <el-table-column prop="matchSource" label="关联数据源" width="160" align="left" header-align="left">
          <template #default="{ row }">{{ row.matchSource || '全部数据源' }}</template>
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
.notify { color: var(--color-text-2); font-size: 13px; }
.hint { font-size: 12px; color: var(--color-text-3); margin-left: 10px; }
.total-line { font-size: 13px; color: var(--color-text-3); margin-top: 12px; }
</style>
