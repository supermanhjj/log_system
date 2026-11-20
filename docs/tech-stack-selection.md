# RuiLog 日志分析平台 —— 技术栈选型与方案设计（功能点 × 技术栈映射、检索语言 PPL 方案、双引擎与流处理架构）

> 产品品牌：2026-11-18 定名 **RuiLog 日志分析平台**（原标题「统一日志分析平台」，仅品牌更名，技术选型结论不变）。
> 文档状态：技术栈选型与方案设计基线（**v2.0**，2026-11-19 重大升级：**ClickHouse + OpenSearch 双引擎落地、Kafka 成为数据主干、Flink 实时流处理 Job 上线**——对标云智慧 DOLA，从"选型方案"推进到"可运行商用级链路"。此前 v1.5 决策全部保留：采集层 Fluent Bit、Agent 管控面纳入一阶段、业务主机单 Fluent Bit 进程、检索语言直接 PPL 不做转换层）。本文件聚焦**技术选型与方案设计**（第 1–10 章 + 功能点索引）；**已落地的 Docker 骨架、构建镜像与运行服务现状另见 `container-deployment.md`；双引擎/流处理的落地细节见第 10 章**。
> 关联资产：`prototype/`（高保真原型，10 个页面规格 + `agents.html`）、`prototype/specs/*.json`
> 适用范围：本仓库 `log_system` 的后续开发环境与云上微服务部署

---

## 1. 文档目的与范围

本文件回答三个问题，并作为后续容器化落地的设计基线：

1. **功能点 × 技术栈映射**：原型 10 个页面定义的所有功能点，能否被选定的技术栈完整覆盖，逐模块对齐。
2. **检索语言方案**：本版直接采用 OpenSearch 原生 **PPL** 作为检索语言，不构建 SPL→PPL 转换层（转换层列入第 9 章后期可选）。
3. **Docker 搭建思路**：开发环境 + 云上部署如何隔离、要产出哪些文件、每个文件的设计要点（**仅方案，不执行**）。

> ⚠️ 一个必须先讲清的前提：原型里的 `agents.html`（路由 `/agents`）是**采集器 Agent 管理**（主机列表/心跳/滚动升级），**不是 AI 智能体对话页面**。本仓库原型目前**未定义任何 AI 智能体对话功能点**。技术栈方案里提到的「智能体服务 / AI 智能体模块」属未来扩展方向，原型阶段未引入；**2026-11-14 起新增独立配置中心**（独立 `ai-service` + 前端「智能管理」页，属新增功能而非原型功能点），除模型接入 / PPL 语法规则库 / 能力注册表配置外，**已追加 NL→PPL 运行时**（`POST /nl2ppl` + 配置页「智能检索预览」，基于规则库调用已配置模型生成 PPL）；检索页「智能检索」弹窗已接入（自然语言 → PPL → 填入检索框执行），见第 8、9 章。

---

## 2. 技术栈总览（已确定 + 必要的补充）

### 2.1 已确定部分（用户拍板）

| 层 | 选型 | 角色 |
|----|------|------|
| 前端 | **Vue3**（已确认，放弃 React 备选） | 全部页面 UI，承接现有原型交互 |
| 采集器 | **Fluent Bit**（开源 C agent，已确认替换原自研 Go 采集器） | 主机侧**唯一部署组件**（单进程）；日志采集与转发，原生对接 Kafka / OpenSearch，信创友好、极轻量；**管控逻辑以随包附带的管控脚本（由 Fluent Bit `exec` 输入插件定时调用）+ 配置热重载承载，主机侧不另起独立进程**，服务端 `agent-service`(FastAPI+PG+Redis) 本版一并纳入，见 3.2(8) |
| 检索引擎（明细/全文） | **OpenSearch 3.9**（已确认） | 明细检索、全文/PPL、上下文回溯（ES 的开源分支，信创友好） |
| 聚合引擎 | **ClickHouse 24.3**（v2.0 已落地） | 总览/仪表盘/直方图/级别分布/P95 等重聚合；列式存储 + ReplacingMergeTree 幂等（对标云智慧双引擎） |
| 实时计算 | **Flink 1.18**（v2.0 已上线正式 Job） | Kafka 消费 → 清洗富化 → 双写 OS/CH + 死信；Java DataStream，checkpoint EXACTLY_ONCE |
| 消息队列 | **Kafka 3.7**（v2.0 成为数据主干） | ingest→Flink 解耦缓冲，幂等 producer(acks=all)，按 tenant_id 分区；日志易/云智慧共同选择 |
| 关系型主数据 | **PostgreSQL**（已确认必选） | 用户/租户/角色/规则/数据源/仪表盘/系统参数/审计等结构化主数据 |
| 缓存 / 心跳 | **Redis**（已确认本版引入） | Agent 心跳去重与超时判定、检索结果缓存、会话；随 Agent 管理一起开发 |

### 2.2 仍需补充的组件（原型隐含、此前未列）

| 组件 | 必要性 | 理由（来自规格） |
|------|--------|------------------|
| **API 网关 / LB** | 必选（生产） | 统一入口、鉴权、限流；微服务多无状态服务需要前置路由。 |
| **对象存储 OSS/S3** | 按需（后期） | 原始日志归档（规格未显式要求，但生产必备；原型未涉及）。见第 9 章后期可引入清单。 |

> 结论：以 **Vue3 前端 + Python/FastAPI 应用微服务 + Fluent Bit（主机侧唯一采集/管控组件）+ OpenSearch(明细全文) + ClickHouse(聚合) 双引擎 + Kafka(主干) + Flink(流处理) + PostgreSQL + Redis** 为主体。对象存储仍列后期（见第 9 章）。采集层采用开源 Fluent Bit 替换原自研 Go 采集器，且**业务主机只部署这一个 Fluent Bit 进程**——管控能力以随包脚本（`exec` 插件调用）+ 配置热重载承载，不另起 Supervisor。**v2.0 起 Kafka/Flink/ClickHouse 从"设计/占位"转为"实际运行链路"**，详见第 10 章。下面映射按此组合展开。

---

## 3. 功能点 × 技术栈映射（逐页面）

下表以原型规格（10 个页面 + `agents.html`）为基准，逐功能点对齐技术栈落点。

### 3.1 总览表

| 页面（规格） | 路由 | 核心功能点 | 技术栈落点 | 覆盖 |
|----|----|----|----|----|
| 登录 `AUTH-0100` | `/login` | 账号密码、租户选择、记住账号、登录态机（成功/失败/无权限） | Vue3 + FastAPI(鉴权服务) + **PostgreSQL**(账号/租户) + JWT | ✅ 已落地（2026-11-16） |
| 运行总览 `OVW-0101` | `/overview` | 时间范围、写入趋势、告警分布、检索耗时 P50·P95、告警事件表、导出报表 | Vue3 + OpenSearch(聚合) + ECharts + FastAPI(指标聚合接口) | ✅ |
| 日志检索 `LOG-0201` | `/search` | **PPL 检索**、时间范围、直方图、日志列表、分页、详情抽屉、保存视图/导出 | Vue3 + **OpenSearch PPL** + FastAPI(检索网关) + PG(视图) | ✅ 本版直接 PPL，无转换层 |
| 仪表盘列表 `DSH-0301` | `/dashboards` | 筛选、仪表盘 CRUD、删除确认、状态 | Vue3 + FastAPI(仪表盘服务) + PG + OpenSearch(图表数据) | ✅ |
| 仪表盘详情 `DSH-0302` | `/dashboards/core-business` | 图表绑定检索语句、Tabs、异常明细表、分享/编辑 | Vue3 + OpenSearch(PPL 取数) + ECharts + PG(看板配置) | ⚠️ 依赖 PPL |
| 告警事件 `ALM-0401` | `/alarms/events` | 列表、批量关闭、详情抽屉、状态机（告警中/已恢复） | Vue3 + FastAPI(告警服务) + PG(事件) + OpenSearch(溯源) | ✅ |
| 告警规则 `ALM-0402` | `/alarms/rules` | 规则 CRUD、启用开关、**实时判定**、保存生效 | Vue3 + FastAPI(规则管理) + **Flink**(实时判定 Job) + PG(规则) + Kafka(日志流) | ✅ Flink 核心用武之地 |
| 数据源 `DAT-0501` | `/data-sources` | 数据源 CRUD、类型(Agent/Syslog/HTTP/Kafka)、连通性校验、导出配置 | Vue3 + FastAPI(接入服务) + PG + Kafka topic 管理 | ✅ |
| Agent 管理 `DAT-0502` | `/agents` | 主机列表、**心跳(>3min 离线)**、批量升级(滚动/断点续传)、重启、进度 | Vue3 + **Fluent Bit**(主机侧唯一进程，管控脚本随包部署、`exec` 插件调起) + FastAPI(`agent-service`) + PG(主机元数据) + Redis(心跳) | ✅ 本版纳入，见 3.2(8) |
| 用户与角色 `SYS-0601` | `/settings/users` | Tabs(用户/角色/系统参数/审计)、用户 CRUD、启用开关、导出 | Vue3 + FastAPI(系统服务) + **PostgreSQL**(RBAC/审计) | ✅ 已落地（2026-11-16，四 Tab 全通） |

### 3.2 逐模块细化（含规格原文要点）

#### （1）登录 `AUTH-0100`
- 功能点：`account`/`password` 必填；`tenant` 枚举（总部生产 / 省侧运维 / 测试租户）；记住账号；状态机 `empty/loading/error/noPermission`。
- 落点：
  - 前端：Vue3 表单 + 表单校验（FRM-001/005、INP-001/005）。
  - 后端：FastAPI 鉴权服务，BCrypt 密码哈希，JWT（或 Session）签发；租户选择决定数据范围。
  - 存储：PostgreSQL `users` / `tenants` / `roles` 表。
  - 性能目标：`api_p95 < 800ms`、`lcp < 2000ms`（规格 `perf` 字段）。
- **已落地（2026-11-16）**：`auth-service` 提供 `/tenants`、`/login`、`/me`、`/logout`、`/password`；
  密码用 bcrypt(cost=12)，JWT 与 `system-service` 共用 `JWT_SECRET`；失败区分 400（缺字段）/401（账号或密码错，统一文案防枚举）/403（停用或租户不匹配）。
  登录选定租户写入前端 `app.tenant`，与 `logs-<tenant>` 索引编码对齐（default/acme/gp）。

#### （2）运行总览 `OVW-0101`
- 功能点：时间范围（近1h/24h/7d/30d）；3 张图（写入趋势「万条」、告警级别分布「严重/警告/提示」、检索耗时分布「P50/P95 ms」）；告警事件表（alarmName/level/source/occurTime/status）；异常状态标签 + 告警 badge；刷新 / 导出报表。
- 落点：
  - 图表数据来自 OpenSearch 聚合（`date_histogram` 写量、`terms` 告警级别、`percentiles` 检索耗时 P50/P95）。
  - 前端 ECharts 渲染；FastAPI 聚合接口做统一封装。
  - 导出报表：后端生成 CSV/Excel（或调用对象存储）。

#### （3）日志检索 `LOG-0201` —— ⚠️ 关键改造页
- 功能点：时间范围（近1h/1d/1w/1m/1y）；**PPL 检索语句输入**（本版直接采用 PPL，示例 `source=nginx and status>=500`）；日志分布直方图；日志列表（time/level/source/host/message + 多选 + 批量条 20/50/100）；详情抽屉（time/level/source/host/traceId/message）；保存为视图 / 导出（单次上限约 5000 条）。
- `level` 枚举：DEBUG/INFO/WARN/ERROR/FATAL。
- 落点：**OpenSearch PPL**（本版直接采用，详见第 4 章）+ FastAPI 检索网关（鉴权、合法性校验、分页、限流）+ PG（保存的视图）。
- 直方图：OpenSearch `date_histogram`；详情：`_id` 直取。

#### （4）仪表盘 `DSH-0301` / `DSH-0302`
- 功能点（列表）：筛选、仪表盘表（name/folder/owner/chartCount/updateTime/status）、删除确认 modal、新建/导出。
- 功能点（详情）：时间范围；图表**绑定检索语句**（`source:order-service`、`status>=500`、`level:ERROR`）；4 张图（日志量 / 接口错误率% / Top10 异常主机 / 检索响应耗时 P50·P95·P99）；Tabs（总览/异常明细/变更记录）；明细表。
- 落点：图表配置存 PG（看板 → 图表 → 关联 PPL 语句）；图表取数走 OpenSearch PPL；ECharts 渲染；分享 = 生成只读链接（需鉴权服务配合）。

#### （5）告警事件 `ALM-0401`
- 功能点：列表（alarmName/level/source/occurTime/status/owner/action）；批量关闭（确认 modal）；详情抽屉；导出；状态机（告警中 → 已恢复 / 关闭）。
- `level`：严重/警告/提示；`status`：告警中/维护中/已恢复。
- 落点：FastAPI 告警服务 + PG（事件主表、状态流转）+ OpenSearch（告警关联的原始日志溯源）。

#### （6）告警规则 `ALM-0402` —— Flink 核心场景
- 功能点：规则表（ruleName/dataSource/condition/level/notify/status）；启用开关（即时请求）；新建规则 dialog；**保存后实时生效**；实时判定日志触发告警。
- 落点：
  - 规则管理：FastAPI + PG（规则表）。
  - **实时判定**：Flink Job 消费 Kafka 日志流 → 匹配规则 `condition` → 触发 → 写告警事件（PG）+ 通知（notify 渠道）。
  - 这是 Flink 在架构里不可替代的环节（实时流判定，非批处理）。

#### （7）数据源 `DAT-0501`
- 功能点：数据源表（name/type/status/lastReceive/dailyVolume/action）；新建数据源 drawer（name/type/encoding/topic/region）；连通性校验；导出配置。
- `type`：Agent / Syslog / HTTP / Kafka；`status`：运行中/告警中/异常/离线/维护中。
- 落点：FastAPI 接入服务 + PG（数据源配置）+ Kafka（topic 创建/校验）+ **Fluent Bit**（实际采集，替代原自研 Go 采集器）。

#### （8）Agent 管理 `DAT-0502` —— 本版（一阶段）纳入（业务主机仅部署单一 Fluent Bit 进程）
- 功能点（原型定义）：主机表（hostname/ip/version/status/heartbeat/throughput/action）；筛选（主机名/版本/状态/区域）；**心跳超过 3 分钟标记离线**；批量升级（**滚动升级 + 断点续传**，进度 76% 示例）；下载安装包；重启指令；Agent 详情抽屉（含 config）。
- **本版决策（采集层选型 + 管控面范围，满足"主机只部署一个 Fluent Bit"约束）**：
  - **采集器采用开源 Fluent Bit**（CNCF 毕业，C 语言，<1MB、10–30MB RAM，原生对接 Kafka/OpenSearch），自带采集、重试、磁盘缓冲、多输出，无需自研采集逻辑；信创友好、可静态编译移植到国产 OS/ARM。
  - **⚠️ 关键约束：业务主机只部署一个 Fluent Bit 进程，不另起 Supervisor 守护进程。** Fluent Bit 不提供集中管控（心跳/配置下发/灰度升级），因此把"管控客户端"能力**下沉到随 Fluent Bit 安装包一同分发的「管控脚本」**，由 Fluent Bit 自身的 `exec` 输入插件按 `interval_sec` 定时调用，脚本在 Fluent Bit 同一进程上下文内完成管控动作。具体机制：
    - **心跳上报**：管控脚本定时（如每 30s）向 `agent-service` 上报主机状态（版本、吞吐、运行态、最近日志量）；服务端 Redis 存最近心跳时间戳，定时任务扫 **>3min 无心跳**标记离线。Fluent Bit 进程若异常退出，心跳自然停止 → 自动判定离线（语义正确）。
    - **配置拉取与下发**：服务端下发新采集配置，`agent-service` 写入主机侧配置；管控脚本拉取后落盘，并触发 Fluent Bit **配置热重载**（v2.1+ 支持 `SIGHUP` 信号或 `POST /api/v2/reload` HTTP 端点），**无需重启进程**即可生效。
    - **接收重启/升级指令**：脚本轮询服务端指令队列；重启 = 触发热重载或重启 systemd 单元；升级 = HTTP `Range` 断点续传下载新 Fluent Bit 二进制 + 替换 + 重启服务（滚动调度由服务端编排，逐台执行不中断整体采集）。
  - **服务端 `agent-service`（FastAPI + PG + Redis）**：主机元数据 CRUD、心跳超时判定、升级任务编排（滚动、逐台调度）、安装包托管与版本管理。
- **部署与镜像约束**：
  - ⚠️ **实测修正（2026-11-12）**：fluent/fluent-bit:5.2.0 官方 distroless 镜像**未编译 `exec` 输入插件**（仅含 `exec_wasi`，不适用 shell 脚本）。开发镜像即采用 distroless 基底，仅通过多阶段构建从 debian:trixie-slim 注入 `/bin/sh`+`curl`（供容器内网连通性校验与 `control.sh` 备选）；心跳改用 **dummy 输入 + http 输出原生上报** agent-service（等效 exec 调脚本语义）。**生产若仍需 `exec` 管控脚本**，须换用含 `exec` 插件的 Fluent Bit 构建（非官方 distroless 版）。
  - **K8s 场景**：agent 升级直接走 DaemonSet 滚动更新（更新镜像即升级），无需脚本自升级；脚本自升级路径主要适用于**裸金属 / systemd** 主机。
- **本版即做（落地顺序）**：
  1. Fluent Bit 部署配置接入 OpenSearch（systemd / K8s DaemonSet，`tail`+`systemd` 输入 → `out_opensearch` 输出）；多租户 `tenant_id` 由 `app_log` 正则解析器**直接产出**（`record_modifier` 仅支持字面量、无法引用其他字段），再经 `rewrite_tag` 把租户注入 tag、按 tag 路由到 `logs-<tenant>` 三个固定索引（opensearch 输出 `Index` 参数不支持运行时 record accessor，`${tenant_id}` 会被当环境变量展开为空），日志流从业务主机直达 OpenSearch 可检索。
  2. `agent-service` + 随包管控脚本打通**心跳上报 / 离线判定 / 配置拉取 / 远程重启**，全程单进程。
  3. 批量升级做**基础版**（服务端滚动调度 + 断点续传下载替换），"零中断极致编排"作为后续优化。
- 落点：FastAPI `agent-service` + PG（主机元数据/升级任务）+ Redis（心跳时间戳）+ 随包管控脚本（exec 插件调起）+ 单一 Fluent Bit 进程。详见第 9 章「采集层选型结论」。

#### （9）用户与角色 `SYS-0601`
- 功能点：Tabs（**用户管理 / 角色权限 / 系统参数 / 审计日志**——原型只细化了「用户管理」，其余三个 Tab 为同级待实现）；用户表（userName/account/role/tenant/status/lastLogin/action）；新建用户 modal；启用开关；导出。
- **角色权限模型（已确认，共三类）**：
  | 角色 | 权限范围 |
  |------|----------|
  | **超级管理员** | 最高权限：用户/角色/租户管理、系统参数、审计日志、全部数据源与告警配置、检索查看 |
  | **运维人员** | 检索查看 + **可配置权限**（数据源接入、告警规则配置、仪表盘编辑等运维操作） |
  | **查看人员** | 仅检索查看权限（只读，无配置能力） |
  - 实现：RBAC 三表 `users` / `roles` / `permissions`，角色枚举即上述三类，权限点按「资源:操作」粒度定义。
- **多租户隔离（已确认）：行级隔离 + 租户 ID（tenant_id）**。每一张主数据表（用户、规则、数据源、仪表盘、告警事件等）均带 `tenant_id` 列；所有读写 SQL / OpenSearch 查询强制附加 `tenant_id` 过滤（在 FastAPI 鉴权中间件统一注入），实现租户间数据不可见。规格中 `tenant` 枚举（总部生产 / 省侧运维 / 测试租户）即租户维度。
- 落点：FastAPI 系统服务 + PostgreSQL（RBAC 三表、tenants、audit_log、system_params；各业务表含 `tenant_id`）。
- **已落地（2026-11-16）**：`system-service` 为六表的唯一建表方，前端 `/settings/users` 四 Tab 全部接真实接口；
  权限点按 12 个资源 × 动作定义，改角色权限实时生效（不写进 Token，每次现查 permissions）。
  ⚠️ 与上述「三类角色」的差异：原型 `settings.html` 角色列表含第四类「审计员」，本次按原型实现 4 个内置角色（数据驱动，可在角色表内调整）。
- ⚠️ 仍待细化（非阻塞本版）：系统参数的具体项、审计日志的字段与保留期——规格未展开，需补 PRD（不影响一阶段开发）。

### 3.3 覆盖度结论

- **100% 已定义功能点可被该技术栈覆盖**，检索语言本版直接采用 **OpenSearch PPL**（第 4 章），不构建 SPL→PPL 转换层。
- **真正的工作量不在选型，而在「按规格反推后端业务逻辑」**：SPL 语义、告警规则引擎、多租户隔离粒度、心跳/升级编排、P95<800ms 性能目标，这些规格里还是交互层定义，后端语义需补全。

---

## 4. 检索语言方案（本版直接采用 OpenSearch PPL）

### 4.1 背景与决策
- 原型检索框示例使用 `source:nginx AND status>=500`，语法风格为 **Splunk SPL / 日志易自研 SPL**。
- **OpenSearch 没有原生 SPL**，但提供 **PPL（Piped Processing Language，管道处理语言）**，语法刻意贴近 Splunk SPL（由 AWS OpenSearch 团队维护，插件 `opensearch-sql` 同时支持 SQL 与 PPL）。
- **本版决策**：直接采用 OpenSearch PPL 作为检索语言，前端检索框按 PPL 语义提供输入，**不构建 SPL → PPL 转换/翻译层**（转换层列入第 9 章「后期可引入组件」，后续按兼容需求再评估）。
- 因而本版无需在检索网关做语法归一/改写，用户输入经合法性校验后直接作为 PPL 提交给 OpenSearch 执行。

### 4.2 本版范围与边界

| 项 | 本版做法 |
|----|----------|
| 检索语言 | **OpenSearch PPL**（原生，`opensearch-sql` 插件） |
| 前端检索框 | 按 PPL 语法提供输入与提示，示例写法更新为 PPL 风格（见 4.3） |
| 后端检索网关 | **仅做鉴权、合法性校验、分页、限流、超时**；不做 SPL→PPL 改写 |
| SPL→PPL 转换层 | **本版不做**，移入第 9 章后期可选 |

> 说明：因不做转换层，原型中原有的 SPL 风格写法（`source:nginx AND status>=500`）在本版统一改为 PPL 风格（`source=nginx and status>=500`）。若后续需兼容历史 SPL 查询/固化视图，再在第 9 章评估补转换层。

### 4.3 PPL 语法规范（本版检索框直接采用）
本版检索框直接按以下 PPL 语法规范提供输入，典型示例：

| PPL 写法 | 说明 |
|----------|------|
| `source=nginx` | 等号替代冒号 |
| `and` / `or` | 大小写不敏感 |
| `status >= 500` | 比较表达式 |
| `\|` | 管道，串联处理步骤 |

典型 PPL 示例（对应仪表盘 `status>=500`）：
```
source=nginx | where status >= 500 | stats count() by host | sort - count
```
对应 `level:ERROR`：
```
source=* | where level = 'ERROR' | stats count() by host
```

### 4.4 风险与边界
- **函数差异**：日志易 SPL 有 300+ 函数，PPL 函数集是子集。若将来要做复杂管道（时间窗口、机器学习），需评估 PPL 覆盖度，必要时在 FastAPI 检索网关层用 Python 补足或回退到 OpenSearch DSL；若届时决定兼容 SPL 写法，引入第 9 章的「SPL→PPL 转换层」。
- **安全**：检索语句是用户可控输入，**必须参数化 / 白名单校验**，禁止把原串直接拼进任何系统命令；PPL 注入风险等同 SQL 注入。由于本版无转换层，前端应约束输入为合法 PPL，后端对语句做语法/函数白名单校验后再下发。
- **性能**：复杂 PPL 可能打满集群，检索网关需加超时（规格 `error` 态「检索超时，请缩小时间范围」）、分页、结果上限（导出 5000 条上限）。

---

## 5. 数据层补充说明

| 组件 | 角色 | 引入时机（已确认） |
|------|------|----------|
| **PostgreSQL** | 全部结构化主数据 + 行级租户隔离 | **本版必带** |
| OpenSearch | 明细索引、全文检索、PPL、上下文回溯 | **本版必带**（明细引擎） |
| **ClickHouse** | 日志聚合宽表 + 分钟级预聚合（总览/仪表盘/直方图/P95） | **v2.0 已落地**（聚合引擎，与 OS 双引擎分工） |
| Kafka | ingest→Flink 数据主干、削峰缓冲、死信 | **v2.0 成为主干**（幂等 producer，按 tenant 分区） |
| Flink | 消费 Kafka → 清洗富化 → 双写 OS/CH + 死信 | **v2.0 正式 Job 上线**（Java DataStream，checkpoint 精确一次） |
| **Redis** | Agent 心跳时间戳与超时判定、检索结果缓存、会话 | **本版引入**（检索缓存/会话 + Agent 心跳，随 Agent 管理纳入一阶段） |
| 对象存储 | 原始日志归档 | **后期可引入**（见第 9 章） |

> 双引擎分工铁律：**明细/全文/PPL/上下文走 OpenSearch，重聚合走 ClickHouse**。用户自由 PPL 查询必须留 OS（PPL 是 OS 专属语言，CH 不支持）；系统内置固定维度聚合（总览/仪表盘/直方图）走 CH。切换由 `AGG_ENGINE=os|ch` 控制，失败自动回退 OS。详见第 10 章。

> 一句话：**应用层（前端 + 后端微服务）100% 容器化微服务化；数据/检索/中间件层，开发期全部跑在容器内（含 PostgreSQL / OpenSearch / ClickHouse / Kafka / Flink / Redis，便于本地联调，无生产数据），上线后生产环境的数据层（OpenSearch / ClickHouse / RDS(PostgreSQL) / Kafka / Redis 实例）改为云托管，自管容器只用于开发联调。**

---

## 6. 微服务拆分与容器边界

### 6.1 无状态 vs 有状态（云上部署铁律）
- **无状态（放心容器化生产）**：前端（Nginx 托管 Vue 产物）、各 FastAPI 微服务、API 网关。可水平扩容、滚动发布、重启无数据丢失——容器的甜区。
- **有状态（开发期容器化、生产期云托管）**：OpenSearch、PostgreSQL、Kafka、Flink 状态、Redis。开发期全部以容器 sidecar 跑在本地（无生产数据，便于联调）；上线后生产环境改用云托管实例（OpenSearch 服务 / RDS(PostgreSQL) / Kafka 实例 / Redis 实例），托管服务自带备份、容灾、数据安全——契合国网场景的硬要求。

### 6.2 服务清单与容器映射

| 服务（建议名） | 技术 | 状态 | 职责 | 对应功能模块 |
|----------------|------|------|------|--------------|
| `web` (前端) | Nginx + Vue3 产物 | 无状态 | 静态资源托管、SPA 路由 | 全部页面 |
| `auth-service` | FastAPI | 无状态 | 登录、JWT 签发、`/me` 返回用户与权限（**不做管理面 CRUD**；RBAC 判定由各服务自行校验同一 Token） | AUTH-0100 |
| `search-service` | FastAPI | 无状态 | 检索网关：PPL 检索封装（含合法性校验）、鉴权、分页、限流 | LOG/DSH |
| `dashboard-service` | FastAPI | 无状态 | 看板/图表配置 CRUD、取数编排 | DSH |
| `alarm-service` | FastAPI | 无状态 | 告警事件管理、状态机、通知 | ALM-01 |
| `ingest-service` | FastAPI | 无状态 | 数据源管理、连通性校验、Kafka topic | DAT-01 |
| `agent-service` | FastAPI | 无状态 | Agent 主机元数据、心跳超时、升级编排（本版纳入一阶段，见 3.2(8)） | DAT-02 |
| `system-service` | FastAPI | 无状态 | 用户/角色/参数/审计的**管理面 CRUD**，且为 RBAC 六表的唯一建表方 | SYS-0601 |
| `log-agent` (采集器) | **Fluent Bit**（开源 C agent，**主机侧唯一进程**） | 有状态(部署在主机侧) | 主机日志采集与转发：tail/systemd/syslog/http → Kafka / OpenSearch；**管控脚本随包部署、由 `exec` 插件调起，不另起进程**（替代原自研 Go 采集器） | DAT-01/02 |
| `alarm-flink-job` | Flink | 有状态 | 实时消费 Kafka → 匹配规则 → 触发告警 | ALM-02 |
| OpenSearch | 托管/容器 | 有状态 | 日志索引与检索 | 全局 |
| PostgreSQL | 托管/容器 | 有状态 | 主数据 | 全局 |
| Kafka | 托管/容器 | 有状态 | 日志流缓冲 | 全局 |
| Redis | 容器/托管 | 有状态 | 检索缓存/会话；Agent 心跳（本版） | 性能/DAT-02 |

> 微服务允许分服务选型，但本方案统一后端为 **FastAPI（Python）** 以复用 AI/数据生态（智能体、pandas 分析）；仅实时判定用 Flink，采集器改用开源 Fluent Bit（不再自研 Go 采集器）。

---

## 7. Docker 搭建思路（纯方案，待通知再执行）

### 7.1 隔离目标
- 本机只保留：Docker、编辑器、源码（bind mount 进容器）。
- 关在容器内：语言运行时、编译器、包管理器、依赖、热更工具、运行进程。
- 好处：本机干净、环境可复现、`dev ≈ prod` 同源镜像、一键 `docker compose down -v` 清理。

### 7.2 需产出的文件清单（与语言无关部分）

```
log_system/
├─ Dockerfile                 # 多阶段：builder(全工具链) → runtime(精简运行)
├─ docker-compose.yml         # dev / prod 两套 profile 或服务
├─ compose.override.yml       # 本地开发覆盖（挂源码、开端口、开热更）
├─ .dockerignore              # 排除 .git / 依赖目录 / .env / node_modules 等
├─ .env.example               # 配置与密钥分离，绝不写进镜像
├─ nginx.conf                 # 前端托管 + 反代后端 API（prod 用）
├─ .devcontainer/             # 可选：IDE 直接连容器开发
│   └─ devcontainer.json
└─ (各服务子目录自带 Dockerfile 或复用根 Dockerfile 多 target)
```

### 7.3 多阶段 Dockerfile 思路
- **builder 阶段**：装全工具链（Python/Node 构建依赖）、装依赖、构建前端产物（Vue `npm run build` → `dist/`）、跑后端依赖安装。**采集器已改用 Fluent Bit（官方镜像/二进制，无需在镜像内编译 Go 工具链）**；如后续自研管控面亦用 FastAPI，不引入 Go 构建链。
- **runtime 阶段**：用精简基础镜像（如 `python:3.11-slim` / `node:20-alpine` / `nginx:alpine`），只拷运行所需产物 + 依赖，**非 root 用户**运行，镜像尽量小、可移植。
- dev 与 prod 通过 `target` 选择：dev 用 builder（带热更），prod 用 runtime。

### 7.4 docker-compose 思路
- **dev profile**：应用服务源码 bind mount 进容器 + 开热重载（uvicorn `--reload` / vite dev server）+ 映射端口到本机浏览器；**数据层全部以 sidecar 容器拉起联调**：PostgreSQL、OpenSearch、Kafka、Flink（单机 `flink:1.18-scala_2.12` 或本地 standalone）、**Redis（本版用于检索缓存/会话与 Agent 心跳）**。此环境仅含开发/测试数据，不上生产。
- **prod profile**：镜像化应用服务（不带源码挂载、不带 dev 依赖）、Nginx 反代；**数据层改为云托管**（OpenSearch 服务 / RDS(PostgreSQL) / Kafka 实例 / Redis 实例），compose 内不再自建数据层（若需演示可自建但标注「仅演示」）；健康检查 + 重启策略。
- 两套编排不冲突：开发期容器化数据层联调，生产期数据层优先托管。

### 7.5 .dockerignore / 安全
- 排除 `.git`、`node_modules`、`__pycache__`、`*.pyc`、`.env`、`venv`、`dist`（构建期再生成）等；**绝不把密钥、`.env` 打进镜像**。
- 配置通过 `.env` + `compose.override.yml` 分离，敏感信息走密钥管理（生产用云 KMS/Secret）。

### 7.6 nginx.conf 思路（prod 前端 + 反代）
- 托管 Vue `dist/` 静态资源；`try_files` 支持 SPA history 路由回退到 `index.html`。
- `/api/` 反代到各 FastAPI 服务（或直接反代 API 网关）；WebSocket（如实时日志推流）单独 `upgrade` 配置。

### 7.7 .devcontainer（可选增强）
- 让 IDE 直接把容器当开发机，本机连语言运行时都不用装；`devcontainer.json` 指定镜像、挂载源码、自动装依赖、转发端口。

### 7.8 落地顺序建议（骨架不返工）
1. **先立静态前端骨架**：Nginx + 多阶段 Dockerfile + compose dev/prod + .dockerignore，把现有原型直接托管进容器跑通 dev。
2. 逐步加 `auth-service` / `search-service` 等 FastAPI 容器，接 PostgreSQL。
3. 引入 OpenSearch / Kafka / Flink sidecar，把检索与实时告警跑通。
4. 引入 **Fluent Bit** 完成日志采集配置接入 Kafka / OpenSearch；`agent-service`（服务端管控面）+ **随包管控脚本（exec 插件调起，不另起进程）** 打通 Agent 管理（心跳/配置拉取/重启/批量升级），完成 DAT-01/02。

---

## 8. 待拍板 / 风险清单（决策追踪）

| # | 项 | 状态 | 结论 |
|---|----|------|------|
| 1 | 前端框架 | ✅ 已确认 | **Vue3**（放弃 React 备选） |
| 2 | PostgreSQL 必选 | ✅ 已确认 | 必带，承载全部结构化主数据 |
| 3 | 多租户隔离粒度 | ✅ 已确认 | **行级 + 租户 ID（tenant_id）**，鉴权中间件统一注入过滤 |
| 4 | 角色权限模型 | ✅ 已确认（角色）/ ⚠️ 待细化（参数·审计 PRD） | 三类角色：超级管理员 / 运维人员 / 查看人员（详见 3.2(9)） |
| 5 | AI 智能体模块 | ✅ 已确认（2026-11-14 更新） | **配置中心与 NL→PPL 生成端点均已引入**：独立 `ai-service` + 前端「智能管理」页（模型接入两页签：商用 API / 本地自定义；PPL 语法规则库 / 能力注册表 / 智能检索预览）；检索页「智能检索」弹窗已接入 |
| 6 | 生产数据层部署 | ✅ 已确认 | 开发期全容器内；上线后数据层云托管 |
| 7 | Redis / ClickHouse 引入 | ✅ 已确认 / ✅ v2.0 已落地 | **Redis 本版引入**（检索缓存/会话 + Agent 心跳）；**ClickHouse v2.0 落地为聚合引擎**（与 OS 双引擎，见第 10 章） |
| 9 | Kafka/Flink/ClickHouse 从占位转正式链路 | ✅ v2.0 已完成 | ingest 写 Kafka（幂等 producer）→ Flink Java Job 消费双写 OS/CH → AGG_ENGINE 切换查询；端到端 + 幂等 + 故障恢复均已验证 |
| 8 | 采集器选型 + 主机部署约束 | ✅ 已确认 | **采用开源 Fluent Bit 替换原自研 Go 采集器**；**业务主机仅部署单一 Fluent Bit 进程**，管控客户端以随包脚本（exec 插件）+ 配置热重载承载，不另起 Supervisor；Agent 管理（管控面）评估为中等工作量、**纳入一阶段** |

> 未关闭项：系统参数的具体项、审计日志的字段与保留期（不影响一阶段开发，补 PRD 即可）。

---

## 9. 后期可引入组件（Roadmap，避免遗忘）

本版（一阶段）**明确不引入**、但技术演进或业务增长后大概率需要的组件，集中记录以免遗漏。任一组件转入「本版引入」时，同步更新第 2、3、5、6、7 章并勾掉本表对应行。

| 组件 | 引入触发条件 / 场景 | 关联模块 | 备注 |
|------|---------------------|----------|------|
| **AI 智能体模块（运行时）** | 需要实现日志智能问答、根因分析对话、自然语言转检索等 | 独立 `ai-service`（FastAPI + LLM）；**不叫 agent-service**，该名已被采集端管控服务占用 | 配置中心与 `POST /nl2ppl` 生成端点已于 2026-11-14 落地（能力 `nl2ppl` 状态 `available`）；检索页「智能检索」弹窗已于 2026-11-14 接入 |
| **SPL → PPL 转换层** | 需兼容日志易式 SPL 历史查询、固化视图，或前端仍想保留 SPL 写法 | `search-service` 检索网关 | **本版（一阶段）不做**：直接采用 OpenSearch PPL，前端检索框按 PPL 语义；转换层仅作为后续兼容增强评估，非必需 |
| ~~**ClickHouse**~~ | ~~日志量或聚合维度复杂到 OpenSearch 吃力~~ | 检索 / 总览 / 仪表盘加速 | **已在 v2.0 引入，移出后期清单（见第 10 章）** |
| **对象存储 OSS/S3** | 原始日志归档、合规留存、降低 OpenSearch 存储成本 | 采集管道、归档策略 | 生产必备，原型未涉及 |
| **Elasticsearch（替代 OpenSearch）** | 仅在生态强绑定 ES 时才考虑 | 检索层 | 默认 OpenSearch（开源分支、信创更友好），一般无需切换 |
| **服务网格 / K8s Ingress** | 微服务规模扩大、需细粒度流量治理 | 部署层 | 云上微服务化后期标配 |
| **告警通知渠道扩展** | 邮件 / 短信 / 钉钉 / 企业微信 / Webhook 等 | ALM-02 `notify` 字段 | 规则表已预留 `notify` 字段 |
| **多集群 / 异地容灾** | 国网高可用、等保要求 | 数据层、部署层 | 生产云托管自带部分能力 |

### 9.1 采集层选型结论（Fluent Bit 已采纳，非后期项）
- **决策**：放弃原自研 Go 采集器，采用开源 **Fluent Bit**（CNCF 毕业项目，C 语言，<1MB、10–30MB RAM，信创友好、可静态编译移植到国产 OS/ARM）作为主机侧采集 agent。
- **可覆盖的采集对象与下游**：文件 `tail`、systemd、syslog、http/tcp/udp、K8s、Windows 事件等；下游原生对接 **Kafka / OpenSearch / 对象存储**（`out_kafka` / `out_opensearch` 等插件），覆盖本平台所需的全部采集与接入（对应原型 DAT-0501 四类接入）。
- **管控面自研（本版纳入一阶段，且主机不另起进程）**：Fluent Bit **不提供**集中管控面（心跳采集 / 配置下发 / 灰度升级），对应原型 `agents.html` 的 Agent 管理模块（DAT-0502）**本版一并纳入**。落实方式满足"主机只部署一个 Fluent Bit"约束：**管控客户端不单独成守护进程**，而是随 Fluent Bit 安装包一同分发的「管控脚本」，由 Fluent Bit 的 **`exec` 输入插件**按 `interval_sec` 定时调用，在 Fluent Bit 进程内完成①心跳上报②配置/版本轮询③接收重启/升级指令④执行断点续传升级；配置下发靠 Fluent Bit **配置热重载**（`SIGHUP` 或 `POST /api/v2/reload`）无需重启。服务端仍以 FastAPI + PG + Redis 心跳做 `agent-service`。**部署约束**：`exec` 插件需 `/bin/sh`，Fluent Bit 镜像不得用 distroless（须 Debian/Ubuntu/Alpine 等带 shell 镜像）；K8s 场景升级走 DaemonSet 滚动更新、无需脚本自升级，脚本自升级路径仅面向裸金属/systemd 主机。**⚠️ 实测补充（2026-11-12）**：fluent/fluent-bit:5.2.0 官方 distroless 镜像**未编译 `exec` 输入插件**（仅含 `exec_wasi`，无法以 sh 调起管控脚本），故开发环境心跳改为 **dummy 输入 + http 输出原生上报** agent-service（等效语义、零额外进程）；若生产环境坚持用 `exec` 调 `control.sh`，须改用含 `exec` 插件的 Fluent Bit 构建（非官方 distroless 版）。
- **本版即做**：① Fluent Bit 的**部署与配置接入 OpenSearch**（主机侧以 systemd 服务 / K8s DaemonSet 部署，`tail`+`systemd` 输入 → `out_opensearch` 输出，`tenant_id` 经 `record_modifier` 打标），使日志流从业务主机直达 OpenSearch 可检索；② `agent-service` + 随包管控脚本打通心跳上报/离线判定/配置拉取/远程重启（全程单进程）；③ 批量升级做基础版（服务端滚动调度 + 断点续传下载替换），"零中断极致编排"作后续优化。
- **自研 Go 采集器能力对照**：Fluent Bit 可覆盖本平台实际场景子集且生态成熟；完全自研难以短期达到其十年级生产打磨水位，且需永久承担维护债。仅在出现 Fluent Bit 覆盖不了的私有协议源时，再在其前后挂轻量 Go 服务做二次处理（非二选一）。

---

## 10. 双引擎 + 流处理架构（v2.0 已落地，2026-11-19）

> 本节记录 Kafka/Flink/ClickHouse 从"设计占位"转为"实际运行链路"的架构改造，对标云智慧 DOLA 的"Kafka + Flink + ClickHouse/ES 双引擎"。目标：真正商用级的高性能、高可靠日志平台，而非演示系统。

### 10.0 改造总结（TL;DR）

| 维度 | 改造前（v1.x） | 改造后（v2.0） |
|------|------|------|
| 写入链路 | Fluent Bit → ingest → **直写 OpenSearch**（单引擎） | Fluent Bit → ingest → **Kafka 主干** → Flink → ClickHouse + OpenSearch（双引擎）+ DLQ |
| Kafka | 占位：仅 topic 管理，日志不经过 | **数据主干**：幂等 producer（acks=all、key=tenant）→ 6 分区 raw topic |
| Flink | 占位：空 JM/TM，无作业 | **正式 Job**：Java DataStream 消费 → 清洗富化 → 三路 sink（CH / OS / DLQ），checkpoint EXACTLY_ONCE |
| 聚合查询 | OpenSearch 单引擎 | **ClickHouse 聚合**（`AGG_ENGINE=ch`），失败自动回退 OS；明细/PPL/上下文仍走 OS |
| 幂等与可靠性 | 无端到端幂等 | Kafka 幂等生产 + Flink checkpoint 恢复 + CH ReplacingMergeTree(event_id) + OS `_id` 覆盖 + DLQ 保底 |
| 运行容器 | 18 个 | **19 个**（新增 ClickHouse；另加 kafka-init / flink-init 两个一次性初始化容器） |
| 定位 | 功能演示 | 对标云智慧 DOLA 的商用级链路（已端到端 + 幂等 + 故障恢复验证） |

> 一句话：把"日志直接写检索库"升级为"**Kafka 解耦 + Flink 流处理 + 双引擎分工**"，写入幂等、故障可恢复、查询可降级，具备上生产的骨架。新增的模块/组件/依赖/开关/文件见 **10.8**，服务地址与端口总表见 **10.9**。

### 10.1 数据主干拓扑

```
业务主机 Fluent Bit（单进程）
   │ tail/systemd/syslog → tenant 来源打标
   ▼ HTTP /ingest/collector
ingest-service（鉴权 / 租户判定 / 时间归一化 / event_id 生成）
   │ Kafka 幂等 producer（acks=all, enable.idempotence, key=tenant_id）
   ▼ topic: ruilog.logs.raw（6 分区）
Flink 1.18 Java DataStream Job（checkpoint EXACTLY_ONCE / 10s）
   ├─ 解析 + 富化（ts 归一化、snake_case 字段映射）
   ├─ sink1 → ClickHouse ruilog.logs（ReplacingMergeTree(ver)，event_id 幂等）
   ├─ sink2 → OpenSearch logs-<tenant>（FLINK_WRITE_OS=true 时，_id=event_id 覆盖）
   └─ sink3 → Kafka ruilog.logs.dlq（解析失败死信，不丢数据）
```

写入模式开关 `INGEST_STORAGE_MODE`：
- `direct`：旧链路，ingest 直写 OS（回滚用）
- `dual`（当前默认，过渡）：ingest 同时写 OS 与 Kafka，前端查询无感
- `kafka`（稳态目标）：ingest 只写 Kafka，Flink 统一写 OS+CH（配 `FLINK_WRITE_OS=true`）

### 10.2 双引擎分工铁律

| 查询类型 | 引擎 | 原因 |
|----------|------|------|
| 明细检索 `/query`、日志详情、上下文 `/context` | OpenSearch | 全文/倒排、按 ID/filter 取明细 |
| 用户自由 PPL `/ppl` | OpenSearch | **PPL 是 OS 专属语言，CH 不支持**——不推翻"直接 PPL、无转换层"决策 |
| 总览 `/overview`、仪表盘聚合、直方图、级别分布、P95 | ClickHouse | 后端写死 SQL、前端参数驱动，无 PPL 兼容问题，列式聚合快 |
| 检索耗时洞察 `top_queries-*` | OpenSearch | OS query insights 自产数据，CH 无对应 |

查询开关 `AGG_ENGINE=ch|os`：**CH 失败自动回退 OS**，保证可用性；明细/PPL/上下文永远走 OS，不受开关影响。

### 10.3 ClickHouse 表设计（`deploy/shared/clickhouse/init/01-schema.sql`）

| 表 | 引擎 | 用途 |
|----|------|------|
| `ruilog.logs` | ReplacingMergeTree(ver) | 明细宽表，`ORDER BY (tenant_id, ts, event_id)`，TTL 180 天 |
| `ruilog.logs_minute_agg` + `mv_logs_minute` | SummingMergeTree + 物化视图 | 分钟级预聚合（count/err/warn/5xx），总览趋势直接查它 |
| `ruilog.logs_dlq` | MergeTree | 解析失败死信，TTL 30 天 |

- **租户隔离**：`tenant_id` 为排序键前缀 + 查询必带过滤，与 OS 的 `logs-<tenant>` 物理隔离语义一致（行级 + 租户 ID）。
- **时间语义**：`DateTime64(3,'UTC')`，值来自归一化后的 `@timestamp`，严禁用 Fluent Bit 的 `date`（epoch 秒）。TTL 表达式须 `toDateTime(ts)`（CH 24.x 不接受 DateTime64 直接做 TTL）。
- **幂等**：确定性 `event_id`（tenant|ts|traceId|source|host|message 的 SHA1）+ ReplacingMergeTree，重放/恢复不产生重复计数（查询端 `FINAL` 触发去重）。

### 10.4 可靠性机制（商用级）

| 机制 | 实现 | 验证 |
|------|------|------|
| 生产端 exactly-once | confluent-kafka 幂等 producer + acks=all + delivery callback | `kafkaWriter.health` 显示 delivered 递增、failed=0 |
| 消费端不丢 | KafkaSource `committedOffsets(EARLIEST)`（无位点回退 earliest），checkpoint EXACTLY_ONCE 10s 写挂载卷 | 作业重启后从已提交位点续读，不重放、不重复 ✅ |
| 写入幂等 | CH ReplacingMergeTree ORDER BY (tenant_id, ts, event_id) + OS `_id=event_id` | 重放同 event_id，FINAL 计数不变 ✅ |
| **双引擎口径一致** | **sink 三条件刷盘**：缓冲满 `FLINK_SINK_BATCH`(200) / 距上次刷盘超 `FLINK_SINK_FLUSH_MS`(5s) / **checkpoint 前 `snapshotState()` 强制 flush** | CH 落后上游 ≤5s；同窗口 CH 与 OS 计数逐租户一致 ✅ |
| 故障恢复 | restart-strategy fixed-delay + 从 checkpoint 恢复 | TM 重启后 Job 恢复、CH 计数只增不减 ✅ |
| 数据不丢 | 解析失败写 DLQ，主流不中断 | — |
| 查询降级 | AGG_ENGINE=ch 失败自动回退 OS | — |

> 注 1：`kafka-python` 不支持幂等 producer，商用链路改用 **confluent-kafka（librdkafka）**——这是从"能跑"到"可靠"的关键选型修正。
>
> 注 2（2026-11-19 实战踩坑）：sink 早期**只在缓冲区攒满 BATCH_SIZE 时刷盘**，没有 checkpoint 钩子。低流量租户约 20 条/分钟，意味着数据要在内存里滞留 **10 分钟以上**才落库，表现为"检索（OS）能查到、概览（CH）查不到"，两个引擎数字对不上；作业重启时未刷盘的缓冲区还会丢。修复方式：`ClickHouseSink` / `OpenSearchSink` 实现 `CheckpointedFunction`，在 `snapshotState()` 中强制 flush，并叠加 5s 时间兜底。**结论：凡是自研批量 sink，必须把"刷盘"与 checkpoint 绑定，不能只依赖批量阈值。**

### 10.5 落地文件清单

| 文件 | 作用 |
|------|------|
| `deploy/shared/clickhouse/init/01-schema.sql` | CH 库/表/物化视图初始化（entrypoint 自动执行） |
| `deploy/shared/scripts/kafka/init-topics.sh` | topic 幂等初始化（raw 6 分区 / dlq 3 分区） |
| `services/ingest-service/app/kafka_writer.py` | 幂等 Kafka producer + 模式开关 |
| `services/dashboard-service/app/ch_client.py` | CH HTTP 聚合客户端（count/terms/histogram + 回退） |
| `flink-jobs/java/` | Flink DataStream Job（pom + LogPipelineJob.java + Dockerfile + 提交脚本） |
| `flink-jobs/lib/` | Kafka connector + kafka-clients（注入 Flink 镜像 lib） |
| `tools/backfill-to-clickhouse.py` | OS→CH 历史回填（scroll + 幂等写入，dry-run 支持） |
| `docker-compose.yml` / `.env` | CH/kafka-init/flink-init 编排 + 模式开关配置 |

### 10.6 端到端验证结果（2026-11-19）

- ✅ Kafka 消息结构：`tenant_id` 服务端判定、`@timestamp` UTC 毫秒归一化、`event_id` 确定性哈希、key=tenant 分区
- ✅ Flink→CH：600+ 条历史日志按租户正确落表（acme 200 / gp 239 / default 141 / unknown 20），字段完整、空 event_id=0
- ✅ 物化视图预聚合：级别分布 ERROR/WARN/INFO 计数与明细一致
- ✅ 双引擎查询：dashboard `/overview` 在 `AGG_ENGINE=ch` 下写入量/级别分布与 CH 数据完全吻合
- ✅ 幂等：重放同 event_id 后 FINAL 计数不变
- ✅ 故障恢复：TM 重启→Job 自动恢复→CH 数据只增不减
- ✅ 回填脚本：dry-run 扫描 26550 条 acme 历史日志正常

### 10.7 后续演进（不影响当前可用）

- **告警判定迁移到 Flink 窗口算子**：当前告警仍由 alarm-service 对 OS/CH 请求时聚合判定；真正的流上实时判定（滚动窗口 + 阈值）列为下一步。
- **稳态切换**：验证期结束后 ingest 切 `INGEST_STORAGE_MODE=kafka` + `FLINK_WRITE_OS=true`，OS 明细也由 Flink 统一写，ingest 彻底解耦。
- **生产云托管**：CH/Kafka/OS 生产走云托管实例；Flink 用 checkpoint 存对象存储 + savepoint 自动恢复策略。
- **回填全量历史**：`tools/backfill-to-clickhouse.py --all --days 180` 补齐上线前历史。

### 10.8 本次改造新增 / 变更清单（模块 · 组件 · 服务 · 依赖 · 开关 · 文件）

> 集中列出 v2.0 改造相比 v1.x 的全部增量，便于Review、部署核对与回滚。

#### （1）新增基础组件（Docker 服务）

| 组件 | 容器名 | 镜像 | 版本 | 角色 | 端口（宿主←容器） |
|------|--------|------|------|------|------|
| **ClickHouse** | `log_system-clickhouse-1` | `clickhouse/clickhouse-server` | 24.3 | 聚合引擎（双引擎之一），明细宽表 + 分钟级预聚合 | `8123←8123`（HTTP）、`9000←9000`（Native） |
| **Kafka** | `log_system-kafka-1` | `bitnami/kafka` | 3.7 | 数据主干（v1.x 已在编排但仅占位，v2.0 真正承载日志流） | `9092←9092` |
| **Flink JobManager** | `log_system-flink-jobmanager-1` | `log_system-flink:1.18`（自建，基于 `flink:1.18-scala_2.12`） | 1.18 | 作业调度 / checkpoint 协调 / Web UI | `8081←8081` |
| **Flink TaskManager** | `log_system-flink-taskmanager-1` | 同上 | 1.18 | 执行 DataStream Job（2 slots） | 仅容器内（`6123`、`8081`） |
| `kafka-init`（一次性） | `log_system-kafka-init-1` | `bitnami/kafka:3.7` | 3.7 | 幂等创建 `ruilog.logs.raw`(6 分区) / `ruilog.logs.dlq`(3 分区)，跑完即退出 | 无 |
| `flink-init`（一次性） | `log_system-flink-init-1` | `flink:1.18-scala_2.12` | 1.18 | 修正常量挂载卷属主为 `flink:flink`（否则 JM 无法建 checkpoint 目录），跑完即退出 | 无 |

> 自建镜像 `log_system-flink:1.18` = 官方 `flink:1.18-scala_2.12` + `flink-jobs/lib/` 下的 Kafka connector / kafka-clients（官方镜像不含 connector）。因 arm64 本机无 JDK、镜像无 Python，Java Job 用 Maven 容器构建。

#### （2）新增 / 改造代码模块

| 文件 | 归属服务 | 作用 |
|------|----------|------|
| `services/ingest-service/app/kafka_writer.py` | ingest-service | **新增**：`confluent_kafka.Producer` 幂等写入（acks=all、enable.idempotence、key=tenant_id），替换不支持幂等的 `kafka-python` |
| `services/ingest-service/app/main.py` | ingest-service | **改造**：`/ingest`、`/ingest/collector` 按 `INGEST_STORAGE_MODE` 走 direct/dual/kafka；新增 `tenant_from_index()`；health 暴露 storageMode / kafkaWriter |
| `services/dashboard-service/app/ch_client.py` | dashboard-service | **新增**：ClickHouse HTTP 聚合客户端（count / terms / histogram + 失败回退 OS） |
| `services/dashboard-service/app/main.py` | dashboard-service | **改造**：`count_docs()` / `terms_on()` 支持 `AGG_ENGINE=ch\|os` 分支；总览写入趋势/量、告警级别分布走 CH |
| `deploy/shared/clickhouse/init/01-schema.sql` | ClickHouse | **新增**：`ruilog.logs`(ReplacingMergeTree) + `ruilog.logs_minute_agg`/`mv_logs_minute`(SummingMergeTree+物化视图) + `ruilog.logs_dlq`，entrypoint 首次启动自动执行 |
| `deploy/shared/scripts/kafka/init-topics.sh` | kafka-init | **新增**：幂等建 topic（raw 6 分区 / dlq 3 分区） |
| `flink-jobs/java/pom.xml`、`src/.../LogPipelineJob.java` | Flink Job | **新增**：Java DataStream 主程序（解析富化 → CH/OS/DLQ 三 sink + snake_case 显式字段映射 + ISO 时间归一化） |
| `flink-jobs/Dockerfile`、`submit-job.sh`、`lib/*.jar`、`settings.xml` | Flink Job | **新增**：自建镜像、`flink run -D` 提交脚本、Kafka connector jar、Maven 阿里云 mirror 配置 |
| `tools/backfill-to-clickhouse.py` | 运维工具 | **新增**：OS → CH 历史回填（scroll + 幂等写入 + dry-run） |
| `scripts/validate-chain.sh` | 运维工具 | **新增**：全链路验证 T1–T6（容器内网 curl + Kafka 自检） |

#### （3）新增 Python 依赖（`services/requirements.txt`）

| 依赖 | 版本 | 用途 |
|------|------|------|
| `confluent-kafka` | 2.3.0 | 幂等 producer（商用可靠性关键选型，`kafka-python` 不支持）；Flink 侧由 connector 提供 |
| `clickhouse-driver` | 0.2.7 | dashboard-service 访问 ClickHouse（本项目用其 HTTP 接口） |

#### （4）新增 / 变更配置开关（`.env` / `.env.example`）

| 变量 | 当前值 | 含义 |
|------|--------|------|
| `KAFKA_BROKER` | `kafka:9092` | Kafka broker 内网地址 |
| `KAFKA_TOPIC_RAW` / `KAFKA_TOPIC_DLQ` | `ruilog.logs.raw` / `ruilog.logs.dlq` | 主流 / 死信 topic |
| `FLINK_JOBMANAGER` | `flink-jobmanager:8081` | Flink JM 内网地址 |
| `CLICKHOUSE_HOST` / `_PORT` / `_NATIVE_PORT` | `clickhouse` / `8123` / `9000` | CH 连接（HTTP / Native） |
| `CLICKHOUSE_DB` / `_USER` / `_PASSWORD` | `ruilog` / `ruilog` / `ruilogpass` | CH 库与账号 |
| `INGEST_STORAGE_MODE` | `dual` | 写入模式：`direct`(回滚) / `dual`(当前过渡，写 OS+Kafka) / `kafka`(稳态，只写 Kafka) |
| `AGG_ENGINE` | `ch` | 聚合引擎：`ch`(ClickHouse) / `os`(OpenSearch 回退) |
| `FLINK_WRITE_OS`（提交作业参数） | `false` | 是否由 Flink 写 OS 明细：dual 阶段 false（OS 仍由 ingest 写），稳态切 true |

#### （5）新增持久化卷

| 卷 | 用途 |
|----|------|
| `chdata` | ClickHouse 数据目录 |
| `kafkadata` | Kafka 数据目录 |
| `flinkckpt` | Flink checkpoint（EXACTLY_ONCE 状态，重启不丢） |
| `flinksp` | Flink savepoint |

### 10.9 服务地址与端口总表（v2.1 现状，20 个常驻容器）

> 全部容器在 `log_net`（bridge）内网，宿主机映射端口仅供本地调试；生产环境数据层改云托管、不对外映射。

| 分层 | 服务 / 容器 | 内网地址:端口 | 宿主映射端口 | 说明 |
|------|-------------|---------------|--------------|------|
| 数据层 | PostgreSQL | `postgres:5432` | 5432 | 主数据 |
| 数据层 | OpenSearch | `opensearch:9200` | 9200 / 9600 | 明细 / 全文 / PPL（安全插件已关） |
| 数据层 | OpenSearch Dashboards | `opensearch-dashboards:5601` | 5601 | 可视化（免登录）；经 web `/osd/` 反代 |
| 数据层 | Redis | `redis:6379` | 6379 | 缓存 / 会话 / Agent 心跳 |
| 数据层 | **Kafka** | `kafka:9092` | 9092 | 数据主干（raw / dlq topic） |
| 数据层 | **ClickHouse** | `clickhouse:8123`（HTTP）/ `:9000`（Native） | 8123 / 9000 | 聚合引擎（双引擎之一） |
| 流处理 | **Flink JobManager** | `flink-jobmanager:8081` | 8081 | Web UI / 作业提交 / checkpoint 协调 |
| 流处理 | **Flink TaskManager** | `flink-taskmanager:6123` | 无 | 执行节点（2 slots） |
| 应用层 | web（Nginx + Vue3） | `web:80` | **8080** | 前端 + 统一反代网关 |
| 应用层 | auth-service | `auth-service:8000` | 8001 | 登录 / JWT / 权限 |
| 应用层 | search-service | `search-service:8000` | 8002 | 日志检索（PPL） |
| 应用层 | dashboard-service | `dashboard-service:8000` | 8003 | 仪表盘（含 CH 聚合） |
| 应用层 | alarm-service | `alarm-service:8000` | 8004 | 告警 |
| 应用层 | ingest-service | `ingest-service:8000` | 8005 | 日志接入（`/ingest`、`/ingest/collector`、Kafka writer） |
| 应用层 | agent-service | `agent-service:8000` | 8006 | Agent 管控面 / 心跳 |
| 应用层 | system-service | `system-service:8000` | 8007 | 平台管理（用户 / 角色 / 租户 / 参数 / 审计） |
| 应用层 | ai-service | `ai-service:8000` | 8008 | AI 能力配置 / NL→PPL |
| 采集层 | Fluent Bit | `fluentbit:2020` | 无 | 主机侧唯一采集进程 → `POST /ingest/collector` |
| 测试 | log-generator | — | 无 | 模拟多租户业务日志，写共享卷 `app_logs` |
| 测试 | sys-sim | — | 无 | **唯一保留的模拟容器**：busybox syslogd 真实落盘系统日志（约 1MB 内存） |

**常驻容器合计 20 个**（v2.0 的 19 个 + 系统日志源 `sys-sim`；中间件与数据库日志采自平台自身的 web/redis/kafka/postgres，不额外占容器）；另有 `kafka-init` / `flink-init` 两个一次性容器（`restart: "no"`，跑完退出，不计入常驻）。

**常用浏览器入口**：前端 `http://localhost:8080`（需登录）、OpenSearch Dashboards `http://localhost:5601`、**Flink Web UI `http://localhost:8081`**、ClickHouse HTTP `http://localhost:8123`、各微服务 Swagger `http://localhost:<8001–8008>/docs`、健康检查 `<...>/health`。

> nginx 反代前缀：`/api/auth|search|dashboards|alarms|data|agents|system|ai/` → 对应服务 `:8000`；`/osd/` → opensearch-dashboards:5601。用 `resolver 127.0.0.11 valid=10s` + 变量 `proxy_pass` 做运行期 DNS 解析，避免后端容器重建换 IP 后串服务 / 502（曾踩坑，见 `container-deployment.md` 3.6）。

---

### 10.10 v2.1 多类型日志采集（系统 / 中间件 / 数据库 / 业务，2026-11-19 已落地）

v2.0 解决的是"日志怎么流、存哪、查得快"；v2.1 解决的是"**采集面够不够宽**"——原来只接业务应用日志，系统日志、中间件日志、数据库日志一条都没采，对标日志易/云智慧的全栈日志纳管是硬缺口。

完整契约与踩坑见 **`docs/log-ingestion-model.md`**，此处只记架构级结论：

| 维度 | 结论 |
|------|------|
| 索引策略 | **单索引 + `log_class` 字段**（不按类型拆索引）。理由：跨类型关联排查是主场景，拆索引等于每次都要 fan-out；`log_class` 作为 keyword 维度，聚合/筛选性能足够 |
| 类型枚举 | `system` / `middleware` / `database` / `business`（预留 `network` / `container`） |
| 来源类型 | `syslog` `auth` `kern` `cron` `nginx-access` `nginx-error` `redis` `mysql-error` `mysql-slow` `postgres` `app` |
| 采集端 | 单个 Fluent Bit，多 input + tag 分族（`ruilog.<log_class>.<source_type>`），**采集端静态打标**，网关只做校验与补充 |
| 租户判定 | 四级优先级：显式 `tenant_id` > **`hosts` 表按 host 查（v2.1 新增）** > `src_file` 文件名 > `logs-unknown`。`hosts` 表把"主机 → 租户"从文件名里解耦，系统/中间件/数据库日志本来就没有租户信息，只能靠 CMDB 归属 |
| 级别归一 | syslog severity 数字与词、redis 字符、HTTP status 码统一映射到 `DEBUG/INFO/WARN/ERROR/FATAL`（`ingest-service/app/log_schema.py`） |
| 脱敏 | 网关侧对 `message`/`attrs` 做 5 组正则打码（password / token / bearer / URL 凭证），对应云智慧"采集阶段脱敏" |
| 存储 | OS 侧 `attrs` 用 **`flat_object`**（OpenSearch 无 ES 的 `flattened`）；CH 侧新增 7 列 + 物化视图加 `log_class` 维度 |
| 贯通 | 检索（筛选/列/语法）、看板（维度/配色/总览卡）、告警（`match_log_class` + 9 条模板）、智能检索（`BASE_FIELDS` + 业务词）四路全部打通 |

**验证数据（真实进程产真格式日志，非脚本伪造）**：

| 类型 | 采集源 | 日志如何落地 |
|------|--------|-------------|
| system | `sys-sim`（唯一保留的模拟容器，busybox syslogd） | `ruilog_logs/syslog` |
| middleware | **平台自身的 `web`(nginx) / `redis` / `kafka`** | nginx 显式 log / redis `--logfile` / kafka log4j `RollingFileAppender` |
| database | **平台自身的 `postgres`** | `logging_collector=on` + 相对 `log_directory` |
| business | `log-generator` | 原生多租户业务日志文件 |

### 10.10.1 二次调整：从"造模拟容器"改为"采平台自身组件"（同日晚间）

首版为四类日志各起了模拟容器（共 7 个 + 1 个 init），跑通后暴露出两个问题：**资源代价不合理**（仅 `mysql-sim` 常驻 370MB，整批约 410MB）与**真实性冗余**（平台的 nginx/redis/postgres/kafka 本身就是真进程，再模拟一遍没有增量价值）。

调整原则：**能采自己的就采自己，只有采不到的才起容器。**

| 项 | 调整前 | 调整后 |
|----|--------|--------|
| 常驻容器 | 26 | **20** |
| 模拟容器内存 | 约 410MB | **0.9MB**（仅 `sys-sim`） |
| 镜像占用 | 14.19GB | **13.4GB**（删 `mysql:8.0` 795MB） |
| 系统日志 | `sys-sim` | **保留**——容器共享宿主内核，读不到宿主 syslog/journald，kernel/auth/cron 无替代来源 |
| MySQL | `mysql-sim` + `mysql-traffic` | **不部署**，解析器与规则模板保留为"按需启用"（接入真实 MySQL 时取消注释） |

采集侧的 7 个落地坑（软链到 stdout / 非 root 写不了卷 / bitnami 只写 stdout / PG 相对路径 / PG 时区 / YAML 吃尾随空格 / Kafka 启动配置 dump）见 `docs/log-ingestion-model.md` 7.1。

**验证**：四类日志增量在 3 分钟窗口内 OS 与 CH **完全相等**（system 369、middleware 4、business 47、database 2），Flink 作业 RUNNING，`log_class`/`source_type`/`attrs` 全部正确落库。

**这一轮最有价值的两个教训**（都属"接口 200 但数据是错的"，比报错更危险）：

1. **CH `terms_on` 只认 4 个字段，其余静默换成 `level`** —— 总览「日志类型分布」直接变成级别分布且毫无异常。已改为白名单 + 抛异常回退 OS。
2. **OS `terms` 聚合默认丢弃缺字段文档** —— 图里各类之和小于总量。已加 `"missing": ""` 落桶并标注「未分类（改造前）」。

> 产品原则：**宁可显示"未分类"，不可猜一个分类塞进去**。历史数据的分类补正属于数据治理动作，需单独决策（按 host/source 确定性映射，或整批清理），不混在功能改造里做。

---

---

## 附：原型功能点索引（备查）

| 规格 ID | 页面 | 路由 |
|---------|------|------|
| P-AUTH-0100 | 登录 | /login |
| P-OVW-0101 | 运行总览 | /overview |
| P-LOG-0201 | 日志检索 | /search |
| P-DSH-0301 | 仪表盘（列表） | /dashboards |
| P-DSH-0302 | 仪表盘（详情） | /dashboards/core-business |
| P-ALM-0401 | 告警事件 | /alarms/events |
| P-ALM-0402 | 告警规则 | /alarms/rules |
| P-DAT-0501 | 数据源 | /data-sources |
| P-DAT-0502 | Agent 管理 | /agents |
| P-SYS-0601 | 用户与角色 | /settings/users |

---
