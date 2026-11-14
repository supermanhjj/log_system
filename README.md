# log_system

统一日志分析平台（Vibe_Coding 工作区）。初始化于 2026-11-11，最近更新 2026-11-14。

> 从「高保真原型 + Docker 骨架」推进到「Vue3 前端 + 8 个 FastAPI 微服务 + 真实采集/检索/告警链路」；当前为**本地开发与演示环境**，18 个容器 running。

## 当前进展

| 模块 | 入口 | 现状 |
|---|---|---|
| 运行总览 | `/overview` | 4 指标卡 + 4 图 + 最近告警表，全部取自 OpenSearch 真实聚合；30s 缓存 / 60s 刷新 |
| 日志分析 | `/search` | PPL 单框检索（含 `\|` 走 `/_plugins/_ppl`）、直方图、详情与上下文、告警关联回填；AI 智能检索运行时待接入 |
| 告警中心 | `/alarms/events`、`/alarms/rules` | 规则 CRUD/启停落 PG、请求时同步判定、状态覆盖、关联 PPL 跳检索页 |
| 数据接入 | `/data-sources`、`/agents` | 数据源自动发现与真实连通性探测、字段元数据来自真实 mapping、Agent 心跳与指令队列 |
| 智能管理 | `/ai/management` | 独立 `ai-service` 配置中心：模型接入三页签、PPL 语法规则库、AI 能力注册表（**仅配置，不含 NL→PPL 生成端点**） |

架构：Vue3 + Element Plus + ECharts → nginx（`/api/*` 反代）→ FastAPI 微服务 → OpenSearch / PostgreSQL / Redis；采集侧 `log-generator`（模拟业务）→ Fluent Bit → `logs-<tenant>`。

## 文档索引（`docs/`）

| 文档 | 内容 |
|---|---|
| [module-overview.md](docs/module-overview.md) | 运行总览：建设思路、已落地能力、接口与待办 |
| [module-log-search.md](docs/module-log-search.md) | 日志分析：PPL 检索链路、安全闸门缺口、AI 待接入边界 |
| [module-alarm-center.md](docs/module-alarm-center.md) | 告警中心：设计与实现的判定引擎偏差、事件状态、关联日志 |
| [module-data-ingestion.md](docs/module-data-ingestion.md) | 数据接入：配置面/数据面分离、字段字典、采集与管控链路 |
| [tech-stack-selection.md](docs/tech-stack-selection.md) | 技术栈选型与功能点 × 技术栈映射（设计基线 v1.5） |
| [container-deployment.md](docs/container-deployment.md) | 容器化部署、镜像与运行服务现状 |
| [service-inventory.md](docs/service-inventory.md) | 服务清单、端口、nginx 路由、索引现状 |

根目录 `code-search.md` / `code-data.md` 为检索与接入模块的早期实现分析。

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
#    前端            http://localhost:8080        （无需登录）
#    后端 Swagger    http://localhost:8001..8008/docs
#    OS Dashboards   http://localhost:5601 或 http://localhost:8080/osd/
#    Flink Web UI    http://localhost:8081

# 5. 重置环境（会清空数据卷）
docker compose down -v
```

开发期后端源码经 bind mount 热更（`uvicorn --reload`）；生产/演示编排见 `docker-compose.prod.yml`（数据层改为云托管，前端 dist 与 AI CLI 挂载需另行处理）。

## 已知边界

- 登录鉴权未接（`auth-service` 为骨架）；租户由前端透传，未做后端强绑定。
- PQL 分支未传时间范围、`/pql` 仅关键字黑名单，缺索引/函数白名单。
- AI 运行时能力（自然语言转 PPL）尚未接入检索页；`ai-service` 容器内无 Node 运行时，CLI 仅路径可达。
- Kafka / Flink 为设计稿链路，当前日志由 Fluent Bit 直写 OpenSearch；`logs-template` 索引模板无代码定义，部署时需手工 PUT。
- 日志源为 `log-generator` 模拟数据，非生产日志。

> Apple Silicon：OpenSearch 若报架构错误可加 `platform: linux/amd64`；Flink 默认以 amd64 模拟运行。
