<script setup>
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowDown,
  DataLine,
  Document,
  MagicStick,
  Monitor,
  Odometer,
  SetUp,
  User,
  Warning,
} from '@element-plus/icons-vue'
import { useAppStore } from './stores/app'
import { useAuthStore } from './stores/auth'

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const auth = useAuthStore()

// 登录页不套平台布局（原型 AUTH-0100 为独立表单页）
const isLoginPage = computed(() => route.path === '/login')

// 左侧菜单：与原型一致分 6 组，资源标识与后端 permissions.resource 对齐
const groups = [
  {
    title: '概览',
    items: [{ path: '/overview', title: '运行总览', icon: Odometer, resources: ['overview'] }],
  },
  {
    title: '日志分析',
    items: [
      { path: '/search', title: '日志检索', icon: Document, resources: ['search'] },
      { path: '/dashboards', title: '仪表盘', icon: DataLine, resources: ['dashboards'] },
    ],
  },
  {
    title: '告警中心',
    items: [
      { path: '/alarms/events', title: '告警事件', icon: Warning, resources: ['alarms'] },
      { path: '/alarms/rules', title: '告警规则', icon: SetUp, resources: ['alarms'] },
    ],
  },
  {
    title: '数据接入',
    items: [
      { path: '/data-sources', title: '数据源', icon: Monitor, resources: ['data'] },
      { path: '/agents', title: 'Agent 管理', icon: Monitor, resources: ['agents'] },
    ],
  },
  {
    title: '智能管理',
    items: [{ path: '/ai/management', title: 'AI 能力配置', icon: MagicStick, resources: ['ai'] }],
  },
  {
    title: '平台管理',
    // 平台管理是四 Tab 容器：任一 Tab 有权限即可进入（如审计员只有 audit:view）
    items: [{
      path: '/settings/users',
      title: '用户与角色',
      icon: User,
      resources: ['settings', 'user', 'role', 'param', 'audit'],
    }],
  },
]

// 无 view 权限的菜单不渲染（后端接口同样会 403，这里只是不做无效入口）
const visibleGroups = computed(() => groups
  .map((g) => ({ ...g, items: g.items.filter((m) => m.resources.some((r) => auth.has(r, 'view'))) }))
  .filter((g) => g.items.length))

const topTabs = [
  { key: 'overview', label: '概览', path: '/overview', resources: ['overview'] },
  { key: 'search', label: '日志分析', path: '/search', resources: ['search'] },
  { key: 'alarms', label: '告警中心', path: '/alarms/events', resources: ['alarms'] },
  { key: 'data-sources', label: '数据接入', path: '/data-sources', resources: ['data'] },
  { key: 'ai', label: '智能管理', path: '/ai/management', resources: ['ai'] },
  {
    key: 'settings', label: '平台管理', path: '/settings/users',
    resources: ['settings', 'user', 'role', 'param', 'audit'],
  },
]
const visibleTopTabs = computed(() => topTabs.filter((t) => t.resources.some((r) => auth.has(r, 'view'))))

function topKeyOf(path) {
  if (path === '/overview') return 'overview'
  if (path.startsWith('/search') || path.startsWith('/dashboards')) return 'search'
  if (path.startsWith('/alarms')) return 'alarms'
  if (path.startsWith('/data-sources') || path.startsWith('/agents')) return 'data-sources'
  if (path.startsWith('/settings')) return 'settings'
  if (path.startsWith('/ai')) return 'ai'
  return ''
}
const activeTopKey = computed(() => topKeyOf(route.path))

function go(path) {
  if (route.path !== path) router.push(path)
}

// ---- 顶栏用户区 ----
const isAdmin = computed(() => auth.user?.roleKey === 'platform_admin')
const initial = computed(() => (auth.displayName || '?').slice(0, 1))

const pwdVisible = ref(false)
const pwdSaving = ref(false)
const pwdFormRef = ref(null)
const pwdForm = reactive({ oldPassword: '', newPassword: '', confirmPassword: '' })

const pwdRules = {
  oldPassword: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 8, max: 32, message: '密码长度 8-32 位', trigger: 'blur' },
    {
      validator: (_r, v, cb) => {
        if (!v || (/[A-Za-z]/.test(v) && /\d/.test(v))) return cb()
        cb(new Error('需同时包含字母与数字'))
      },
      trigger: 'blur',
    },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_r, v, cb) => (v === pwdForm.newPassword ? cb() : cb(new Error('两次输入不一致'))),
      trigger: 'blur',
    },
  ],
}

function openPassword() {
  Object.assign(pwdForm, { oldPassword: '', newPassword: '', confirmPassword: '' })
  pwdVisible.value = true
}

async function submitPassword() {
  const ok = await pwdFormRef.value?.validate().catch(() => false)
  if (!ok) return
  pwdSaving.value = true
  try {
    await auth.changePassword({ oldPassword: pwdForm.oldPassword, newPassword: pwdForm.newPassword })
    pwdVisible.value = false
    ElMessage.success('密码已修改，请使用新密码重新登录')
    await auth.logout()
    router.replace('/login')
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '修改密码失败')
  } finally {
    pwdSaving.value = false
  }
}

async function onUserCommand(cmd) {
  if (cmd === 'password') return openPassword()
  if (cmd === 'logout') {
    await auth.logout()
    router.replace('/login')
  }
}
</script>

<template>
  <router-view v-if="isLoginPage" />

  <div v-else class="layout">
    <header class="topbar">
      <div class="brand">
        <img src="/logo.jpg" class="brand-logo" alt="国家电网标识" />
        <span class="brand-name">统一日志分析平台</span>
      </div>

      <nav class="topbar-nav" aria-label="产品导航">
        <button
          v-for="t in visibleTopTabs"
          :key="t.key"
          type="button"
          class="topbar-nav__item"
          :class="{ 'is-active': activeTopKey === t.key }"
          @click="go(t.path)"
        >
          {{ t.label }}
        </button>
      </nav>

      <div class="topbar-right">
        <!-- 平台管理员可切租户巡检；其他角色锁定在自己的租户（行级隔离） -->
        <el-select
          v-if="isAdmin"
          :model-value="app.tenant"
          size="small"
          style="width: 130px"
          @change="app.setTenant"
        >
          <el-option v-for="t in app.tenants" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>
        <span v-else class="tenant-static">{{ auth.tenantName }}</span>

        <el-dropdown trigger="click" @command="onUserCommand">
          <span class="user">
            <span class="user-avatar">{{ initial }}</span>
            <span class="user-name">{{ auth.displayName || '未登录' }}</span>
            <el-tag v-if="auth.roleName" size="small" effect="plain" class="role-tag">{{ auth.roleName }}</el-tag>
            <el-icon class="user-caret"><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item disabled>
                {{ auth.user?.account }} · {{ auth.tenantName }}
              </el-dropdown-item>
              <el-dropdown-item command="password" divided>修改密码</el-dropdown-item>
              <el-dropdown-item command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <div class="body">
      <aside class="sidebar">
        <div v-for="g in visibleGroups" :key="g.title" class="sidebar__group">
          <div class="sidebar__group-title">{{ g.title }}</div>
          <div
            v-for="m in g.items"
            :key="m.path"
            class="sidebar__item"
            :class="{ active: route.path === m.path }"
            @click="go(m.path)"
          >
            <el-icon class="sidebar__icon"><component :is="m.icon" /></el-icon>
            <span>{{ m.title }}</span>
          </div>
        </div>
        <div class="sidebar__foot">Token V3.1.0</div>
      </aside>

      <main class="content">
        <router-view />
      </main>
    </div>

    <el-dialog v-model="pwdVisible" title="修改密码" width="420px" :close-on-click-modal="false">
      <el-form ref="pwdFormRef" :model="pwdForm" :rules="pwdRules" label-width="88px">
        <el-form-item label="当前密码" prop="oldPassword">
          <el-input v-model="pwdForm.oldPassword" type="password" show-password placeholder="请输入当前密码" />
        </el-form-item>
        <el-form-item label="新密码" prop="newPassword">
          <el-input v-model="pwdForm.newPassword" type="password" show-password placeholder="8-32 位，含字母与数字" />
        </el-form-item>
        <el-form-item label="确认新密码" prop="confirmPassword">
          <el-input v-model="pwdForm.confirmPassword" type="password" show-password placeholder="再次输入新密码" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdVisible = false">取消</el-button>
        <el-button type="primary" :loading="pwdSaving" @click="submitPassword">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.layout { display: flex; flex-direction: column; height: 100%; }
.topbar {
  height: 56px;
  background: var(--nav-bg);
  color: #fff;
  display: flex;
  align-items: center;
  padding: 0 20px;
  flex-shrink: 0;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 1px;
  flex-shrink: 0;
}
/* 原型 logo 无边框：去掉白底/圆角/内边距，直接置于深绿背景上 */
.brand-logo { height: 30px; width: 30px; object-fit: contain; }
.brand-name { color: #fff; }

/* 标题栏页签：容器与顶栏同色（深墨绿融为一体），仅选中项浮出浅青实底；选中态为小圆角矩形（非药丸） */
.topbar-nav {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: 28px;
}
.topbar-nav__item {
  border: none;
  cursor: pointer;
  padding: 7px 18px;
  border-radius: 6px;
  font-size: 14px;
  line-height: 1;
  color: #fff;
  background: transparent;
  transition: background var(--duration-fast, 100ms), color var(--duration-fast, 100ms);
}
.topbar-nav__item:hover { background: rgba(255, 255, 255, 0.12); color: #fff; }
.topbar-nav__item.is-active {
  background: var(--nav-item-bg-active);
  color: #fff;
  font-weight: 600;
}

.topbar-right { display: flex; align-items: center; gap: 14px; margin-left: auto; }
.tenant-static { font-size: 13px; opacity: 0.78; }

/* 用户区：头像 + 姓名 + 角色标签 + 下拉 */
.user { display: flex; align-items: center; gap: 8px; cursor: pointer; outline: none; }
.user-avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
}
.user-name { font-size: 14px; color: #fff; }
.role-tag {
  background: rgba(255, 255, 255, 0.14) !important;
  border-color: rgba(255, 255, 255, 0.32) !important;
  color: #fff !important;
  font-size: 11px;
  height: 19px;
  padding: 0 6px;
}
.user-caret { font-size: 12px; opacity: 0.7; }

.body { display: flex; flex: 1; min-height: 0; }

/* 左侧菜单：深墨绿背景 + 分组 + 整行选中高亮（原型样式） */
.sidebar {
  width: 210px;
  background: var(--nav-bg);
  padding: 12px 0 0;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
}
.sidebar__group { margin-bottom: 4px; }
.sidebar__group-title {
  padding: 16px 20px 8px;
  font-size: 12px;
  letter-spacing: 1px;
  color: rgba(255, 255, 255, 0.5);
}
.sidebar__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 20px;
  cursor: pointer;
  color: var(--nav-item-text);
  font-size: 14px;
  border-left: 3px solid transparent;
}
.sidebar__item:hover { background: var(--nav-item-bg-hover); }
.sidebar__item.active {
  background: var(--nav-item-bg-active);
  color: #fff;
  font-weight: 600;
}
.sidebar__icon { width: 18px; height: 18px; }
.sidebar__foot {
  margin-top: auto;
  padding: 12px 20px;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.4);
  border-top: 1px solid rgba(255, 255, 255, 0.12);
}

.content { flex: 1; min-width: 0; overflow: auto; padding: 16px 24px; background: #F0F2F5; }
</style>
