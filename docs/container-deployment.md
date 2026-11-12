# 统一日志分析平台 —— 容器骨架与搭建现状

> 本文档记录 v1.5 路线下已落地的 Docker 容器骨架、构建镜像与运行服务现状（截至 2026-11-12，全链路验证 T1–T6 全绿）。技术选型与方案设计详见 `tech-stack-selection.md`。

---

## 1. 已落地 Docker 骨架文件清单（2026-11-11，依 v1.5 路线）

> 本骨架已按《技术栈选型》第 2/5/6/7 章落地，采集层采用 Fluent Bit（非自研 Go），`agent-service` 管控面纳入一阶段。GitHub 上原仅为 v1.2（Go 采集器）旧版；本骨架与下列文件已随文档更新于 2026-11-12 提交并推送至 `main` 分支（镜像与运行服务现状见第 2 节）。

| 文件 / 目录 | 角色 |
|------------|------|
| `docker-compose.yml` | 基础编排：数据层 + 7 个 FastAPI 微服务 + Nginx + Fluent Bit（服务名 DNS：`log_net`） |
| `compose.override.yml` | 开发覆盖：bind mount 源码、`uvicorn --reload`、映射端口到本机 |
| `docker-compose.prod.yml` | 生产/演示占位：数据层改云托管（profile `local-data`）、应用层无挂载 |
| `Dockerfile` | FastAPI 多阶段镜像（builder 装依赖 → runtime slim，非 root） |
| `.env.example` / `.env` | 配置与密钥分离（`.env` 已 gitignore） |
| `.dockerignore` | 排除 `.git`/`.env`/依赖目录等，绝不把密钥打进镜像 |
| `nginx/nginx.conf` | 托管前端 + `/api/*` 反代到各微服务（前缀路由去前缀） |
| `services/<name>/app/main.py` | 7 个微服务骨架（auth/search/dashboard/alarm/ingest/agent/system），含 `/health` 与 `agent-service` `/heartbeat` |
| `services/requirements.txt` | 后端共用依赖（fastapi/uvicorn/psycopg2/redis/kafka-python/opensearch-py/jose…） |
| `fluentbit/` | Fluent Bit **distroless 多阶段镜像**（debian:trixie-slim 注入 shell/curl，因官方 5.2.0-arm64 distroless 无 `/bin/sh`/curl 且库须 glibc 2.38+/OpenSSL 3.4+ 匹配基底）+ `fluent-bit.conf`（tail→OpenSearch；**心跳用 dummy 输入+http 输出上报 agent-service**，因 distroless 无 exec 插件）+ `scripts/control.sh`（保留作含 exec 版 Fluent Bit 的管控脚本备选） |
| `fluentbit/sample/app.log` | 样例日志（参考用；实时采集源已改为 log-generator，见下） |
| `tools/log-generator/` | **测试业务系统模拟器**：持续输出 `[LEVEL] <msg> tenant=<id>` 多租户日志到共享卷 `app_logs`，由 Fluent Bit `tail` 采集，构成"业务系统→采集→多租户索引"完整测试链路（无真实业务时可反复测） |
| `flink-jobs/README.md` | 实时告警 Job 占位说明（Java/Scala 或 PyFlink 待定） |

**应用层镜像基底**：`python:3.11-slim`（7 服务共用）；**采集器**：`fluent/fluent-bit:5.2.0-arm64`（Apple Silicon 原生，免 QEMU 模拟；生产 x86_64 主机改多架构 tag `5.2.0`）；**数据层**：`postgres:16-alpine` / `opensearchproject/opensearch:3.9.0` / `opensearch-dashboards:3.9.0` / `redis:7-alpine` / `bitnami/kafka:3.7` / `flink:1.18-scala_2.12`（amd64 模拟）。

**多租户测试链路（log-generator）**：生成器写到 `app_logs` 卷的日志经 Fluent Bit `app_log` 解析器**直接产出 `tenant_id` 字段**（正则捕获组 `(?<tenant_id>...)`），再由 `rewrite_tag` 把租户注入 tag（`tenant.<id>`），按 tag 路由到 `logs-default` / `logs-acme` / `logs-gp` 三个独立索引（opensearch 输出 `Index` 不支持运行时 `${tenant_id}` record accessor，故用 tag 路由），直接验证 3.2(9) 的行级多租户隔离。`scripts/validate-chain.sh` 的 T2 已升级为跨租户索引计数校验；**T4 PPL 探测当前 ⚠️ 未就绪**（需装 `opensearch-sql` 插件，主链路用 `_search` 不受影响）；**T6 Kafka 判定已修正**（broker 可达即 PASS，Kafka 3.7 默认无 topic 不算不可达）。

---

## 2. 容器化部署镜像与运行服务现状（2026-11-12）

> 截至 2026-11-12，全栈 17 个 `log_system-*` 容器全部运行（Up），全链路验证 T1–T6 全绿。以下为当时实际落地的镜像与容器清单。已清理首轮并发构建竞态产生的 3 个孤儿镜像 `log_system-{agent,system,dashboard}-service` 及构建残留 `debian:bookworm-slim` / `alpine:latest` / `fluent/fluent-bit:5.2.0`，仅保留可用镜像。

### 2.1 基础镜像（Registry 拉取，未改动）

| 镜像 | 大小 | 用途 |
|------|------|------|
| `postgres:16-alpine` | 288MB | 关系型库（用户/角色/配置持久化），`log_system-postgres` 直接运行 |
| `opensearchproject/opensearch:3.9.0` | 1.31GB | 日志检索引擎，`log_system-opensearch` 直接运行 |
| `opensearchproject/opensearch-dashboards:3.9.0` | 1.75GB | 可视化，`log_system-opensearch-dashboards` 直接运行 |
| `redis:7-alpine` | 39.9MB | 缓存 / 会话 / 心跳时间戳，`log_system-redis` 直接运行 |
| `bitnami/kafka:3.7` | 668MB | 消息缓冲，`log_system-kafka` 直接运行（当前无 topic 属正常） |
| `nginx:1.27-alpine` | 49.7MB | 反向代理，`log_system-web` 直接运行（`/api/*` 转发微服务） |
| `flink:1.18-scala_2.12` | 782MB | 流计算，`log_system-flink-{job,task}manager` 直接运行（本地 arm64，免 QEMU） |
| `python:3.11-slim` | 150MB | **构建父镜像**：`log_system-backend` 与 `log_system-log-generator` 的 FROM |
| `fluent/fluent-bit:5.2.0-arm64` | 134MB | **构建父镜像**：`log_system-fluentbit` 的 FROM（Apple Silicon 原生） |
| `debian:trixie-slim` | 101MB | **构建辅助**：多阶段向 distroless Fluent Bit 注入 shell/curl（glibc 2.38+ / OpenSSL 3.4+ 匹配基底） |

### 2.2 自建镜像（Dockerfile 实际构建，承载本平台代码）

| 镜像 | 大小 | 基于 | 承载内容 |
|------|------|------|----------|
| `log_system-backend:latest` | 258MB | `python:3.11-slim` + `requirements.txt` | 7 个 FastAPI 微服务（共用一份代码层，不同 `command` 启动） |
| `log_system-fluentbit:latest` | 144MB | `fluent/fluent-bit:5.2.0-arm64` + trixie 注入 | Fluent Bit 采集器（配置/脚本 bind mount 热更） |
| `log_system-log-generator:latest` | 150MB | `python:3.11-slim` | 测试业务系统，持续吐多租户日志到 `app_logs` 卷 |

> 已清理的残留：`log_system-{agent,system,dashboard}-service:latest`（首轮并发构建同一 Dockerfile 竞态产出，引用数 0、内容等同 backend，约 774MB）、`debian:bookworm-slim`、`alpine:latest`、`fluent/fluent-bit:5.2.0`（非 arm64 测试拉取），均不再保留。

### 2.3 运行服务情况（17 个 `log_system-*` 容器，全部 Up）

| 容器 | 镜像 | 用途 |
|------|------|------|
| `log_system-postgres-1` | `postgres:16-alpine` | 关系型库：用户/角色/配置等持久化 |
| `log_system-opensearch-1` | `opensearch:3.9.0` | 日志检索引擎，存多租户索引 `logs-{default,acme,gp}` |
| `log_system-opensearch-dashboards-1` | `opensearch-dashboards:3.9.0` | OpenSearch 可视化 Web 界面 |
| `log_system-redis-1` | `redis:7-alpine` | 缓存 / 会话 / 心跳时间戳 |
| `log_system-kafka-1` | `bitnami/kafka:3.7` | 日志缓冲消息队列（P2 实时告警接入用，当前无 topic） |
| `log_system-web-1` | `nginx:1.27-alpine` | 反向代理，`/api/*` 转发到各微服务 |
| `log_system-flink-jobmanager-1` | `flink:1.18` | 流计算作业管理（P2 告警作业） |
| `log_system-flink-taskmanager-1` | `flink:1.18` | 流计算任务执行 |
| `log_system-auth-service-1` | `log_system-backend` | 认证 / 鉴权 |
| `log_system-search-service-1` | `log_system-backend` | 日志检索（→ OpenSearch `_search`） |
| `log_system-dashboard-service-1` | `log_system-backend` | 看板聚合 |
| `log_system-alarm-service-1` | `log_system-backend` | 告警（→ Kafka） |
| `log_system-ingest-service-1` | `log_system-backend` | 采集接入（→ Kafka） |
| `log_system-agent-service-1` | `log_system-backend` | 管控面 / 心跳接收（`/heartbeat`） |
| `log_system-system-service-1` | `log_system-backend` | 系统 / 健康检查 |
| `log_system-fluentbit-1` | `log_system-fluentbit` | 采集器：`tail` 读日志 → 解析多租户 → `rewrite_tag` 路由 → OpenSearch |
| `log_system-log-generator-1` | `log_system-log-generator` | 测试业务系统，持续写多租户日志供采集 |

> 另有一套外部 Dify 项目栈（`docker-*` 容器 + `langgenius/dify-*` 等镜像）与本平台无关，未纳入、未改动。

---

---

## 3. 全链路验证踩坑与关键决策（2026-11-12）

> 本段记录 v1.5 路线下「Docker 部署 + 全链路验证」跑通过程中实测推翻原假设的 4 个关键点，供后续迭代与移植生产环境时对照。

### 3.1 Fluent Bit 5.2.0 官方 distroless 镜像无 `exec` 输入插件
- **现象**：`fluent-bit.conf` 用 `[INPUT] exec ... Command sh control.sh` 启动即报 `section 'exec' tried to instance a plugin name that doesn't exist`，容器 Exited(1)。
- **根因**：`fluent/fluent-bit:5.2.0` 与 `5.2.0-arm64` 均为 distroless，**只含 `exec_wasi`（WASM），不含 `exec` shell 插件**。
- **决策**：心跳改为 fluent-bit 原生 `dummy` 输入 + `http` 输出 POST `agent-service:8000/heartbeat`，等效"主机侧 Fluent Bit 定时上报"语义，且不依赖 shell 脚本。`scripts/control.sh` 保留，作为"换用含 exec 插件的 Fluent Bit 构建"时的管控脚本备选。
- **移植提示**：生产若坚持 `exec` 调管控脚本，须改用含 exec 插件的 Fluent Bit 构建（非官方 distroless 版，或自编译开启 `FLB_IN_EXEC`）。

### 3.2 distroless 镜像无 shell/curl，需多阶段注入且库版本须匹配
- **现象**：直用 `fluent/fluent-bit:5.2.0-arm64` 跑 `sh`/`curl` 报 `exec /bin/sh: no such file`；自注入 debian:bookworm 的 shell/curl 后又报 `GLIBC_2.38` / `OPENSSL_3.4.0 not found`。
- **根因**：该镜像底层是 glibc 2.38+/OpenSSL 3.4+（Debian trixie 级），bookworm(2.36/3.0) 工具链版本过低且被优先加载（路径 `/lib/aarch64-linux-gnu` 优先于 `/usr/lib/...`）。
- **决策**：helper 用 `debian:trixie-slim`（glibc 2.38+/OpenSSL 3.4+）收集 `/bin/sh`+`/usr/bin/curl`+动态库+链接器，按原绝对路径 `COPY --from=helper /staging/ /` 进 distroless；脚本可执行位在 helper 阶段 `chmod`（目标镜像无 chmod）。

### 3.3 OpenSearch 输出 `Index` 不支持运行时 record accessor
- **现象**：`Index logs-${tenant_id}` 实际落索引 `logs-`（空），所有日志进同一字面量索引，多租户隔离失效。
- **根因**：`${tenant_id}` 在配置加载时被当**环境变量**展开（未定义→空），opensearch 输出 `Index` 参数不支持每条记录运行时取值。
- **决策**：`app_log` 正则解析器直接产出 `tenant_id` 字段（捕获组 `(?<tenant_id>...)`，因 `record_modifier` 仅支持字面量、无法引用其他字段）；再用 `rewrite_tag` 把租户注入 tag（`tenant.<id>`，前缀刻意避开 rewrite_tag 自身 `Match log.*` 以免 emitter 循环），三租户各配一个 `Match tenant.<id>` + 固定 `Index logs-<id>` 的 output 路由。

### 3.4 Kafka 3.7 默认无 topic，validate T6 判定修正
- **现象**：`kafka-topics.sh --list` 返回空，旧脚本据此判"Kafka 不可达" FAIL。
- **根因**：Kafka 3.7 默认 `auto.create.topics.enable=false`，无 topic 属正常，不等于 broker 不可达（命令无连接错误）。
- **决策**：T6 改为检查"命令无连接/超时错误即 broker 可达"，无 topic 时给 info 提示而非 FAIL。

### 3.5 PPL 插件未就绪（informational）
- OpenSearch 3.9.0 默认未装 `opensearch-sql` 插件，T4 PPL 探测返回 `'OpenSearch' object has no attribute 'sql'`。主链路检索走 `_search` 不受影响；如需 PPL/SPL 类查询需单独安装该插件（生产可选）。
