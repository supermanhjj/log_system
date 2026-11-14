# 日志分析模块 · 建设思路与开发进展

> 快照日期：2026-11-14（GMT+8）｜基于当前工作区真实代码整理，行号随代码变动可能偏移
> 关联模块：[运行总览](module-overview.md) · [告警中心](module-alarm-center.md) · [数据接入](module-data-ingestion.md)
> 设计基线：[技术栈选型与方案设计](tech-stack-selection.md) · [服务清单](service-inventory.md)｜实现分析另见仓库根目录 `code-search.md`

---

## 0. 本期全局进展快照

| 层次 | 当前状态 |
|---|---|
| 前端 | 已从 `prototype/` 静态高保真 HTML 升级为 **Vue3 + Element Plus + ECharts + Pinia** 单页应用，构建产物 `frontend/dist` 由 nginx 托管；本次提交的是源码，`dist/` 按 `.gitignore` 不入库，clone 后需 `npm run build` |
| 网关 | nginx 统一入口 `http://localhost:8080`，`/api/search/ → search-service:8000/`（`nginx/nginx.conf:22`） |
| 后端 | 8 个微服务容器内统一 `:8000`，宿主机 `8001–8008` 暴露；PG 存主数据、OpenSearch 存日志与聚合 |
| 采集 | `log-generator` → 共享卷 → Fluent Bit → 按租户写入 `logs-default / logs-acme / logs-gp` |
| 运行环境 | **18 个容器 running，8001–8008 `/health` 全部 200**（`docs/service-inventory.md:5,110`）；`ai-service` 属「智能管理」配置中心，运行时 NL→PPL 尚未接入本模块 |

---

## 1. 模块定位与建设思路

### 1.1 设计目标（来自原型规格）

原型 `LOG-0201` 定义 PPL 检索语句输入、时间范围、日志分布直方图、日志列表与分页、详情抽屉、保存视图与导出（`docs/tech-stack-selection.md:57,83-87`）。核心决策是：**检索语言直接采用 OpenSearch 原生 PPL，不构建 SPL→PPL 转换层**（`:14`），检索网关负责鉴权、合法性校验、分页与限流。

### 1.2 落地时的五条思路

| 思路 | 具体做法 | 代码落点 |
|---|---|---|
| **单检索框 + 语法自动分流** | 用户只面对一个 `splText` 输入框；含 `\|` 判定为 PPL 管道分析走 `/pql`，否则走 DSL 列表查询 `/query`。避免「全文检索 / 管道分析」两个入口割裂 | `frontend/src/views/SearchView.vue:70,135,200-243` |
| **检索网关隔离引擎细节** | 前端不直连 OpenSearch，4 个接口统一经 `api/search.js` → nginx → `search-service`；后端负责拼 DSL、补 `source=`、透传 `schema/datarows` | `frontend/src/api/search.js:4-24`、`services/search-service/app/main.py:206-353` |
| **按租户映射索引，前端只传 tenant** | `INDEX_BY_TENANT{default,acme,gp}`，`all/*` → `logs-*`；切租户自动重查 | `main.py:48-58`、`SearchView.vue:190-198,533-539` |
| **时间范围与直方图联动** | 快捷/自定义时间 → `buildTimeRange()`；点柱缩放时间窗；所有列表/直方图按同一时间窗取数 | `SearchView.vue:13-24,151-161,258-265,290-294` |
| **详情页用上下文而非只给单条** | `openDetail()` 展示字段全貌，`loadContext()` 按 `_id` 前后取上下文，`loadMore()` 受 `MORE_MAX=200` 上限约束 | `SearchView.vue:303-330,339-340,387-435` |

数据流：`SearchView.vue → /api/search/{query,histogram,pql,context} → search-service → OpenSearch(client.search / _plugins/_ppl) → logs-<tenant>`。

### 1.3 告警联动的现成范式

告警中心「查看关联日志」把生成好的 PPL 通过路由 query 传入：`/search?ppl=...&from=...&to=...&note=...`（`frontend/src/views/AlarmEvents.vue:149-158`）；检索页 `applyIncomingQuery()` 回填 `splText`、时间窗与说明，并在 `onMounted` 自动执行（`SearchView.vue:544-568`），页内再次跳转由 `watch(route.query.ppl)` 接住（`:575-580`）。这套「生成 → 回填 → 执行」范式是后续 AI 智能检索面板可直接复用的落点。

---

## 2. 已落地能力清单

| 能力 | 前端落点 | 后端落点 | 状态 |
|---|---|---|---|
| PPL 单框输入、回车执行、语法 chips | `SearchView.vue:70,137-142,245-248,629-637` | — | ✅ |
| DSL 列表检索（关键词、时间、租户、分页） | `:200-217` | `POST /query`（`main.py:206-241`） | ✅ 真实 `client.search` |
| PPL 管道分析 | `runPql():219-238` | `POST /pql`（`main.py:244-272`）→ `httpx.post(/_plugins/_ppl)` | ✅ 真实；`schema[].name` 映射 `datarows` |
| 时间范围（快捷 + 自定义 + 点柱缩放 + 重置） | `:13-19,151-161,258-265,290-294,597-615` | DSL 侧 `range @timestamp` | ✅ |
| 命中直方图与按级别聚合、耗时卡片 | `:491-523,649-660` | `POST /histogram`（`main.py:275-320`），`date_histogram over_time` + `terms by_level` | ✅ |
| 结果表、列显隐、分页 | `:86-104,711-753` | `/query` 返回 `total / took_ms / hits` | ✅ |
| 日志详情抽屉、上下文、加载更多、`traceId` 复制 | `:303-330,387-461,759-816` | `POST /context`（`main.py:323-353`） | ✅ |
| 勾选行导出 CSV、复制检索语句 | `exportSelected():466-481`、`copyStatement():482-486` | — | ✅（前端侧） |
| 告警关联日志跳转与回填执行 | `applyIncomingQuery():544-558` | 关联 PPL 由 `alarm-service` 生成 | ✅ |
| 保存视图 / 全量导出 | 前端已移除入口（`code-search.md:117`）、`onExport()` 仅提示（`:487-489`） | `GET/POST /saved-views`（`main.py:357-364`） | ❌ stub |
| AI 智能检索（NL→PPL） | **无运行时调用** | `ai-service` 明确不提供生成端点 | ❌ 待接入 |

---

## 3. 接口与数据结构

| 前端函数（`api/search.js`） | 后端路由 | 说明 |
|---|---|---|
| `searchQuery`（`:6-9`） | `POST /query` | DSL 列表，返回 `total / took_ms / hits` |
| `searchHistogram`（`:11-14`） | `POST /histogram` | 时间桶 + 级别分布 |
| `searchPql`（`:16-19`） | `POST /pql` | PPL 管道，返回 `schema / datarows / total` |
| `searchContext`（`:21-24`） | `POST /context` | 按 `_id` 取上下文 |

- 请求体：`PqlReq{ pql: str, tenant: str = "default" }`（`main.py:149-151`）——**没有 timeRange 字段**。
- 安全校验：黑名单正则拒绝 `drop|delete|update|insert|create|alter|grant`（`main.py:253`）+ 非 `source=` 开头时自动补 `source=logs-<tenant>`（`:255-256`）；**没有索引白名单、函数白名单**：docstring 声称「仅查 logs-*」但代码未对 `source=` 值做校验。
- 超时/异常：PPL 调用 `httpx timeout=30`（`:262`）、前端 `axios 20000`（`api/search.js:4`）；各端点异常统一转 `HTTPException(502)`。
- 索引：`logs-default / logs-acme / logs-gp`、`logs-*`（`main.py:48-58`）。**`logs-template` 索引模板无代码定义**，字段类型仅记录于 `code-search.md:64-78`，运行时需手工 PUT 模板。
- PG：本服务不建表；`saved-views` stub 未落库（`main.py:356-364` 注释「PG 持久化待阶段 4 统一实现」）。

---

## 4. 原型对照与差异

| 原型（`prototype/search.html`） | 当前实现 |
|---|---|
| PPL 语句、时间范围、直方图、列表、详情抽屉、保存视图、导出 | 前 5 项已实现且接真实后端；保存视图与全量导出为 stub |
| `timechart` 语法 chip（`prototype/search.html:54`） | `CHIPS` 未包含（`SearchView.vue:137-142`）；`docs/tech-stack-selection.md:188-189` 记录「PPL 函数集是子集，复杂管道需评估覆盖度」 |
| 多选 + 批量条 20/50/100、保存为视图 | 多选导出已做，保存视图入口已移除 |
| 静态假日志/假直方图 | 已全部替换为 OpenSearch 真实检索与聚合 |

---

## 5. 真实实现 vs 占位 / 模拟

**真实实现**：`/query`、`/pql`、`/histogram`、`/context` 四个端点均真实请求 OpenSearch（DSL 与 `/_plugins/_ppl`）；时间窗、租户、分页、上下文、告警回填均为实链路；`code-search.md:149-153` 记录 5 端点 curl 实测通过。

**占位 / 需知悉的简化**：

1. `saved-views` 为 stub，前端入口已移除（`main.py:357-364`）。
2. `onExport()` 仅 `ElMessage.info('导出结果（上限 5,000 条）规划中')`（`SearchView.vue:487-489`）。
3. `auth-service` 仍是骨架占位；租户由前端 Pinia 透传，未做后端鉴权强绑定（`code-search.md:158`）。
4. `build_must():104-106` 中 `tenant=all` 的强隔离为 `pass` 占位。
5. 日志数据由 `log-generator` 模拟业务系统产生（非真实生产日志）。

---

## 6. 当前边界与后续待办

| 优先级 | 待办 | 说明 |
|---|---|---|
| **P0** | `/pql` 补 `timeRange` | `runPql():223` 只传 `{pql, tenant}`，PPL 分支**不受页面时间范围约束**；这是「最近 10 分钟」类语义落地的硬缺口 |
| **P0** | 补 PPL 安全闸门 | 现仅关键字黑名单；应按 `docs/tech-stack-selection.md:189` 要求补索引白名单、只读命令/函数白名单、字段白名单、`head` 上限、tenant 后端强绑定 |
| P1 | AI 智能检索 | `ai-service` 配置中心（模型接入三页签 / PPL 语法规则库 / 能力注册表）已落地，但 `/nl2ppl` 生成端点与检索页 AI 面板**尚未接入**；`/grammar/preview` 只做字面规则命中、不调用模型（`ai-service/app/main.py:729-748`）；能力注册表 `nl2ppl` 状态为 `planned` |
| P1 | `saved-views` 落库 + 前端入口 | 与 auth/RBAC 一并考虑 |
| P1 | `timechart` 等 PPL 函数实测 | 先 `curl /_plugins/_ppl` 验证支持清单，再固化语法 chips 与语法库 |
| P2 | 全量导出、构建体积优化、单节点 yellow 处理 | `code-search.md:156-164` 有记录；告警回填 7 天会产生长区间零值桶 |

---

## 7. 验证记录与运行入口

- 页面：`http://localhost:8080/search`；后端 Swagger `http://localhost:8002/docs`。
- 直连示例：`curl -X POST http://localhost:8002/pql -H 'Content-Type: application/json' -d '{"pql":"source=logs-default | where level=ERROR | head 100","tenant":"default"}'`。
- 本期已验证：4 个端点真实返回、告警跳转回填自动执行、无头浏览器检索页控制台零报错；18 容器 running、8001–8008 health 200。
- 前端构建：`npm --prefix frontend run build`。

---

## 8. 相关文件索引

| 文件 | 作用 |
|---|---|
| `frontend/src/views/SearchView.vue` | 检索主页面（929 行 SFC，能力中枢） |
| `frontend/src/api/search.js` | 4 个检索接口封装 |
| `services/search-service/app/main.py` | 检索网关、PPL 转发与安全校验 |
| `services/ai-service/app/main.py` | 智能管理配置中心（**非**运行时生成端点） |
| `code-search.md` | 本模块早期实现分析与 curl 实测记录 |
| `prototype/search.html`、`docs/tech-stack-selection.md`（第 4 章） | 原型与 PPL 选型决策 |
