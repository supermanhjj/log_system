<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Refresh, Plus, Download, Upload, Connection, Delete, Edit, Setting,
} from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import {
  GRAMMAR_CATEGORIES, getAIMeta, listProviders, createProvider, updateProvider,
  deleteProvider, setDefaultProvider, testProvider, listGrammar, createGrammar,
  updateGrammar, deleteGrammar, importGrammar, exportGrammar, syncFields,
  previewGrammar, listCapabilities, updateCapability,
} from '../api/ai'

const app = useAppStore()
const loading = ref(false)
const activeTab = ref('models')
const activeKind = ref('commercial')
const meta = ref({ kinds: [], presets: {}, grammarCategories: [], cliHint: {}, cliModels: [], cliModelSource: '', counts: {} })

// ---- 模型接入 ----
const providers = ref([])
const defaultId = ref(null)
const providerDrawer = ref(false)
const providerSaving = ref(false)
const editingProviderId = ref(null)
const probeResult = ref(null)
const probeLoadingId = ref(null)
const runVersion = ref(false)

function blankProviderForm(kind) {
  return {
    name: '', kind, provider: 'custom', base_url: '', model_name: '', api_key: '',
    cli_path: '', work_dir: '', timeout: 30, enabled: true,
    note: '', probe_path: '',
  }
}
const providerForm = reactive(blankProviderForm('commercial'))

const presetOptions = computed(() => {
  const all = meta.value.presets || {}
  return Object.entries(all)
    .filter(([, p]) => p.kind === providerForm.kind)
    .map(([key, p]) => ({ key, label: p.label || key, ...p }))
})

function providersOf(kind) {
  return providers.value.filter((p) => p.kind === kind)
}
function kindLabel(kind) {
  return (meta.value.kinds || []).find((k) => k.key === kind)?.label || kind
}
function presetLabel(key) {
  return meta.value.presets?.[key]?.label || key
}
function applyPresetValues(key) {
  const p = meta.value.presets?.[key] || {}
  providerForm.provider = key
  providerForm.base_url = p.base_url || ''
  providerForm.model_name = p.model_name || ''
  providerForm.probe_path = p.probe_path || (p.kind === 'cli' ? '' : '/v1/models')
  providerForm.cli_path = p.cli_path || ''
}
function openProviderCreate(kind) {
  editingProviderId.value = null
  Object.assign(providerForm, blankProviderForm(kind))
  applyPresetValues(kind === 'commercial' ? 'deepseek' : kind === 'local' ? 'ollama' : 'codebuddy')
  providerForm.name = `${kindLabel(kind)}接入 ${providersOf(kind).length + 1}`
  probeResult.value = null
  providerDrawer.value = true
}
function openProviderEdit(row) {
  editingProviderId.value = row.id
  Object.assign(providerForm, {
    name: row.name, kind: row.kind, provider: row.provider, base_url: row.base_url,
    model_name: row.model_name, api_key: '', cli_path: row.cli_path,
    work_dir: row.work_dir, timeout: row.timeout,
    enabled: row.enabled, note: row.note, probe_path: row.extra?.probe_path || '',
  })
  probeResult.value = null
  providerDrawer.value = true
}
async function saveProvider() {
  if (!providerForm.name.trim()) return ElMessage.warning('请填写接入名称')
  if (providerForm.kind !== 'cli' && !providerForm.base_url.trim().startsWith('http')) {
    return ElMessage.warning('请填写 http(s) 开头的 Base URL')
  }
  if (providerForm.kind === 'cli' && !providerForm.cli_path.trim()) {
    return ElMessage.warning('请填写 CLI 可执行文件路径')
  }
  const extra = {}
  if (providerForm.probe_path) extra.probe_path = providerForm.probe_path
  const payload = {
    name: providerForm.name.trim(), kind: providerForm.kind, provider: providerForm.provider,
    base_url: providerForm.base_url.trim(), model_name: providerForm.model_name.trim(),
    api_key: providerForm.api_key.trim(), cli_path: providerForm.cli_path.trim(),
    work_dir: providerForm.work_dir.trim(),
    timeout: Number(providerForm.timeout) || 30, enabled: providerForm.enabled,
    note: providerForm.note, extra,
  }
  providerSaving.value = true
  try {
    if (editingProviderId.value) await updateProvider(editingProviderId.value, payload)
    else await createProvider(payload)
    ElMessage.success('接入配置已保存')
    providerDrawer.value = false
    await loadProviders()
  } catch (e) {
    ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
  } finally {
    providerSaving.value = false
  }
}
async function removeProvider(row) {
  try {
    await ElMessageBox.confirm(`确认删除接入「${row.name}」？`, '删除确认', { type: 'warning' })
  } catch { return }
  try {
    await deleteProvider(row.id)
    ElMessage.success('已删除')
    await loadProviders()
  } catch (e) {
    ElMessage.error('删除失败：' + (e.response?.data?.detail || e.message))
  }
}
async function makeDefault(row) {
  try {
    await setDefaultProvider(row.id)
    ElMessage.success(`已将「${row.name}」设为默认接入`)
    await loadProviders()
  } catch (e) {
    ElMessage.error('设置失败：' + (e.response?.data?.detail || e.message))
  }
}
async function runProbe(row) {
  probeLoadingId.value = row.id
  probeResult.value = null
  try {
    const res = await testProvider(row.id, { run_version: row.kind === 'cli' && runVersion.value })
    probeResult.value = { ...res, name: row.name }
    await loadProviders()
  } catch (e) {
    probeResult.value = { ok: false, name: row.name, detail: e.response?.data?.detail || e.message, elapsedMs: 0 }
  } finally {
    probeLoadingId.value = null
  }
}

// ---- PPL 语法规则库 ----
const grammarFilters = reactive({ category: '', keyword: '', enabled: '' })
const grammarRows = ref([])
const grammarTotal = ref(0)
const grammarCounts = ref({})
const grammarDrawer = ref(false)
const grammarSaving = ref(false)
const editingRuleId = ref(null)
const grammarForm = reactive({
  category: 'command', rule_key: '', rule_value: '', priority: 0, enabled: true, note: '',
})
const previewQuestion = ref('查询最近10分钟的错误日志')
const previewResult = ref(null)
const fileInput = ref(null)

function categoryLabel(key) {
  return GRAMMAR_CATEGORIES.find((c) => c.key === key)?.label || key
}
function openGrammarCreate() {
  editingRuleId.value = null
  Object.assign(grammarForm, {
    category: grammarFilters.category || 'command', rule_key: '', rule_value: '',
    priority: 0, enabled: true, note: '',
  })
  grammarDrawer.value = true
}
function openGrammarEdit(row) {
  editingRuleId.value = row.id
  Object.assign(grammarForm, {
    category: row.category, rule_key: row.rule_key, rule_value: row.rule_value,
    priority: row.priority, enabled: row.enabled, note: row.note,
  })
  grammarDrawer.value = true
}
async function saveGrammar() {
  if (!grammarForm.rule_key.trim()) return ElMessage.warning('请填写规则键')
  grammarSaving.value = true
  try {
    const payload = { ...grammarForm, rule_key: grammarForm.rule_key.trim() }
    if (editingRuleId.value) await updateGrammar(editingRuleId.value, payload)
    else await createGrammar(payload)
    ElMessage.success('规则已保存')
    grammarDrawer.value = false
    await loadGrammar()
  } catch (e) {
    ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
  } finally {
    grammarSaving.value = false
  }
}
async function removeGrammar(row) {
  try {
    await ElMessageBox.confirm(`确认删除规则「${row.rule_key}」？`, '删除确认', { type: 'warning' })
  } catch { return }
  try {
    await deleteGrammar(row.id)
    ElMessage.success('已删除')
    await loadGrammar()
  } catch (e) {
    ElMessage.error('删除失败：' + (e.response?.data?.detail || e.message))
  }
}
async function doExport() {
  try { await exportGrammar(); ElMessage.success('规则库已导出') }
  catch (e) { ElMessage.error('导出失败：' + (e.response?.data?.detail || e.message)) }
}
function pickImportFile() { fileInput.value?.click() }
async function onImportFile(e) {
  const file = e.target.files?.[0]
  if (!file) return
  try {
    const payload = JSON.parse(await file.text())
    const rules = Array.isArray(payload) ? payload : payload.rules
    if (!Array.isArray(rules)) throw new Error('文件缺少 rules 数组')
    const res = await importGrammar({ rules, mode: 'merge' })
    ElMessage.success(`导入完成：新增 ${res.created} 条，更新 ${res.updated} 条`)
    await loadGrammar()
  } catch (err) {
    ElMessage.error('导入失败：' + (err.response?.data?.detail || err.message))
  } finally {
    e.target.value = ''
  }
}
async function doSyncFields() {
  try {
    const res = await syncFields(app.tenant)
    ElMessage.success(`已按 ${res.index || '真实索引'} 的 mapping 同步 ${res.synced} 个字段`)
    await loadGrammar()
  } catch (e) {
    ElMessage.error('同步失败：' + (e.response?.data?.detail || e.message))
  }
}
async function doPreview() {
  try { previewResult.value = await previewGrammar(previewQuestion.value) }
  catch (e) { ElMessage.error('预览失败：' + (e.response?.data?.detail || e.message)) }
}

// ---- AI 能力扩展 ----
const capabilities = ref([])
const capConfigTexts = reactive({})

async function toggleCapability(row, val) {
  row.enabled = val
  try { await updateCapability(row.cap_key, { enabled: val }) }
  catch (e) { row.enabled = !val; ElMessage.error('更新失败：' + (e.response?.data?.detail || e.message)) }
}
async function saveCapability(row) {
  let config
  try { config = JSON.parse(capConfigTexts[row.cap_key] || '{}') }
  catch { return ElMessage.warning('扩展配置需为合法 JSON') }
  try {
    await updateCapability(row.cap_key, { config, note: row.note || '' })
    ElMessage.success('能力配置已保存')
  } catch (e) {
    ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
  }
}

// ---- 加载 ----
async function loadProviders() {
  const res = await listProviders()
  providers.value = res.items || []
  defaultId.value = res.defaultId ?? null
}
async function loadGrammar() {
  const res = await listGrammar(grammarFilters)
  grammarRows.value = res.items || []
  grammarTotal.value = res.total || 0
  grammarCounts.value = res.byCategory || {}
}
async function loadCapabilities() {
  const res = await listCapabilities()
  capabilities.value = res.items || []
  ;(res.items || []).forEach((c) => { capConfigTexts[c.cap_key] = JSON.stringify(c.config || {}, null, 2) })
}
async function loadAll() {
  loading.value = true
  try {
    const [m] = await Promise.all([
      getAIMeta(), loadProviders(), loadGrammar(), loadCapabilities(),
    ])
    meta.value = m || meta.value
    if (!meta.value.kinds?.length) meta.value.kinds = [
      { key: 'commercial', label: '商用 API', desc: '', hint: '' },
      { key: 'local', label: '本地自定义', desc: '', hint: '' },
      { key: 'cli', label: 'WorkBuddy CLI', desc: '', hint: '' },
    ]
  } catch (e) {
    ElMessage.error('AI 配置服务不可用：' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}
function onRefresh() { loadAll() }
function onGrammarReset() {
  Object.assign(grammarFilters, { category: '', keyword: '', enabled: '' })
  loadGrammar()
}
onMounted(loadAll)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-head">
      <div class="breadcrumb"><span>智能管理</span><span class="sep">/</span><span class="cur">AI 能力配置</span></div>
      <div class="head-bar">
        <div>
          <h1 class="title">智能管理</h1>
          <p class="desc">模型来源接入、PPL 语法规则库与 AI 能力扩展配置</p>
        </div>
        <div class="head-actions">
          <el-button :icon="Refresh" @click="onRefresh">刷新</el-button>
        </div>
      </div>
    </div>

    <el-card shadow="never" class="config-card">
      <el-tabs v-model="activeTab" class="main-tabs">
        <!-- ============ 模型接入 ============ -->
        <el-tab-pane label="模型接入" name="models">
          <el-tabs v-model="activeKind" class="sub-tabs">
            <el-tab-pane v-for="k in meta.kinds" :key="k.key" :label="k.label" :name="k.key">
              <div class="section-note">{{ k.desc }}<span v-if="k.hint"> · {{ k.hint }}</span></div>
              <div class="toolbar">
                <div class="toolbar-left">
                  <span class="muted">共 {{ providersOf(k.key).length }} 个接入配置</span>
                  <el-checkbox v-if="k.key === 'cli'" v-model="runVersion" class="probe-check">
                    测试时试运行版本命令（--version）
                  </el-checkbox>
                </div>
                <el-button type="primary" :icon="Plus" @click="openProviderCreate(k.key)">新建接入</el-button>
              </div>
              <el-table :data="providersOf(k.key)" stripe size="small" class="data-table">
                <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
                <el-table-column label="接入方式" min-width="130">
                  <template #default="{ row }">{{ presetLabel(row.provider) }}</template>
                </el-table-column>
                <el-table-column :label="k.key === 'cli' ? 'CLI 路径' : 'Base URL'" min-width="210" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span class="mono">{{ k.key === 'cli' ? row.cli_path : row.base_url }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="模型" min-width="150" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span class="mono">{{ row.model_name || '—' }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="timeout" label="超时(s)" width="82" align="center" />
                <el-table-column label="默认" width="70" align="center">
                  <template #default="{ row }">
                    <el-tag v-if="row.id === defaultId" size="small" effect="plain" type="success">默认</el-tag>
                    <span v-else class="muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column label="状态" width="86" align="center">
                  <template #default="{ row }">
                    <el-tag size="small" effect="plain" :type="row.enabled ? 'success' : 'info'">
                      {{ row.enabled ? '已启用' : '已停用' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="最近探测" width="110" align="center">
                  <template #default="{ row }">
                    <el-tooltip v-if="row.extra?.lastProbe" :content="row.extra.lastProbe.detail" placement="top">
                      <el-tag size="small" effect="plain" :type="row.extra.lastProbe.ok ? 'success' : 'danger'">
                        {{ row.extra.lastProbe.ok ? '通过' : '未通过' }}
                      </el-tag>
                    </el-tooltip>
                    <span v-else class="muted">未测试</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="250" fixed="right">
                  <template #default="{ row }">
                    <div class="op-actions">
                      <el-button link type="primary" size="small" :loading="probeLoadingId === row.id"
                        @click="runProbe(row)">测试连接</el-button>
                      <el-button link type="primary" size="small" @click="openProviderEdit(row)">编辑</el-button>
                      <el-button link type="primary" size="small" :disabled="row.id === defaultId"
                        @click="makeDefault(row)">设为默认</el-button>
                      <el-button link type="danger" size="small" @click="removeProvider(row)">删除</el-button>
                    </div>
                  </template>
                </el-table-column>
                <template #empty>
                  <div class="empty-box"><p>暂无{{ k.label }}接入配置</p>
                    <el-button size="small" type="primary" @click="openProviderCreate(k.key)">新建接入</el-button>
                  </div>
                </template>
              </el-table>
            </el-tab-pane>
          </el-tabs>

          <div v-if="probeResult" class="probe-result">
            <el-alert :type="probeResult.ok ? 'success' : 'error'" :closable="true" show-icon
              :title="`${probeResult.name} · 探测${probeResult.ok ? '通过' : '未通过'} · ${probeResult.elapsedMs}ms`"
              @close="probeResult = null">
              <template #default>
                <div class="probe-detail">{{ probeResult.detail }}</div>
                <div class="muted">{{ probeResult.checkedAt }} · {{ probeResult.url || probeResult.resolvedPath || '' }}</div>
              </template>
            </el-alert>
          </div>
        </el-tab-pane>

        <!-- ============ PPL 语法规则库 ============ -->
        <el-tab-pane label="PPL 语法规则库" name="grammar">
          <div class="section-note">
            规则为可编辑默认模板，供后续自然语言转 PPL 注入使用；字段别名可按索引真实 mapping 同步。
          </div>
          <div class="filter-card">
            <div class="filter-row">
              <div class="filter-item"><label>规则分类</label>
                <el-select v-model="grammarFilters.category" placeholder="全部分类" clearable>
                  <el-option v-for="c in GRAMMAR_CATEGORIES" :key="c.key" :label="c.label" :value="c.key" />
                </el-select>
              </div>
              <div class="filter-item"><label>关键词</label>
                <el-input v-model="grammarFilters.keyword" placeholder="规则键 / 规则值" clearable
                  @keyup.enter="loadGrammar" />
              </div>
              <div class="filter-item"><label>状态</label>
                <el-select v-model="grammarFilters.enabled" placeholder="全部" clearable>
                  <el-option label="已启用" value="true" /><el-option label="已停用" value="false" />
                </el-select>
              </div>
              <div class="filter-actions">
                <el-button type="primary" @click="loadGrammar">查询</el-button>
                <el-button @click="onGrammarReset">重置</el-button>
              </div>
            </div>
          </div>
          <div class="toolbar">
            <div class="toolbar-left stat-chips">
              <span class="muted">共 {{ grammarTotal }} 条规则</span>
              <el-tag v-for="c in GRAMMAR_CATEGORIES" :key="c.key" size="small" effect="plain">
                {{ c.label }} {{ grammarCounts[c.key] || 0 }}
              </el-tag>
            </div>
            <div class="head-actions">
              <el-button :icon="Connection" @click="doSyncFields">同步索引字段</el-button>
              <el-button :icon="Upload" @click="pickImportFile">导入</el-button>
              <el-button :icon="Download" @click="doExport">导出</el-button>
              <el-button type="primary" :icon="Plus" @click="openGrammarCreate">新建规则</el-button>
              <input ref="fileInput" type="file" accept=".json,application/json" class="hidden-file"
                @change="onImportFile" />
            </div>
          </div>
          <el-table :data="grammarRows" stripe size="small" class="data-table" max-height="420">
            <el-table-column label="分类" width="120">
              <template #default="{ row }"><el-tag size="small" effect="plain">{{ categoryLabel(row.category) }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="rule_key" label="规则键" min-width="140" show-overflow-tooltip />
            <el-table-column prop="rule_value" label="规则值" min-width="250" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.rule_value }}</span></template>
            </el-table-column>
            <el-table-column prop="priority" label="优先级" width="80" align="center" />
            <el-table-column label="状态" width="86" align="center">
              <template #default="{ row }">
                <el-tag size="small" effect="plain" :type="row.enabled ? 'success' : 'info'">
                  {{ row.enabled ? '已启用' : '已停用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="note" label="说明" min-width="180" show-overflow-tooltip />
            <el-table-column label="操作" width="130" fixed="right">
              <template #default="{ row }">
                <div class="op-actions">
                  <el-button link type="primary" size="small" @click="openGrammarEdit(row)">编辑</el-button>
                  <el-button link type="danger" size="small" @click="removeGrammar(row)">删除</el-button>
                </div>
              </template>
            </el-table-column>
            <template #empty><div class="empty-box"><p>当前筛选条件下没有规则</p></div></template>
          </el-table>

          <div class="preview-box">
            <div class="section-title">规则命中预览</div>
            <div class="preview-row">
              <el-input v-model="previewQuestion" placeholder="输入一句自然语言，查看将注入模型的语法片段"
                @keyup.enter="doPreview" />
              <el-button type="primary" @click="doPreview">预览命中</el-button>
            </div>
            <div v-if="previewResult" class="preview-body">
              <div class="muted">命中 {{ previewResult.matched }} 条 · {{ previewResult.note }}</div>
              <pre class="code-block">{{ previewResult.promptContext || '（无命中规则）' }}</pre>
            </div>
          </div>
        </el-tab-pane>

        <!-- ============ AI 能力扩展 ============ -->
        <el-tab-pane label="能力扩展" name="capabilities">
          <div class="section-note">
            能力注册表为后续 AI 能力预留配置位；当前四项均为「仅登记配置」，运行时能力（含自然语言转 PPL）待接入。
          </div>
          <div class="cap-grid">
            <div v-for="c in capabilities" :key="c.cap_key" class="cap-card">
              <div class="cap-head">
                <div>
                  <div class="cap-title">{{ c.name }}</div>
                  <el-tag size="small" effect="plain" :type="c.status === 'available' ? 'success' : 'info'">
                    {{ c.status === 'available' ? '运行时已接入' : '仅登记配置' }}
                  </el-tag>
                </div>
                <el-switch :model-value="c.enabled" @change="(v) => toggleCapability(c, v)" />
              </div>
              <p class="cap-desc">{{ c.description }}</p>
              <div class="cap-label">扩展配置（JSON）</div>
              <el-input v-model="capConfigTexts[c.cap_key]" type="textarea" :rows="3" class="mono" />
              <div class="cap-label">备注</div>
              <el-input v-model="c.note" placeholder="可记录接入计划与约束" />
              <div class="cap-foot">
                <el-button type="primary" size="small" @click="saveCapability(c)">保存配置</el-button>
              </div>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 模型接入表单 -->
    <el-drawer v-model="providerDrawer" :title="editingProviderId ? '编辑接入配置' : '新建接入配置'" size="560px">
      <el-form :model="providerForm" label-width="118px">
        <el-form-item label="接入类型">
          <el-tag effect="plain">{{ kindLabel(providerForm.kind) }}</el-tag>
        </el-form-item>
        <el-form-item label="名称"><el-input v-model="providerForm.name" placeholder="便于识别的接入名称" /></el-form-item>
        <el-form-item label="接入预设">
          <el-select v-model="providerForm.provider" style="width:100%" @change="applyPresetValues">
            <el-option v-for="p in presetOptions" :key="p.key" :label="p.label" :value="p.key" />
          </el-select>
          <div class="form-hint">选择预设自动填充地址 / 路径，之后可按需修改</div>
        </el-form-item>
        <el-divider content-position="left">{{ providerForm.kind === 'cli' ? '命令行参数' : '接口参数' }}</el-divider>
        <template v-if="providerForm.kind !== 'cli'">
          <el-form-item label="Base URL"><el-input v-model="providerForm.base_url" placeholder="https://api.deepseek.com" /></el-form-item>
          <el-form-item label="模型名"><el-input v-model="providerForm.model_name" placeholder="deepseek-chat" /></el-form-item>
          <el-form-item label="API Key">
            <el-input v-model="providerForm.api_key" type="password" show-password
              :placeholder="editingProviderId ? '留空保持原密钥不变' : '仅存服务端，列表脱敏显示'" />
          </el-form-item>
          <el-form-item label="探测路径"><el-input v-model="providerForm.probe_path" placeholder="/v1/models" /></el-form-item>
        </template>
        <template v-else>
          <el-form-item label="CLI 路径">
            <el-input v-model="providerForm.cli_path" class="mono" placeholder="~/.local/bin/codebuddy" />
            <div class="form-hint">{{ meta.cliHint?.note || '路径按容器内视角解析' }}</div>
          </el-form-item>
          <el-form-item label="选择模型">
            <el-select v-model="providerForm.model_name" filterable allow-create default-first-option
              style="width:100%" placeholder="选择或输入模型 ID">
              <el-option v-for="m in (meta.cliModels || [])" :key="m" :label="m" :value="m" />
            </el-select>
            <div class="form-hint">{{ meta.cliModelSource || '选择调用 CLI 时使用的模型；可自定义输入模型 ID。' }}</div>
          </el-form-item>
          <el-form-item label="工作目录"><el-input v-model="providerForm.work_dir" class="mono" placeholder="留空使用服务默认目录" /></el-form-item>
        </template>
        <el-form-item label="超时(秒)"><el-input-number v-model="providerForm.timeout" :min="1" :max="300" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="providerForm.enabled" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="providerForm.note" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="providerDrawer = false">取消</el-button>
        <el-button type="primary" :loading="providerSaving" @click="saveProvider">保存</el-button>
      </template>
    </el-drawer>

    <!-- 语法规则表单 -->
    <el-drawer v-model="grammarDrawer" :title="editingRuleId ? '编辑规则' : '新建规则'" size="520px">
      <el-form :model="grammarForm" label-width="100px">
        <el-form-item label="分类">
          <el-select v-model="grammarForm.category" style="width:100%">
            <el-option v-for="c in GRAMMAR_CATEGORIES" :key="c.key" :label="c.label" :value="c.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="规则键"><el-input v-model="grammarForm.rule_key" placeholder="字段别名 / 业务词 / 命令名" /></el-form-item>
        <el-form-item label="规则值"><el-input v-model="grammarForm.rule_value" type="textarea" :rows="3" class="mono" /></el-form-item>
        <el-form-item label="优先级"><el-input-number v-model="grammarForm.priority" :min="0" :max="999" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="grammarForm.enabled" /></el-form-item>
        <el-form-item label="说明"><el-input v-model="grammarForm.note" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="grammarDrawer = false">取消</el-button>
        <el-button type="primary" :loading="grammarSaving" @click="saveGrammar">保存</el-button>
      </template>
    </el-drawer>
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
.desc { font-size: 13px; color: var(--color-text-3); margin: 4px 0 0; }
.head-actions { display: flex; gap: 8px; flex-shrink: 0; align-items: center; }
.config-card, .table-card { border-radius: 4px; }
.section-note { font-size: 12px; color: var(--color-text-3); line-height: 1.7; padding: 2px 0 10px; }
.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin: 8px 0 12px; flex-wrap: wrap; }
.toolbar-left { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.probe-check { margin-left: 4px; }
.muted { color: var(--color-text-3); font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
.op-actions { display: flex; align-items: center; justify-content: center; flex-wrap: nowrap; gap: 2px; }
.empty-box { padding: 18px 0; text-align: center; color: var(--color-text-3); font-size: 13px; }
.empty-box p { margin: 0 0 8px; }
.data-table { width: 100%; }
.filter-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 12px 14px; }
.filter-row { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; }
.filter-item { display: flex; flex-direction: column; gap: 6px; min-width: 170px; }
.filter-item label { font-size: 12px; color: var(--color-text-2); }
.filter-actions { display: flex; gap: 8px; margin-left: auto; }
.stat-chips { gap: 6px; }
.probe-result { margin-top: 14px; }
.probe-detail { font-size: 12px; line-height: 1.6; word-break: break-all; }
.hidden-file { display: none; }
.preview-box { margin-top: 16px; border-top: 1px solid var(--color-border-1); padding-top: 14px; }
.section-title { font-size: 14px; font-weight: 600; color: var(--color-text-1); margin-bottom: 10px; }
.preview-row { display: flex; gap: 8px; }
.preview-body { margin-top: 10px; }
.code-block {
  margin: 8px 0 0; padding: 10px 12px; background: #f5f7fa; border: 1px solid var(--color-border-1);
  border-radius: 4px; font-size: 12px; line-height: 1.7; white-space: pre-wrap; word-break: break-all;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
.form-hint { font-size: 12px; color: var(--color-text-3); line-height: 1.5; margin-top: 4px; }
.cap-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; padding: 4px 0; }
.cap-card { background: #fff; border: 1px solid var(--color-border-1); border-radius: 4px; padding: 14px 16px; }
.cap-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.cap-title { font-size: 15px; font-weight: 600; color: var(--color-text-1); margin-bottom: 6px; }
.cap-desc { font-size: 13px; color: var(--color-text-2); line-height: 1.7; margin: 10px 0; min-height: 44px; }
.cap-label { font-size: 12px; color: var(--color-text-2); margin: 10px 0 5px; }
.cap-foot { display: flex; justify-content: flex-end; margin-top: 12px; }
:deep(.main-tabs .el-tabs__item) { font-size: 14px; height: 42px; }
:deep(.sub-tabs .el-tabs__item) { font-size: 13px; height: 36px; }
:deep(.el-drawer__body) { padding-top: 8px; }
</style>
