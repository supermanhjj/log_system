# code-data.md · 数据接入模块（数据源 + Agent 管理）实现总结

> 用途：记录 `ingest-service` / `agent-service` 两个后端与 `/data-sources`、`/agents` 两个前端页面的实现结构与设计取舍，对齐 `code-search.md` 的模块文档模板。
> 最后更新：2026-11-14

---

## 1. 模块定位与目标

| 页面 | 原型 | 后端 | 职责 |
|---|---|---|---|
| 数据源 | `prototype/data-sources.html` / `P-DAT-0501` | `ingest-service` | 日志来源自动发现、接入配置登记、连通性校验、状态与健康度 |
| Agent 管理 | `prototype/agents.html` / `P-DAT-0502` | `agent-service` | 心跳注册与超时判定、采集范围配置、重启/升级指令队列、接入配置生成 |

**核心原则（用户明确要求）**：高保真原型里的 6 个数据源 / 6 台主机、76% 升级进度、12ms 延迟等全是静态假数据，只用于对齐布局和字段，**不得作为实现数据**。所有指标必须来自当前真实技术路线与已跑通的接入链路。

**「上线后接入形态会很复杂」的应对**：采用「事实层自动发现 + 配置层人工补充」双层模型，字段名不写死，规则阈值可配，元数据可任意扩展。

---

## 2. 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI 0.115 + opensearch-py 2.6 + psycopg2 + redis-py 5.1 + kafka-python + httpx |
| 事实层 | OpenSearch 3.9（`logs-default` / `logs-acme` / `logs-gp`） |
| 配置层 | PostgreSQL 16（`data_sources` / `ingest_settings` / `agents` / `agent_commands` / `agent_upgrade_tasks`） |
| 心跳层 | Redis 7（最近心跳、累计次数、近期间隔） |
| 采集层 | Fluent Bit（容器 `fluentbit`，`dummy` + `http` 每 30s 上报 `/heartbeat`） |
| 前端 | Vue 3 + Vite + Element Plus + Pinia + Axios |
| 网关 | nginx `/api/data/` → ingest-service、`/api/agents/` → agent-service |

---

## 3. 文件清单

```
services/ingest-service/app/main.py      # 数据源后端（重写，v0.5.0）
services/agent-service/app/main.py       # Agent 管控后端（重写，v0.5.0）
frontend/src/api/data.js                # 数据源 axios 封装
frontend/src/api/agents.js              # Agent axios 封装
frontend/src/views/DataSources.vue      # 数据源页
frontend/src/views/AgentManagement.vue  # Agent 管理页
frontend/src/router/index.js            # /data-sources、/agents 由 Placeholder 换为真实视图
frontend/dist/                          # npm run build 产物（index-BBzTwryV.js）
```

未改动：`nginx/nginx.conf`（反代路由早已就位）、`compose.override.yml`（两服务源码已 bind mount + `--reload`）、`fluentbit/*`（心跳链路未动，保持跑通状态）。

---

## 4. 数据源（ingest-service）

### 4.1 双层数据模型

- **事实层**：对当前租户索引做一次 `terms` 聚合（来源字段，size 500，近 30 天）+ 子聚合（`max` 时间、`filter` 近 1h/24h/7d/今日、`terms` 级别/主机/租户），一次查询拿到所有来源的真实统计，避免 N+1。当前自动发现 5 个来源：`order-service` / `user-service` / `payment-gateway` / `inventory-svc` / `api-gateway`。
- **配置层**：`data_sources` 按 `(tenant, source_key)` 唯一，存日志里没有、必须人工补充的元数据：显示名、接入类型（Agent/Syslog/HTTP/Kafka/自定义）、接收地址、区域、编码、解析规则、采样率、目标索引、**字段角色映射 field_map**、标签、扩展属性 JSONB、状态覆盖、静默阈值、备注。
- **合并**：未登记来源以「自动发现」形态展示；已登记但索引无日志的以「待接入」展示。删除登记不会删日志，来源仍会回到自动发现列表。

### 4.2 字段角色自适应（关键的"灵活"点）

`index_fields()` 读真实 mapping → `detect_roles()` 按候选名 + 类型探测 6 种角色（source/time/level/host/tenant/message）→ `agg_field()` 对 text 字段自动改用 `.keyword` 子字段。全局设置（`ingest_settings` 单行 JSONB）与单条 `field_map` 都可覆盖，留空即自动探测。当前索引探测结果：

| 角色 | 真实字段 | 类型 |
|---|---|---|
| source | `source` | keyword |
| time | `@timestamp` | date |
| level | `level` | keyword |
| host | `host` | keyword |
| tenant | `tenant_id` | keyword |
| message | `message` | text |

上线后日志字段叫 `service` / `log_time` / `severity` 也能自动识别，识别不了可在「接入设置」里指定。

### 4.3 状态与指标（全部真实）

- 状态优先级：人工覆盖（维护中/已停用）> 数据推导。
  - `gap ≤ silence_warn_min`（默认 5min）→ 运行中；`≤ silence_offline_min`（默认 60min）→ 告警中；否则异常；无日志 → 待接入。
  - 近 1h 健康度 < 70% 时，即使有日志也降为「告警中」（口径与 dashboard-service 一致：近1h ÷ 近7d 同时段均值）。
- 指标：最近接收 = `max(@timestamp)`；日增量 = 东八区自然日 00:00 起 count；另给近 24h / 近 1h / 近 30d、错误率、级别/主机/租户分布。
- 详情：近 24h `date_histogram`（`extended_bounds` 铺满 25 桶）、按真实 mapping 逐字段 `exists` 聚合算覆盖率、最近 5 条原始整行 `raw`。

### 4.4 连通性校验（真实探测，绝不返回假成功）

| 类型 | 探测方式 | 实测结果 |
|---|---|---|
| Agent | 在真实索引按 `source_field=source_key` 查近 24h 日志，附带管控面 Agent 概况 | `order-service` → 1592 条，正常；不存在的源 → 明确失败 |
| Syslog | `socket.create_connection(host, port, timeout=2.5)` | `10.24.8.100:5514` → 超时，如实失败 |
| HTTP | TCP + `httpx.get` 拿真实状态码 | `http://opensearch:9200` → 200 |
| Kafka | TCP + `KafkaConsumer.topics()` 取真实 topic 列表 | broker 可达，当前 topic 为空（如实显示） |

### 4.5 API 契约

| 端点 | 方法 | 说明 |
|---|---|---|
| `/meta` | GET | 真实索引、mapping、字段角色探测、可聚合字段、接入类型、组件可达性 |
| `/sources` | GET | 自动发现 + 配置合并，支持 keyword/type/status/region/registered/分页 |
| `/sources/{key}` | GET | 详情：趋势、字段覆盖率、分布、样例原始日志、字段角色 |
| `/sources` `/sources/{key}` | POST / PUT / DELETE | 登记配置 CRUD |
| `/sources/batch-status` | POST | 批量启用/停用/维护（写状态覆盖，未登记自动补最小登记） |
| `/sources/test` | POST | 真实连通性探测 |
| `/settings` | GET / PUT | 全局字段角色、静默阈值、自动发现开关与天数 |
| `/export` | GET | 导出真实配置 JSON（含自动发现清单，带 Content-Disposition） |
| `/health` `/` | GET | 探针与自述 |

---

## 5. Agent 管理（agent-service）

### 5.1 心跳（真实链路）

- 当前采集端 `fluentbit` 用原生 `dummy` + `http` 每 30s POST `/heartbeat`（distroless 官方镜像无 `exec` 插件，见 `fluent-bit.conf` 注释）。
- **服务端一律以接收时刻为心跳时间**：dummy payload 里 `ts` 是写死占位串 `2026-01-01T00:00:00Z`，只作留档并在详情里明示，不参与状态判定（防客户端时钟漂移）。
- 写 Redis：`logsys:agent:hb:<host>`（JSON，7 天过期）、`logsys:agent:count:<host>`（累计次数）、`logsys:agent:recent:<host>`（最近 20 次时间戳，算平均间隔）。Redis 不可用时降级用 PG `last_seen`，心跳接口仍返回成功并标 `degraded`。
- 首次心跳自动注册 PG `agents` 行，无需手工录入主机。实测间隔 30.0s、累计心跳持续增长。
- **IP 三级来源（不造假数据）**：dummy 心跳不带 `ip`，故服务端从 HTTP 请求真实观测连接对端（反代部署采信 `X-Forwarded-For` 首跳），写入 Redis `peerIp` + PG `agents.observed_ip`（老库用 `ADD COLUMN IF NOT EXISTS` 平滑迁移）。展示优先级 **人工登记 `agents.ip` > 采集端自报 `hb.ip` > 服务端观测**；人工值永不被自动值覆盖，自动值随每次心跳更新，容器/Agent 换 IP 自动跟上。实测当前 `fluentbit` 观测为 `172.20.0.17`。列表/详情带来源 tooltip，编辑弹窗对自动值不预填，避免误固化成人工登记；「主机名 / IP」筛选可直接搜观测 IP。
- 状态：维护中（人工）> 离线（>180s 无心跳）> 告警中（平均间隔 >90s）> 运行中。阈值可按每台 Agent 配置。

### 5.2 采集速率（口径诚实）

当前心跳不带吞吐，因此**不编造 Agent 自报速率**，而是由平台侧对 OpenSearch 真实日志聚合反推：近 5min count / 300s，并在 UI 明示口径。每台 Agent 可配 `host_values` / `source_keys` 圈定自己负责的日志范围，字段角色也可按 `field_map` 覆盖。

实测：不过滤时 0.99 条/秒（全量），配置只负责 `host-01`/`host-02` 后降到 0.34 条/秒，覆盖分布只返回这两个 host —— 证明范围配置真实生效。

### 5.3 指令队列与升级编排（真实，不是假进度）

- `/heartbeat` 响应带 `commands[]`（最多 5 条 pending/delivered），并把状态推进为 `delivered`。实测 `restart` 指令下一次真实心跳即被带出。
- 重启 / 批量升级生成 `agent_commands` 记录；升级任务 `agent_upgrade_tasks` 支持目标版本、滚动并发、跳过离线、备注。
- 进度分两段真实统计：**指令下发**（delivered/total）、**Agent 确认**（acked/total）。`/commands/{id}/ack` 供随包管控脚本回传结果，全部确认后任务自动 completed。
- **当前限制（已在页面明示）**：开发期采集端是原生 dummy 心跳，不消费 HTTP 响应体、也不回传 ack，因此「Agent 确认」会真实停在 0%，不是伪造的 76%。接入随包管控脚本后自动推进。
- 「下载安装包」改为**下载接入配置**：按当前环境实时生成可直接落盘的 Fluent Bit `.conf`（SERVICE / tail / rewrite_tag 多租户路由 / opensearch 输出 / dummy 心跳 / 管控脚本 ack 接入说明，68 行），比一个假 tar.gz 有实际价值。

### 5.4 API 契约

| 端点 | 方法 | 说明 |
|---|---|---|
| `/heartbeat` | POST | 上报 + 自动注册 + 下发待执行指令 |
| `/agents` | GET | 列表（心跳状态 + 真实速率 + 覆盖分布 + 汇总 stats） |
| `/agents/{host}` | GET / PUT | 详情（心跳/覆盖/配置/指令历史）、编辑元数据 |
| `/agents/{host}/command` | POST | 下发重启/重载/升级指令 |
| `/agents/{host}/status` | POST | 维护状态覆盖 |
| `/commands/{id}/ack` | POST | 管控脚本回传执行结果 |
| `/upgrade-tasks` | GET / POST | 升级任务列表 / 创建（滚动编排） |
| `/upgrade-tasks/{id}/cancel` | POST | 取消任务 |
| `/install-config` | GET | 生成 Fluent Bit 接入配置文本（附件下载） |

---

## 6. PG 表

| 表 | 用途 |
|---|---|
| `data_sources` | 数据源登记配置（含 field_map / tags / extra JSONB，`(tenant, source_key)` 唯一） |
| `ingest_settings` | 全局接入设置（单行 JSONB，key='global'） |
| `agents` | 主机元数据（自动注册，含负责范围、字段角色、状态覆盖、心跳计数、`ip` 人工登记 / `observed_ip` 自动观测分离） |
| `agent_commands` | 指令队列（pending → delivered → acked/failed） |
| `agent_upgrade_tasks` | 滚动升级任务 |

---

## 7. 前端实现

两个页面对齐原型的页头/筛选栏/批量条/表格/分页/抽屉结构，沿用 `AlarmRules.vue` 的样式体系（`page-head` / `filter-card` / `table-card`）与设计令牌（青绿 `#03857E`、表头 `#E6FCF8`），并复用之前踩坑修好的「选择列与数据列同底」样式。

- **DataSources.vue**：自动发现说明条（含 OpenSearch/PG/Kafka/管控服务组件可达性）、筛选、批量状态、状态 tag 带判定依据 tooltip、详情抽屉（指标卡/24h 纯 CSS 趋势柱/级别与主机分布/字段角色与覆盖率/样例原始日志/接入配置）、新建编辑抽屉（按类型给接入指引、字段角色下拉来自真实 mapping、标签与扩展属性 JSON）、真实连通性结果面板、全局接入设置弹窗、导出配置下载、30s 自动刷新、租户联动。
- **AgentManagement.vue**：升级任务卡（双进度 + 真实限制说明 + 逐台状态 chips）、汇总状态条、筛选、批量升级/重启、详情抽屉（心跳信息/采集覆盖/配置/指令历史）、编辑弹窗（负责范围、字段角色、阈值、IP 自动/人工区分）、升级弹窗、接入配置下载弹窗、30s 自动刷新。

---

## 8. 验证结果（2026-11-14，经 nginx:8080 全链路）

- 后端 `py_compile` 通过；前端 `npm run build` 通过（`index-BBzTwryV.js`）。
- 自动发现 5 个真实来源，字段角色 6/6 自动探测正确；详情 25 个趋势桶、8 个字段覆盖率、5 条样例日志。
- 连通性 5 组探测（含 2 组如实失败）、CRUD、批量状态、导出 JSON、设置 PUT 全部通过。
- Agent 真实心跳自动注册（间隔 30s）、编辑后速率按范围真实变化、重启指令由下一次心跳带出（delivered）、升级任务 ack 后进度 100% 且任务 completed、接入配置 68 行生成。
- 无头浏览器实测两页面：数据源 5 行 / Agent 1 行，详情抽屉、新建抽屉、连通性结果、设置、编辑、下载、批量升级弹窗全部渲染正常，**控制台零报错**。
- 验证产生的登记/任务/指令/状态覆盖已全部清理，恢复干净演示态：5 个自动发现来源、1 台运行中 Agent、0 个升级任务。

---

## 9. 已知限制与后续

1. **升级确认进度停在 0%**：采集端为原生 dummy 心跳（单向 HTTP），不消费响应体、不回传 ack。需换成含 `exec` 插件的 Fluent Bit 构建并启用 `fluentbit/scripts/control.sh` 管控脚本，`/commands/{id}/ack` 已备好。
2. **平台侧状态 ≠ 实际采集开关**：批量停用/维护只写平台登记状态，不会停止 Fluent Bit 采集（页面已明示）；真正的采集控制需要配置热重载（`SIGHUP` / `POST /api/v2/reload`）与指令队列打通。
3. **区域/接入类型/解析规则是人工元数据**：真实日志里没有这些字段，只能登记补充；未登记显示「自动发现」「—」。
4. **Kafka topic 当前为空**：`KafkaConsumer.topics()` 如实返回空列表；采集过 Kafka 的链路仍是 `docs/tech-stack-selection.md` 的 P2 待办。
5. **缓存**：数据源合并结果 15s、采集速率 15s 内存缓存，刷新按钮可 force 重算；多实例部署时应换成 Redis 缓存。
6. 接入设置目前是全局单行，未按租户分档；单条数据源的 `field_map` 已可覆盖，若上线后多租户 schema 差异大，可扩展为按租户设置。
7. 前端单 JS chunk ≈ 2.43MB（gzip 793KB），后续可做路由级 code-splitting。
