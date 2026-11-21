<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus, MagicStick, Clock } from '@element-plus/icons-vue'
import {
  getPipelineStatus, listRules, saveParser, saveRule, deleteRule,
  previewParse, replayLogs,
} from '../api/pipelines'
import { useAuthStore } from '../stores/auth'
import { useAppStore } from '../stores/app'
import {
  PARSE_TEMPLATES, CONTRACT_FIELDS, detectTemplate, pickAlias, ruleCode,
} from '../utils/parseTemplates'

const auth = useAuthStore()
const app = useAppStore()
const isAdmin = computed(() => auth.user?.roleKey === 'platform_admin')

const status = ref({ mode: 'active', ruleCount: 0, parserCount: 0 })
const rules = ref([])
const loading = ref(false)

const MODE_TEXT = {
  active: { type: 'success', label: '已生效', desc: '新日志会按下列规则抽取字段；未匹配的日志保留原文并标记 unmatched。' },
  shadow: { type: 'warning', label: '影子模式', desc: '解析结果只写入 attrs.__shadow，不改变现有字段，适合先比对再切换。' },
  off: { type: 'info', label: '已停用', desc: '当前不执行任何解析，字段抽取全部关闭。' },
}
const modeInfo = computed(() => MODE_TEXT[status.value.mode] || MODE_TEXT.active)

async function loadAll() {
  loading.value = true
  try {
    const [s, r] = await Promise.all([getPipelineStatus(), listRules()])
    status.value = s
    rules.value = r
  } catch (e) {
    ElMessage.error('加载解析规则失败：' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}
onMounted(loadAll)

// ---------------------------------------------------------------------------
// 接入向导：选模板 → 贴样例 → 确认映射 → 保存
// ---------------------------------------------------------------------------
const wizardVisible = ref(false)
const wizardStep = ref(0)
const wizardAuto = ref(false)
const template = ref(null)
const sample = ref('')
const sourcePath = ref('')
const previewing = ref(false)
const previewResult = ref(null)
const mapPick = reactive({})
const ruleName = ref('')
const priority = ref(10)
const saving = ref(false)

function openWizard(tpl = null) {
  wizardVisible.value = true
  wizardStep.value = 0
  wizardAuto.value = false
  template.value = tpl
  sample.value = tpl?.sample || ''
  sourcePath.value = ''
  previewResult.value = null
  ruleName.value = ''
  priority.value = 10
  Object.keys(mapPick).forEach((k) => delete mapPick[k])
  if (tpl) {
    titleForRule()
    wizardStep.value = 1
  }
}

function openAutoDetect() {
  openWizard(null)
  wizardAuto.value = true
  wizardStep.value = 1
  sample.value = ''
}

function chooseTemplate(tpl) {
  wizardVisible.value = true
  template.value = tpl
  wizardAuto.value = false
  sample.value = tpl.sample || ''
  previewResult.value = null
  sourcePath.value = ''
  priority.value = 10
  titleForRule()
  wizardStep.value = 1
}

function titleForRule() {
  const t = template.value
  if (!t) return
  ruleName.value = sourcePath.value
    ? `${t.name} · ${sourcePath.value}`
    : `${t.name} · 全部日志`
}

function goBack() {
  if (wizardStep.value === 2) { wizardStep.value = 1; return }
  if (wizardStep.value === 1) { wizardStep.value = 0; return }
}

async function detectAndPreview() {
  if (!sample.value.trim()) return ElMessage.warning('先贴一行真实日志')
  const hit = detectTemplate(sample.value)
  if (!hit) {
    ElMessage.warning('没认出来这是什么格式，请回到上一步从模板列表里选一个')
    wizardStep.value = 0
    return
  }
  template.value = hit
  wizardAuto.value = false
  titleForRule()
  await doPreview()
}

async function doPreview() {
  if (!template.value) return
  if (!sample.value.trim()) return ElMessage.warning('先贴一行真实日志')
  previewing.value = true
  try {
    const fields = {}
    const payload = { parser: { ...template.value.parser, type: template.value.parser.type }, sample: sample.value, mapping: fields }
    previewResult.value = await previewParse(payload)
    if (previewResult.value.matched) {
      for (const f of CONTRACT_FIELDS) {
        const picked = pickAlias(previewResult.value.fields, f.key) || ''
        mapPick[f.key] = picked
      }
      // 规则的默认名称/优先级跟着来源路径走：不填路径时降低优先级，避免抢走别的日志
      priority.value = sourcePath.value.trim() ? 10 : 200
      if (!ruleName.value) titleForRule()
    }
    wizardStep.value = 2
  } catch (e) {
    ElMessage.error('试解析失败：' + (e.response?.data?.detail || e.message))
  } finally { previewing.value = false }
}

const fieldKeys = computed(() => Object.keys(previewResult.value?.fields || {}))
const usedFieldKeys = computed(() => Object.values(mapPick).filter(Boolean))
const attrSourceKeys = computed(() => Object.values(template.value?.attrs || {}).map((v) => String(v).split(':')[0]))
const extraFieldKeys = computed(() =>
  fieldKeys.value.filter((k) => !usedFieldKeys.value.includes(k) && !attrSourceKeys.value.includes(k)))

function buildMapping() {
  const mapping = { ...(template.value?.attrs || {}) }
  for (const f of CONTRACT_FIELDS) {
    const picked = mapPick[f.key]
    if (!picked) continue
    mapping[f.key] = f.key === 'status' ? `${picked}:int` : picked
  }
  for (const key of extraFieldKeys.value) {
    const target = ['ts', 'time', '@timestamp'].includes(key) ? 'attrs.body_ts' : `attrs.${key}`
    if (mapping[target] === undefined) mapping[target] = key
  }
  return mapping
}

async function submitWizard() {
  if (!template.value) return
  const matched = previewResult.value?.matched
  if (!matched) return ElMessage.warning('样例没匹配上，先回上一步调整或换个模板')
  saving.value = true
  try {
    const codes = ruleCode(template.value.key)
    const parserDef = { ...template.value.parser, type: template.value.parser.type }
    await saveParser({
      code: codes.parser,
      name: `${template.value.name}（${new Date().toLocaleDateString('zh-CN')}）`,
      type: parserDef.type,
      definition: parserDef,
      description: `模板接入：${template.value.summary}`,
      enabled: true,
    })
    const path = sourcePath.value.trim()
    await saveRule({
      code: codes.rule,
      name: ruleName.value.trim() || template.value.name,
      priority: Number(priority.value) || 100,
      match: path ? { path_glob: [path] } : {},
      parserCode: codes.parser,
      mapping: buildMapping(),
      tenantCode: '',
      enabled: true,
    })
    ElMessage.success('已保存并生效（30 秒内全集群生效）')
    wizardVisible.value = false
    loadAll()
  } catch (e) {
    ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
  } finally { saving.value = false }
}

// ---------------------------------------------------------------------------
// 规则管理与历史重放
// ---------------------------------------------------------------------------
const BUILTIN_RULES = new Set(['java-logback-business', 'syslog-system'])

function matchText(row) {
  const g = row.match?.path_glob
  if (!g) return '全部日志'
  return Array.isArray(g) ? g.join('、') : String(g)
}

async function removeRule(row) {
  await ElMessageBox.confirm(`删除规则「${row.name}」？之后这类日志会回到未匹配状态。`, '删除规则',
    { type: 'warning' })
  try {
    await deleteRule(row.code)
    ElMessage.success('已删除')
    loadAll()
  } catch (e) {
    ElMessage.error('删除失败：' + (e.response?.data?.detail || e.message))
  }
}

const replayVisible = ref(false)
const replayForm = reactive({ tenant: app.tenant || 'i6000', days: 1, limit: 1000, dryRun: true })
const replayResult = ref(null)
const replaying = ref(false)
function openReplay() {
  replayForm.tenant = app.tenant || 'i6000'
  replayResult.value = null
  replayVisible.value = true
}
async function doReplay() {
  replaying.value = true
  try {
    replayResult.value = await replayLogs({ ...replayForm, tenant: replayForm.tenant.trim() })
    ElMessage.success(`扫描 ${replayResult.value.scanned} 条，命中 ${replayResult.value.matched} 条`)
  } catch (e) {
    ElMessage.error('重放失败：' + (e.response?.data?.detail || e.message))
  } finally { replaying.value = false }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="header-row">
          <span class="title">日志解析</span>
          <el-tag :type="modeInfo.type" effect="light">{{ modeInfo.label }}</el-tag>
          <span class="note">{{ status.ruleCount ?? 0 }} 条规则 · {{ status.parserCount ?? 0 }} 个解析器</span>
          <div class="grow" />
          <el-button :icon="Clock" @click="openReplay" :disabled="!isAdmin">历史重放</el-button>
          <el-button :icon="Refresh" @click="loadAll">刷新</el-button>
          <el-button v-if="isAdmin" type="primary" :icon="Plus" @click="openWizard()">接入一种日志</el-button>
        </div>
      </template>
      <el-alert :type="modeInfo.type === 'info' ? 'info' : modeInfo.type" :closable="false" show-icon
        :title="modeInfo.desc" />
      <div class="tip">不需要懂正则或 JSON：选一个和你日志长得像的模板，贴一行真实日志，确认抽出的字段，保存即可。</div>
    </el-card>

    <el-card shadow="never">
      <template #header><span class="card-title">日志模板库</span></template>
      <div class="tpl-grid">
        <button v-for="t in PARSE_TEMPLATES" :key="t.key" class="tpl-card" @click="chooseTemplate(t)">
          <div class="tpl-name">{{ t.name }}</div>
          <div class="tpl-sum">{{ t.summary }}</div>
          <code class="tpl-sample">{{ t.sample.slice(0, 96) }}{{ t.sample.length > 96 ? '…' : '' }}</code>
        </button>
        <button class="tpl-card auto" @click="openAutoDetect">
          <div class="tpl-name">不确定是什么格式？</div>
          <div class="tpl-sum">贴一行日志，我来识别该用哪个模板</div>
          <div class="auto-hint"><el-icon><MagicStick /></el-icon> 自动识别</div>
        </button>
      </div>
    </el-card>

    <el-card shadow="never" v-loading="loading">
      <template #header>
        <div class="header-row">
          <span class="card-title">已生效的规则</span>
          <span class="note">优先级数字小的先匹配，命中第一条就停</span>
        </div>
      </template>
      <el-table :data="rules" size="small" border>
        <el-table-column prop="priority" label="优先级" width="76" />
        <el-table-column prop="name" label="规则" min-width="200" show-overflow-tooltip />
        <el-table-column label="适用日志" min-width="200">
          <template #default="{ row }">{{ matchText(row) }}</template>
        </el-table-column>
        <el-table-column label="来源" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="BUILTIN_RULES.has(row.code) ? 'info' : 'success'" effect="plain">
              {{ BUILTIN_RULES.has(row.code) ? '内置' : '模板接入' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">{{ row.enabled ? '启用' : '停用' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" v-if="isAdmin">
          <template #default="{ row }">
            <el-button v-if="!BUILTIN_RULES.has(row.code)" link type="danger" @click="removeRule(row)">删除</el-button>
            <span v-else class="note">系统内置</span>
          </template>
        </el-table-column>
        <template #empty><div class="empty">还没有任何规则，点右上角「接入一种日志」开始</div></template>
      </el-table>
    </el-card>

    <!-- 接入向导 -->
    <el-dialog v-model="wizardVisible" :title="wizardAuto ? '自动识别并接入' : '接入一种日志'" width="760px"
      :close-on-click-modal="false">
      <el-steps :active="wizardStep" simple style="margin-bottom:16px">
        <el-step title="选类型" />
        <el-step title="贴样例" />
        <el-step title="确认并保存" />
      </el-steps>

      <!-- 步骤 1：选模板 -->
      <div v-if="wizardStep === 0" class="wizard-tpls">
        <button v-for="t in PARSE_TEMPLATES" :key="t.key" class="tpl-card" @click="chooseTemplate(t)">
          <div class="tpl-name">{{ t.name }}</div>
          <div class="tpl-sum">{{ t.summary }}</div>
        </button>
        <button class="tpl-card auto" @click="openAutoDetect">
          <div class="tpl-name">不确定是什么格式？</div>
          <div class="tpl-sum">贴一行日志，自动判断</div>
        </button>
      </div>

      <!-- 步骤 2：贴样例 -->
      <div v-else-if="wizardStep === 1">
        <el-alert v-if="wizardAuto" type="info" :closable="false" show-icon style="margin-bottom:12px"
          title="先贴一行真实日志，点「识别并预览」，我会挑一个合适的模板。" />
        <el-form label-width="100px" label-position="left">
          <el-form-item label="日志类型">
            <span v-if="template">{{ template.name }}</span>
            <span v-else class="note">尚未识别</span>
          </el-form-item>
          <el-form-item label="样例日志" required>
            <el-input v-model="sample" type="textarea" :rows="6"
              placeholder="从目标机器上复制一整行真实日志，不要只贴片段" />
            <div class="form-tip">一行一条；带堆栈的日志贴第一行即可（堆栈需要采集端开启多行合并）</div>
          </el-form-item>
          <el-form-item label="来源路径">
            <el-input v-model="sourcePath" :placeholder="template?.pathHint || '/var/log/app/*.log'" />
            <div class="form-tip">只对这个路径的日志生效，支持 * 通配；留空表示所有日志（会降低优先级兜底）</div>
          </el-form-item>
        </el-form>
      </div>

      <!-- 步骤 3：预览 + 字段确认 -->
      <div v-else>
        <el-alert v-if="previewResult && !previewResult.matched" type="warning" :closable="false" show-icon
          title="这条样例和模板对不上：确认日志类型选对了、贴的是完整一行；也可以换一个模板重试。" />
        <template v-else>
          <el-alert type="success" :closable="false" show-icon
            :title="`已抽出 ${fieldKeys.length} 个字段，确认下面几个关键字段的对应关系即可保存`" />
          <div class="preview-grid">
            <div>
              <div class="label">抽出的字段（原样）</div>
              <el-table :data="fieldKeys.map((k) => ({ k, v: previewResult.fields[k] }))" size="small" border
                max-height="260">
                <el-table-column prop="k" label="字段" width="140" show-overflow-tooltip />
                <el-table-column prop="v" label="值" show-overflow-tooltip />
              </el-table>
            </div>
            <div>
              <div class="label">对到平台字段</div>
              <div v-for="f in CONTRACT_FIELDS" :key="f.key" class="map-row">
                <span class="map-label" :title="f.hint">{{ f.label }}</span>
                <el-select v-model="mapPick[f.key]" size="small" clearable placeholder="不映射" style="width:100%">
                  <el-option v-for="k in fieldKeys" :key="k" :label="k" :value="k" />
                </el-select>
              </div>
              <div class="form-tip">
                其余 {{ extraFieldKeys.length }} 个字段会自动存进扩展属性，不影响检索；
                状态码映射后，5xx 会自动判成 ERROR。
              </div>
            </div>
          </div>

          <el-form label-width="100px" label-position="left" style="margin-top:14px">
            <el-form-item label="规则名称">
              <el-input v-model="ruleName" placeholder="给这条规则起个名字" />
            </el-form-item>
            <el-form-item label="优先级">
              <el-input-number v-model="priority" :min="1" :max="9999" controls-position="right" style="width:130px" />
              <span class="form-tip" style="margin-left:10px">越小越先匹配；有来源路径时建议 10，兜底规则建议 200</span>
            </el-form-item>
          </el-form>
        </template>
      </div>

      <template #footer>
        <el-button @click="wizardVisible = false">取消</el-button>
        <el-button v-if="wizardStep > 0" @click="goBack">上一步</el-button>
        <el-button v-if="wizardStep === 1 && wizardAuto" type="primary" :loading="previewing" @click="detectAndPreview">
          识别并预览
        </el-button>
        <el-button v-else-if="wizardStep === 1" type="primary" :loading="previewing" @click="doPreview">
          试解析
        </el-button>
        <el-button v-else-if="wizardStep === 2 && previewResult?.matched" type="primary" :loading="saving"
          @click="submitWizard">保存并生效</el-button>
      </template>
    </el-dialog>

    <!-- 历史重放 -->
    <el-dialog v-model="replayVisible" title="历史重放（按当前规则重算存量日志）" width="560px">
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom:12px"
        title="先用「仅预览」看命中情况，确认没问题再取消勾选写回。写回保持原 event_id，不会产生重复数据。" />
      <el-form label-width="90px">
        <el-form-item label="租户"><el-input v-model="replayForm.tenant" placeholder="租户编码" /></el-form-item>
        <el-form-item label="时间范围">
          <span>近</span>
          <el-input-number v-model="replayForm.days" :min="1" :max="30" style="margin:0 8px" />
          <span>天</span>
        </el-form-item>
        <el-form-item label="条数上限">
          <el-input-number v-model="replayForm.limit" :min="1" :max="10000" :step="1000" />
        </el-form-item>
        <el-form-item label="写回">
          <el-checkbox v-model="replayForm.dryRun">仅预览（不写回）</el-checkbox>
        </el-form-item>
      </el-form>
      <pre v-if="replayResult" class="result">{{ JSON.stringify(replayResult, null, 2) }}</pre>
      <template #footer>
        <el-button @click="replayVisible = false">关闭</el-button>
        <el-button type="primary" :loading="replaying" @click="doReplay">执行</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; padding: 4px; }
.header-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.title { font-size: 16px; font-weight: 600; color: var(--color-text-1); }
.card-title { font-size: 14px; font-weight: 600; }
.note { color: var(--color-text-3); font-size: 12px; }
.grow { flex: 1; }
.tip { margin-top: 10px; font-size: 12px; color: var(--color-text-3); }
.tpl-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.wizard-tpls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.tpl-card { text-align: left; border: 1px solid var(--color-border-1); background: #fff; border-radius: 6px;
  padding: 12px; cursor: pointer; transition: border-color .15s, box-shadow .15s; min-height: 112px; }
.tpl-card:hover { border-color: #03857E; box-shadow: 0 2px 10px rgba(3,133,126,.08); }
.tpl-card.auto { border-style: dashed; background: #FAFFFE; }
.tpl-name { font-size: 13px; font-weight: 600; color: var(--color-text-1); margin-bottom: 4px; }
.tpl-sum { font-size: 12px; color: var(--color-text-3); line-height: 1.5; margin-bottom: 8px; }
.tpl-sample { display: block; font-size: 11px; color: #7A8793; background: #F7FAFA; border-radius: 3px;
  padding: 4px 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.auto-hint { font-size: 12px; color: #03857E; display: flex; align-items: center; gap: 4px; }
.preview-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 12px; }
.label { font-size: 12px; color: var(--color-text-3); margin-bottom: 6px; }
.map-row { display: grid; grid-template-columns: 56px 1fr; gap: 8px; align-items: center; margin-bottom: 8px; }
.map-label { font-size: 13px; color: var(--color-text-2); }
.form-tip { font-size: 12px; color: var(--color-text-3); line-height: 1.6; margin-top: 2px; }
.empty { padding: 20px 0; color: var(--color-text-3); font-size: 13px; }
.result { background: var(--el-fill-color-light); padding: 12px; border-radius: 4px; max-height: 320px;
  overflow: auto; font-size: 12px; white-space: pre-wrap; }
@media (max-width: 900px) {
  .tpl-grid, .wizard-tpls, .preview-grid { grid-template-columns: 1fr; }
}
</style>
