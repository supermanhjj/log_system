# 仪表盘模块 · 建设思路与开发进展

> 快照日期：2026-11-17（GMT+8）｜基于当前工作区真实代码整理
> 关联原型：`prototype/dashboards.html`（P-DSH-0301 列表）、`prototype/dashboard-detail.html`（P-DSH-0302 详情）
> 关联模块：[运行总览](module-overview.md)（共用 OpenSearch 聚合原语与租户路由）

---

## 1. 本期落地范围

从占位页（`router` 原指向 `Placeholder`）落地为可用的仪表盘：**列表 + 详情 + 拖拽式编辑器**。此前 `README` 与 `module-overview.md` 均标注「`/dashboards` 不在本期范围」，本次补齐。

| 能力 | 前端落点 | 后端落点 | 状态 |
|---|---|---|---|
| 看板列表：筛选（名称/负责人/更新时间）、批量选择、新建、删除 | `DashboardsView.vue` | `GET /dashboards`、`POST /dashboards`、`DELETE /dashboards/{id}` | ✅ |
| 看板详情：时间范围、指标卡、图表网板、明细表、刷新/分享/返回 | `DashboardDetailView.vue`（查看模式） | `GET /dashboards/{id}`、`POST /query` | ✅ |
| **拖拽编辑**：组件库拖入 → 画布重排 → 配置面板设维度/指标/筛选/宽度 | `DashboardDetailView.vue`（编辑模式） | `PUT /dashboards/{id}`（widgets JSONB 持久化） | ✅ |
| 图表数据：按每个组件的维度配置实时聚合 | ECharts 按 `type` 渲染 | `POST /query` → `aggregate_widget()` | ✅ 真实取数 |

设计取舍：**配置存库、数据不固化**。一条 `dashboards` 记录存看板元信息 + `widgets` JSONB 布局；每次打开看板由 `POST /query` 按各组件的维度配置对 `logs-*` 实时聚合，与运行总览共用 `index_for / terms_on / agg_search / _time_must` 原语。不建指标快照表、不设定时任务。

---

## 2. 可配置维度（回答「能实现哪些维度的展示」）

前端编辑面板的字典与后端 `DIMENSIONS` / `CHART_TYPES` 共用同一份口径（`GET /dashboards/meta` 下发），避免两边漂移。

### 2.1 图表类型 × 可用维度

| 图表类型 | 可用维度 | 说明 |
|---|---|---|
| 指标卡 kpi | 总量 none | 单值：日志条数 / 去重主机 / 去重来源 / 错误占比 |
| 折线图 line、面积图 area | 时间 time | `date_histogram(@timestamp)` 趋势，间隔随范围（1h→5m，1d→1h，7d/30d→1d） |
| 柱状图 bar、横向排行 hbar | level / source / host / status / traceId / tenant | `terms` 聚合，Top N 可调（1–50） |
| 占比饼图 pie、环形 donut | level / source / host / status / tenant | `terms` 聚合 + 占比 |
| 日志明细表 table | records（日志明细） | 按筛选条件取最近 N 条原始日志（时间倒序） |

### 2.2 统计指标 metric

| metric | 口径 | 返回 unit |
|---|---|---|
| count | 命中文档数（默认） | 条 |
| distinct_host | `cardinality(host)` | 个 |
| distinct_source | `cardinality(source)` | 个 |
| error_rate | `level∈{ERROR,FATAL}` 占比 | % |

### 2.3 筛选 filter

`level[]`（级别，多选）、`source`、`host`、`status`、`traceId`、`tenant_id`（精确匹配，留空/全部不过滤）。与维度正交：任意「维度 × 指标 × 筛选」组合均可配置，例如「按 host 分组的 ERROR 日志 Top10」「按 time 的 5xx 趋势」。

### 2.4 数据来源边界

日志字段来自 Fluent Bit `app_log` parser：`level / source / host / traceId / tenant_id / status / message / @timestamp`。维度可用性取决于日志 mapping；`records` 明细表返回原始 `_source`。日志源为 `log-generator` 模拟数据（非生产）。

---

## 3. 接口

| 端点 | 方法 | 说明 |
|---|---|---|
| `/dashboards` | GET | 列表，`chartCount` = widgets 长度 |
| `/dashboards/{id}` | GET | 单个看板详情（含 widgets 配置） |
| `/dashboards/meta` | GET | 图表类型 / 可配维度 / 时间范围字典 |
| `/dashboards` | POST | 新建（name ≤24 字，非空） |
| `/dashboards/{id}` | PUT | 保存（字段白名单更新，含 widgets JSONB） |
| `/dashboards/{id}` | DELETE | 删除 |
| `/query` | POST | 批量聚合：body `{range_in, tenant, widgets:[...], force?}` → `{range, results:[...], errors:[...]}` |

`POST /query` 单组件失败返回 `degraded:true` 的空结果并记入 `errors`，不影响整板其他图表。nginx 路由：`/api/dashboards/ → dashboard-service:8000/`（注意复数 dashboards）。

---

## 4. 持久化与状态

- PG 表 `dashboards` 由 `init_dashboard_table()` 在启动时幂等建表；空表时 seed 2 个看板（核心业务日志看板 11 组件、网关访问质量 4 组件）。
- 配置保存后刷新页面 / 换浏览器不丢；`status` 字段支持「运行中 / 维护中」（列表展示，维护态暂不强制禁止编辑）。
- 图表数据不落库，每次由 `/query` 现算；租户范围由前端透传 `app.tenant`，后端 `index_for` 路由到 `logs-<tenant>`（与其余业务服务一致的隔离边界，行级鉴权尚未强绑）。

---

## 5. 边界与后续待办

| 优先级 | 待办 | 说明 |
|---|---|---|
| P1 | 配置鉴权与归属 | 当前看板 CRUD 不校验 Token（与其余 6 个业务服务一致），负责人为展示字段，未做强隔离 |
| P1 | 分享链接落库 | 现「分享」仅复制当前 URL，未生成带有效期的分享令牌 |
| P2 | 维护态禁止编辑 | 原规格「维护中仪表盘不可编辑」未强制，仅列表展示状态 |
| P2 | 空/无权限/骨架屏 | 详情页空态已做，skeleton 与无权限态未做（与运行总览同类缺口） |
| P2 | `/query` 结果缓存 | 运行总览 `/overview` 有 30s 缓存，`/query` 目前每次实时聚合；演示数据量小可接受 |
| P2 | 导出配置 | 列表「导出」为前端提示占位，未生成配置文件下载 |
