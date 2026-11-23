<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Delete, Edit, Plus, Refresh } from '@element-plus/icons-vue'
import {
  createTenant, deleteNamespaceMapping, deleteTenant, listNamespaceMappings,
  listTenants, saveNamespaceMapping, toggleTenant, updateTenant,
} from '../api/system'
import { useAppStore } from '../stores/app'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const app = useAppStore()

const activeTab = ref('tenants')

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

// namespace 映射只能指向已启用租户；停用租户不出现在下拉里，避免建成即拒收。
const enabledTenants = computed(() => rows.value.filter((r) => r.enabled))

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

// ---- Namespace 映射 ----
const mappingLoading = ref(false)
const mappingError = ref('')
const mappingRows = ref([])
const mappingNote = ref('')
const mappingFilters = reactive({ keyword: '', tenant: '' })

const mappingDialogVisible = ref(false)
const mappingDialogMode = ref('create')
const mappingSaving = ref(false)
const mappingFormRef = ref(null)
const mappingForm = reactive({ namespace: '', tenant: '', note: '' })

const namespacePattern = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/
const mappingRules = {
  namespace: [
    { required: true, message: '请输入 namespace', trigger: 'blur' },
    {
      validator: (_r, v, cb) => {
        const value = (v || '').trim()
        if (!value) return cb(new Error('请输入 namespace'))
        if (value.length > 63) return cb(new Error('namespace 最长 63 个字符'))
        if (!namespacePattern.test(value)) {
          return cb(new Error('只能用小写字母、数字和短横线，且首尾不能是短横线'))
        }
        return cb()
      },
      trigger: 'blur',
    },
  ],
  tenant: [{ required: true, message: '请选择归属租户', trigger: 'change' }],
}

const mappingRowsFiltered = computed(() => {
  const kw = mappingFilters.keyword.trim().toLowerCase()
  return mappingRows.value.filter((r) => {
    if (mappingFilters.tenant && r.tenant !== mappingFilters.tenant) return false
    if (!kw) return true
    return [r.namespace, r.tenant, r.tenantName, r.note]
      .some((v) => String(v || '').toLowerCase().includes(kw))
  })
})

const mappingSnippetVisible = ref(false)
const mappingSnippetText = ref('')
const mappingSnippetTitle = ref('')

async function loadMappings() {
  mappingLoading.value = true
  mappingError.value = ''
  try {
    const res = await listNamespaceMappings()
    mappingRows.value = res.items || []
    mappingNote.value = res.note || ''
  } catch (e) {
    mappingError.value = errText(e, 'Namespace 映射加载失败')
    mappingRows.value = []
  } finally {
    mappingLoading.value = false
  }
}

function onTabChange(name) {
  if (name === 'mappings') loadMappings()
}

function openMappingCreate() {
  if (!enabledTenants.value.length) {
    ElMessage.warning('当前没有启用中的租户，请先在「租户列表」新建或启用租户')
    return
  }
  mappingDialogMode.value = 'create'
  Object.assign(mappingForm, {
    namespace: '', tenant: mappingFilters.tenant || enabledTenants.value[0]?.code || '', note: '',
  })
  mappingDialogVisible.value = true
}

function openMappingEdit(row) {
  mappingDialogMode.value = 'edit'
  Object.assign(mappingForm, {
    namespace: row.namespace, tenant: row.tenant, note: row.note || '',
  })
  mappingDialogVisible.value = true
}

async function submitMapping() {
  if (!mappingFormRef.value) return
  try {
    await mappingFormRef.value.validate()
  } catch {
    return
  }
  mappingSaving.value = true
  try {
    const payload = {
      namespace: mappingForm.namespace.trim(),
      tenant: mappingForm.tenant,
      note: mappingForm.note.trim(),
    }
    const res = await saveNamespaceMapping(payload)
    ElMessage.success(res.message || '映射已保存')
    mappingDialogVisible.value = false
    await loadMappings()
  } catch (e) {
    ElMessage.error(errText(e, '映射保存失败'))
  } finally {
    mappingSaving.value = false
  }
}

async function onDeleteMapping(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除 namespace「${row.namespace}」的归属映射？删除后该 namespace 的日志会被 ingest 拒收。`,
      '删除 Namespace 映射',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await deleteNamespaceMapping(row.namespace)
    ElMessage.success(res.message || '映射已删除')
    await loadMappings()
  } catch (e) {
    ElMessage.error(errText(e, '删除失败'))
  }
}

function openSnippet(row) {
  const inputName = `${row.namespace}-logs`.replace(/[^a-z0-9-]/g, '-')
  mappingSnippetTitle.value = `${row.namespace} 的采集器片段`
  mappingSnippetText.value = [
    '# 合并到 deploy/k8s/collector/values-offline.yaml 的 inputs 下',
    '# 如果文件里还是 inputs: []，先删掉这一行，再粘贴下面内容',
    'inputs:',
    `  - name: ${inputName}`,
    `    namespace: ${row.namespace}`,
    '    # container: <容器名，可选；同 namespace 多容器分帧规则不同时再填>',
    '    multiline: none',
    '    labels:',
    '      log_class: business',
    '      source_type: app',
    '      host: ${NODE_NAME}',
    '',
  ].join('\n')
  mappingSnippetVisible.value = true
}

async function copySnippet() {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(mappingSnippetText.value)
    } else {
      const ta = document.createElement('textarea')
      ta.value = mappingSnippetText.value
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      ta.remove()
    }
    ElMessage.success('片段已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选中文本复制')
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
          <p class="desc">租户是日志数据的隔离空间；Namespace 映射决定 K8s 日志归属</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" @click="activeTab === 'mappings' ? loadMappings() : load()">刷新</el-button>
          <el-button v-if="activeTab === 'tenants' && canCreate" type="primary" :icon="Plus" @click="openCreate">新建租户</el-button>
          <el-button v-if="activeTab === 'mappings' && canUpdate" type="primary" :icon="Plus" @click="openMappingCreate">新增映射</el-button>
        </div>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="page-tabs" @tab-change="onTabChange">
      <el-tab-pane label="租户列表" name="tenants">
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
      </el-tab-pane>

      <el-tab-pane label="Namespace 映射" name="mappings">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="未登记映射的 namespace 会被 ingest 拒收；新增映射后，还需要在采集器 values 中增加对应 inputs 并 helm upgrade。"
        />
        <el-alert v-if="mappingError" type="error" :closable="false" show-icon :title="mappingError" />

        <section class="card">
          <div class="filter-row">
            <div class="filter-item">
              <label>搜索</label>
              <el-input v-model="mappingFilters.keyword" placeholder="namespace / 租户 / 备注" clearable />
            </div>
            <div class="filter-item">
              <label>归属租户</label>
              <el-select v-model="mappingFilters.tenant" placeholder="全部租户" clearable filterable>
                <el-option v-for="t in rows" :key="t.code" :label="`${t.name}（${t.code}）`" :value="t.code" />
              </el-select>
            </div>
            <div class="filter-actions">
              <el-button @click="Object.assign(mappingFilters, { keyword: '', tenant: '' })">重置</el-button>
            </div>
          </div>

          <el-table v-loading="mappingLoading" :data="mappingRowsFiltered" row-key="namespace" size="default" stripe>
            <el-table-column label="Namespace" min-width="180">
              <template #default="{ row }">
                <span class="mono name-cell">{{ row.namespace }}</span>
              </template>
            </el-table-column>

            <el-table-column label="归属租户" min-width="190">
              <template #default="{ row }">
                <div>{{ row.tenantName || '—' }}</div>
                <div class="muted mono">{{ row.tenant }}</div>
              </template>
            </el-table-column>

            <el-table-column label="租户状态" width="100" align="center">
              <template #default="{ row }">
                <el-tag :type="row.tenantEnabled ? 'success' : 'warning'" size="small" effect="plain">
                  {{ row.tenantEnabled ? '已启用' : '已停用' }}
                </el-tag>
              </template>
            </el-table-column>

            <el-table-column label="备注" min-width="180">
              <template #default="{ row }"><span class="muted">{{ row.note || '—' }}</span></template>
            </el-table-column>

            <el-table-column prop="updatedAt" label="更新时间" width="160">
              <template #default="{ row }"><span class="muted">{{ row.updatedAt || '—' }}</span></template>
            </el-table-column>

            <el-table-column label="操作" width="250" align="center" fixed="right">
              <template #default="{ row }">
                <div class="op-actions">
                  <el-button link type="primary" :icon="CopyDocument" @click="openSnippet(row)">生成片段</el-button>
                  <el-button v-if="canUpdate" link type="primary" :icon="Edit" @click="openMappingEdit(row)">编辑</el-button>
                  <el-button v-if="canUpdate" link type="danger" :icon="Delete" @click="onDeleteMapping(row)">删除</el-button>
                </div>
              </template>
            </el-table-column>
          </el-table>

          <p class="note">{{ mappingNote }}</p>
        </section>
      </el-tab-pane>
    </el-tabs>

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

    <el-dialog
      v-model="mappingDialogVisible"
      :title="mappingDialogMode === 'create' ? '新增 Namespace 映射' : `编辑映射 · ${mappingForm.namespace}`"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form ref="mappingFormRef" :model="mappingForm" :rules="mappingRules" label-width="96px">
        <el-form-item label="Namespace" prop="namespace">
          <el-input
            v-model="mappingForm.namespace"
            :disabled="mappingDialogMode === 'edit'"
            placeholder="例如 prod-order"
          />
          <div class="form-hint">
            对应 K8s namespace；只能用小写字母、数字和短横线，最长 63 个字符。
          </div>
        </el-form-item>
        <el-form-item label="归属租户" prop="tenant">
          <el-select v-model="mappingForm.tenant" placeholder="选择租户" filterable style="width: 100%">
            <el-option
              v-for="t in enabledTenants"
              :key="t.code"
              :label="`${t.name}（${t.code}）`"
              :value="t.code"
            />
          </el-select>
          <div class="form-hint">只列出已启用租户；停用租户不能接收日志。</div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="mappingForm.note" type="textarea" :rows="2" placeholder="例如 订单系统生产 namespace" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="mappingDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="mappingSaving" @click="submitMapping">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="mappingSnippetVisible" :title="mappingSnippetTitle" width="720px">
      <p class="snippet-hint">
        把下面内容合并到 <span class="mono">deploy/k8s/collector/values-offline.yaml</span> 的
        <span class="mono">inputs</span> 下；多个 namespace 的条目要放在同一个 <span class="mono">inputs</span> 列表里。
      </p>
      <el-input v-model="mappingSnippetText" type="textarea" :rows="13" readonly />
      <template #footer>
        <el-button @click="mappingSnippetVisible = false">关闭</el-button>
        <el-button type="primary" :icon="CopyDocument" @click="copySnippet">复制片段</el-button>
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
.page-tabs {
  background: #fff;
  border: 1px solid var(--color-border-1);
  border-radius: 4px;
  padding: 0;
}
.page-tabs :deep(.el-tabs__header) { margin: 0; padding: 0 18px; }
.page-tabs :deep(.el-tabs__content) { padding: 14px 0 16px; overflow: visible; }
.page-tabs :deep(.el-alert) { margin: 0 18px 12px; }
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
.snippet-hint { margin: 0 0 10px; font-size: 12px; color: var(--color-text-3); line-height: 1.6; }
</style>
