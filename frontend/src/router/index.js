import { createRouter, createWebHistory } from 'vue-router'
import SearchView from '../views/SearchView.vue'
import OverviewView from '../views/OverviewView.vue'
import AlarmEvents from '../views/AlarmEvents.vue'
import AlarmRules from '../views/AlarmRules.vue'
import DataSources from '../views/DataSources.vue'
import AgentManagement from '../views/AgentManagement.vue'
import AIManagement from '../views/AIManagement.vue'
import Placeholder from '../views/Placeholder.vue'

const routes = [
  { path: '/', redirect: '/search' },
  { path: '/search', name: 'search', component: SearchView, meta: { title: '日志检索' } },
  { path: '/overview', name: 'overview', component: OverviewView, meta: { title: '运行总览' } },
  { path: '/dashboards', name: 'dashboards', component: Placeholder, meta: { title: '仪表盘' } },
  { path: '/alarms/events', name: 'alarm-events', component: AlarmEvents, meta: { title: '告警事件' } },
  { path: '/alarms/rules', name: 'alarm-rules', component: AlarmRules, meta: { title: '告警规则' } },
  { path: '/data-sources', name: 'data-sources', component: DataSources, meta: { title: '数据源' } },
  { path: '/agents', name: 'agents', component: AgentManagement, meta: { title: 'Agent 管理' } },
  { path: '/ai/management', name: 'ai-management', component: AIManagement, meta: { title: '智能管理' } },
  { path: '/settings/users', name: 'settings-users', component: Placeholder, meta: { title: '用户与角色' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
