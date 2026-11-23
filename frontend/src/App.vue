<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
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
  Setting,
  User,
  OfficeBuilding,
  DataBoard,
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
    items: [
      { path: '/overview', title: '运行总览', icon: Odometer, resources: ['overview'] },
      { path: '/monitor', title: '自监测总览', icon: Monitor, resources: ['monitor'] },
    ],
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
      { path: '/settings/pipelines', title: '解析规则', icon: SetUp, resources: ['data'] },
      { path: '/agents', title: 'Agent 管理', icon: Monitor, resources: ['agents'] },
    ],
  },
  {
    title: '智能管理',
    items: [{ path: '/ai/management', title: 'AI 能力配置', icon: MagicStick, resources: ['ai'] }],
  },
  {
    title: '平台管理',
    items: [
      {
        path: '/settings/tenants',
        title: '租户管理',
        icon: OfficeBuilding,
        resources: ['tenant'],
      },
      {
        path: '/settings/users',
        title: '用户角色',
        icon: User,
        resources: ['settings', 'user', 'role'],
      },
      {
        path: '/settings/storage',
        title: '日志空间',
        icon: DataBoard,
        resources: ['storage'],
      },
      {
        path: '/settings/system',
        title: '系统设置',
        icon: Setting,
        resources: ['param', 'audit'],
      },
    ],
  },
]

// 无 view 权限的菜单不渲染（后端接口同样会 403，这里只是不做无效入口）
const visibleGroups = computed(() => groups
  .map((g) => ({ ...g, items: g.items.filter((m) => m.resources.some((r) => auth.has(r, 'view'))) }))
  .filter((g) => g.items.length))

const settingsLandingPath = computed(() => {
  if (auth.has('user', 'view') || auth.has('role', 'view') || auth.has('settings', 'view')) return '/settings/users'
  if (auth.has('tenant', 'view')) return '/settings/tenants'
  if (auth.has('param', 'view') || auth.has('audit', 'view')) return '/settings/system'
  return '/settings/storage'
})
const topTabs = computed(() => ([
  { key: 'overview', label: '概览', path: '/overview', resources: ['overview', 'monitor'] },
  { key: 'search', label: '日志分析', path: '/search', resources: ['search'] },
  { key: 'alarms', label: '告警中心', path: '/alarms/events', resources: ['alarms'] },
  { key: 'data-sources', label: '数据接入', path: '/data-sources', resources: ['data'] },
  { key: 'ai', label: '智能管理', path: '/ai/management', resources: ['ai'] },
  {
    key: 'settings', label: '平台管理', path: settingsLandingPath.value,
    resources: ['settings', 'user', 'role', 'tenant', 'param', 'audit', 'storage'],
  },
]))
const visibleTopTabs = computed(() => topTabs.value.filter((t) => t.resources.some((r) => auth.has(r, 'view'))))

function topKeyOf(path) {
  if (path === '/overview' || path.startsWith('/monitor')) return 'overview'
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

// 租户清单以服务端 tenants 表为唯一真源：登录后/身份变化时拉取，不再写死前端数组。
// 非管理员锁定在登录租户（后端同样强制），管理员额外可选「全部租户」巡检。
async function syncTenants() {
  if (!auth.user) return
  app.setTenant(auth.user.tenant || 'i6000')
  await app.loadTenants({ includeAll: isAdmin.value })
}
onMounted(syncTenants)
watch(() => auth.user?.id, syncTenants)
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
        <span class="brand-name"><b class="brand-product">RuiLog</b><span class="brand-cn">日志分析平台</span></span>
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
      <el-form ref="pwdFormRef" :model="pwdForm" :rules="pwdRules" label-width="104px" class="pwd-form">
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
  padding: 0 16px;
  flex-shrink: 0;
}
/* 品牌区：logo + 上下两行产品名（RuiLog 上、日志分析平台下，参考「瑞链 / RUI SEEKING」堆叠式） */
.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  /* 顶栏左侧内边距 16px + 本宽度 = 210px，与左侧菜单栏等宽对齐，
     导航起点正好落在菜单栏右边界之外，"概览"不会压在菜单栏上方 */
  width: calc(210px - 16px);
  flex-shrink: 0;
}
/* 原型 logo 无边框：去掉白底/圆角/内边距，直接置于深绿背景上 */
.brand-logo { height: 30px; width: 30px; object-fit: contain; flex-shrink: 0; }
.brand-name {
  color: #8FE6D8;
  display: flex;
  flex-direction: column;
  justify-content: center;
  line-height: 1.15;
  white-space: nowrap;
}
/* 主名 RuiLog 放大；副名「日志分析平台」小字 + 大字距，同色形成品牌层级 */
.brand-product { font-size: 18px; font-weight: 700; letter-spacing: 0.5px; }
.brand-cn { font-size: 11px; font-weight: 500; letter-spacing: 2.5px; opacity: 0.92; }

/* 标题栏页签：容器与顶栏同色（深墨绿融为一体），仅选中项浮出浅青实底；选中态为小圆角矩形（非药丸） */
.topbar-nav {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: 0;   /* 品牌区已与菜单栏同宽，导航紧接其后，无需额外间距 */
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

/* 修改密码弹窗：标签「确认新密码」含必填星号较长，禁止换行避免错乱 */
.pwd-form :deep(.el-form-item__label) { white-space: nowrap; }
</style>
