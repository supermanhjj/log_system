import { createRouter, createWebHistory } from 'vue-router'
import { ElMessage } from 'element-plus'
import SearchView from '../views/SearchView.vue'
import OverviewView from '../views/OverviewView.vue'
import SelfMonitor from '../views/SelfMonitor.vue'
import AlarmEvents from '../views/AlarmEvents.vue'
import AlarmRules from '../views/AlarmRules.vue'
import DataSources from '../views/DataSources.vue'
import AgentManagement from '../views/AgentManagement.vue'
import AIManagement from '../views/AIManagement.vue'
import UserRoles from '../views/UserRoles.vue'
import SystemSettings from '../views/SystemSettings.vue'
import LogStorage from '../views/LogStorage.vue'
import PipelineManagement from '../views/PipelineManagement.vue'
import TenantManagement from '../views/TenantManagement.vue'
import LoginView from '../views/LoginView.vue'
import DashboardsView from '../views/DashboardsView.vue'
import DashboardDetailView from '../views/DashboardDetailView.vue'
import { useAuthStore } from '../stores/auth'
import { useAppStore } from '../stores/app'

// meta.perm / permAny 与后端 permissions 表的 (resource, action) 一一对应；
// 后端每个管理接口自身也会校验，前端这层只决定「看不看得到」，不是安全边界。
// 平台管理页是四个 Tab 的容器，入口按「任一 Tab 有权限」放行（如审计员只有 audit:view）。
const SETTINGS_ENTRY = [
  ['settings', 'view'], ['user', 'view'], ['role', 'view'],
  ['tenant', 'view'], ['param', 'view'], ['audit', 'view'],
]
const routes = [
  { path: '/login', name: 'login', component: LoginView, meta: { title: '登录', public: true } },
  { path: '/', redirect: '/search' },
  { path: '/search', name: 'search', component: SearchView, meta: { title: '日志检索', perm: ['search', 'view'] } },
  { path: '/overview', name: 'overview', component: OverviewView, meta: { title: '运行总览', perm: ['overview', 'view'] } },
  { path: '/monitor', name: 'monitor', component: SelfMonitor, meta: { title: '自监测总览', perm: ['monitor', 'view'] } },
  { path: '/dashboards', name: 'dashboards', component: DashboardsView, meta: { title: '仪表盘', perm: ['dashboards', 'view'] } },
  { path: '/dashboards/:id', name: 'dashboard-detail', component: DashboardDetailView, meta: { title: '看板详情', perm: ['dashboards', 'view'] } },
  { path: '/alarms/events', name: 'alarm-events', component: AlarmEvents, meta: { title: '告警事件', perm: ['alarms', 'view'] } },
  { path: '/alarms/rules', name: 'alarm-rules', component: AlarmRules, meta: { title: '告警规则', perm: ['alarms', 'view'] } },
  { path: '/data-sources', name: 'data-sources', component: DataSources, meta: { title: '数据源', perm: ['data', 'view'] } },
  { path: '/agents', name: 'agents', component: AgentManagement, meta: { title: 'Agent 管理', perm: ['agents', 'view'] } },
  { path: '/ai/management', name: 'ai-management', component: AIManagement, meta: { title: '智能管理', perm: ['ai', 'view'] } },
  { path: '/settings/users', name: 'settings-users', component: UserRoles, meta: { title: '用户与角色', permAny: SETTINGS_ENTRY } },
  { path: '/settings/tenants', name: 'settings-tenants', component: TenantManagement, meta: { title: '租户管理', perm: ['tenant', 'view'] } },
  { path: '/settings/system', name: 'settings-system', component: SystemSettings, meta: { title: '系统设置', permAny: [['param','view'],['audit','view']] } },
  { path: '/settings/storage', name: 'settings-storage', component: LogStorage, meta: { title: '日志空间管理', permAny: [['storage', 'view']] } },
  { path: '/settings/pipelines', name: 'settings-pipelines', component: PipelineManagement, meta: { title: '解析规则', perm: ['data', 'view'] } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    // 已登录再访问登录页，直接进主界面，避免重复登录
    if (auth.token && auth.user) return { path: '/overview' }
    return true
  }

  if (!auth.token) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  if (!auth.user) {
    // 刷新页面后只剩 Token：向 /me 取回用户与权限
    try {
      await auth.fetchMe()
      if (auth.user?.tenant) useAppStore().setTenant(auth.user.tenant)
    } catch {
      auth.clear()
      return { path: '/login', query: { redirect: to.fullPath } }
    }
  }

  const needAny = to.meta.permAny
  const denied = needAny
    ? !needAny.some(([r, a]) => auth.has(r, a))
    : (to.meta.perm && !auth.has(to.meta.perm[0], to.meta.perm[1]))
  if (denied) {
    ElMessage.warning(`当前角色「${auth.roleName || '未知'}」无权访问该页面`)
    return { path: '/overview' }
  }
  return true
})

// 系统名常驻：不随菜单变化拼前缀，浏览器标签页始终是平台名称
router.afterEach(() => {
  document.title = 'RuiLog 日志分析平台'
})

export default router
