<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { listTenants } from '../api/auth'
import { useAuthStore } from '../stores/auth'
import { useAppStore } from '../stores/app'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const app = useAppStore()

const formRef = ref(null)
const submitting = ref(false)
const tenants = ref([])
const tenantLoading = ref(false)
const errorKind = ref('') // error | noPermission
const errorText = ref('')

const form = reactive({
  account: auth.rememberedAccount(),
  password: '',
  tenant: '',
  remember: !!auth.rememberedAccount(),
})

const rules = {
  account: [{ required: true, message: '请输入账号', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
  tenant: [{ required: true, message: '请选择所属租户', trigger: 'change' }],
}

async function loadTenants() {
  tenantLoading.value = true
  try {
    const res = await listTenants()
    tenants.value = res.items || []
    if (!form.tenant && tenants.value.length) form.tenant = tenants.value[0].code
  } catch (e) {
    errorKind.value = 'error'
    errorText.value = '登录服务暂不可用：' + (e.response?.data?.detail || e.message) +
      '（确认 auth-service 容器已启动）'
  } finally {
    tenantLoading.value = false
  }
}

function redirectTarget() {
  const target = route.query.redirect
  if (typeof target === 'string' && target.startsWith('/') && !target.startsWith('/login')) {
    return target
  }
  return '/overview'
}

async function submit() {
  errorKind.value = ''
  errorText.value = ''
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  submitting.value = true
  try {
    const data = await auth.login({ ...form })
    // 登录选定的租户即全局租户：检索 / 告警 / 数据接入链路都按它取数
    if (data.user?.tenant) app.setTenant(data.user.tenant)
    ElMessage.success(data.message || '登录成功')
    router.replace(redirectTarget())
  } catch (e) {
    const status = e.response?.status
    const detail = e.response?.data?.detail
    if (status === 403) {
      errorKind.value = 'noPermission'
      errorText.value = detail || '该账号未开通平台权限，请联系管理员'
    } else if (status === 401 || status === 400) {
      errorKind.value = 'error'
      errorText.value = detail || '账号或密码错误，请重新输入'
    } else {
      errorKind.value = 'error'
      errorText.value = '登录失败：' + (detail || e.message)
    }
    form.password = ''
  } finally {
    submitting.value = false
  }
}

onMounted(loadTenants)
</script>

<template>
  <div class="login">
    <div class="login__glow login__glow--a" aria-hidden="true"></div>
    <div class="login__glow login__glow--b" aria-hidden="true"></div>

    <section class="login__panel">
      <header class="login__brand">
        <img src="/logo.jpg" class="login__logo" alt="国家电网标识" />
        <div class="login__brand-text">
          <h1>统一日志分析平台</h1>
          <p>日志检索 · 告警处置 · 智能分析</p>
        </div>
      </header>

      <el-alert
        v-if="errorKind"
        class="login__alert"
        :type="errorKind === 'noPermission' ? 'warning' : 'error'"
        :closable="false"
        show-icon
        :title="errorKind === 'noPermission' ? '无平台访问权限' : '登录失败'"
      >
        <template #default>
          <div class="login__alert-body">
            <span>{{ errorText }}</span>
            <span v-if="errorKind === 'noPermission'" class="login__alert-hint">请联系平台管理员开通账号或调整角色</span>
          </div>
        </template>
      </el-alert>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        class="login__form"
        @submit.prevent="submit"
      >
        <el-form-item label="账号" prop="account">
          <el-input
            v-model="form.account"
            :prefix-icon="User"
            placeholder="请输入账号"
            autocomplete="username"
            clearable
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            :prefix-icon="Lock"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            show-password
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="所属租户" prop="tenant">
          <el-select
            v-model="form.tenant"
            placeholder="请选择所属租户"
            style="width: 100%"
            :loading="tenantLoading"
          >
            <el-option v-for="t in tenants" :key="t.code" :label="t.name" :value="t.code" />
          </el-select>
        </el-form-item>

        <div class="login__row">
          <el-checkbox v-model="form.remember" label="记住账号" />
          <span class="login__hint">忘记密码请联系平台管理员重置</span>
        </div>

        <el-button
          type="primary"
          class="login__submit"
          :loading="submitting"
          @click="submit"
        >
          {{ submitting ? '正在登录' : '登录' }}
        </el-button>
      </el-form>

      <footer class="login__foot">
        <span>Token V3.1.0</span>
        <span>国家电网 · 数字化运维</span>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.login {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  overflow: hidden;
  background: linear-gradient(135deg, #00453D 0%, #005E53 42%, #0A7C70 100%);
}
.login__glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(90px);
  opacity: 0.42;
  pointer-events: none;
}
.login__glow--a { width: 520px; height: 520px; background: #17B39F; top: -180px; right: -120px; }
.login__glow--b { width: 420px; height: 420px; background: #0A4F47; bottom: -160px; left: -100px; }

.login__panel {
  position: relative;
  width: 420px;
  max-width: calc(100vw - 32px);
  background: #fff;
  border-radius: 10px;
  padding: 32px 32px 20px;
  box-shadow: 0 24px 60px rgba(0, 38, 33, 0.32);
}
.login__brand { display: flex; align-items: center; gap: 14px; margin-bottom: 22px; }
.login__logo { width: 44px; height: 44px; object-fit: contain; flex-shrink: 0; }
.login__brand-text h1 { margin: 0; font-size: 20px; font-weight: 600; color: var(--color-text-1); letter-spacing: 0.5px; }
.login__brand-text p { margin: 4px 0 0; font-size: 12px; color: var(--color-text-3); letter-spacing: 0.5px; }

.login__alert { margin-bottom: 16px; }
.login__alert-body { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.login__alert-hint { font-size: 12px; color: var(--color-text-3); }

.login__form :deep(.el-form-item) { margin-bottom: 18px; }
.login__form :deep(.el-form-item__label) { font-size: 13px; color: var(--color-text-2); padding-bottom: 6px; }
.login__form :deep(.el-input__wrapper) { padding: 2px 12px; }

.login__row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.login__hint { font-size: 12px; color: var(--color-text-3); }

.login__submit { width: 100%; height: 42px; font-size: 15px; letter-spacing: 2px; }

.login__foot {
  display: flex;
  justify-content: space-between;
  margin-top: 20px;
  padding-top: 14px;
  border-top: 1px solid var(--color-border-1);
  font-size: 12px;
  color: var(--color-text-3);
}
</style>
