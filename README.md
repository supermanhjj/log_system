# log_system

**RuiLog 日志分析平台**（Vibe_Coding 工作区；产品品牌 RuiLog，2026-11-18 定名）。初始化于 2026-11-11，最近更新 2026-11-19。

> 从「高保真原型 + Docker 骨架」推进到「Vue3 前端 + 8 个 FastAPI 微服务 + 真实采集/检索/告警链路 + 登录与 RBAC + **多租户隔离**」；当前为**本地开发与验证环境**，18 个容器 running。
>
> 项目定位：对标日志易 / 云智慧 DOLA 的**商用日志分析平台**，本地 Docker 只是开发验证环境；技术方案按「上云、接真实各类系统」的标准设计，不以「本地能演示」为上限。

## 当前进展

| 模块 | 入口 | 现状 |
|---|---|---|
| 登录 | `/login` | 账号/密码/租户真实校验 PG（bcrypt），签发 JWT；失败区分「密码错 401」与「账号停用/租户不匹配 403」；记住账号；改密后强制重新登录 |
| 运行总览 | `/overview` | 4 指标卡 + 4 图 + 最近告警表，全部取自 OpenSearch 真实聚合；30s 缓存 / 60s 刷新；**存储容量概览**四卡按角色口径（管理员看平台总容量，租户看本租户配额）；检索耗时 P95 达标线由系统参数驱动 |
| 日志分析 | `/search` | PPL 单框检索（含 `\|` 走 `/_plugins/_ppl`）、直方图、详情与上下文、告警关联回填；检索页已接入「智能检索」弹窗（自然语言转 PPL、复制、填入检索框执行） |
| 仪表盘 | `/dashboards`、`/dashboards/:id` | 看板列表（筛选/新建/批量删除）+ 详情 + **拖拽式编辑器**：从组件库拖入图表、画布内重排与调宽、配置展示维度；配置存 PG，图表数据按维度对 OpenSearch 实时聚合（8 类图表 / 9 类维度） |
| 告警中心 | `/alarms/events`、`/alarms/rules` | 规则 CRUD/启停落 PG、请求时同步判定、状态覆盖、关联 PPL 跳检索页 |
| 数据接入 | `/data-sources`、`/agents` | 数据源自动发现与真实连通性探测、字段元数据来自真实 mapping、Agent 心跳与指令队列 |
| 智能管理 | `/ai/management` | 独立 `ai-service`：模型接入两页签（商用 API / 本地自定义）、PPL 语法规则库、AI 能力注册表，以及 **NL→PPL 生成端点与「智能检索预览」**（中文问句 → 规则库 → 已选模型 → PPL） |
| 平台管理 · 用户角色 | `/settings/users` | 独立 `system-service`：用户管理（CRUD/启停/重置密码/批量/导出）、角色权限矩阵（改完实时生效）；顶栏显示真实登录用户与角色并支持修改密码，菜单按权限过滤 |
| 平台管理 · 系统设置 | `/settings/system` | 系统参数（操作审计 / 单次检索上限 / **检索耗时目标** / 会话超时 / 默认时区）+ 审计日志（筛选/分页/导出）；检索耗时目标真实驱动运行总览 P95 达标判定 |
| 平台管理 · 日志空间 | `/settings/storage` | 容量概览（含趋势）、存储配置（**平台总容量为配置值**，不再查物理卷）、索引管理（真实体积/文档数/状态/清理，索引由采集自动建）、租户配额（之和受总容量约束）；已用来自 OpenSearch `_cat/indices` 真实取数；**容量四卡口径按角色切换**（平台管理员看平台总容量、租户看本租户配额），三项闭合 |
| **平台管理 · 租户管理** | `/settings/tenants` | 租户 CRUD（编码不可改、保留字禁用）+ 配额 + 启停 + 删除保护（有用户/有日志/当前登录租户均拒绝）；新建自动建 `logs-<code>` 索引、删除连带 drop |

架构：Vue3 + Element Plus + ECharts → nginx（`/api/*` 反代，运行期 DNS 解析）→ FastAPI 微服务 → OpenSearch / PostgreSQL / Redis。
采集侧为**两层平面**：K8s（`i6000` 命名空间里的 `i6000-order` / `i6000-sys` → DaemonSet 采集器，采 CRI 容器日志）与 Docker Compose（平台自身 web/redis/kafka/postgres → Fluent Bit），统一 HTTP `POST /ingest/collector` → 服务端归一 + 解析管线 + 租户裁决 → `logs-<tenant>`；**判不出归属直接拒收并返回原因，不再落 `logs-unknown`**。写入侧经 Kafka + Flink 落 ClickHouse（聚合）/ OpenSearch（明细检索）。当前形态详见 [current-architecture-2026-11-20.md](docs/current-architecture-2026-11-20.md)。

**多租户模型**：租户是「日志从哪来」的部署元数据，由采集端注入来源信息（容器日志用 K8s namespace/label、系统日志用实例 tag→CMDB、App 直报用 Token），服务端 `derive_tenant()` 统一裁决，**不从日志正文探测，不静默归 default**。查询侧所有业务端点按 JWT 声明强制解析租户，前端传值仅作意图。

## 文档索引（`docs/`）

| 文档 | 内容 |
|---|---|
| [current-architecture-2026-11-20.md](docs/current-architecture-2026-11-20.md) | **当前架构与实现现状快照**：上 K8s 后的两层采集平面、字段契约、写入/查询分工、本轮调整清单、实测状态与遗留问题 |
| [module-overview.md](docs/module-overview.md) | 运行总览：建设思路、已落地能力、接口与待办 |
| [module-log-search.md](docs/module-log-search.md) | 日志分析：PPL 检索链路、安全闸门缺口、AI 生成端点接入现状 |
| [module-dashboard.md](docs/module-dashboard.md) | 仪表盘：列表/详情/拖拽编辑器、配置存储与维度聚合口径、可配置维度字典 |
| [module-alarm-center.md](docs/module-alarm-center.md) | 告警中心：设计与实现的判定引擎偏差、事件状态、关联日志 |
| [module-data-ingestion.md](docs/module-data-ingestion.md) | 数据接入：配置面/数据面分离、字段字典、采集与管控链路 |
| [module-platform-admin.md](docs/module-platform-admin.md) | 平台管理：登录、双服务职责边界、RBAC 六表与权限矩阵、接口、验证与待办 |
| [module-log-storage.md](docs/module-log-storage.md) | 日志空间管理：容量口径、趋势来源、索引状态判据、清理语义、权限与验证 |
| [service-auth.md](docs/service-auth.md) | 登录与鉴权模块（auth-service）：开发内容、登录链路、JWT 与权限下发、失败语义、踩坑与边界 |
| [service-system.md](docs/service-system.md) | 用户、角色与日志空间（system-service）：九表与种子、用户管理、权限矩阵、日志空间真实取数、保护性约束、审计 |
| [tech-stack-selection.md](docs/tech-stack-selection.md) | 技术栈选型与功能点 × 技术栈映射（设计基线 v1.5） |
| [container-deployment.md](docs/container-deployment.md) | 容器化部署、镜像与运行服务现状（第 4 章含 ai-service 与 NL→PPL 运行时） |
| [service-inventory.md](docs/service-inventory.md) | 服务清单、端口、nginx 路由、索引现状 |

根目录 `code-search.md` / `code-data.md` 为检索与接入模块的早期实现分析。

## 目录结构（按部署方式归类）

```
.
├── docker-compose.yml / compose.override.yml / docker-compose.prod.yml   # 容器部署入口（保留在根目录）
├── deploy/
│   ├── docker/     # 仅容器部署：nginx 反代与静态托管、fluentbit 采集器、OpenSearch Dashboards 配置
│   ├── k8s/        # 仅 K8s 部署：platform（kustomize base + overlays/local、cloud、cloud-ha）、collector（Helm DaemonSet）、i6000-demo（演示业务清单）
│   ├── shared/     # 两种部署共用：clickhouse 建表/迁移、opensearch 索引模板与 ISM、kafka/opensearch 运维脚本
│   └── release/    # 发布：dockerfiles（backend/web/flink-job）、build-images.sh、load-images.sh、package.sh
├── services/ frontend/ flink-jobs/ tools/   # 业务源码（与部署方式无关）
└── scripts/validate-chain.sh                # 全链路自检
```

| 场景 | 命令 |
|---|---|
| 容器部署（本地开发） | `docker compose up -d --build` |
| K8s 部署（本地 Docker Desktop） | 镜像统一为 `ruilog-<组件>`：`ruilog-backend:k8s`、`ruilog-web:k8s`、`ruilog-flink:1.18`、`ruilog-flink-job:k8s`。构建后执行 `sh deploy/release/load-images.sh` → `sh deploy/k8s/apply.sh local` |
| K8s 部署（云上测试） | 修改 `deploy/k8s/platform/overlays/cloud/kustomization.yaml` 的镜像仓库/StorageClass/PVC → `sh deploy/k8s/apply.sh cloud`；采集器用 `-f deploy/k8s/collector/values-cloud.yaml` |
| K8s 部署（3 节点生产形态） | `sh deploy/k8s/apply.sh cloud-ha`（前提：Kafka/OS 已扩到 3 节点） |
| 发布（多架构镜像推送） | `REGISTRY=<registry> TAG=1.0.0 sh deploy/release/build-images.sh` |
| 发布（交付包归档） | `VERSION=1.0.0 sh deploy/release/package.sh` → `dist/ruilog-deploy-1.0.0.tar.gz` |

## 本地启动

```bash
# 1. 准备配置（.env 已被 .gitignore 忽略，绝不入库）
cp .env.example .env

# 2. 构建前端（产物 frontend/dist 由 web 容器托管，dist/ 不入库）
npm --prefix frontend install
npm --prefix frontend run build

# 3. 启动全部容器（开发期自动叠加 compose.override.yml）
docker compose up -d --build

# 4. 访问
#    前端            http://localhost:8080        （需登录：admin / Admin@123）
#    租户管理        http://localhost:8080/settings/tenants
#    后端 Swagger    http://localhost:8001..8008/docs
#    OS Dashboards   http://localhost:5601 或 http://localhost:8080/osd/
#    Flink Web UI    http://localhost:8081

# 5. 重置环境（会清空数据卷）
docker compose down -v
```

开发期后端源码经 bind mount 热更（`uvicorn --reload`）；生产/演示编排见 `docker-compose.prod.yml`（数据层改为云托管，前端 dist 需另行处理）。

## 已知边界

- **登录、RBAC 与多租户隔离已落地**：`search` / `dashboard` / `alarm` / `ingest` 四服务各自引入 `authz.py`，全部业务端点校验 Bearer 并按 JWT 强制解析租户；看板与告警规则已补 `tenant` 列做租户级隔离。剩余 `/agents`、`/ai` 的主机与模型配置尚未租户化（详见 `module-platform-admin.md` 第 9 节）。
- 种子账号仅保留 `admin`（`admin / Admin@123`，生产部署前用 `AUTH_SEED_ADMIN_PASSWORD` 覆盖并改掉）；其他账号由管理员在「用户管理」中创建，不再预置演示账号。
- `/pql` 仅关键字黑名单，缺索引/函数白名单；时间范围已随请求注入（`source` 段后作为独立 `where`）。
- AI 自然语言转 PPL 的**生成端点、配置页预览与检索页「智能检索 · ChatPPL」弹窗均已可用**；时间范围在 `source` 段后作为独立 `where` 注入，与关键字检索口径一致。
- 智能检索的**租户维度随检索范围变化**：单租户范围租户是检索上下文（出现即剥除）；「全部租户」范围可用虚拟字段 `tenant`（如 `stats count() by tenant, level`、`where tenant = '总部生产'`），由检索网关翻译为物理字段并在结果中显示租户中文名。详见 `docs/module-log-search.md` 第 9 章。
- 模型接入只支持**商用 API** 与**本地自定义**（Ollama / oMLX，走 OpenAI 兼容 HTTP）；WorkBuddy / CodeBuddy CLI 接入方式已于 2026-11-17 下线，容器不再挂载宿主 CLI。
- Kafka / Flink 为设计稿链路；当前采集链路是 Fluent Bit → `POST /ingest/collector` → OpenSearch（服务端统一接入，便于按来源判定租户）。`logs-template` 索引模板已由 `system-service` 的 `ensure_tenant_index()` 显式建 mapping，新建租户自动就绪。
- 日志空间管理的**总容量/租户配额是配置值**，日志已用与索引数据来自 OpenSearch 真实取数；物理卷读数仅供参考。总容量未配置时如实显示「未配置」，不回落成 Docker 虚拟磁盘大小。容量趋势在每日快照攒满 2 个点前为「按真实日志量推算」，页面已标注来源。清理只在手动触发时按保留期执行，**未接定时任务与 ISM 策略**。
- 日志源为 `log-generator` 模拟数据，非生产日志。

> Apple Silicon：OpenSearch 若报架构错误可加 `platform: linux/amd64`；Flink 默认以 amd64 模拟运行。
