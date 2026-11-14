# log_system 服务清单与访问指引

> 生成时间：2026-11-12 19:33（GMT+8）；2026-11-14 更新：新增 ai-service 配置中心、前端 Vue3 化、四份模块进展文档
> 环境：本机 Docker 开发联调环境（`docker compose up` 自动叠加 `compose.override.yml`）
> 现状：**18 个服务容器全部 running**，8 个后端微服务 `/health` 健康检查均返回 `200`，OpenSearch 已采集到多租户日志。

---

## 一、总览

| 分层 | 服务数 | 说明 |
|---|---|---|
| 数据 / 中间件层 | 7 | postgres、opensearch、opensearch-dashboards、redis、kafka、flink-jobmanager、flink-taskmanager |
| 应用层（FastAPI 微服务 + 前端） | 9 | web、auth-service、search-service、dashboard-service、alarm-service、ingest-service、agent-service、system-service、ai-service |
| 采集层 | 1 | fluentbit |
| 测试业务模拟器 | 1 | log-generator |

---

## 二、服务清单（容器名 / 地址 / 端口 / 状态）

### 1. 数据 / 中间件层

| 容器名 | 服务 | 镜像 | 宿主机地址 | 映射端口 | 容器端口 | 状态 | 角色 |
|---|---|---|---|---|---|---|---|
| `log_system-postgres-1` | postgres | postgres:16-alpine | localhost | 5432 | 5432 | running | 关系型数据库（业务/元数据存储） |
| `log_system-opensearch-1` | opensearch | opensearchproject/opensearch:3.9.0 | localhost | 9200 / 9600 | 9200 / 9600 | running | 搜索引擎 / 日志存储（安全插件已关） |
| `log_system-opensearch-dashboards-1` | opensearch-dashboards | opensearchproject/opensearch-dashboards:3.9.0 | localhost | 5601 | 5601 | running | 日志检索与可视化看板（免登录） |
| `log_system-redis-1` | redis | redis:7-alpine | localhost | 6379 | 6379 | running | 缓存 / 轻量消息 |
| `log_system-kafka-1` | kafka | bitnami/kafka:3.7 | localhost | 9092 | 9092 | running | 消息队列（日志接入/告警解耦） |
| `log_system-flink-jobmanager-1` | flink-jobmanager | flink:1.18-scala_2.12 | localhost | 8081 | 8081 | running | 流处理作业管理 UI |
| `log_system-flink-taskmanager-1` | flink-taskmanager | flink:1.18-scala_2.12 | （仅容器网络） | — | 6123 / 8081 | running | 流处理任务执行节点 |

### 2. 应用层（前端 + 微服务）

| 容器名 | 服务 | 镜像 | 宿主机地址 | 映射端口 | 容器端口 | 状态 | 角色 |
|---|---|---|---|---|---|---|---|
| `log_system-web-1` | web | nginx:1.27-alpine | localhost | 8080 | 80 | running | 前端静态页 + 后端反向代理网关 |
| `log_system-auth-service-1` | auth-service | log_system-backend | localhost | 8001 | 8000 | running | 认证 / 鉴权（为后续接入预留，当前原型未接登录） |
| `log_system-search-service-1` | search-service | log_system-backend | localhost | 8002 | 8000 | running | 日志检索 |
| `log_system-dashboard-service-1` | dashboard-service | log_system-backend | localhost | 8003 | 8000 | running | 看板数据后端 |
| `log_system-alarm-service-1` | alarm-service | log_system-backend | localhost | 8004 | 8000 | running | 告警 |
| `log_system-ingest-service-1` | ingest-service | log_system-backend | localhost | 8005 | 8000 | running | 日志接入 |
| `log_system-agent-service-1` | agent-service | log_system-backend | localhost | 8006 | 8000 | running | 运维 Agent 管控面 |
| `log_system-system-service-1` | system-service | log_system-backend | localhost | 8007 | 8000 | running | 系统管理 |
| `log_system-ai-service-1` | ai-service | log_system-backend | localhost | 8008 | 8000 | running | AI 能力配置中心（模型接入 / PPL 语法规则库 / 能力注册表） |

### 3. 采集层 & 测试模拟器

| 容器名 | 服务 | 镜像 | 宿主机地址 | 映射端口 | 容器端口 | 状态 | 角色 |
|---|---|---|---|---|---|---|---|
| `log_system-fluentbit-1` | fluentbit | log_system-fluentbit | （仅容器网络） | — | 2020 | running | 采集 agent（tail 读取共享卷日志 → 写 OpenSearch / Kafka） |
| `log_system-log-generator-1` | log-generator | log_system-log-generator | （无端口） | — | — | running | 测试业务模拟器，向共享卷持续输出多租户日志 |

---

## 三、最常用访问入口（直接开浏览器）

| 看什么 | 地址 | 说明 |
|---|---|---|
| 前端页面 | http://localhost:8080 | Vue3 SPA（frontend/dist 挂载），无需登录 |
| 日志看板（OpenSearch Dashboards） | http://localhost:5601 | 日志检索 / 可视化，**免登录** |
| Flink 流处理界面 | http://localhost:8081 | 实时作业监控 |
| 看板（经 nginx 反代） | http://localhost:8080/osd/ | 与 5601 等价，走 web 网关 |

> 后端微服务均提供 Swagger 文档：浏览器打开 `http://localhost:<端口>/docs`；存活检查 `http://localhost:<端口>/health`。

---

## 四、前端（web）反向代理路由

`web` 容器（nginx）统一接收 8080 请求，按前缀转发到对应微服务（均走 Docker 内网 `:8000`，不经宿主机端口）：

| 访问路径 | 转发到 |
|---|---|
| `/` | Vue3 SPA 静态资源（./frontend/dist，由 compose.override.yml 只读挂载） |
| `/api/auth/` | auth-service:8000 |
| `/api/search/` | search-service:8000 |
| `/api/dashboards/` | dashboard-service:8000 |
| `/api/alarms/` | alarm-service:8000 |
| `/api/data/` | ingest-service:8000 |
| `/api/agents/` | agent-service:8000 |
| `/api/system/` | system-service:8000 |
| `/api/ai/` | ai-service:8000 |
| `/osd/` | opensearch-dashboards:5601（含 WebSocket 升级，看板便捷入口） |

---

## 五、采集数据现状（OpenSearch 索引）

数据链路：`log-generator`（多租户日志）→ `fluentbit`（tail 采集）→ `opensearch`，按租户分索引。

| 索引 | 文档数 | 存储 | 说明 |
|---|---|---|---|
| `logs-default` | 6076 | 517.9kb | default 租户日志 |
| `logs-acme` | 6086 | 476.7kb | acme 租户日志 |
| `logs-gp` | 5984 | 449.5kb | gp 租户日志 |
| `top_queries-2026.11.12-89283` | 79 | 604.2kb | 查询分析索引 |
| `.kibana_1` 等系统索引 | — | — | Dashboards 内部索引（无需关注） |

> 文档数随 `log-generator` 持续输出实时增长（当前约 1.8 万条）。

**在面板查看步骤**：OpenSearch Dashboards（5601）→ Stack Management → Index Patterns → 新建 `logs-*`（时间字段选 `@timestamp`）→ Discover 选 `logs-*`、把时间范围调大即可看到日志流；再经 Visualize + Dashboards 可拼成固定看板。

---

## 六、关键说明

- **免登录原因**：开发环境 OpenSearch 用 `DISABLE_SECURITY_PLUGIN=true`、Dashboards 用 `DISABLE_SECURITY_DASHBOARDS_PLUGIN=true` 关闭鉴权。`.env` 中的 `OPENSEARCH_USER`/`OPENSEARCH_PASSWORD`（均为 `admin`）仅用于微服务连接后端，看板本身免登录。
- **端口规律**：8 个后端微服务容器内统一跑 `:8000`，经宿主机 `8001–8008` 暴露；前端统一 `8080`，看板 `5601`，Flink `8081`，数据库/消息中间件各用标准端口（postgres 5432 / redis 6379 / kafka 9092 / opensearch 9200）。
- **容器网络**：`fluentbit`、`log-generator`、`flink-taskmanager` 不暴露宿主机端口，仅通过 `log_net` 桥接网络通信。

---

## 七、常用运维命令

```bash
# 查看所有服务状态
docker compose ps

# 查看某服务实时日志（如看板无数据时排查）
docker compose logs -f dashboard-service

# 停止并保留数据卷
docker compose down
# 停止并清空数据（重置开发环境）
docker compose down -v
```
