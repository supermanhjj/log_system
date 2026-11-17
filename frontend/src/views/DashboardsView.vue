<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus, Download, Search } from '@element-plus/icons-vue'
import {
  listDashboards, createDashboard, deleteDashboard, getDashboardMeta,
} from '../api/dashboards'

const router = useRouter()

const loading = ref(false)
const allItems = ref([])
const filters = reactive({ name: '', owner: '', updateTime: '' })
const meta = reactive({ ranges: [] })

const filtered = computed(() => allItems.value.filter((d) => {
  if (filters.name && !d.name.toLowerCase().includes(filters.name.toLowerCase())) return false
  if (filters.owner && !String(d.owner || '').includes(filters.owner)) return false
  if (filters.updateTime && !String(d.updatedAt || '').startsWith(filters.updateTime)) return false
  return true
}))

const selected = ref([])
function onSelectionChange(rows) { selected.value = rows }

async function load() {
  loading.value = true
  try {
    allItems.value = await listDashboards()
  } catch {
    ElMessage.error('仪表盘列表加载失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

function resetFilters() {
  Object.assign(filters, { name: '', owner: '', updateTime: '' })
}

function openDashboard(row) {
  router.push(`/dashboards/${row.id}`)
}

// —— 新建 ——
const createVisible = ref(false)
const createSaving = ref(false)
const createForm = reactive({ name: '', description: '', defaultRange: '近 24 小时' })
function openCreate() {
  Object.assign(createForm, { name: '', description: '', defaultRange: '近 24 小时' })
  createVisible.value = true
}
async function submitCreate() {
  if (!createForm.name.trim()) return ElMessage.warning('请输入仪表盘名称')
  createSaving.value = true
  try {
    const created = await createDashboard({
      name: createForm.name.trim(),
      description: createForm.description, defaultRange: createForm.defaultRange,
      widgets: [],
    })
    createVisible.value = false
    ElMessage.success('仪表盘已创建，进入编辑添加图表')
    router.push(`/dashboards/${created.id}?edit=1`)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '创建失败')
  } finally {
    createSaving.value = false
  }
}

// —— 删除（单条 / 批量）——
async function doDelete(rows) {
  const ok = await ElMessageBox.confirm(
    `确认删除所选 ${rows.length} 个仪表盘？删除后图表配置不可恢复。`,
    '删除仪表盘',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
  ).then(() => true).catch(() => false)
  if (!ok) return
  try {
    for (const d of rows) await deleteDashboard(d.id)
    ElMessage.success(`已删除 ${rows.length} 个仪表盘`)
    await load()
  } catch {
    ElMessage.error('删除失败，请稍后重试')
  }
}

function onExport() {
  ElMessage.success(`已导出 ${filtered.value.length} 个仪表盘的配置清单（演示）`)
}

onMounted(async () => {
  load()
  try { Object.assign(meta, await getDashboardMeta()) } catch { /* 字典失败不影响列表 */ }
})
</script>

<template>
  <div class="page">
    <!-- 页头 -->
    <div class="page-head">
      <div class="breadcrumb"><span>日志分析</span><span class="sep">/</span><span class="cur">仪表盘</span></div>
      <div class="head-bar">
        <div>
          <h1 class="title">仪表盘</h1>
          <p class="desc">沉淀排障视图，支持拖拽配置图表</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" @click="load">刷新</el-button>
          <el-button type="primary" :icon="Plus" @click="openCreate">新建仪表盘</el-button>
          <el-button :icon="Download" @click="onExport">导出</el-button>
        </div>
      </div>
    </div>

    <!-- 筛选栏 -->
    <section class="filter-bar" aria-label="筛选条件">
      <div class="field">
        <label>仪表盘名称</label>
        <el-input v-model="filters.name" placeholder="请输入名称关键词" clearable />
      </div>
      <div class="field">
        <label>负责人</label>
        <el-input v-model="filters.owner" placeholder="请输入姓名" clearable />
      </div>
      <div class="field">
        <label>更新时间</label>
        <el-input v-model="filters.updateTime" placeholder="YYYY-MM-DD" clearable />
      </div>
      <div class="filter-actions">
        <el-button type="primary" :icon="Search" @click="() => {}">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </div>
    </section>

    <!-- 列表 -->
    <section class="list-card">
      <div class="batch-bar">
        <span>已选 <strong>{{ selected.length }}</strong> 项</span>
        <div class="batch-actions">
          <el-button link type="primary" size="small" @click="onExport">导出所选</el-button>
          <el-button link type="danger" size="small" :disabled="!selected.length" @click="doDelete(selected)">批量删除</el-button>
        </div>
      </div>
      <el-table
        :data="filtered" v-loading="loading" stripe size="default"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column label="仪表盘名称" min-width="180">
          <template #default="{ row }">
            <a class="link-name" @click="openDashboard(row)">{{ row.name }}</a>
          </template>
        </el-table-column>
        <el-table-column prop="owner" label="负责人" min-width="120" />
        <el-table-column label="图表数" min-width="100">
          <template #default="{ row }"><span class="num">{{ row.chartCount }}</span></template>
        </el-table-column>
        <el-table-column prop="updatedAt" label="更新时间" min-width="170" />
        <el-table-column label="状态" min-width="110">
          <template #default="{ row }">
            <span class="status-tag" :class="row.status === 'running' ? 'status-tag--normal' : 'status-tag--warning'">
              <i class="dot"></i>{{ row.status === 'running' ? '运行中' : '维护中' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right" align="center" header-align="center">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDashboard(row)">查看</el-button>
            <el-button link type="primary" size="small" @click="router.push(`/dashboards/${row.id}?edit=1`)">编辑</el-button>
            <el-button link type="danger" size="small" @click="doDelete([row])">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <span class="pager-total">共 {{ filtered.length }} 个</span>
      </div>
    </section>

    <!-- 新建弹窗 -->
    <el-dialog v-model="createVisible" title="新建仪表盘" width="520px" :close-on-click-modal="false">
      <el-form :model="createForm" label-width="88px">
        <el-form-item label="名称" required>
          <el-input v-model="createForm.name" maxlength="24" show-word-limit placeholder="请输入仪表盘名称" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createForm.description" type="textarea" :rows="3" placeholder="说明看板用途与适用场景" />
        </el-form-item>
        <el-form-item label="默认时间">
          <el-select v-model="createForm.defaultRange" style="width:100%">
            <el-option v-for="r in (meta.ranges?.length ? meta.ranges : ['近 1 小时','近 24 小时','近 7 天'])" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSaving" @click="submitCreate">保存</el-button>
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

.filter-bar {
  display: flex; flex-wrap: wrap; align-items: flex-end; gap: 12px 16px;
  background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 18px;
}
.field { display: flex; flex-direction: column; gap: 6px; min-width: 180px; }
.field label { font-size: 12px; color: var(--color-text-2); }
.field :deep(.el-input), .field :deep(.el-select) { width: 100%; }
.filter-actions { display: flex; gap: 8px; margin-left: auto; }

.list-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; }
.batch-bar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 16px; border-bottom: 1px solid var(--color-border-1);
  font-size: 13px; color: var(--color-text-2); background: #FAFBFC;
}
.batch-bar strong { color: var(--color-primary); }
.batch-actions { display: flex; gap: 12px; }
.link-name { color: var(--color-link); cursor: pointer; }
.link-name:hover { text-decoration: underline; }
.page :deep(.el-table) { padding: 0 18px; }
.num { font-variant-numeric: tabular-nums; font-weight: 600; }
.pager { display: flex; justify-content: flex-end; padding: 12px 16px; font-size: 12px; color: var(--color-text-3); }

.status-tag { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; padding: 2px 8px; border-radius: 10px; }
.status-tag .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.status-tag--normal { background: var(--tag-normal-bg); color: var(--tag-normal-text); }
.status-tag--normal .dot { background: var(--tag-normal-text); }
.status-tag--warning { background: var(--tag-warning-bg); color: var(--tag-warning-text); }
.status-tag--warning .dot { background: var(--tag-warning-text); }
</style>
