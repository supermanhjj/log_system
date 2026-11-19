<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getPipelineStatus, listParsers, saveParser, deleteParser,
  listRules, saveRule, deleteRule, previewParse, replayLogs,
  listCollectors, saveCollector, securityAudit, PARSER_TYPES,
} from '../api/pipelines'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const isAdmin = computed(() => auth.user?.roleKey === 'platform_admin')
const tab = ref('parsers')

const status = ref({ mode: 'shadow', ruleCount: 0, parserCount: 0 })
const parsers = ref([])
const rules = ref([])
const collectors = ref([])
const audits = ref([])
const loading = ref(false)

async function loadAll() {
  loading.value = true
  try {
    const [s, p, r, c, a] = await Promise.all([
      getPipelineStatus(), listParsers(), listRules(), listCollectors(), securityAudit(),
    ])
    status.value = s
    parsers.value = p
    rules.value = r
    collectors.value = c
    audits.value = a
  } catch (e) {
    ElMessage.error('加载解析规则失败：' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}
onMounted(loadAll)

// ---- 解析器编辑 ----
const parserDialog = ref(false)
const parserForm = reactive({ code: '', name: '', type: 'grok', definition: '', description: '', enabled: true })
function editParser(row) {
  Object.assign(parserForm, {
    code: row?.code || '', name: row?.name || '', type: row?.ptype || 'grok',
    definition: JSON.stringify(row?.definition || { type: 'grok', pattern: '' }, null, 2),
    description: row?.description || '', enabled: row?.enabled ?? true,
  })
  parserDialog.value = true
}
async function submitParser() {
  try {
    await saveParser({
      code: parserForm.code, name: parserForm.name, type: parserForm.type,
      definition: JSON.parse(parserForm.definition), description: parserForm.description,
      enabled: parserForm.enabled,
    })
    ElMessage.success('解析器已保存，规则缓存 30 秒内刷新')
    parserDialog.value = false
    loadAll()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '保存失败（检查 JSON 格式）')
  }
}
async function removeParser(code) {
  try {
    await ElMessageBox.confirm(`删除解析器「${code}」？引用它的规则会失效。`, '确认', { type: 'warning' })
    await deleteParser(code)
    loadAll()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error('删除失败') }
}

// ---- 规则编辑 ----
const ruleDialog = ref(false)
const ruleForm = reactive({ code: '', name: '', priority: 100, parserCode: '', match: '{}', mapping: '{}', tenantCode: '', enabled: true })
function editRule(row) {
  Object.assign(ruleForm, {
    code: row?.code || '', name: row?.name || '', priority: row?.priority ?? 100,
    parserCode: row?.parser_code || parsers.value[0]?.code || '',
    match: JSON.stringify(row?.match || {}, null, 2),
    mapping: JSON.stringify(row?.mapping || {}, null, 2),
    tenantCode: row?.tenant_code || '', enabled: row?.enabled ?? true,
  })
  ruleDialog.value = true
}
async function submitRule() {
  try {
    await saveRule({
      code: ruleForm.code, name: ruleForm.name, priority: Number(ruleForm.priority),
      parserCode: ruleForm.parserCode,
      match: JSON.parse(ruleForm.match), mapping: JSON.parse(ruleForm.mapping),
      tenantCode: ruleForm.tenantCode, enabled: ruleForm.enabled,
    })
    ElMessage.success('规则已保存')
    ruleDialog.value = false
    loadAll()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '保存失败（检查 JSON 格式）')
  }
}
async function removeRule(code) {
  try {
    await ElMessageBox.confirm(`删除规则「${code}」？`, '确认', { type: 'warning' })
    await deleteRule(code)
    loadAll()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error('删除失败') }
}

// ---- 试解析 ----
const tryDef = ref(JSON.stringify({
  type: 'grok',
  pattern: '%{TIMESTAMP_ISO8601:ts}\\s+\\[%{JAVATHREAD:thread}\\]\\s+%{LOGLEVEL:level}\\s+%{JAVACLASS:logger}\\s+-\\s+%{GREEDYDATA:message}',
}, null, 2))
const trySample = ref('2026-11-19 18:02:11.334 [http-nio-8080-exec-3] INFO  c.s.i.order.OrderController - 开始处理订单')
const tryResult = ref(null)
async function doPreview() {
  try {
    tryResult.value = await previewParse({
      parser: JSON.parse(tryDef.value), sample: trySample.value, mapping: {},
    })
    if (!tryResult.value.matched) ElMessage.warning('未匹配：检查模式或样例日志')
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '试解析失败')
  }
}

// ---- 重放 ----
const replayForm = reactive({ tenant: 'default', days: 1, limit: 1000, dryRun: true })
const replayResult = ref(null)
async function doReplay() {
  try {
    replayResult.value = await replayLogs({ ...replayForm })
    ElMessage.success(`扫描 ${replayResult.value.scanned} 条，命中 ${replayResult.value.matched} 条`)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '重放失败')
  }
}

// ---- 采集器凭证 / 租户白名单 ----
const collectorDialog = ref(false)
const collectorForm = reactive({ code: '', name: '', tenantAllow: '', key: '' })
function editCollector(row) {
  Object.assign(collectorForm, {
    code: row?.code || '', name: row?.name || '',
    tenantAllow: (row?.tenant_allow || []).join(', '), key: '',
  })
  collectorDialog.value = true
}
async function submitCollector() {
  try {
    await saveCollector({
      code: collectorForm.code.trim(), name: collectorForm.name,
      tenantAllow: collectorForm.tenantAllow.split(/[,，\s]+/).filter(Boolean),
      key: collectorForm.key,
    })
    ElMessage.success('采集器凭证已保存；留空 key 表示沿用原凭证')
    collectorDialog.value = false
    loadAll()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '保存失败')
  }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="header-row">
          <span>服务端解析管线</span>
          <el-tag :type="status.mode === 'active' ? 'success' : status.mode === 'off' ? 'info' : 'warning'">
            当前模式：{{ status.mode || '—' }}
          </el-tag>
          <span class="note">{{ status.ruleCount ?? 0 }} 条规则 · {{ status.parserCount ?? 0 }} 个解析器</span>
        </div>
      </template>
      <el-alert type="info" :closable="false" show-icon
        title="shadow 模式：新解析只写入 attrs.__shadow，不影响现有字段。比对无误后再把环境变量 PIPELINE_MODE 改为 active。" />

      <el-tabs v-model="tab" style="margin-top:12px">
        <!-- 解析器 -->
        <el-tab-pane label="解析器" name="parsers">
          <div class="toolbar">
            <el-button v-if="isAdmin" type="primary" @click="editParser(null)">新建解析器</el-button>
            <el-button @click="loadAll">刷新</el-button>
          </div>
          <el-table :data="parsers" v-loading="loading" size="small" border>
            <el-table-column prop="code" label="编码" width="160" />
            <el-table-column prop="name" label="名称" min-width="160" />
            <el-table-column prop="ptype" label="类型" width="100" />
            <el-table-column prop="description" label="说明" min-width="220" show-overflow-tooltip />
            <el-table-column prop="enabled" label="启用" width="80">
              <template #default="{ row }">{{ row.enabled ? '是' : '否' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="150" v-if="isAdmin">
              <template #default="{ row }">
                <el-button link type="primary" @click="editParser(row)">编辑</el-button>
                <el-button link type="danger" @click="removeParser(row.code)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 规则 -->
        <el-tab-pane label="管线规则" name="rules">
          <div class="toolbar">
            <el-button v-if="isAdmin" type="primary" @click="editRule(null)">新建规则</el-button>
            <el-button @click="loadAll">刷新</el-button>
            <span class="note">priority 小的先匹配；第一条命中即执行</span>
          </div>
          <el-table :data="rules" v-loading="loading" size="small" border>
            <el-table-column prop="priority" label="优先级" width="80" />
            <el-table-column prop="code" label="编码" width="180" />
            <el-table-column prop="name" label="名称" min-width="160" />
            <el-table-column prop="parser_code" label="解析器" width="160" />
            <el-table-column prop="tenant_code" label="归属租户" width="120" />
            <el-table-column label="启用" width="70">
              <template #default="{ row }">{{ row.enabled ? '是' : '否' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="150" v-if="isAdmin">
              <template #default="{ row }">
                <el-button link type="primary" @click="editRule(row)">编辑</el-button>
                <el-button link type="danger" @click="removeRule(row.code)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 试解析 -->
        <el-tab-pane label="试解析" name="preview">
          <el-alert type="info" :closable="false" show-icon
            title="贴一段现场真实日志，实时查看抽取结果。保存前就能验证，不用改 Fluent Bit 配置。" />
          <div class="preview-grid">
            <div>
              <div class="label">解析器定义（JSON，含 type）</div>
              <el-input v-model="tryDef" type="textarea" :rows="12" />
              <div class="label" style="margin-top:12px">样例日志</div>
              <el-input v-model="trySample" type="textarea" :rows="4" />
              <el-button type="primary" style="margin-top:10px" @click="doPreview">试解析</el-button>
            </div>
            <div>
              <div class="label">解析结果</div>
              <pre class="result">{{ tryResult ? JSON.stringify(tryResult, null, 2) : '（尚未解析）' }}</pre>
            </div>
          </div>
        </el-tab-pane>

        <!-- 重放 -->
        <el-tab-pane label="历史重放" name="replay">
          <el-alert type="warning" :closable="false" show-icon
            title="重放按当前规则重新解析历史日志。写回保持原 event_id 不变，不产生重复数据。" />
          <div class="replay-form">
            <el-input v-model="replayForm.tenant" placeholder="租户编码" style="width:180px" />
            <span class="note">近</span>
            <el-input-number v-model="replayForm.days" :min="1" :max="30" />
            <span class="note">天，最多</span>
            <el-input-number v-model="replayForm.limit" :min="1" :max="10000" :step="1000" />
            <span class="note">条</span>
            <el-checkbox v-model="replayForm.dryRun">仅预览（不写回）</el-checkbox>
            <el-button type="primary" @click="doReplay" :disabled="!isAdmin">执行</el-button>
          </div>
          <pre class="result" style="margin-top:12px">{{ replayResult ? JSON.stringify(replayResult, null, 2) : '（尚未执行）' }}</pre>
        </el-tab-pane>

        <!-- 采集器凭证与审计 -->
        <el-tab-pane label="采集器与审计" name="collectors">
          <el-alert type="warning" :closable="false" show-icon
            title="租户白名单为空表示不限制（兼容旧凭证）。生产应为每个采集器显式配置允许的租户，越界日志会被拒收并留审计。" />
          <div class="toolbar" style="margin-top:12px">
            <el-button v-if="isAdmin" type="primary" @click="editCollector(null)">新建采集器</el-button>
            <el-button @click="loadAll">刷新</el-button>
          </div>
          <el-table :data="collectors" v-loading="loading" size="small" border>
            <el-table-column prop="code" label="采集器编码" width="180" />
            <el-table-column prop="name" label="名称" min-width="160" />
            <el-table-column label="允许的租户" min-width="240">
              <template #default="{ row }">
                <el-tag v-for="t in (row.tenant_allow || [])" :key="t" size="small" style="margin-right:4px">{{ t }}</el-tag>
                <span v-if="!row.tenant_allow?.length" class="note">不限制（旧凭证兼容）</span>
              </template>
            </el-table-column>
            <el-table-column prop="last_seen" label="最后心跳" width="180" />
            <el-table-column label="操作" width="90" v-if="isAdmin">
              <template #default="{ row }">
                <el-button link type="primary" @click="editCollector(row)">编辑</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="label" style="margin-top:22px">归属冲突 / 越界审计（最近 100 条）</div>
          <el-table :data="audits" size="small" border>
            <el-table-column prop="ts" label="时间" width="180" />
            <el-table-column prop="collector_code" label="采集器" width="150" />
            <el-table-column prop="action" label="动作" width="130" />
            <el-table-column prop="injected" label="注入租户" width="130" />
            <el-table-column prop="inferred" label="推导租户" width="130" />
            <el-table-column prop="final_tenant" label="最终归属" width="130" />
            <el-table-column prop="host" label="主机" min-width="130" />
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <el-dialog v-model="parserDialog" :title="parserForm.code ? '编辑解析器' : '新建解析器'" width="640px">
      <el-form label-width="90px">
        <el-form-item label="编码"><el-input v-model="parserForm.code" :disabled="!!parserForm.code" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="parserForm.name" /></el-form-item>
        <el-form-item label="类型">
          <el-select v-model="parserForm.type">
            <el-option v-for="t in PARSER_TYPES" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="定义 JSON"><el-input v-model="parserForm.definition" type="textarea" :rows="10" /></el-form-item>
        <el-form-item label="说明"><el-input v-model="parserForm.description" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="parserForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="parserDialog = false">取消</el-button>
        <el-button type="primary" @click="submitParser">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="ruleDialog" :title="ruleForm.code ? '编辑规则' : '新建规则'" width="640px">
      <el-form label-width="100px">
        <el-form-item label="编码"><el-input v-model="ruleForm.code" :disabled="!!ruleForm.code" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="ruleForm.name" /></el-form-item>
        <el-form-item label="优先级"><el-input-number v-model="ruleForm.priority" :min="1" :max="9999" /></el-form-item>
        <el-form-item label="解析器">
          <el-select v-model="ruleForm.parserCode">
            <el-option v-for="p in parsers" :key="p.code" :label="p.name" :value="p.code" />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配条件"><el-input v-model="ruleForm.match" type="textarea" :rows="5" /></el-form-item>
        <el-form-item label="字段映射"><el-input v-model="ruleForm.mapping" type="textarea" :rows="6" /></el-form-item>
        <el-form-item label="归属租户"><el-input v-model="ruleForm.tenantCode" placeholder="可选，仅作归属建议" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="ruleForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleDialog = false">取消</el-button>
        <el-button type="primary" @click="submitRule">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="collectorDialog" title="采集器凭证" width="520px">
      <el-form label-width="100px">
        <el-form-item label="编码"><el-input v-model="collectorForm.code" :disabled="!!collectorForm.code" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="collectorForm.name" /></el-form-item>
        <el-form-item label="允许租户">
          <el-input v-model="collectorForm.tenantAllow" placeholder="逗号分隔，如 acme, default" />
        </el-form-item>
        <el-form-item label="凭证 Key">
          <el-input v-model="collectorForm.key" type="password" show-password
            placeholder="新建必填；编辑留空表示沿用原凭证" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="collectorDialog = false">取消</el-button>
        <el-button type="primary" @click="submitCollector">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { padding: 4px; }
.header-row { display: flex; align-items: center; gap: 12px; }
.note { color: #909399; font-size: 12px; }
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.preview-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 14px; }
.label { font-size: 13px; margin-bottom: 6px; color: #606266; }
.result { background: var(--el-fill-color-light); padding: 12px; border-radius: 4px;
  min-height: 200px; max-height: 480px; overflow: auto; font-size: 12px; white-space: pre-wrap; }
.replay-form { display: flex; align-items: center; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
</style>
