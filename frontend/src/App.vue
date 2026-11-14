<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAppStore } from './stores/app'
import {
  Document,
  DataLine,
  Odometer,
  Warning,
  SetUp,
  Monitor,
  User,
  MagicStick,
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const app = useAppStore()

// 左侧菜单：与原型一致，分 5 组
const groups = [
  {
    title: '概览',
    items: [{ path: '/overview', title: '运行总览', icon: Odometer }],
  },
  {
    title: '日志分析',
    items: [
      { path: '/search', title: '日志检索', icon: Document },
      { path: '/dashboards', title: '仪表盘', icon: DataLine },
    ],
  },
  {
    title: '告警中心',
    items: [
      { path: '/alarms/events', title: '告警事件', icon: Warning },
      { path: '/alarms/rules', title: '告警规则', icon: SetUp },
    ],
  },
  {
    title: '数据接入',
    items: [
      { path: '/data-sources', title: '数据源', icon: Monitor },
      { path: '/agents', title: 'Agent 管理', icon: Monitor },
    ],
  },
  {
    title: '智能管理',
    items: [{ path: '/ai/management', title: 'AI 能力配置', icon: MagicStick }],
  },
  {
    title: '平台管理',
    items: [{ path: '/settings/users', title: '用户与角色', icon: User }],
  },
]

// 标题栏页签：与原型一致，点击进入各组代表页面
const topTabs = [
  { key: 'overview', label: '概览', path: '/overview' },
  { key: 'search', label: '日志分析', path: '/search' },
  { key: 'alarms', label: '告警中心', path: '/alarms/events' },
  { key: 'data-sources', label: '数据接入', path: '/data-sources' },
  { key: 'ai', label: '智能管理', path: '/ai/management' },
  { key: 'settings', label: '平台管理', path: '/settings/users' },
]

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
</script>

<template>
  <div class="layout">
    <header class="topbar">
      <div class="brand">
        <img src="/logo.jpg" class="brand-logo" alt="国家电网标识" />
        <span class="brand-name">统一日志分析平台</span>
      </div>

      <nav class="topbar-nav" aria-label="产品导航">
        <button
          v-for="t in topTabs"
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
        <el-select
          :model-value="app.tenant"
          @change="app.setTenant"
          size="small"
          style="width: 160px"
        >
          <el-option
            v-for="t in app.tenants"
            :key="t.value"
            :label="t.label"
            :value="t.value"
          />
        </el-select>
        <span class="user">黄金金</span>
      </div>
    </header>

    <div class="body">
      <aside class="sidebar">
        <div v-for="g in groups" :key="g.title" class="sidebar__group">
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

.topbar-right { display: flex; align-items: center; gap: 16px; margin-left: auto; }
.user { font-size: 14px; opacity: 0.9; }

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
