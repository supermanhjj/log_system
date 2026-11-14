# 统一日志分析平台 —— 技术栈选型与方案设计（功能点 × 技术栈映射、检索语言 PPL 方案）

> 文档状态：技术栈选型与方案设计基线（v1.5，已纳入 2026-11-11 确认的架构决策：采集层采用 Fluent Bit 替换自研 Go 采集器、Agent 管控面纳入一阶段；**关键约束：业务主机仅部署单一 Fluent Bit 进程，管控逻辑以随包管控脚本 + 配置热重载承载，不另起 Supervisor 守护进程**；检索语言本版直接采用 OpenSearch PPL、不做 SPL→PPL 转换层）。本文件聚焦**技术选型与方案设计**（第 1–9 章 + 功能点索引）；**已落地的 Docker 骨架、构建镜像与运行服务现状另见 `container-deployment.md`**。
> 关联资产：`prototype/`（高保真原型，10 个页面规格 + `agents.html`）、`prototype/specs/*.json`
> 适用范围：本仓库 `log_system` 的后续开发环境与云上微服务部署

---

## 1. 文档目的与范围

本文件回答三个问题，并作为后续容器化落地的设计基线：

1. **功能点 × 技术栈映射**：原型 10 个页面定义的所有功能点，能否被选定的技术栈完整覆盖，逐模块对齐。
2. **检索语言方案**：本版直接采用 OpenSearch 原生 **PPL** 作为检索语言，不构建 SPL→PPL 转换层（转换层列入第 9 章后期可选）。
3. **Docker 搭建思路**：开发环境 + 云上部署如何隔离、要产出哪些文件、每个文件的设计要点（**仅方案，不执行**）。

> ⚠️ 一个必须先讲清的前提：原型里的 `agents.html`（路由 `/agents`）是**采集器 Agent 管理**（主机列表/心跳/滚动升级），**不是 AI 智能体对话页面**。本仓库原型目前**未定义任何 AI 智能体对话功能点**。技术栈方案里提到的「智能体服务 / AI 智能体模块」属未来扩展方向，原型阶段未引入；**2026-11-14 起新增独立配置中心**（独立 `ai-service` + 前端「智能管理」页，属新增功能而非原型功能点），仅承载模型接入 / PPL 语法规则库 / 能力注册表配置，**运行时 AI 能力（自然语言转 PPL 生成）仍待接入**，见第 8、9 章。

---

## 2. 技术栈总览（已确定 + 必要的补充）

### 2.1 已确定部分（用户拍板）

| 层 | 选型 | 角色 |
|----|------|------|
| 前端 | **Vue3**（已确认，放弃 React 备选） | 全部页面 UI，承接现有原型交互 |
| 采集器 | **Fluent Bit**（开源 C agent，已确认替换原自研 Go 采集器） | 主机侧**唯一部署组件**（单进程）；日志采集与转发，原生对接 Kafka / OpenSearch，信创友好、极轻量；**管控逻辑以随包附带的管控脚本（由 Fluent Bit `exec` 输入插件定时调用）+ 配置热重载承载，主机侧不另起独立进程**，服务端 `agent-service`(FastAPI+PG+Redis) 本版一并纳入，见 3.2(8) |
| 检索引擎（主） | **OpenSearch**（已确认） | 日志存储、全文检索、聚合分析（ES 的开源分支，信创友好） |
| 实时计算 | **Flink**（已确认） | 日志流实时处理：告警实时判定、趋势计算 |
| 消息队列 | **Kafka**（已确认） | 采集与消费解耦、缓冲（日志易/云智慧共同选择） |
| 关系型主数据 | **PostgreSQL**（已确认必选） | 用户/租户/角色/规则/数据源/仪表盘/系统参数/审计等结构化主数据 |
| 缓存 / 心跳 | **Redis**（已确认本版引入） | Agent 心跳去重与超时判定、检索结果缓存、会话；随 Agent 管理一起开发 |
| 列式分析（可选） | **ClickHouse** | 大日志量下的聚合加速，初期可不上（见第 9 章，列为后期可引入） |

### 2.2 仍需补充的组件（原型隐含、此前未列）

| 组件 | 必要性 | 理由（来自规格） |
|------|--------|------------------|
| **API 网关 / LB** | 必选（生产） | 统一入口、鉴权、限流；微服务多无状态服务需要前置路由。 |
| **对象存储 OSS/S3** | 按需（后期） | 原始日志归档（规格未显式要求，但生产必备；原型未涉及）。见第 9 章后期可引入清单。 |

> 结论：以 **Vue3 前端 + Python/FastAPI 应用微服务 + Fluent Bit（主机侧唯一采集/管控组件）+ OpenSearch(主) + Kafka + Flink + PostgreSQL + Redis** 为主体。ClickHouse、对象存储列为后期可引入项（见第 9 章），避免遗忘。采集层已确定采用开源 Fluent Bit 替换原自研 Go 采集器，且**业务主机只部署这一个 Fluent Bit 进程**——其"管控客户端"能力（心跳上报/配置拉取/重启/升级）通过随包附带的管控脚本（`exec` 插件定时调用）+ 配置热重载实现，不另起 Supervisor 守护进程；服务端 `agent-service` 纳入一阶段。下面映射按此组合展开。

---

## 3. 功能点 × 技术栈映射（逐页面）

下表以原型规格（10 个页面 + `agents.html`）为基准，逐功能点对齐技术栈落点。

### 3.1 总览表

| 页面（规格） | 路由 | 核心功能点 | 技术栈落点 | 覆盖 |
|----|----|----|----|----|
| 登录 `AUTH-0100` | `/login` | 账号密码、租户选择、记住账号、登录态机（成功/失败/无权限） | Vue3 + FastAPI(鉴权服务) + **PostgreSQL**(账号/租户) + JWT | ✅ |
| 运行总览 `OVW-0101` | `/overview` | 时间范围、写入趋势、告警分布、检索耗时 P50·P95、告警事件表、导出报表 | Vue3 + OpenSearch(聚合) + ECharts + FastAPI(指标聚合接口) | ✅ |
| 日志检索 `LOG-0201` | `/search` | **PPL 检索**、时间范围、直方图、日志列表、分页、详情抽屉、保存视图/导出 | Vue3 + **OpenSearch PPL** + FastAPI(检索网关) + PG(视图) | ✅ 本版直接 PPL，无转换层 |
| 仪表盘列表 `DSH-0301` | `/dashboards` | 筛选、仪表盘 CRUD、删除确认、状态 | Vue3 + FastAPI(仪表盘服务) + PG + OpenSearch(图表数据) | ✅ |
| 仪表盘详情 `DSH-0302` | `/dashboards/core-business` | 图表绑定检索语句、Tabs、异常明细表、分享/编辑 | Vue3 + OpenSearch(PPL 取数) + ECharts + PG(看板配置) | ⚠️ 依赖 PPL |
| 告警事件 `ALM-0401` | `/alarms/events` | 列表、批量关闭、详情抽屉、状态机（告警中/已恢复） | Vue3 + FastAPI(告警服务) + PG(事件) + OpenSearch(溯源) | ✅ |
| 告警规则 `ALM-0402` | `/alarms/rules` | 规则 CRUD、启用开关、**实时判定**、保存生效 | Vue3 + FastAPI(规则管理) + **Flink**(实时判定 Job) + PG(规则) + Kafka(日志流) | ✅ Flink 核心用武之地 |
| 数据源 `DAT-0501` | `/data-sources` | 数据源 CRUD、类型(Agent/Syslog/HTTP/Kafka)、连通性校验、导出配置 | Vue3 + FastAPI(接入服务) + PG + Kafka topic 管理 | ✅ |
| Agent 管理 `DAT-0502` | `/agents` | 主机列表、**心跳(>3min 离线)**、批量升级(滚动/断点续传)、重启、进度 | Vue3 + **Fluent Bit**(主机侧唯一进程，管控脚本随包部署、`exec` 插件调起) + FastAPI(`agent-service`) + PG(主机元数据) + Redis(心跳) | ✅ 本版纳入，见 3.2(8) |
| 用户与角色 `SYS-0601` | `/settings/users` | Tabs(用户/角色/系统参数/审计)、用户 CRUD、启用开关、导出 | Vue3 + FastAPI(系统服务) + **PostgreSQL**(RBAC/审计) | ✅ 部分待细化 |

### 3.2 逐模块细化（含规格原文要点）

#### （1）登录 `AUTH-0100`
- 功能点：`account`/`password` 必填；`tenant` 枚举（总部生产 / 省侧运维 / 测试租户）；记住账号；状态机 `empty/loading/error/noPermission`。
- 落点：
  - 前端：Vue3 表单 + 表单校验（FRM-001/005、INP-001/005）。
  - 后端：FastAPI 鉴权服务，BCrypt 密码哈希，JWT（或 Session）签发；租户选择决定数据范围。
  - 存储：PostgreSQL `users` / `tenants` / `roles` 表。
  - 性能目标：`api_p95 < 800ms`、`lcp < 2000ms`（规格 `perf` 字段）。

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
| OpenSearch | 日志索引、全文检索、聚合 | **本版必带** |
| Kafka | 采集→消费解耦缓冲，Flink 输入源 | **本版必带**（支撑实时告警） |
| Flink | 实时告警判定、趋势计算 | **本版必带**（否则告警规则无法「实时生效」） |
| **Redis** | Agent 心跳时间戳与超时判定、检索结果缓存、会话 | **本版引入**（检索缓存/会话 + Agent 心跳，随 Agent 管理纳入一阶段） |
| ClickHouse | 大日志量聚合加速 | **后期可引入**（见第 9 章） |
| 对象存储 | 原始日志归档 | **后期可引入**（见第 9 章） |

> 一句话：**应用层（前端 + 后端微服务）100% 容器化微服务化；数据/检索/中间件层，开发期全部跑在容器内（含 PostgreSQL / OpenSearch / Kafka / Flink / Redis，便于本地联调，无生产数据），上线后生产环境的数据层（OpenSearch / RDS(PostgreSQL) / Kafka / Redis 实例）改为云托管，自管容器只用于开发联调。**

---

## 6. 微服务拆分与容器边界

### 6.1 无状态 vs 有状态（云上部署铁律）
- **无状态（放心容器化生产）**：前端（Nginx 托管 Vue 产物）、各 FastAPI 微服务、API 网关。可水平扩容、滚动发布、重启无数据丢失——容器的甜区。
- **有状态（开发期容器化、生产期云托管）**：OpenSearch、PostgreSQL、Kafka、Flink 状态、Redis。开发期全部以容器 sidecar 跑在本地（无生产数据，便于联调）；上线后生产环境改用云托管实例（OpenSearch 服务 / RDS(PostgreSQL) / Kafka 实例 / Redis 实例），托管服务自带备份、容灾、数据安全——契合国网场景的硬要求。

### 6.2 服务清单与容器映射

| 服务（建议名） | 技术 | 状态 | 职责 | 对应功能模块 |
|----------------|------|------|------|--------------|
| `web` (前端) | Nginx + Vue3 产物 | 无状态 | 静态资源托管、SPA 路由 | 全部页面 |
| `auth-service` | FastAPI | 无状态 | 登录、JWT、租户、RBAC 判定 | AUTH/SYS |
| `search-service` | FastAPI | 无状态 | 检索网关：PPL 检索封装（含合法性校验）、鉴权、分页、限流 | LOG/DSH |
| `dashboard-service` | FastAPI | 无状态 | 看板/图表配置 CRUD、取数编排 | DSH |
| `alarm-service` | FastAPI | 无状态 | 告警事件管理、状态机、通知 | ALM-01 |
| `ingest-service` | FastAPI | 无状态 | 数据源管理、连通性校验、Kafka topic | DAT-01 |
| `agent-service` | FastAPI | 无状态 | Agent 主机元数据、心跳超时、升级编排（本版纳入一阶段，见 3.2(8)） | DAT-02 |
| `system-service` | FastAPI | 无状态 | 用户/角色/参数/审计 | SYS |
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
| 5 | AI 智能体模块 | ✅ 已确认（2026-11-14 更新） | **配置中心已引入**：独立 `ai-service` + 前端「智能管理」页（模型接入三页签 / PPL 语法规则库 / 能力注册表）；**运行时能力（NL→PPL 生成）仍待接入** |
| 6 | 生产数据层部署 | ✅ 已确认 | 开发期全容器内；上线后数据层云托管 |
| 7 | Redis / ClickHouse 引入 | ✅ 已确认 | **Redis 本版引入**（检索缓存/会话 + Agent 心跳，随 Agent 管理纳入一阶段）；ClickHouse 列后期 |
| 8 | 采集器选型 + 主机部署约束 | ✅ 已确认 | **采用开源 Fluent Bit 替换原自研 Go 采集器**；**业务主机仅部署单一 Fluent Bit 进程**，管控客户端以随包脚本（exec 插件）+ 配置热重载承载，不另起 Supervisor；Agent 管理（管控面）评估为中等工作量、**纳入一阶段** |

> 未关闭项：系统参数的具体项、审计日志的字段与保留期（不影响一阶段开发，补 PRD 即可）。

---

## 9. 后期可引入组件（Roadmap，避免遗忘）

本版（一阶段）**明确不引入**、但技术演进或业务增长后大概率需要的组件，集中记录以免遗漏。任一组件转入「本版引入」时，同步更新第 2、3、5、6、7 章并勾掉本表对应行。

| 组件 | 引入触发条件 / 场景 | 关联模块 | 备注 |
|------|---------------------|----------|------|
| **AI 智能体模块（运行时）** | 需要实现日志智能问答、根因分析对话、自然语言转检索等 | 独立 `ai-service`（FastAPI + LLM）；**不叫 agent-service**，该名已被采集端管控服务占用 | 配置中心已于 2026-11-14 落地；**NL→PPL 生成端点与检索页智能检索仍待接入** |
| **SPL → PPL 转换层** | 需兼容日志易式 SPL 历史查询、固化视图，或前端仍想保留 SPL 写法 | `search-service` 检索网关 | **本版（一阶段）不做**：直接采用 OpenSearch PPL，前端检索框按 PPL 语义；转换层仅作为后续兼容增强评估，非必需 |
| **ClickHouse** | 日志量或聚合维度复杂到 OpenSearch 吃力（大表聚合 / 长周期统计慢） | 检索 / 总览 / 仪表盘加速 | 云智慧双引擎思路（OS 全文 + CH 聚合） |
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
