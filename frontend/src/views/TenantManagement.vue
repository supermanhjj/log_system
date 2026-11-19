<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Edit, Plus, Refresh } from '@element-plus/icons-vue'
import {
  createTenant, deleteTenant, listTenants, toggleTenant, updateTenant,
} from '../api/system'
import { useAppStore } from '../stores/app'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const app = useAppStore()

const canCreate = computed(() => auth.has('tenant', 'create'))
const canUpdate = computed(() => auth.has('tenant', 'update'))
const canDelete = computed(() => auth.has('tenant', 'delete'))

const loading = ref(false)
const error = ref('')
const rows = ref([])
const note = ref('')
const filters = reactive({ keyword: '', enabled: '' })

const enabledOptions = [
  { value: 'true', label: '启用' },
  { value: 'false', label: '停用' },
]

function errText(e, fallback) {
  const status = e.response?.status
  if (status === 401) {
    auth.clear()
    router.replace({ path: '/login', query: { redirect: '/settings/tenants' } })
    return '登录态已失效，请重新登录'
  }
  if (status === 403) return e.response?.data?.detail || '当前角色无此操作权限'
  return e.response?.data?.detail || e.message || fallback
}

function fmtBytes(n) {
  const v = Number(n || 0)
  if (!v) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let x = v
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1 }
  return `${x.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await listTenants(filters)
    rows.value = res.items || []
    note.value = res.note || ''
  } catch (e) {
    error.value = errText(e, '租户列表加载失败')
    rows.value = []
  } finally {
    loading.value = false
  }
}

function resetFilters() {
  Object.assign(filters, { keyword: '', enabled: '' })
  load()
}

// ---- 新建 / 编辑 ----
const dialogVisible = ref(false)
const dialogMode = ref('create')
const saving = ref(false)
const formRef = ref(null)
const form = reactive({ code: '', name: '', quotaGB: 0, enabled: true })

const codePattern = /^[a-z][a-z0-9_]{1,31}$/
const rules = {
  code: [
    { required: true, message: '请输入租户编码', trigger: 'blur' },
    {
      validator: (_r, v, cb) => (codePattern.test(v || '') ? cb() : cb(new Error('小写字母开头，可含小写字母/数字/下划线，2-32 位'))),
      trigger: 'blur',
    },
  ],
  name: [{ required: true, message: '请输入租户名称', trigger: 'blur' }],
}

function openCreate() {
  dialogMode.value = 'create'
  Object.assign(form, { code: '', name: '', quotaGB: 0, enabled: true })
  dialogVisible.value = true
}

function openEdit(row) {
  dialogMode.value = 'edit'
  Object.assign(form, {
    code: row.code, name: row.name, quotaGB: row.quotaGB || 0, enabled: row.enabled,
  })
  dialogVisible.value = true
}

async function submit() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  saving.value = true
  try {
    if (dialogMode.value === 'create') {
      const res = await createTenant({ code: form.code, name: form.name, quotaGB: form.quotaGB })
      ElMessage.success(res.message || '租户已创建')
    } else {
      const res = await updateTenant(form.code, {
        name: form.name, quotaGB: form.quotaGB, enabled: form.enabled,
      })
      ElMessage.success(res.message || '租户已保存')
    }
    dialogVisible.value = false
    await refreshAll()
  } catch (e) {
    ElMessage.error(errText(e, '保存失败'))
  } finally {
    saving.value = false
  }
}

async function onToggle(row, val) {
  try {
    const res = await toggleTenant(row.code, val)
    row.enabled = val
    ElMessage.success(res.message || '状态已更新')
    await refreshAll()
  } catch (e) {
    ElMessage.error(errText(e, '状态更新失败'))
    load()
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除租户「${row.name}」（${row.code}）？仅当该租户无用户、无日志数据时允许删除，操作会记入审计。`,
      '删除租户',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await deleteTenant(row.code)
    ElMessage.success(res.message || '租户已删除')
    await refreshAll()
  } catch (e) {
    ElMessage.error(errText(e, '删除失败'))
  }
}

// 租户变更后同步顶栏下拉（唯一真源在服务端，前端只重取）
async function refreshAll() {
  await load()
  await app.loadTenants({ includeAll: auth.user?.roleKey === 'platform_admin' })
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div class="breadcrumb"><span>平台管理</span><span class="sep">/</span><span class="cur">租户管理</span></div>
      <div class="head-bar">
        <div>
          <h1 class="title">租户管理</h1>
          <p class="desc">租户是日志数据的隔离空间</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" @click="load">刷新</el-button>
          <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openCreate">新建租户</el-button>
        </div>
      </div>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" />

    <section class="card">
      <div class="filter-row">
        <div class="filter-item">
          <label>租户</label>
          <el-input v-model="filters.keyword" placeholder="编码或名称" clearable @keyup.enter="load" />
        </div>
        <div class="filter-item">
          <label>启用状态</label>
          <el-select v-model="filters.enabled" placeholder="全部状态" clearable>
            <el-option v-for="o in enabledOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </div>
        <div class="filter-actions">
          <el-button type="primary" @click="load">查询</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </div>
      </div>

      <el-table v-loading="loading" :data="rows" row-key="code" size="default" stripe>
        <el-table-column label="租户" min-width="200">
          <template #default="{ row }">
            <div class="tenant-cell">
              <span class="name-cell">{{ row.name }}</span>
              <el-tag v-if="row.isReserved" size="small" type="info" effect="plain">系统保留</el-tag>
              <el-tag v-else-if="!row.enabled" size="small" type="warning" effect="plain">已停用</el-tag>
            </div>
            <div class="muted mono">{{ row.code }} · {{ row.index }}</div>
          </template>
        </el-table-column>

        <el-table-column label="用户数" width="92" align="right">
          <template #default="{ row }"><span class="num">{{ row.userCount }}</span></template>
        </el-table-column>

        <el-table-column label="日志量" width="110" align="right">
          <template #default="{ row }"><span class="num">{{ row.docs.toLocaleString() }}</span></template>
        </el-table-column>

        <el-table-column label="已用空间" width="110" align="right">
          <template #default="{ row }"><span class="num">{{ fmtBytes(row.usedBytes) }}</span></template>
        </el-table-column>

        <el-table-column label="配额" width="100" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.quotaGB > 0 ? row.quotaGB + ' GB' : '不限' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="启用" width="86" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :disabled="!canUpdate || row.isReserved || row.code === auth.tenant"
              @change="(v) => onToggle(row, v)"
            />
          </template>
        </el-table-column>

        <el-table-column prop="createdAt" label="创建时间" width="160">
          <template #default="{ row }"><span class="muted">{{ row.createdAt || '—' }}</span></template>
        </el-table-column>

        <el-table-column label="操作" width="130" align="center" fixed="right">
          <template #default="{ row }">
            <div class="op-actions">
              <el-button v-if="canUpdate" link type="primary" :icon="Edit" @click="openEdit(row)">编辑</el-button>
              <el-button
                v-if="canDelete"
                link
                type="danger"
                :icon="Delete"
                :disabled="row.isReserved || row.code === auth.tenant"
                @click="onDelete(row)"
              >删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <p class="note">{{ note }}</p>
    </section>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新建租户' : `编辑租户 · ${form.code}`"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="96px">
        <el-form-item label="租户编码" prop="code">
          <el-input
            v-model="form.code"
            :disabled="dialogMode === 'edit'"
            placeholder="例如 finance（小写字母开头）"
          />
          <div class="form-hint">
            编码决定日志索引名，是隔离边界，创建后不可修改。
          </div>
        </el-form-item>
        <el-form-item label="租户名称" prop="name">
          <el-input v-model="form.name" placeholder="例如 金融业务" />
        </el-form-item>
        <el-form-item label="日志配额">
          <el-input-number v-model="form.quotaGB" :min="0" :max="1048576" :step="1" />
          <span class="form-hint">单位 GB；0 表示不设上限（与「日志空间」共用同一份配额）</span>
        </el-form-item>
        <el-form-item v-if="dialogMode === 'edit'" label="启用状态">
          <el-switch v-model="form.enabled" :disabled="form.code === auth.tenant" />
          <span v-if="form.code === auth.tenant" class="form-hint">不能停用当前登录账号所属的租户</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
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
.head-bar { display: flex; justify-content: space-between; align-items: flex-start; margin-top: 8px; gap: 12px; }
.title { font-size: 18px; font-weight: 600; color: var(--color-text-1); margin: 0; }
.desc { margin: 4px 0 0; font-size: 12px; color: var(--color-text-3); }
.head-actions { display: flex; gap: 8px; flex-shrink: 0; }

.card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; }
.filter-row {
  display: flex;
  align-items: flex-end;
  gap: 14px;
  flex-wrap: wrap;
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-border-1);
}
.filter-item { display: flex; flex-direction: column; gap: 6px; }
.filter-item label { font-size: 12px; color: var(--color-text-2); }
.filter-item :deep(.el-input), .filter-item :deep(.el-select) { width: 170px; }
.filter-actions { display: flex; gap: 8px; margin-left: auto; }

.page :deep(.el-table) { padding: 0 18px; }
.tenant-cell { display: flex; align-items: center; gap: 6px; }
.name-cell { color: var(--color-text-1); font-weight: 600; }
.muted { color: var(--color-text-3); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.num { font-variant-numeric: tabular-nums; font-weight: 600; }
.op-actions { display: flex; align-items: center; justify-content: center; gap: 2px; }
.op-actions .el-button { margin: 0; }
.note { margin: 0; padding: 10px 18px 16px; font-size: 12px; color: var(--color-text-3); }
.form-hint { font-size: 12px; color: var(--color-text-3); margin-left: 8px; }
</style>
