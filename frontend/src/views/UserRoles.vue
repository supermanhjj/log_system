<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Edit, Key, Plus, Refresh } from '@element-plus/icons-vue'
import {
  batchEnabled as apiBatchEnabled,
  batchResetPassword,
  createUser,
  exportUsersCsv,
  getMeta,
  listRoles,
  listUsers,
  resetUserPassword,
  toggleUser,
  updateRole,
  updateUser,
} from '../api/system'
import { useAppStore } from '../stores/app'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const app = useAppStore()

const me = computed(() => auth.user || {})

// ---- 页签：按权限决定可见性 ----
const TABS = [
  { key: 'users', label: '用户管理', perm: ['user', 'view'] },
  { key: 'roles', label: '角色权限', perm: ['role', 'view'] },
]
const visibleTabs = computed(() => TABS.filter((t) => auth.has(t.perm[0], t.perm[1])))
const activeTab = ref('users')
watch(visibleTabs, (list) => {
  if (list.length && !list.some((t) => t.key === activeTab.value)) activeTab.value = list[0].key
}, { immediate: true })

const meta = ref({ roles: [], tenants: [], permissionTree: [], actionLabels: {} })
const metaError = ref('')

function errText(e, fallback) {
  const status = e.response?.status
  if (status === 401) {
    auth.clear()
    router.replace({ path: '/login', query: { redirect: '/settings/users' } })
    return '登录态已失效，请重新登录'
  }
  if (status === 403) return e.response?.data?.detail || '当前角色无此操作权限'
  return e.response?.data?.detail || e.message || fallback
}

// ---- 用户管理 ----
const userLoading = ref(false)
const userError = ref('')
const users = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const selection = ref([])
const filters = reactive({ keyword: '', role: '', tenant: '', enabled: '' })

const enabledOptions = [
  { value: 'true', label: '启用' },
  { value: 'false', label: '停用' },
]

async function loadUsers() {
  userLoading.value = true
  userError.value = ''
  try {
    const res = await listUsers({ ...filters, page: page.value, pageSize: pageSize.value })
    users.value = res.items || []
    total.value = res.total || 0
  } catch (e) {
    userError.value = errText(e, '数据加载失败，请稍后重试')
    users.value = []
    total.value = 0
  } finally {
    userLoading.value = false
  }
}

function searchUsers() {
  page.value = 1
  loadUsers()
}

function onSelectionChange(rows) {
  selection.value = rows || []
}

function resetFilters() {
  Object.assign(filters, { keyword: '', role: '', tenant: '', enabled: '' })
  searchUsers()
}

async function onToggle(row, val) {
  try {
    const res = await toggleUser(row.id, val)
    row.enabled = val
    ElMessage.success(res.message || '状态已更新')
  } catch (e) {
    ElMessage.error(errText(e, '状态更新失败'))
    loadUsers()
  }
}

async function onBatchEnabled(enabled) {
  const ids = selection.value.map((r) => r.id)
  if (!ids.length) return ElMessage.warning('请先选择用户')
  try {
    await ElMessageBox.confirm(
      `确认${enabled ? '启用' : '停用'}所选 ${ids.length} 个用户？${enabled ? '' : '停用后该用户的登录态立即失效。'}`,
      enabled ? '批量启用' : '批量停用',
      { type: enabled ? 'info' : 'warning', confirmButtonText: '确认', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await apiBatchEnabled(ids, enabled)
    ElMessage.success(res.message || '已处理')
    if (res.skipped?.length) ElMessage.warning('已跳过：' + res.skipped.join('、'))
    selection.value = []
    loadUsers()
  } catch (e) {
    ElMessage.error(errText(e, '批量操作失败'))
  }
}

async function onBatchReset() {
  const ids = selection.value.map((r) => r.id)
  if (!ids.length) return ElMessage.warning('请先选择用户')
  try {
    await ElMessageBox.confirm(`确认重置所选 ${ids.length} 个用户的密码？`, '批量重置密码', {
      type: 'warning', confirmButtonText: '确认', cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    const res = await batchResetPassword(ids)
    showCredentials('批量重置密码结果', res.items || [])
    selection.value = []
    loadUsers()
  } catch (e) {
    ElMessage.error(errText(e, '批量重置失败'))
  }
}

async function onResetPassword(row) {
  try {
    await ElMessageBox.confirm(
      `确认重置「${row.name}」的密码？重置后该用户需使用临时密码重新登录。`,
      '重置密码',
      { type: 'warning', confirmButtonText: '确认', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await resetUserPassword(row.id)
    showCredentials('密码已重置', [{ account: row.account, name: row.name, tempPassword: res.tempPassword }])
    loadUsers()
  } catch (e) {
    ElMessage.error(errText(e, '重置失败'))
  }
}

// 临时密码只显示这一次（后端不存明文）
const credVisible = ref(false)
const credTitle = ref('')
const credRows = ref([])
function showCredentials(title, rows) {
  credTitle.value = title
  credRows.value = rows
  credVisible.value = true
}
async function copyCred(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选中')
  }
}

// ---- 新建 / 编辑用户 ----
const dialogVisible = ref(false)
const dialogMode = ref('create')
const saving = ref(false)
const formRef = ref(null)
const form = reactive({
  id: null, account: '', name: '', roleKey: '', tenant: '', phone: '', email: '', enabled: true,
})

const accountPattern = /^[a-z][a-z0-9_]{2,31}$/
const formRules = {
  name: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  account: [
    { required: true, message: '请输入账号', trigger: 'blur' },
    {
      pattern: accountPattern,
      message: '小写字母开头，可含小写字母、数字与下划线，长度 3-32',
      trigger: 'blur',
    },
  ],
  roleKey: [{ required: true, message: '请选择角色', trigger: 'change' }],
  tenant: [{ required: true, message: '请选择所属租户', trigger: 'change' }],
}

function resetForm() {
  Object.assign(form, {
    id: null, account: '', name: '', roleKey: '', tenant: app.tenant || '', phone: '',
    email: '', enabled: true,
  })
}

function openCreate() {
  dialogMode.value = 'create'
  resetForm()
  const pick = meta.value.roles.find((r) => r.key === 'ops_duty') || meta.value.roles[0]
  form.roleKey = pick?.key || ''
  if (!meta.value.tenants.some((t) => t.code === form.tenant)) {
    form.tenant = meta.value.tenants[0]?.code || ''
  }
  dialogVisible.value = true
}

function openEdit(row) {
  dialogMode.value = 'edit'
  Object.assign(form, {
    id: row.id,
    account: row.account,
    name: row.name,
    roleKey: row.roleKey,
    tenant: row.tenant,
    phone: row.phone || '',
    email: row.email || '',
    enabled: row.enabled,
  })
  dialogVisible.value = true
}

async function submitForm() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  saving.value = true
  try {
    if (dialogMode.value === 'create') {
      const res = await createUser({
        account: form.account, name: form.name, roleKey: form.roleKey,
        tenant: form.tenant, phone: form.phone, email: form.email, enabled: form.enabled,
      })
      ElMessage.success(res.message || '用户已保存')
      dialogVisible.value = false
      showCredentials('用户已创建 · 初始密码', [
        { account: res.account, name: form.name, tempPassword: res.initialPassword },
      ])
    } else {
      const res = await updateUser(form.id, {
        name: form.name, roleKey: form.roleKey, tenant: form.tenant,
        phone: form.phone, email: form.email, enabled: form.enabled,
      })
      ElMessage.success(res.message || '用户已保存')
      dialogVisible.value = false
    }
    loadUsers()
  } catch (e) {
    ElMessage.error(errText(e, '保存失败'))
  } finally {
    saving.value = false
  }
}

// ---- 角色权限 ----
const roleLoading = ref(false)
const roleError = ref('')
const roles = ref([])
const permTree = ref([])
const actionLabels = ref({})
const roleNote = ref('')
const roleDialog = reactive({ visible: false, key: '', name: '', scopeDesc: '', permissions: [], isBuiltin: false })
const roleSaving = ref(false)

async function loadRoles() {
  roleLoading.value = true
  roleError.value = ''
  try {
    const res = await listRoles()
    roles.value = res.items || []
    permTree.value = res.permissionTree || []
    actionLabels.value = res.actionLabels || {}
    roleNote.value = res.note || ''
  } catch (e) {
    roleError.value = errText(e, '角色数据加载失败')
  } finally {
    roleLoading.value = false
  }
}

function openRole(row) {
  Object.assign(roleDialog, {
    visible: true,
    key: row.key,
    name: row.name,
    scopeDesc: row.scopeDesc || '',
    permissions: [...(row.permissions || [])],
    isBuiltin: row.isBuiltin,
  })
}

async function saveRole() {
  roleSaving.value = true
  try {
    const res = await updateRole(roleDialog.key, {
      name: roleDialog.name,
      scopeDesc: roleDialog.scopeDesc,
      permissions: roleDialog.permissions,
    })
    ElMessage.success(res.message || '角色权限已保存')
    roleDialog.visible = false
    loadRoles()
    // 改的是自己角色时，权限可能变化，刷新本地权限点
    if (roleDialog.key === auth.user?.roleKey) auth.fetchMe().catch(() => {})
  } catch (e) {
    ElMessage.error(errText(e, '保存失败'))
  } finally {
    roleSaving.value = false
  }
}

function permCount(row) {
  return (row.permissions || []).length
}

// ---- 页头动作 ----
const canCreateUser = computed(() => auth.has('user', 'create'))
const canExport = computed(() => (activeTab.value === 'users' && auth.has('user', 'export')))

function reloadActive() {
  if (activeTab.value === 'users') loadUsers()
  else loadRoles()
}

function onTabChange(key) {
  if (key === 'users' && !users.value.length) loadUsers()
  if (key === 'roles' && !roles.value.length) loadRoles()
}

async function onExport() {
  try {
    await exportUsersCsv(filters)
    ElMessage.success('已导出用户信息（CSV）')
  } catch (e) {
    ElMessage.error(errText(e, '导出失败'))
  }
}

async function loadMeta() {
  try {
    const res = await getMeta()
    meta.value = res
  } catch (e) {
    metaError.value = errText(e, '页面初始化失败')
  }
}

onMounted(async () => {
  await loadMeta()
  if (auth.has('user', 'view')) loadUsers()
  else if (auth.has('role', 'view')) { activeTab.value = 'roles'; loadRoles() }
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div class="breadcrumb"><span>平台管理</span><span class="sep">/</span><span class="cur">用户与角色</span></div>
      <div class="head-bar">
        <div>
          <h1 class="title">用户与角色</h1>
          <p class="desc">按角色分配菜单与数据范围，变更实时生效</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" @click="reloadActive">刷新</el-button>
          <el-button v-if="activeTab === 'users' && canCreateUser" type="primary" :icon="Plus" @click="openCreate">新建用户</el-button>
          <el-button v-if="canExport" :icon="Download" @click="onExport">导出</el-button>
        </div>
      </div>
    </div>

    <el-alert v-if="metaError" type="error" :closable="false" show-icon :title="metaError" style="margin-bottom:0" />

    <section class="card">
      <div class="card-head">
        <el-tabs v-model="activeTab" class="tabs" @tab-change="onTabChange">
          <el-tab-pane v-for="t in visibleTabs" :key="t.key" :label="t.label" :name="t.key" />
        </el-tabs>
        <span class="tenant-hint">
          当前租户：{{ me.tenantName || app.tenant }} ·
          当前角色：{{ me.roleName || '—' }}
        </span>
      </div>

      <!-- ================= 用户管理 ================= -->
      <div v-if="activeTab === 'users'">
        <div class="filter-row">
          <div class="filter-item">
            <label>用户名</label>
            <el-input v-model="filters.keyword" placeholder="姓名或账号" clearable @keyup.enter="searchUsers" />
          </div>
          <div class="filter-item">
            <label>角色</label>
            <el-select v-model="filters.role" placeholder="全部角色" clearable>
              <el-option v-for="r in meta.roles" :key="r.key" :label="r.name" :value="r.key" />
            </el-select>
          </div>
          <div class="filter-item">
            <label>所属租户</label>
            <el-select v-model="filters.tenant" placeholder="全部租户" clearable>
              <el-option v-for="t in meta.tenants" :key="t.code" :label="t.name" :value="t.code" />
            </el-select>
          </div>
          <div class="filter-item">
            <label>启用状态</label>
            <el-select v-model="filters.enabled" placeholder="全部状态" clearable>
              <el-option v-for="o in enabledOptions" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
          </div>
          <div class="filter-actions">
            <el-button type="primary" @click="searchUsers">查询</el-button>
            <el-button @click="resetFilters">重置</el-button>
          </div>
        </div>

        <el-alert v-if="userError" type="error" :closable="false" show-icon :title="userError" class="block-alert">
          <template #default>
            <div class="alert-body">
              <span>用户列表来自 PostgreSQL 实时查询。</span>
              <el-button size="small" type="danger" plain @click="loadUsers">重试</el-button>
            </div>
          </template>
        </el-alert>

        <div class="batch-bar">
          <span>已选 <strong>{{ selection.length }}</strong> 项</span>
          <div class="batch-actions">
            <el-button link :icon="Key" :disabled="!selection.length || !auth.has('user', 'update')" @click="onBatchReset">重置密码</el-button>
            <el-button link :disabled="!selection.length || !auth.has('user', 'update')" @click="onBatchEnabled(false)">批量停用</el-button>
            <el-button link :disabled="!selection.length || !auth.has('user', 'update')" @click="onBatchEnabled(true)">批量启用</el-button>
          </div>
        </div>

        <el-table
          v-loading="userLoading"
          :data="users"
          row-key="id"
          size="default"
          stripe
          @selection-change="onSelectionChange"
        >
          <el-table-column type="selection" width="44" />
          <el-table-column prop="name" label="姓名" min-width="120">
            <template #default="{ row }">
              <span class="name-cell">{{ row.name }}</span>
              <el-tag v-if="row.id === me.id" size="small" type="primary" effect="plain" class="self-tag">当前登录</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="account" label="账号" min-width="130" />
          <el-table-column prop="roleName" label="角色" min-width="110" />
          <el-table-column prop="tenantName" label="所属租户" min-width="110" />
          <el-table-column label="启用" width="86">
            <template #default="{ row }">
              <el-switch
                :model-value="row.enabled"
                :disabled="row.id === me.id || !auth.has('user', 'update')"
                size="small"
                @change="(v) => onToggle(row, v)"
              />
            </template>
          </el-table-column>
          <el-table-column prop="lastLogin" label="最近登录" min-width="160">
            <template #default="{ row }">
              <span :class="{ muted: !row.lastLogin }">{{ row.lastLogin || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="170" fixed="right" align="center" header-align="center">
            <template #default="{ row }">
              <div class="op-actions">
                <el-button link type="primary" :icon="Edit" :disabled="!auth.has('user', 'update')" @click="openEdit(row)">编辑</el-button>
                <el-button link :icon="Key" :disabled="!auth.has('user', 'update')" @click="onResetPassword(row)">重置密码</el-button>
              </div>
            </template>
          </el-table-column>
          <template #empty>
            <div class="empty-box">
              <span>暂无用户{{ filters.keyword || filters.role || filters.tenant || filters.enabled ? '（当前筛选无结果）' : '' }}</span>
            </div>
          </template>
        </el-table>

        <div class="pager">
          <span>共 {{ total }} 个用户</span>
          <div class="pager-right">
            <el-pagination
              v-model:current-page="page"
              v-model:page-size="pageSize"
              :total="total"
              :page-sizes="[20, 50, 100]"
              layout="sizes, prev, pager, next, jumper"
              @current-change="loadUsers"
              @size-change="() => { page = 1; loadUsers() }"
            />
          </div>
        </div>
      </div>

      <!-- ================= 角色权限 ================= -->
      <div v-else-if="activeTab === 'roles'">
        <el-alert v-if="roleError" type="error" :closable="false" show-icon :title="roleError" class="block-alert" />
        <el-table v-loading="roleLoading" :data="roles" row-key="key" stripe>
          <el-table-column prop="name" label="角色名称" min-width="120" />
          <el-table-column prop="scopeDesc" label="权限范围" min-width="240" show-overflow-tooltip />
          <el-table-column label="权限点" width="96" align="center">
            <template #default="{ row }"><span class="num">{{ permCount(row) }}</span></template>
          </el-table-column>
          <el-table-column label="成员数" width="96" align="center">
            <template #default="{ row }"><span class="num">{{ row.memberCount }}</span></template>
          </el-table-column>
          <el-table-column prop="updatedAt" label="更新时间" min-width="160" />
          <el-table-column label="操作" width="110" fixed="right" align="center" header-align="center">
            <template #default="{ row }">
              <div class="op-actions">
                <el-button link type="primary" :icon="Edit" :disabled="!auth.has('role', 'update')" @click="openRole(row)">编辑</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="roleNote" class="note">{{ roleNote }}</p>
      </div>

    </section>

    <!-- 新建 / 编辑用户 -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新建用户' : '编辑用户'"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="formRules" label-width="96px">
        <el-form-item label="姓名" prop="name">
          <el-input v-model="form.name" placeholder="请输入真实姓名" maxlength="16" show-word-limit />
        </el-form-item>
        <el-form-item label="账号" prop="account">
          <el-input
            v-model="form.account"
            :disabled="dialogMode === 'edit'"
            placeholder="小写字母、数字与下划线"
            maxlength="32"
          />
          <span v-if="dialogMode === 'edit'" class="form-hint">账号创建后不可修改</span>
        </el-form-item>
        <el-form-item label="角色" prop="roleKey">
          <el-select v-model="form.roleKey" style="width: 100%">
            <el-option v-for="r in meta.roles" :key="r.key" :label="r.name" :value="r.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="所属租户" prop="tenant">
          <el-select v-model="form.tenant" style="width: 100%">
            <el-option v-for="t in meta.tenants" :key="t.code" :label="t.name" :value="t.code" />
          </el-select>
        </el-form-item>
        <el-form-item label="手机号">
          <el-input v-model="form.phone" placeholder="用于接收告警短信与密码重置" maxlength="20" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="form.email" placeholder="选填" maxlength="64" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
          <span class="form-hint">保存后立即生效，可随时在列表中停用</span>
        </el-form-item>
        <el-form-item v-if="dialogMode === 'create'" label="初始密码">
          <span class="form-hint">由系统生成临时密码，保存后一次性展示，请转交本人并提示首次登录后修改</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>

    <!-- 临时密码展示（只显示一次） -->
    <el-dialog v-model="credVisible" :title="credTitle" width="520px">
      <el-alert type="warning" :closable="false" show-icon title="临时密码仅显示这一次，关闭后无法再次查看" />
      <table class="cred-table">
        <thead>
          <tr><th>账号</th><th>姓名</th><th>临时密码</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="r in credRows" :key="r.account">
            <td class="mono">{{ r.account }}</td>
            <td>{{ r.name }}</td>
            <td class="mono strong">{{ r.tempPassword }}</td>
            <td><el-button link type="primary" @click="copyCred(r.tempPassword)">复制</el-button></td>
          </tr>
        </tbody>
      </table>
      <template #footer>
        <el-button type="primary" @click="credVisible = false">我已记录</el-button>
      </template>
    </el-dialog>

    <!-- 角色权限编辑 -->
    <el-dialog v-model="roleDialog.visible" title="编辑角色权限" width="720px" :close-on-click-modal="false">
      <el-form label-width="88px">
        <el-form-item label="角色名称">
          <el-input v-model="roleDialog.name" style="width: 260px" />
        </el-form-item>
        <el-form-item label="权限范围">
          <el-input v-model="roleDialog.scopeDesc" style="width: 100%" placeholder="用于列表展示的权限范围描述" />
        </el-form-item>
        <el-form-item label="权限点">
          <div class="perm-wrap">
            <el-alert
              v-if="roleDialog.isBuiltin && roleDialog.key === 'platform_admin'"
              type="info"
              :closable="false"
              show-icon
              title="平台管理员为内置最高权限角色，权限不可裁剪（防止把自己锁在门外）"
              style="margin-bottom: 10px"
            />
            <div class="perm-grid">
              <div v-for="g in permTree" :key="g.resource" class="perm-group">
                <div class="perm-group-name">{{ g.label }}</div>
                <el-checkbox-group v-model="roleDialog.permissions" :disabled="roleDialog.key === 'platform_admin'">
                  <el-checkbox
                    v-for="a in g.actions"
                    :key="`${g.resource}:${a}`"
                    :label="`${g.resource}:${a}`"
                  >
                    {{ actionLabels[a] || a }}
                  </el-checkbox>
                </el-checkbox-group>
              </div>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="roleDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="roleSaving" :disabled="roleDialog.key === 'platform_admin'" @click="saveRole">保存</el-button>
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
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 18px;
  border-bottom: 1px solid var(--color-border-1);
}
.tabs { flex: 1; }
.tabs :deep(.el-tabs__header) { margin: 0; }
.tabs :deep(.el-tabs__nav-wrap::after) { display: none; }
.tenant-hint { font-size: 12px; color: var(--color-text-3); flex-shrink: 0; }

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

.block-alert { margin: 12px 18px; width: auto; }
.alert-body { display: flex; justify-content: space-between; align-items: center; gap: 12px; }

.batch-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 18px 2px;
  font-size: 13px;
  color: var(--color-text-2);
}
.batch-bar strong { color: #03857E; }
.batch-actions { display: flex; gap: 4px; }
.batch-actions .el-button { margin: 0; }

.page :deep(.el-table) { padding: 0 18px; }
.name-cell { color: var(--color-text-1); font-weight: 600; }
.self-tag { margin-left: 6px; }
.muted { color: var(--color-text-3); }
.num { font-variant-numeric: tabular-nums; font-weight: 600; }
.op-actions { display: flex; align-items: center; justify-content: center; gap: 2px; flex-wrap: nowrap; }
.op-actions .el-button { margin: 0; }
.empty-box { padding: 22px 0; color: var(--color-text-3); font-size: 13px; }

.pager {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 18px 16px;
  font-size: 13px;
  color: var(--color-text-3);
}
.pager-right { display: flex; align-items: center; gap: 12px; }

.note { margin: 0; padding: 10px 18px 16px; font-size: 12px; color: var(--color-text-3); }

/* 系统参数 */
.param-body { padding: 18px; max-width: 860px; }
.param-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; }
.param-label { width: 96px; flex-shrink: 0; text-align: right; font-size: 13px; color: var(--color-text-2); }
.param-control { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.param-note { font-size: 12px; color: var(--color-text-3); }

/* 弹窗内 */
.form-hint { font-size: 12px; color: var(--color-text-3); margin-left: 8px; }
.cred-table { width: 100%; margin-top: 12px; border-collapse: collapse; font-size: 13px; }
.cred-table th, .cred-table td { border-bottom: 1px solid var(--color-border-1); padding: 8px 10px; text-align: left; }
.cred-table th { color: var(--color-text-2); font-weight: 600; background: #F7FAFA; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.strong { color: #03857E; font-weight: 600; }

.perm-wrap { width: 100%; }
.perm-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 20px; }
.perm-group { padding: 5px 0; border-bottom: 1px dashed var(--color-border-1); }
.perm-group-name { font-size: 12px; color: var(--color-text-3); margin-bottom: 1px; }
.perm-group :deep(.el-checkbox) { margin-right: 14px; height: 24px; }
</style>
