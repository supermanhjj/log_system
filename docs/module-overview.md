# 运行总览模块 · 建设思路与开发进展

> 快照日期：2026-11-14（GMT+8）｜基于当前工作区真实代码整理，行号随代码变动可能偏移
> 关联模块：[日志分析](module-log-search.md) · [告警中心](module-alarm-center.md) · [数据接入](module-data-ingestion.md)
> 设计基线：[技术栈选型与方案设计](tech-stack-selection.md) · [服务清单](service-inventory.md) · [容器化部署](container-deployment.md)

---

## 0. 本期全局进展快照

| 层次 | 当前状态 |
|---|---|
| 前端 | 已从 `prototype/` 静态高保真 HTML 升级为 **Vue3 + Element Plus + ECharts + Pinia** 单页应用，构建产物 `frontend/dist` 由 nginx 托管；本次提交的是源码，`dist/` 按 `.gitignore` 不入库，clone 后需 `npm run build` |
| 网关 | nginx 统一入口 `http://localhost:8080`，`/api/*` 按前缀反代到 8 个 FastAPI 微服务（`/api/dashboards/ → dashboard-service:8000/`） |
| 后端 | 8 个微服务容器内统一 `:8000`，宿主机 `8001–8008` 暴露；PG 存主数据、OpenSearch 存日志与聚合、Redis 存心跳与缓存 |
| 采集 | `log-generator`（模拟业务日志）→ 共享卷 → Fluent Bit → 按租户写入 `logs-default / logs-acme / logs-gp` |
| 运行环境 | **18 个容器 running，8001–8008 `/health` 全部 200**（`docs/service-inventory.md:5,110`）；新增 `ai-service` 属「智能管理」配置中心，不在本四模块范围内 |
| 文档 | 本次新增 4 份模块进展文档，均落到 `docs/` |

---

## 1. 模块定位与建设思路

### 1.1 设计目标（来自原型规格）

原型 `P-OVW-0101` 定义「时间范围 + 4 张核心指标卡 + 3 类图表 + 最近告警表 + 导出报表」，技术选型把它映射为「Vue3 + ECharts + FastAPI 聚合接口 + OpenSearch 聚合」（`docs/tech-stack-selection.md:56,76-81`）。原型里的数值是写死样例（`prototype/overview.html:33,15-283`）：`12,486 万条 / 36 / 286ms / 18/20`、SVG 折线与环形图、`data-toast` 假导出。

### 1.2 落地时的四条思路

| 思路 | 具体做法 | 代码落点 |
|---|---|---|
| **单一数据源，指标全部现算** | 不建 PG 指标表、不写定时任务、不落汇总数据；`GET /overview` 一次请求内并发拼装 6 类指标，全部来自 OpenSearch 真实聚合（`count` / `date_histogram` / `terms` / `percentiles`） | `services/dashboard-service/app/main.py:351-488` |
| **接口收敛，前端只取一次** | 前端只调 `getOverview(range, tenant, force)` 一个接口，拿到 `stats / trend / levelDist / latency / health / recentAlarms` 再分发给 ECharts；避免多接口拼装带来的状态不一致 | `frontend/src/api/overview.js:11-15`、`frontend/src/views/OverviewView.vue:58` |
| **轻缓存 + 可强刷** | 服务端内存缓存 30s（`CACHE_TTL_MS`），key 为 `tenant|range`，`force` 参数绕过；前端 60s 自动刷新、切租户自动重取、手动刷新带 `force` | `main.py:284-296,323-327`、`OverviewView.vue:247-282,54-73` |
| **耗时指标复用引擎自身洞察数据** | 检索耗时不自己埋点，改查 OpenSearch Query Insights 生成的 `top_queries-*` 索引里 `measurements.latency.number`，做 P95 与分桶；索引缺失时返回空柱而非伪造数字 | `main.py:34,205-278` |

数据流：`OverviewView.vue → GET /api/dashboards/overview?range_in&tenant&force → nginx → dashboard-service → OpenSearch(logs-*、top_queries-*)`。

### 1.3 与最初设计的偏差（如实记录）

- 检索耗时**只做 P95，未做 P50**（`main.py:242`），原型规格要求 P50/P95 双图例（`prototype/specs/P-OVW-0101.json:51-57`）。
- 「告警级别分布」实际由日志 `level` 的 `terms` 聚合推导（`main.py:379-406`），并非来自告警中心事件库；活动告警取「严重 + 警告」两类原始 level 累加。
- 默认时间范围由原型「近 24 小时」改为「今日」，并新增 5 项范围选项（`OverviewView.vue:11,27,30-39`）。
- 健康度口径按「近 1h 的 `source` 计数 ÷ 近 7d 同时段均值」计算（`main.py:450-488`），与数据接入模块字段覆盖率口径不同。

---

## 2. 已落地能力清单

| 能力 | 前端落点 | 后端落点 | 状态 |
|---|---|---|---|
| 页头：面包屑 / 时间范围 / 刷新 / 导出 | `OverviewView.vue:285-325` | — | ✅ 已实现（导出为提示占位，见第 5 节） |
| 4 张指标卡：今日写入日志、活动告警、检索耗时 P95、在线数据源 | `:326-360`、数据来自 `overview.stats` | `main.py:351-355,379-406,438-448,450-488` | ✅ 真实聚合 |
| 日志写入趋势（面积折线，环比 + 时间桶） | `:361-375`、`renderChart()` 系列 | `main.py:357-377` `date_histogram` | ✅ 真实 |
| 告警级别分布（环形图，中心活动告警数） | `:376-389` | `main.py:379-406` `terms(level, size=50)` | ✅ 真实（源自日志 level） |
| 检索耗时分布（柱图 + 800ms 目标线 + 超标标橙） | `:390-410` | `main.py:205-278,438-448` `percentiles(95)` | ✅ 真实（依赖 Query Insights） |
| 数据源健康度（进度条） | `:411-435` | `main.py:450-488` | ✅ 真实 |
| 最近告警表（6 列，取前 5 条） | `:436-468` | `main.py:408-436` | ⚠️ 日志推导，`status`/`handler` 固定值 |
| 60s 自动刷新 / 手动强刷 / 租户联动 / 错误态与重试 | `:54-81,247-282` | `force` 参数 `:319,323-327` | ✅ 已实现 |
| 东八区展示 | 时间戳本地化 | `main.py:64-81,236,368`（`time_zone: +08:00`） | ✅ 已实现 |
| ECharts 生命周期（`resize` / `dispose`） | `:79-81,247-282` | — | ✅ 已实现 |

---

## 3. 接口与数据结构

| 项 | 内容 |
|---|---|
| 前端封装 | `frontend/src/api/overview.js:7-14`，`axios.create({ baseURL: '/api/dashboards', timeout: 30000 })` |
| 后端路由 | `GET /health`、`GET /`、`GET /overview`（`services/dashboard-service/app/main.py:302-316`）；**仅此 3 个** |
| 请求参数 | `range_in`（今日/近1小时/近7天等 5 项）、`tenant`、`force` |
| 索引映射 | `INDEX_BY_TENANT = {default: logs-default, acme: logs-acme, gp: logs-gp}`，`all/*` → `logs-*`（`main.py:55-61`） |
| 依赖索引 | `logs-*`（写入量、趋势、级别、告警、健康度）、`top_queries-*`（检索耗时，`main.py:34`） |
| PG 表 | **本模块不查 PG、无建表、无 SQL**；主数据表归告警 / 接入 / 系统服务 |
| nginx 路由 | `location /api/dashboards/ { proxy_pass http://dashboard-service:8000/; }`（`nginx/nginx.conf:23`）；注意是复数 `dashboards`，无单数路由 |
| 异常处理 | 聚合失败抛 `HTTPException(502)`（`main.py:141-153`）；耗时洞察异常单独吞掉并返回 `source:"unavailable"` 空柱（`:253-256`） |

---

## 4. 原型对照

| 原型元素 | 现状 |
|---|---|
| 4 项时间选项、默认「近 24 小时」 | 改为 5 项、默认「今日」 |
| 4 张写死指标卡、SVG 图表 | 全部替换为 ECharts + OpenSearch 实时数据 |
| P50 / P95 双图例 | 仅 P95 |
| 空态 / 无权限态 / skeleton（`prototype/specs/P-OVW-0101.json:133-140`） | 仅实现「加载错误 + 重试」，`skeleton`/空态/无权限未做 |
| 导出 CSV/Excel（后端生成） | 前端仅弹提示，无后端端点 |
| 写死的最近告警 5 行 | 改为日志聚合取前 5 行，但状态与处理人仍为固定值 |
| 「同一告警 30 分钟内合并计一次」脚注（`OverviewView.vue:390`） | 保留文案，后端无合并逻辑 |

另有 `/dashboards` 路由仍指向通用占位页（`frontend/src/router/index.js:15`），仪表盘列表/详情不在本模块本期范围内。

---

## 5. 真实实现 vs 占位 / 模拟

**真实实现**：6 类指标与 4 张图全部来自 OpenSearch 聚合；30s 缓存与 `force` 强刷；租户索引路由；东八区；错误态与重试；早期 mock 数据已删除。

**占位 / 需知悉的简化**：

1. 导出报表：`onExport()` 仅 `ElMessage.success`（`OverviewView.vue:83-85`），无后端 CSV/Excel 端点。
2. 最近告警 `status` 恒为「告警中」、`handler` 恒为「—」（`main.py:433-434`）。
3. 告警面板从日志 level 推导，未接 `alarm-service` 的事件/状态（`main.py:18-19`）。
4. 耗时卡片脚注「数据来源：检索网关 · 今日」是展示文案，实际取 `top_queries-*`（按 Super 2026-11-14 要求回退保留）。
5. 「同一告警 30 分钟内合并计一次」无对应去重逻辑。
6. `reactive` 初值（如 `unit:'万条'`、`target:800`）是空壳，加载后被后端覆盖。

---

## 6. 当前边界与后续待办

| 优先级 | 待办 | 说明 |
|---|---|---|
| P0 | 耗时指标前置条件显式化 | 需集群启用 Query Insights（`top_n_queries`）；索引不存在时返回空柱，不伪造数据（`main.py:14-15,255-256`）。实测 P95 约 840.7ms > 800ms 目标，卡片显示「超标」是真实结果 |
| P1 | 告警面板接入 `alarm-service` | 用真实告警事件与状态替换日志 level 推导，补齐处理人、状态机 |
| P1 | 真实导出报表 | 后端生成 CSV/Excel，去掉前端假提示 |
| P2 | 补 P50、`skeleton` / 空态 / 无权限态 | 与规格 `P-OVW-0101` 对齐 |
| P2 | 仪表盘列表 / 详情 | `/dashboards` 现为 `Placeholder` |
| P2 | 脚注与去重规则 | 让文案与实际口径一致，或实现 30 分钟合并 |

---

## 7. 验证记录与运行入口

- 页面：`http://localhost:8080/overview`；后端 Swagger `http://localhost:8003/docs`，健康检查 `/health`。
- 直连接口：`curl 'http://localhost:8003/overview?range_in=今日&tenant=default'`，带 `force=true` 绕过缓存。
- 本期验证：`GET /api/dashboards/overview` 返回 6 类指标；切租户、`force` 强刷、错误态均实测；18 容器 running、8001–8008 health 200（见 `docs/service-inventory.md:5,110`）。
- 前端构建：`npm --prefix frontend run build`；`frontend/dist/` 为构建产物，不入库。

---

## 8. 相关文件索引

| 文件 | 作用 |
|---|---|
| `frontend/src/views/OverviewView.vue` | 页面与 ECharts 渲染 |
| `frontend/src/api/overview.js` | 唯一接口封装与范围选项 |
| `services/dashboard-service/app/main.py` | 指标聚合、缓存、索引映射 |
| `prototype/overview.html`、`prototype/specs/P-OVW-0101.json` | 高保真原型与规格 |
| `docs/tech-stack-selection.md`（3.2 第 2 节） | 原始设计映射 |
| `nginx/nginx.conf:23`、`docker-compose.yml` | 网关与服务编排 |
