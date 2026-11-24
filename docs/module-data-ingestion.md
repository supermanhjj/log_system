# 数据接入模块 · 建设思路与开发进展

> 快照日期：2026-11-18（GMT+8）｜基于当前工作区真实代码整理，行号随代码变动可能偏移
> 关联模块：[运行总览](module-overview.md) · [日志分析](module-log-search.md) · [告警中心](module-alarm-center.md)
> 设计基线：[技术栈选型与方案设计](tech-stack-selection.md) · [服务清单](service-inventory.md)｜实现分析另见仓库根目录 `code-data.md`

---

## 0. 本期全局进展快照

| 层次 | 当前状态 |
|---|---|
| 前端 | 已从 `prototype/` 静态高保真 HTML 升级为 **Vue3 + Element Plus + ECharts + Pinia** 单页应用，构建产物 `frontend/dist` 由 nginx 托管；本次提交的是源码，`dist/` 按 `.gitignore` 不入库，clone 后需 `npm run build` |
| 网关 | nginx 统一入口 `http://localhost:8080`，`/api/data/ → ingest-service:8000/`、`/api/agents/ → agent-service:8000/`（`deploy/docker/nginx/nginx.conf:25-26`） |
| 后端 | 8 个微服务容器内统一 `:8000`，宿主机 `8001–8008` 暴露；PG 存数据源/Agent 主数据，Redis 存心跳，OpenSearch 存日志 |
| 采集 | `log-generator` → 按租户分文件 `/var/log/app/<tenant>.log`（正文不含 tenant 字段）→ Fluent Bit（tail + `Path_Key` + `filename_tenant` 解析 + `DB` 断点）→ **HTTP 上报** ingest-service `POST /ingest/collector` → 服务端 `derive_tenant()` 判定后写入 `logs-<tenant>`；判定不出落 `logs-unknown` |
| 租户归属 | **来源元数据注入型**（对齐上云形态）：租户来自"日志从哪来"，不再从正文探测；统一接入端点 `POST /ingest` 支持 `X-Api-Key`/JWT，写入时强制覆盖 `tenant_id` |
| 运行环境 | **18 个容器 running，8001–8008 `/health` 全部 200**（`docs/service-inventory.md:5,110`，索引文档数约 1.8 万且持续增长） |

---

## 1. 模块定位与建设思路

### 1.1 设计目标（来自原型规格）

两个页面：`DAT-0501` 数据源（列表、CRUD、类型 Agent/Syslog/HTTP/Kafka、连通性校验、导出配置）与 `DAT-0502` Agent 管理（主机列表、心跳 >3min 离线、批量滚动升级、重启、详情）。技术选型的关键约束是「**业务主机仅部署单一 Fluent Bit 进程**，管控逻辑靠随包管控脚本 + 配置热重载承载，不另起 Supervisor 守护进程」（`docs/tech-stack-selection.md:3,111-119`）。

### 1.2 落地时的四条思路（含一个重要职责边界）

| 思路 | 具体做法 | 代码落点 |
|---|---|---|
| **配置面与数据面分离** | `ingest-service` **不接收日志写入、不建索引模板**；日志由 Fluent Bit `opensearch` output 直连写入。服务只负责数据源登记（PG）、字段发现、连通性探测、接入配置导出 | `services/ingest-service/app/main.py`（全文件无 `indices.create / put_index_template / bulk`），`deploy/docker/fluentbit/fluent-bit.conf:53-78` |
| **动态字段字典靠读真实 mapping** | `GET /meta` 调 `indices.get_mapping`，按候选名 + date 类型探测字段角色（time/level/source/host/message/traceId…），人工覆盖优先；text 字段聚合自动转 `.keyword`。检索、告警关联 PPL、语法库都复用这份字典 | `ingest-service/app/main.py:149-157,284-347,667-685` |
| **自动发现替代手工登记** | 数据源列表由 `discover()` 对 `logs-*` 做一次 `terms` 聚合自动发现，15s 缓存；PG 里只存人工补充信息与状态覆盖 | `main.py:354-437,608-641` |
| **管控面用「指令队列 + 心跳」** | `agent-service` 用 Redis 接心跳、PG 存主机元数据与升级任务、指令队列下发重启/配置变更，`install-config` 实时生成 Fluent Bit conf；**与 AI 无关**（名称易与 `ai-service` 混淆） | `services/agent-service/app/main.py:1-19,552-615,825-903` |

数据流：`log-generator →(app_logs 卷)→ fluentbit tail log.* → parser app_log → rewrite_tag 按 tenant_id → 3 个 opensearch output → logs-<tenant>`（`deploy/docker/fluentbit/fluent-bit.conf:20-27,29-39,46-50,53-78`，`deploy/docker/fluentbit/parsers.conf:7-17`）。端口/协议：心跳 HTTP `agent-service:8000/heartbeat`、OpenSearch `9200`、Fluent Bit HTTP_Server `2020`。

### 1.3 与最初设计的偏差

- 设计稿的数据源类型含 Kafka topic 管理、Flink 流处理（`:62,106-109`）；当前 Kafka 仅在连通性探测里被真实探测、未作为日志写入链路（`ingest-service/app/main.py:960-974`），Flink 容器运行但无 Job（`docs/container-deployment.md:70`）。
- **索引模板 `logs-template` 无代码定义文件**（全仓无 `put_index_template/indices.create`），字段类型只记在 `code-search.md:64-78`，运行时靠手工 `curl PUT`；这属于部署前置条件，不是已自动化能力。
- 租户集合 `default / acme / gp` 在 Fluent Bit 配置里硬编码（`fluent-bit.conf:45`），新增租户需改配置。

---

## 2. 已落地能力清单

| 能力 | 前端落点 | 后端落点 | 状态 |
|---|---|---|---|
| 数据源列表 / 筛选 / 分页 / 30s 刷新 | `DataSources.vue:65,84,92,298-302` | `GET /sources`（`ingest-service/app/main.py:698`） | ✅ 自动发现 + PG 覆盖 |
| 详情：趋势、级别/主机分布、字段覆盖率、样例原始日志 | `DataSources.vue:485-552` | `GET /sources/{key}`（`:801`） | ✅ 真实聚合 |
| 新建 / 编辑 / 删除登记 / 批量启停 | `:117-127,145-252` | `POST /sources:742`、`PUT:753`、`DELETE:765`、`POST /sources/batch-status:774` | ✅ 落 PG（删除只删登记，不删日志） |
| 真实连通性测试（TCP / HTTP / Kafka） | `onTest():226-240` | `POST /sources/test:873-976` | ✅ 如实回 errno，不伪造成功 |
| 接入设置 / 字段角色映射 | `openSettings/saveSettings():255-272,617-623` | `GET/PUT /settings:688,693`；`GET /meta:667` | ✅ |
| 接入配置导出 | `onExport():282` | `GET /export:979` | ✅ |
| Agent 列表 / 心跳状态 / 详情 / 配置编辑下发 | `AgentManagement.vue:75,107,128,146,168` | `GET /agents:618`、`GET /agents/{host}:649`、`PUT:683` | ✅ |
| 重启 / 维护 / 批量重启 / 指令队列 ack | `AgentManagement.vue:175-200,284-288` | `POST /agents/{host}/command:705`、`/status:722`、`/commands/{id}/ack:729` | ⚠️ ack 端点已备，采集端未接入 |
| 升级任务：创建 / 取消 / 进度 | `submitUpgrade():254-295` | `GET/POST /upgrade-tasks:773,784`、取消 `:818` | ✅ 真实任务表，非静态 76% |
| 接入新主机（生成接入包 / 签发凭证 / 登记台账） | `AgentManagement.vue` 接入向导 | `POST /install-package`、`GET/DELETE /collector-credentials`；旧 `GET /install-config` 保留为单文件排障入口 | ✅ |
| 心跳自动注册与速率反推 | — | `POST /heartbeat:552-615`，速率由 OpenSearch 反推 `:320-371` | ⚠️ dummy 心跳 ts 为占位串 |

---

## 3. 接口与数据结构

**`ingest-service` 端点**（`services/ingest-service/app/main.py`）：`/health`、`/`、`GET /meta`、`GET/PUT /settings`、`GET/POST /sources`、`PUT/DELETE /sources/{key}`、`POST /sources/batch-status`、`GET /sources/{key}`、`POST /sources/test`、`GET /export`、**`POST /ingest`（通用日志写入）**、**`POST /ingest/collector`（采集器专线上报）**；`startup init_db()`。

**`agent-service` 13 个端点**（`services/agent-service/app/main.py`）：`/health:535`、`/:540`、`POST /heartbeat:552`、`GET /agents:618`、`GET/PUT /agents/{host}:649,683`、`POST /agents/{host}/command:705`、`/status:722`、`POST /commands/{id}/ack:729`、`GET/POST /upgrade-tasks:773,784`、取消 `:818`、`GET /install-config`（单文件兼容）、`POST /install-package`（tar 接入包，host/container 双形态）、`GET /collector-credentials`、`DELETE /collector-credentials/{code}`（管理员）。

| 数据 | 定义 | 说明 |
|---|---|---|
| `data_sources` / `ingest_settings` | `ingest-service/app/main.py:167-200` | 数据源登记与接入设置，PG |
| `agents` / `agent_upgrade_tasks` / `agent_commands` | `agent-service/app/main.py:122-176` | 主机元数据、升级任务、指令队列，PG |
| Redis | `agent-service/app/main.py:552-615` | 最近心跳时间戳与超时判定 |
| OpenSearch 索引 | `logs-<tenant>`（约定式，租户清单以 `tenants` 表为唯一真源，不在代码里硬编码）；`all → logs-*`；未归属 → `logs-unknown` | 索引模板由 system-service `ensure_tenant_index()` 显式建 mapping |
| 前端封装 | `frontend/src/api/data.js:7,20-82`、`agents.js:7,11-66` | `baseURL /api/data`、`/api/agents` |
| 异常与降级 | `ingest-service/app/main.py:202-213,405-406,841-843` | PG/设置读取异常降级启动，聚合异常转 502；`capabilities` 逐组件 ping（`:559-581`） |

### 3.1 租户归属判定与接入鉴权（2026-11-18 改造）

**判定优先级**（`derive_tenant()`，服务端唯一裁决，不信客户端自称）：

| 优先级 | 依据 | 使用场景 |
|---|---|---|
| 1 | 记录自带 `tenant_id`（由采集器注入） | 容器日志（K8s namespace/label）、系统日志（实例 tag→CMDB） |
| 2 | 来源文件名 `Path_Key` → `filename_tenant` 解析 | 本地多文件模拟；生产对应按目录/文件规则归类 |
| 3 | 均无法判定 或 租户不存在/已停用 | 落 `logs-unknown`，响应里 `unassigned` 计数，**绝不静默归 default** |

**两条写入通道**：

| 端点 | 鉴权 | 租户来源 | 用途 |
|---|---|---|---|
| `POST /ingest` | `X-Api-Key`（Key 绑定租户）或 `Authorization: Bearer <JWT>` | Key 绑定值 / JWT `tenant` 声明，**强制覆盖** body 里的 `tenant` | 各类系统、App 直报 |
| `POST /ingest/collector` | `X-Collector-Key`，以 `Depends(require_collector)` **前置校验** | 逐条 `derive_tenant()`，同一批可按租户分桶写多个索引 | Fluent Bit 等采集器专线 |

**保护性约束**：`MAX_PAYLOAD=50000`（超限 413，避免不可重试的 400 造成采集端丢数据）、`MAX_BATCH=1000` 服务端分片 `helpers.bulk`；鉴权前置于 body 解析，未授权一律 401 而非 422（不暴露 schema）。

**读取侧**：`/meta`、`/sources*`、`/export` 全部加 `Depends(current_identity)` + `resolve_tenant()`；`/meta` 的 `tenantOptions` 对普通账号只返回本租户，管理员才返回全量+`all`；`PUT /settings` 是平台级全局配置，仅平台管理员可改。

### 3.2 时间语义归一化（2026-11-18 修正）

不同采集器/来源系统表达"事件时间"的方式差异很大，接入层必须统一，否则**时间会静默退化成采集时刻**——表现为历史日志全挤到当天、按天聚合与趋势图失真、上下文相邻日志错乱。

| 来源形态 | 键名 | 取值样例 |
|---|---|---|
| Fluent Bit 5.x（JSON 输出） | `date` | `1794877201.0`（epoch 秒，浮点） |
| 通用采集器 / 自研 SDK | `@timestamp` / `time` / `timestamp` / `ts` / `event_time` | ISO8601 或 epoch |
| 业务系统直报 | 任意上述键 | `2026-11-17T09:00:01+0800`、`2026-11-17 09:00:01` |

`normalize_record()` 的处理规则：

1. 按优先级在 `TIME_KEYS` 中取第一个非空值，交给 `to_iso_utc()` 归一化为 **UTC ISO8601（毫秒 + Z）**，写入 `@timestamp`。
2. `to_iso_utc()` 支持 epoch 秒/毫秒（`>1e12` 判为毫秒）、ISO8601 带 `Z`/`±HH:MM`/`±HHMM`、`YYYY-MM-DD HH:MM:SS`（无时区按容器本地时区解释）。
3. 全部无法识别时才用采集时刻兜底；其余时间键归一化后从文档中移除，避免同一文档存在多套时间字段。
4. 租户字段仍由服务端强制覆盖，与此无关。

**关键坑（本次实测）**：Fluent Bit 5.2 的 `tail` + `regex` parser 抽取时间后，输出记录里是 `date` 字段而非 `@timestamp`。旧实现只认 `@timestamp/time/timestamp/ts`，于是 4 万条历史日志的 `@timestamp` 全被写成采集瞬间，按天只看到一天。修复后 11-11 ~ 11-18 时间分布恢复正常。

**历史数据补录**：`tools/log-generator/backfill.py` 按日期与时段回填，走真实采集链路（写 `<tenant>.log` → Fluent Bit → `/ingest/collector`），不直写索引：

```bash
docker compose exec -T log-generator python /app/backfill.py \
    --date 2026-11-17 --from 09:00 --to 22:00 --per-min 8
```

`--per-min` 为「每分钟每租户」条数，内部按小时权重浮动（工作时段高、午间与晚间低），`--to` 为右开区间。

---

## 4. 原型对照与差异

| 原型（`prototype/data-sources.html`） | 当前 Vue 实现 |
|---|---|
| 6 行静态假数据（`:96-167`）、4 项筛选（`:35-68`） | 真实自动发现 + PG 覆盖，筛选与分页接后端 |
| 新建抽屉仅 name/type/addr/parse/encoding/region/采样率（`:198-243`） | 增加自动发现说明、接入设置、字段角色映射、移除登记 |
| 「测试连通性」toast 假值「延迟 12ms」（`:246,262`） | 真实 TCP/HTTP/Kafka 探测并回 errno |
| 无详情趋势 / 覆盖率 / 样例 | 详情页补齐分布、覆盖率与样例原始日志 |

| 原型（`prototype/agents.html`） | 当前 Vue 实现 |
|---|---|
| 静态 6 台、升级进度 76% 假数据（`:35-48`） | 真实指令队列进度，ack 停 0 如实展示 |
| 「查看采集日志」「下载安装包」toast（`:28,85-92`） | 改为维护/重启/批量重启/配置编辑 + 下载实时生成的接入配置 |

---

## 5. 真实实现 vs 占位 / 模拟

**真实实现**：PG 配置、OpenSearch 真实 mapping 与聚合、TCP/HTTP/Kafka 连通性探测、指令队列与升级任务、install-config 生成、自动发现与字段角色探测。

**占位 / 需知悉的简化**：

1. `log-generator` 是**模拟被测业务系统**（`docker-compose.yml:215-218`），不是真实生产日志源。
2. `fluent-bit.conf:17` 的 dummy 心跳时间戳是写死占位串（`agent-service/app/main.py:9-10,614`）；心跳无 IP/吞吐真实上报。
3. `deploy/docker/fluentbit/scripts/control.sh:3-5,18-19` 的 ②③ 仍为骨架占位；`ack` 端点已备但采集端未接入（`AgentManagement.vue:284-288`）。
4. `docker-compose.prod.yml:1-3` 是生产编排骨架；Kafka 仅探测、无 topic 写入；Flink 无 Job。
5. 索引模板 `logs-template` 无代码定义，需部署时手工 PUT。
6. `owner` 硬编码「黄金金」（`DataSources.vue:200`）；采样率仅登记、不生效（`:604`）。

---

## 6. 当前边界与后续待办

| 优先级 | 待办 | 说明 |
|---|---|---|
| P1 | 索引模板代码化 | 把 `logs-template` 的 fields/类型落成仓库内 JSON + 初始化脚本，消除「手工 curl PUT」部署前置条件；AI 语法库与告警关联 PPL 都依赖它 |
| P1 | 管控脚本闭环 | 落实随包 `control.sh` 心跳/配置热重载/指令 ack，让升级进度不停在 0；当前 Redis 心跳与 PG 指令队列已就绪 |
| P1 | 租户动态化 | 去掉 Fluent Bit 里 `default/acme/gp` 硬编码，与后端 `INDEX_BY_TENANT` 统一 |
| P2 | Kafka / Flink 链路 | 若按设计稿引入流处理，需补 topic 与真实 Job；否则在设计文档中明确「开发/演示阶段直写 OpenSearch」为正式方案 |
| P2 | 采样率、owner 等登记项生效 | 与 auth/RBAC 接通后去掉硬编码 |
| P2 | 生产编排 | `docker-compose.prod.yml` 目前 `volumes: []`，AI 模块 CLI 挂载与前端 dist 需另行处理；上线前需重建 `ruilog-backend` 镜像 |

---

## 7. 验证记录与运行入口

- 页面：`http://localhost:8080/data-sources`、`/agents`；后端 Swagger `http://localhost:8005/docs`（ingest）、`http://localhost:8006/docs`（agent）。
- 直连示例：`curl 'http://localhost:8005/meta?tenant=default'`、`curl http://localhost:8005/sources`、`curl http://localhost:8006/agents`。
- 链路自检脚本：`scripts/validate-chain.sh:21-93`（T1–T6）；容器定义 `docker-compose.yml:205-231`。
- 本期已验证：`/meta` 真实 mapping、`/sources/test` 真实探测、数据源 CRUD、Agent 指令与升级任务、`/api/agents/install-config` 实时生成 conf；18 容器 running、8001–8008 health 200。
- 前端构建：`npm --prefix frontend run build`。

---

## 8. 相关文件索引

| 文件 | 作用 |
|---|---|
| `frontend/src/views/DataSources.vue`、`AgentManagement.vue` | 数据源与采集端管控两个页面 |
| `frontend/src/api/data.js`、`agents.js` | 接口封装 |
| `services/ingest-service/app/main.py` | 数据源登记、字段元数据、连通性探测、配置导出 |
| `services/agent-service/app/main.py` | 心跳、指令队列、升级任务、接入配置生成（**非 AI 服务**） |
| `deploy/docker/fluentbit/fluent-bit.conf`、`parsers.conf`、`scripts/control.sh` | 采集链路与主机侧管控脚本 |
| `tools/log-generator/generate.py`、`scripts/validate-chain.sh` | 模拟日志源与链路自检 |
| `code-data.md` | 本模块早期实现分析 |
| `prototype/data-sources.html`、`prototype/agents.html` | 高保真原型 |
