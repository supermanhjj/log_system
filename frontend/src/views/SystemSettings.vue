<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Download, Refresh } from '@element-plus/icons-vue'
import {
  exportAuditCsv,
  getParams,
  listAudit,
  putParams,
} from '../api/system'
import { useAppStore } from '../stores/app'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()
const app = useAppStore()

const me = computed(() => auth.user || {})

// ---- 页签：按权限决定可见性 ----
const TABS = [
  { key: 'params', label: '系统参数', perm: ['param', 'view'] },
  { key: 'audit', label: '审计日志', perm: ['audit', 'view'] },
]
const visibleTabs = computed(() => TABS.filter((t) => auth.has(t.perm[0], t.perm[1])))
const activeTab = ref('params')
watch(visibleTabs, (list) => {
  if (list.length && !list.some((t) => t.key === activeTab.value)) activeTab.value = list[0].key
}, { immediate: true })

const metaError = ref('')

function errText(e, fallback) {
  const status = e.response?.status
  if (status === 401) {
    auth.clear()
    router.replace({ path: '/login', query: { redirect: '/settings/system' } })
    return '登录态已失效，请重新登录'
  }
  if (status === 403) return e.response?.data?.detail || '当前角色无此操作权限'
  return e.response?.data?.detail || e.message || fallback
}

// ---- 系统参数 ----
// 数值型参数的单位后缀，仅用于显示，不参与提交
const UNIT = { search_p95_target: 'ms' }

const paramLoading = ref(false)
const paramError = ref('')
const paramSaving = ref(false)
const paramList = ref([])
const paramForm = reactive({})
const paramNote = ref('')

async function loadParams() {
  paramLoading.value = true
  paramError.value = ''
  try {
    const res = await getParams()
    paramList.value = res.items || []
    paramNote.value = res.note || ''
    Object.keys(paramForm).forEach((k) => delete paramForm[k])
    paramList.value.forEach((p) => {
      paramForm[p.key] = p.type === 'bool' ? p.value === 'true' : p.value
    })
  } catch (e) {
    paramError.value = errText(e, '系统参数加载失败')
  } finally {
    paramLoading.value = false
  }
}

async function saveParams() {
  paramSaving.value = true
  try {
    const res = await putParams({ ...paramForm })
    ElMessage.success(res.message || '系统参数已保存')
    loadParams()
  } catch (e) {
    ElMessage.error(errText(e, '保存失败'))
  } finally {
    paramSaving.value = false
  }
}

// ---- 审计日志 ----
const auditLoading = ref(false)
const auditError = ref('')
const auditItems = ref([])
const auditTotal = ref(0)
const auditPage = ref(1)
const auditPageSize = ref(20)
const auditActions = ref([])
const auditFilters = reactive({ keyword: '', action: '', result: '', dateFrom: '', dateTo: '' })
const auditRange = ref([])

watch(auditRange, (v) => {
  auditFilters.dateFrom = v?.[0] || ''
  auditFilters.dateTo = v?.[1] || ''
})

async function loadAudit() {
  auditLoading.value = true
  auditError.value = ''
  try {
    const res = await listAudit({ ...auditFilters, page: auditPage.value, pageSize: auditPageSize.value })
    auditItems.value = res.items || []
    auditTotal.value = res.total || 0
    auditActions.value = res.actions || []
  } catch (e) {
    auditError.value = errText(e, '审计日志加载失败')
    auditItems.value = []
    auditTotal.value = 0
  } finally {
    auditLoading.value = false
  }
}

function searchAudit() {
  auditPage.value = 1
  loadAudit()
}

function resetAudit() {
  Object.assign(auditFilters, { keyword: '', action: '', result: '', dateFrom: '', dateTo: '' })
  auditRange.value = []
  searchAudit()
}

// ---- 页头动作 ----
const canExport = computed(() => activeTab.value === 'audit' && auth.has('audit', 'export'))

function reloadActive() {
  if (activeTab.value === 'params') loadParams()
  else loadAudit()
}

function onTabChange(key) {
  if (key === 'params' && !paramList.value.length) loadParams()
  if (key === 'audit' && !auditItems.value.length) loadAudit()
}

async function onExport() {
  try {
    await exportAuditCsv(auditFilters)
    ElMessage.success('已导出审计日志（CSV）')
  } catch (e) {
    ElMessage.error(errText(e, '导出失败'))
  }
}

onMounted(async () => {
  if (auth.has('param', 'view')) { activeTab.value = 'params'; loadParams() }
  else if (auth.has('audit', 'view')) { activeTab.value = 'audit'; loadAudit() }
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div class="breadcrumb"><span>平台管理</span><span class="sep">/</span><span class="cur">系统设置</span></div>
      <div class="head-bar">
        <div>
          <h1 class="title">系统设置</h1>
          <p class="desc">系统参数与操作审计，变更实时生效</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" @click="reloadActive">刷新</el-button>
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

      <!-- ================= 系统参数 ================= -->
      <div v-if="activeTab === 'params'">
        <el-alert v-if="paramError" type="error" :closable="false" show-icon :title="paramError" class="block-alert" />
        <div v-loading="paramLoading" class="param-body">
          <div v-for="p in paramList" :key="p.key" class="param-row">
            <label class="param-label">{{ p.label }}</label>
            <div class="param-control">
              <template v-if="p.type === 'enum'">
                <el-select v-model="paramForm[p.key]" style="width: 200px">
                  <el-option v-for="o in p.options" :key="o" :label="o" :value="o" />
                </el-select>
              </template>
              <template v-else-if="p.type === 'bool'">
                <el-switch v-model="paramForm[p.key]" />
              </template>
              <template v-else>
                <el-input v-model="paramForm[p.key]" style="width: 120px">
                  <template v-if="UNIT[p.key]" #suffix>{{ UNIT[p.key] }}</template>
                </el-input>
              </template>
              <span class="param-note">{{ p.note }}</span>
            </div>
          </div>
          <div class="param-row">
            <span class="param-label"></span>
            <div class="param-control">
              <el-button type="primary" :loading="paramSaving" :disabled="!auth.has('param', 'update')" @click="saveParams">保存</el-button>
              <el-button @click="loadParams">重置</el-button>
            </div>
          </div>
        </div>
        <p v-if="paramNote" class="note">{{ paramNote }}</p>
      </div>

      <!-- ================= 审计日志 ================= -->
      <div v-else>
        <div class="filter-row">
          <div class="filter-item">
            <label>关键字</label>
            <el-input v-model="auditFilters.keyword" placeholder="操作人 / 账号 / 对象 / 类型" clearable @keyup.enter="searchAudit" />
          </div>
          <div class="filter-item">
            <label>操作类型</label>
            <el-select v-model="auditFilters.action" placeholder="全部类型" clearable>
              <el-option v-for="a in auditActions" :key="a" :label="a" :value="a" />
            </el-select>
          </div>
          <div class="filter-item">
            <label>结果</label>
            <el-select v-model="auditFilters.result" placeholder="全部结果" clearable>
              <el-option label="成功" value="success" />
              <el-option label="失败" value="failed" />
            </el-select>
          </div>
          <div class="filter-item">
            <label>操作时间</label>
            <el-date-picker
              v-model="auditRange"
              type="daterange"
              value-format="YYYY-MM-DD"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              style="width: 240px"
            />
          </div>
          <div class="filter-actions">
            <el-button type="primary" @click="searchAudit">查询</el-button>
            <el-button @click="resetAudit">重置</el-button>
          </div>
        </div>

        <el-alert v-if="auditError" type="error" :closable="false" show-icon :title="auditError" class="block-alert" />

        <el-table v-loading="auditLoading" :data="auditItems" row-key="id" stripe>
          <el-table-column prop="time" label="操作时间" min-width="160" />
          <el-table-column prop="userName" label="操作人" min-width="110" />
          <el-table-column prop="account" label="账号" min-width="120" />
          <el-table-column prop="action" label="操作类型" min-width="120" />
          <el-table-column prop="target" label="操作对象" min-width="200" show-overflow-tooltip />
          <el-table-column prop="ip" label="来源 IP" min-width="130" />
          <el-table-column label="结果" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.result === 'success' ? 'success' : 'danger'" effect="plain">
                {{ row.result === 'success' ? '成功' : '失败' }}
              </el-tag>
            </template>
          </el-table-column>
          <template #empty><div class="empty-box">暂无审计记录</div></template>
        </el-table>

        <div class="pager">
          <span>共 {{ auditTotal }} 条</span>
          <div class="pager-right">
            <el-pagination
              v-model:current-page="auditPage"
              v-model:page-size="auditPageSize"
              :total="auditTotal"
              :page-sizes="[20, 50, 100]"
              layout="sizes, prev, pager, next, jumper"
              @current-change="loadAudit"
              @size-change="() => { auditPage = 1; loadAudit() }"
            />
          </div>
        </div>
      </div>
    </section>
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

.param-body { padding: 18px; max-width: 860px; }
.param-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; }
.param-label { width: 96px; flex-shrink: 0; text-align: right; font-size: 13px; color: var(--color-text-2); }
.param-control { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.param-note { font-size: 12px; color: var(--color-text-3); }

.empty-box { padding: 22px 0; color: var(--color-text-3); font-size: 13px; }
</style>
