# 统一日志分析平台 —— 容器骨架与搭建现状

> 本文档记录 v1.5 路线下已落地的 Docker 容器骨架、构建镜像与运行服务现状（**2026-11-12 基线**：全链路验证 T1–T6 全绿）。技术选型与方案设计详见 `tech-stack-selection.md`；服务清单与端口见 `service-inventory.md`；四个业务模块的建设思路与开发情况见 `module-*.md`。
>
> **2026-11-14 增量（第 4 章）**：上线「智能管理」功能，新增独立 `ai-service` 容器与 `/ai/management` 页面。后端微服务 **7 → 8**、运行容器 **17 → 18**、宿主调试端口扩到 **8008**、nginx 增加 `/api/ai/`。第 1–3 章保留 11-12 基线记录，涉及旧数字处已标注现值。

---

## 1. 已落地 Docker 骨架文件清单（2026-11-11，依 v1.5 路线）

> 本骨架已按《技术栈选型》第 2/5/6/7 章落地，采集层采用 Fluent Bit（非自研 Go），`agent-service` 管控面纳入一阶段。GitHub 上原仅为 v1.2（Go 采集器）旧版；本骨架与下列文件已随文档更新于 2026-11-12 提交并推送至 `main` 分支（镜像与运行服务现状见第 2 节）。

| 文件 / 目录 | 角色 |
|------------|------|
| `docker-compose.yml` | 基础编排：数据层 + **8 个** FastAPI 微服务（含 `ai-service`，`:192-200`）+ Nginx + Fluent Bit（服务名 DNS：`log_net`） |
| `compose.override.yml` | 开发覆盖：bind mount 源码、`uvicorn --reload`、端口 `8001–8008`；`web` 挂 `./frontend/dist`；`ai-service` 额外只读映射 CLI 入口（`:55-62`） |
| `docker-compose.prod.yml` | 生产/演示占位：数据层改云托管（profile `local-data`）、应用层无挂载（`ai-service` 亦为 `volumes: []`，见 4.4/4.5） |
| `Dockerfile` | FastAPI 多阶段镜像（builder 装依赖 → runtime slim，非 root） |
| `.env.example` / `.env` | 配置与密钥分离（`.env` 已 gitignore） |
| `.dockerignore` | 排除 `.git`/`.env`/依赖目录等，绝不把密钥打进镜像 |
| `nginx/nginx.conf` | 托管 Vue3 SPA（`./frontend/dist`）+ `/api/*` 反代到 8 个微服务，含 `/api/ai/ → ai-service:8000/`（`:28`） |
| `frontend/` | Vue3 + Element Plus + ECharts 单页应用源码；构建产物 `frontend/dist/` 由 `web` 容器托管（产物不入库） |
| `services/<name>/app/main.py` | **8 个**微服务（auth/search/dashboard/alarm/ingest/agent/system + **ai-service**），均含 `/health`；另有 `agent-service` `/heartbeat` |
| `services/ai-service/` | 新增 AI 能力配置中心：`app/main.py`（19 个路由 + PG 三表）、`vendor/cli/codebuddy`（宿主 CLI 入口副本，供容器只读挂载） |
| `services/requirements.txt` | 后端共用依赖（fastapi/uvicorn/psycopg2/redis/kafka-python/opensearch-py/jose…） |
| `fluentbit/` | Fluent Bit **distroless 多阶段镜像**（debian:trixie-slim 注入 shell/curl，因官方 5.2.0-arm64 distroless 无 `/bin/sh`/curl 且库须 glibc 2.38+/OpenSSL 3.4+ 匹配基底）+ `fluent-bit.conf`（tail→OpenSearch；**心跳用 dummy 输入+http 输出上报 agent-service**，因 distroless 无 exec 插件）+ `scripts/control.sh`（保留作含 exec 版 Fluent Bit 的管控脚本备选） |
| `fluentbit/sample/app.log` | 样例日志（参考用；实时采集源已改为 log-generator，见下） |
| `tools/log-generator/` | **测试业务系统模拟器**：持续输出 `[LEVEL] <msg> tenant=<id>` 多租户日志到共享卷 `app_logs`，由 Fluent Bit `tail` 采集，构成"业务系统→采集→多租户索引"完整测试链路（无真实业务时可反复测） |
| `flink-jobs/README.md` | 实时告警 Job 占位说明（Java/Scala 或 PyFlink 待定） |

**应用层镜像基底**：`python:3.11-slim`（**8 服务共用**；注意当前 `log_system-backend:latest` 构建于 2026-11-12，镜像内尚无 `ai-service` 代码，开发期靠 bind mount 运行，生产须重建镜像，见 4.5）；**采集器**：`fluent/fluent-bit:5.2.0-arm64`（Apple Silicon 原生，免 QEMU 模拟；生产 x86_64 主机改多架构 tag `5.2.0`）；**数据层**：`postgres:16-alpine` / `opensearchproject/opensearch:3.9.0` / `opensearch-dashboards:3.9.0` / `redis:7-alpine` / `bitnami/kafka:3.7` / `flink:1.18-scala_2.12`（amd64 模拟）。

**多租户测试链路（log-generator）**：生成器写到 `app_logs` 卷的日志经 Fluent Bit `app_log` 解析器**直接产出 `tenant_id` 字段**（正则捕获组 `(?<tenant_id>...)`），再由 `rewrite_tag` 把租户注入 tag（`tenant.<id>`），按 tag 路由到 `logs-default` / `logs-acme` / `logs-gp` 三个独立索引（opensearch 输出 `Index` 不支持运行时 `${tenant_id}` record accessor，故用 tag 路由），直接验证 3.2(9) 的行级多租户隔离。`scripts/validate-chain.sh` 的 T2 已升级为跨租户索引计数校验；**T4 的 Python 客户端探测方式已在 11-14 复核为无效，PPL 服务端实测可用**（见 3.5）；**T6 Kafka 判定已修正**（broker 可达即 PASS，Kafka 3.7 默认无 topic 不算不可达）。

---

## 2. 容器化部署镜像与运行服务现状（2026-11-12）

> 截至 2026-11-12，全栈 17 个 `log_system-*` 容器全部运行（Up），全链路验证 T1–T6 全绿；**2026-11-14 现值 18 个（新增 ai-service），见 4.6**。以下为当时实际落地的镜像与容器清单。已清理首轮并发构建竞态产生的 3 个孤儿镜像 `log_system-{agent,system,dashboard}-service` 及构建残留 `debian:bookworm-slim` / `alpine:latest` / `fluent/fluent-bit:5.2.0`，仅保留可用镜像。

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
| `log_system-backend:latest` | 258MB | `python:3.11-slim` + `requirements.txt` | 8 个 FastAPI 微服务（共用一份代码层，不同 `command` 启动）；**该镜像为 11-12 构建，新增 ai-service 后生产需重建** |
| `log_system-fluentbit:latest` | 144MB | `fluent/fluent-bit:5.2.0-arm64` + trixie 注入 | Fluent Bit 采集器（配置/脚本 bind mount 热更） |
| `log_system-log-generator:latest` | 150MB | `python:3.11-slim` | 测试业务系统，持续吐多租户日志到 `app_logs` 卷 |

> 已清理的残留：`log_system-{agent,system,dashboard}-service:latest`（首轮并发构建同一 Dockerfile 竞态产出，引用数 0、内容等同 backend，约 774MB）、`debian:bookworm-slim`、`alpine:latest`、`fluent/fluent-bit:5.2.0`（非 arm64 测试拉取），均不再保留。

### 2.3 运行服务情况（11-12 基线 17 个 + 11-14 新增 1 个 = 18 个，全部 Up）

| 容器 | 镜像 | 用途 |
|------|------|------|
| `log_system-postgres-1` | `postgres:16-alpine` | 关系型库：用户/角色/配置等持久化 |
| `log_system-opensearch-1` | `opensearch:3.9.0` | 日志检索引擎，存多租户索引 `logs-{default,acme,gp}` |
| `log_system-opensearch-dashboards-1` | `opensearch-dashboards:3.9.0` | OpenSearch 可视化 Web 界面 |
| `log_system-redis-1` | `redis:7-alpine` | 缓存 / 会话 / 心跳时间戳 |
| `log_system-kafka-1` | `bitnami/kafka:3.7` | 日志缓冲消息队列（P2 实时告警接入用，当前无 topic） |
| `log_system-web-1` | `nginx:1.27-alpine` | 反向代理 + Vue3 SPA 静态托管（`./frontend/dist`），`/api/*` 转发到各微服务 |
| `log_system-flink-jobmanager-1` | `flink:1.18` | 流计算作业管理（P2 告警作业） |
| `log_system-flink-taskmanager-1` | `flink:1.18` | 流计算任务执行 |
| `log_system-auth-service-1` | `log_system-backend` | 认证 / 鉴权 |
| `log_system-search-service-1` | `log_system-backend` | 日志检索（→ OpenSearch `_search`） |
| `log_system-dashboard-service-1` | `log_system-backend` | 看板聚合 |
| `log_system-alarm-service-1` | `log_system-backend` | 告警（→ Kafka） |
| `log_system-ingest-service-1` | `log_system-backend` | 采集接入（→ Kafka） |
| `log_system-agent-service-1` | `log_system-backend` | 管控面 / 心跳接收（`/heartbeat`） |
| `log_system-system-service-1` | `log_system-backend` | 系统 / 健康检查 |
| `log_system-ai-service-1` | `log_system-backend` | **2026-11-14 新增**：AI 能力配置中心（模型接入 / PPL 语法规则库 / 能力注册表），宿主 `8008`，详见第 4 章 |
| `log_system-fluentbit-1` | `log_system-fluentbit` | 采集器：`tail` 读日志 → 解析多租户 → `rewrite_tag` 路由 → OpenSearch |
| `log_system-log-generator-1` | `log_system-log-generator` | 测试业务系统，持续写多租户日志供采集 |

> 另有一套外部 Dify 项目栈（`docker-*` 容器 + `langgenius/dify-*` 等镜像）与本平台无关，未纳入、未改动。
> 表中「告警（→ Kafka）」「采集接入（→ Kafka）」是 11-12 的设计描述；当前日志由 Fluent Bit 直写 OpenSearch、告警为请求时同步判定，Kafka 仅参与连通性探测，详见 `module-alarm-center.md` / `module-data-ingestion.md`。

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

### 3.5 PPL 探测方式复盘（11-12 记录 / 11-14 复核，informational）
- 11-12 记录：T4 PPL 探测返回 `'OpenSearch' object has no attribute 'sql'`，主链路检索走 `_search` 不受影响。
- **2026-11-14 复核**：该报错来自 Python `opensearch-py` 客户端没有 `.sql` 属性（探测方式问题），并非服务端 PPL 不可用。当前 `search-service` 以 `httpx POST /_plugins/_ppl` 执行 PPL，实测 `source=logs-default | head 1` 正常返回 `schema/datarows`（见 `module-log-search.md`）。日志检索页的 PQL 分支即基于该端点，故无需再为「PPL 插件未就绪」保留阻断结论。


### 3.6 nginx upstream DNS 缓存导致新服务 502（2026-11-14 补充）

- **现象**：`up -d --force-recreate ai-service` 后直连 `8008` 正常，但 `8080/api/ai/health` 偶发 `502 Bad Gateway`。
- **根因**：nginx 在配置加载时解析 `proxy_pass http://ai-service:8000/` 并缓存 IP；容器重建后 IP 变化，旧 upstream 仍被复用。
- **决策**：重建后端容器后**单独 `restart web`**（见 4.5 步骤 4）；这条同样适用于后续任何后端服务的强制重建。

---

## 4. 智能管理 / `ai-service` 容器（2026-11-14 新增）

> 本次增量的定位是**AI 能力配置中心**，不是运行时推理服务：模型接入配置、PPL 语法规则库、AI 能力注册表落到 PG，并提供真实连通性探测；**自然语言转 PPL 的生成端点、检索页 AI 面板尚未接入**，日志检索主链路（`/query`、`/pql`）未改动。

### 4.1 新增内容与落点

| 层 | 新增内容 | 代码落点 |
|---|---|---|
| 前端 | 侧栏「智能管理」分组 + 顶栏同名页签；独立配置页 `/ai/management`，含「模型接入（商用 API / 本地自定义 / WorkBuddy CLI 三页签）」「PPL 语法规则库」「能力扩展」 | `frontend/src/App.vue:48-49,63`、`router/index.js:20`、`views/AIManagement.vue` |
| 前端 API | `frontend/src/api/ai.js`，`baseURL /api/ai` | `frontend/src/api/ai.js` |
| 后端容器 | 独立 FastAPI 服务 `ai-service`（不叫 `agent-service`，该名已被采集端管控占用）；19 个路由 | `services/ai-service/app/main.py:370-792` |
| 存储 | PG 三表：`ai_model_providers`（`:186`）、`ai_grammar_rules`（`:209`）、`ai_capabilities`（`:224`）；启动时 `init_db()` 建表，PG 不可用则降级启动（`:181-253`） | `services/ai-service/app/main.py` |
| 网关 | `location /api/ai/ { proxy_pass http://ai-service:8000/; }`（去前缀） | `nginx/nginx.conf:28` |
| 编排 | 基础服务定义、开发覆盖、生产编排、`web.depends_on` 同步加入 | 见 4.2 |
| 配置 | `.env.example` 增加 `AI_PG_DSN`（可选覆盖）、`AI_INGEST_META_URL` | `.env.example:36-44`、`ai-service/app/main.py:34-42` |
| CLI 挂载 | 宿主 CLI 入口副本，只读映射进容器 | `services/ai-service/vendor/cli/codebuddy`、`compose.override.yml:60` |

### 4.2 编排逐文件落点

| 文件:行号 | 内容 |
|---|---|
| `docker-compose.yml:111-120` | `web.depends_on` 追加 `ai-service`，保证 nginx 启动时 upstream 可解析 |
| `docker-compose.yml:192-200` | `ai-service`：`image: log_system-backend`、`working_dir: /app/services/ai-service`、`uvicorn app.main:app --port 8000`、`env_file: .env`、`depends_on: [postgres, ingest-service]`（同步索引字段需调 `/meta`） |
| `compose.override.yml:5-10` | `web` 挂载 `./frontend/dist:/usr/share/nginx/html:ro`，端口 `${WEB_PORT:-8080}:80` |
| `compose.override.yml:55-62` | `ai-service` 源码 bind mount + `./services/ai-service/vendor/cli:/home/appuser/.local/bin:ro`，`--reload`、`ports: ["8008:8000"]` |
| `docker-compose.prod.yml:23-26,48-50` | `web` 同样挂 `frontend/dist`；`ai-service` 去掉 `--reload` 且 **`volumes: []`**（无 CLI 挂载） |
| `nginx/nginx.conf:28` | `/api/ai/` 反代 |
| `Dockerfile:22-26` | `COPY services /app/services`、非 root `appuser`（`HOME=/home/appuser`，决定 CLI 默认路径） |
| `services/requirements.txt` | 未新增依赖：`ai-service` 仅用已有的 `fastapi` / `psycopg2-binary` / `httpx` / `pydantic` |

### 4.3 容器画像（实测）

| 项 | 值 |
|---|---|
| 容器 / 镜像 | `log_system-ai-service-1` / `log_system-backend:latest`（8 个后端服务共用一份镜像，不同 `command`） |
| 运行用户 / 工作目录 | `appuser`（非 root，`HOME=/home/appuser`）/ `/app/services/ai-service` |
| 启动命令 | 开发 `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`；生产去掉 `--reload` |
| 端口 | 容器内 `8000`，开发期宿主 `8008`；网关 `http://localhost:8080/api/ai/` |
| 环境 | `env_file: .env` + `TZ=Asia/Shanghai`；`AI_PG_DSN` 缺省用 `POSTGRES_*` 拼 DSN，`AI_INGEST_META_URL` 缺省 `http://ingest-service:8000/meta`（`main.py:34-42`） |
| 健康检查 | `GET /health` → `{"status":"ok","service":"ai-service","version":"0.1.0"}` |
| 接口分组 | `/health`、`/`、`/meta` 3 个；`providers` 6 个（列表/增/改/删/设默认/连通性测试）；`grammar` 8 个（查询/增/改/删/导入/导出/同步字段/命中预览）；`capabilities` 2 个（列表/更新），共 19 个 |
| 安全处理 | API Key 仅存服务端、列表脱敏（`sk-****xxxx`）、编辑留空保持原值；CLI 探测**不执行用户填写的参数**，只查路径/可执行位，勾选时仅执行固定 `<path> --version`；`source/tenant` 等运行时安全闸门仍属后续待办 |

> 注意：API Key 以明文写入 PG，仅在接口层脱敏；生产部署应评估加密或 KMS 托管。

### 4.4 WorkBuddy CLI 的宿主 → 容器挂载

**只读诊断结论**：宿主机 `~/.local/bin/codebuddy` 实际不存在，真身在 `/opt/homebrew/bin/codebuddy`（`#!/usr/bin/env node` 的 JS 脚本，`@tencent-ai/codebuddy-code`，包 275MB）；Docker Desktop 拒绝挂载 `/opt/homebrew`（`mounts denied ... not shared from the host`，改设置属系统级操作，未执行）；且 `ai-service` 容器是 Linux + 无 `node`，macOS 的 Node CLI 即使挂进去也无法执行。

**采用的最小方案**（不碰系统设置、不暴露凭据目录）：

1. 把 CLI 入口真身（14869B / 755 / sha256 与宿主一致）复制为 `services/ai-service/vendor/cli/codebuddy`，入库；源路径在 `/Users`，属 Docker Desktop 默认共享目录。
2. `compose.override.yml:60` 把该目录只读映射到 `/home/appuser/.local/bin`，使页面默认路径 `~/.local/bin/codebuddy` 在容器内可达（`X_OK=YES`）。
3. 「选择模型」下拉的 33 个候选来自 `codebuddy 2.164.0 --help --model` 的真实清单（`main.py:97-98`），支持自定义输入；`cli_args` 已从前后端全链路移除（DB 列保留兼容旧数据）。

**探测如实返回**：默认「测试连接」为路径可达性检查，`ok=true` 但 `runtimeReady=false`，详情明示容器内无 Node 运行时；勾选「试运行 --version」返回 `returncode=127 · env: 'node': No such file or directory`，不伪造通过。

**生产边界**：`docker-compose.prod.yml` 的 `ai-service` 为 `volumes: []`，上线前须另行挂载；若要真正执行 CLI 会话，需容器装 Linux Node + 挂完整包（275MB）+ 认证目录（凭据进容器，须明确授权），或做宿主机 CLI 桥接。三条路径均待拍板。

### 4.5 重建与部署顺序（踩过的坑）

```bash
# 1. 前端：产物不入库，clone / 改前端后必须先构建
npm --prefix frontend install && npm --prefix frontend run build

# 2. 编排语法校验
docker compose -f docker-compose.yml -f compose.override.yml config --quiet

# 3. 改了 compose / 挂载 → 强制重建 ai-service（普通 restart 不会重读 volume）
docker compose -f docker-compose.yml -f compose.override.yml up -d --force-recreate ai-service

# 4. 单独重启 web，刷新 nginx 缓存的 upstream DNS（否则 /api/ai/ 可能 502）
docker compose -f docker-compose.yml -f compose.override.yml restart web

# 5. 验证
curl http://localhost:8008/health && curl http://localhost:8080/api/ai/health
```

- **生产必须重建镜像**：`Dockerfile:22` 用 `COPY services /app/services` 把代码打进镜像，当前 `log_system-backend:latest` 构建于 2026-11-12，**镜像内不含 `ai-service` 目录**；开发期靠 bind mount 才跑得起来。上线前执行 `docker compose build`（或构建脚本）重建后再起生产编排。
- 后端源码小改：开发期 `--reload` 热更，无需重建；改 `requirements.txt` 则要重建镜像。

### 4.6 当前验证结果（2026-11-14 实测）

| 项 | 结果 |
|---|---|
| 容器与健康 | **18 个 `log_system-*` 容器全部 running**，`8001–8008` `/health` 全部 `200`（新增 `8008`）；`/api/ai/health` 正常 |
| 核心接口 | `/meta`（3 类接入 + 33 个 CLI 模型）、`/providers`、`/grammar`、`/capabilities` 经网关与直连均实测通过 |
| 端到端 | 三类接入真实探测（商用不可达如实 `ConnectError`、本地指向内网 `/meta` 返回 200、CLI 路径可达）、密钥脱敏/留空保持、设默认、规则命中预览、导入导出、能力配置更新全部验证；临时验证数据已清理 |
| 语法库 | 实测 33 条规则：`command 6 / field_alias 16 / term_mapping 4 / time_rule 2 / example 2 / deny 3`；「同步索引字段」读 `ingest-service /meta` 真实 mapping |
| 配置现状 | 2 条接入（`deepseek` 商用 API、`WorkBuddy CLI`）、4 个能力开关均关闭；数值随配置变化 |
| 前端 | 无头浏览器实测 3 个主页签 + 3 个子页签、规则表与能力卡渲染、菜单与面包屑正确、**控制台零报错**；`npm run build` 与 `compose config` 通过 |
| CLI 容器内 | `/home/appuser/.local/bin/codebuddy` `-rwxr-xr-x`、`X_OK=YES`、`NODE=MISSING` |

### 4.7 边界与待办

1. **仅配置中心**：无 `/nl2ppl` 生成端点，检索页 AI 智能检索面板未接入；能力开关只登记配置（`nl2ppl` 状态 `planned`）。`/grammar/preview` 只做字面规则命中、不调用模型。
2. **CLI 会话不可在容器内执行**：目前只做到入口只读映射 + 路径探测；彻底接通需装 Node 与完整包、并解决凭据进容器问题。
3. **生产前置**：重建 `log_system-backend` 镜像、补 CLI 挂载、注入 `AI_PG_DSN` / `AI_INGEST_META_URL`（云托管地址）、评估 API Key 加密。
4. **安全闸门待补**：`/pql` 目前仅关键字黑名单、无索引/函数白名单、`timeRange` 未传入，AI 语句落地前必须先补（见 `module-log-search.md` 第 6 节）。

