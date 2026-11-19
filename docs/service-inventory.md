# log_system 服务清单与访问指引

> 生成时间：2026-11-12 19:33（GMT+8）；2026-11-14 更新：新增 ai-service（配置中心 + NL→PPL 生成端点）、前端 Vue3 化、四份模块进展文档
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
| `log_system-auth-service-1` | [auth-service](service-auth.md) | log_system-backend | localhost | 8001 | 8000 | running | 登录 / JWT 签发 / `/me` 当前用户与权限 / 改密（不建表，只读 users·tenants·roles） |
| `log_system-search-service-1` | search-service | log_system-backend | localhost | 8002 | 8000 | running | 日志检索 |
| `log_system-dashboard-service-1` | dashboard-service | log_system-backend | localhost | 8003 | 8000 | running | 看板数据后端 |
| `log_system-alarm-service-1` | alarm-service | log_system-backend | localhost | 8004 | 8000 | running | 告警 |
| `log_system-ingest-service-1` | ingest-service | log_system-backend | localhost | 8005 | 8000 | running | 日志接入 |
| `log_system-agent-service-1` | agent-service | log_system-backend | localhost | 8006 | 8000 | running | 运维 Agent 管控面 |
| `log_system-system-service-1` | [system-service](service-system.md) | log_system-backend | localhost | 8007 | 8000 | running | 平台管理：用户 / 角色权限 / **租户管理** / 系统参数 / 审计日志 / 日志空间管理（九表建表方，容量与索引实时取数） |
| `log_system-ai-service-1` | ai-service | log_system-backend | localhost | 8008 | 8000 | running | AI 能力配置中心 + NL→PPL 生成端点（模型接入 / PPL 语法规则库 / 能力注册表 / 智能检索预览） |

### 3. 采集层 & 测试模拟器

| 容器名 | 服务 | 镜像 | 宿主机地址 | 映射端口 | 容器端口 | 状态 | 角色 |
|---|---|---|---|---|---|---|---|
| `log_system-fluentbit-1` | fluentbit | log_system-fluentbit | （仅容器网络） | — | 2020 | running | 采集 agent（tail 多来源文件 → 注入来源租户 → HTTP 上报 ingest-service `/ingest/collector`） |
| `log_system-log-generator-1` | log-generator | log_system-log-generator | （无端口） | — | — | running | 测试业务模拟器，向共享卷持续输出多租户日志 |

---

## 三、最常用访问入口（直接开浏览器）

| 看什么 | 地址 | 说明 |
|---|---|---|
| 前端页面 | http://localhost:8080 | Vue3 SPA（frontend/dist 挂载），**需登录**：`admin / Admin@123`（平台管理员）、`ops_gaoyuan / Ops@12345`（acme 租户普通账号） |
| 租户管理 | http://localhost:8080/settings/tenants | 平台管理 → 租户管理（新建/编辑/配额/启停/删除，需 `tenant:*` 权限） |
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

> **上游解析**：nginx 对 `proxy_pass` 的域名默认只在启动时解析一次，后端容器重建换 IP 后会打到错的服务（表现为串服务 / 404）。因此 `nginx.conf` 里统一用 `resolver 127.0.0.11 valid=10s` + 变量形式 `proxy_pass`，让解析发生在运行期；变量形式不会自动剥离 location 前缀，故每条路由配一条 `rewrite ... break`。**改完后端不再需要重启 web**。
| `/osd/` | opensearch-dashboards:5601（含 WebSocket 升级，看板便捷入口） |

---

## 五、采集数据现状（OpenSearch 索引）

数据链路（**来源元数据注入型**，租户不再依赖日志正文）：

```
log-generator  →  按租户分文件写出 /var/log/app/<tenant>.log（正文无 tenant 字段）
                        │
                  fluentbit  tail（Path_Key 记录来源文件名，DB 断点防重复）
                        │
                  parser filename_tenant：由文件名解析出 tenant_id
                        │
                  HTTP 上报 ingest-service POST /ingest/collector（X-Collector-Key 鉴权）
                        │
                  normalize_record()：来源事件时间 → @timestamp（归一化为 UTC）
                                     + 租户服务端强制覆盖
                        │
                  derive_tenant() 判定 → helpers.bulk 分片写入 logs-<tenant>
                        └─ 判定不出 / 租户不存在 → logs-unknown（绝静默归 default）
```

> **时间语义**：Fluent Bit 5.x 把抽取到的事件时间放在 `date`（epoch 秒）字段，而不是
> `@timestamp`。接入层必须识别 `date`/`time`/`ts` 等多种时间键并归一化，否则会全部退化成
> 采集时刻——历史日志会整体挤到当天，按天聚合与趋势图失真（本项已于 2026-11-18 修正）。

| 索引 | 文档数 | 存储 | 说明 |
|---|---|---|---|
| `logs-default` | 13884 | 2.8mb | default 租户日志 |
| `logs-acme` | 13792 | 2.8mb | acme 租户日志 |
| `logs-gp` | 13891 | 2.8mb | gp 租户日志 |
| `logs-unknown` | 2208 | 585kb | **未归属日志**：来源无法判定租户的被隔离在此，供治理归位 |
| `top_queries-*` | — | — | 查询分析索引（性能观测用） |
| `.kibana_1` 等系统索引 | — | — | Dashboards 内部索引（无需关注） |

**时间分布**（按 `@timestamp`，本地时区）：

| 日期 | 条数 | 说明 |
|---|---|---|
| 2026-11-11 ~ 11-16 | 每人 2880 | 启动回填基线，2 条/分钟 |
| 2026-11-17 | 23040 | 基线 + 09:00-22:00 补录（8 条/分钟·租户，按业务峰谷浮动） |
| 2026-11-18 | 持续增长 | 凌晨至 11:47 基线 + 11:47 起实时流（1 条/秒） |

补录工具：`tools/log-generator/backfill.py`，走与实时流完全相同的采集链路（写
`/var/log/app/<tenant>.log` → Fluent Bit → `/ingest/collector`），不绕过采集器直写索引：

```bash
docker compose exec -T log-generator python /app/backfill.py \
    --date 2026-11-17 --from 09:00 --to 22:00 --per-min 8
```

> 文档数随 `log-generator` 持续输出实时增长。`unattributed.log` 以 5% 比例模拟「来源不明系统」，用于验证未归属治理链路。

**租户归属四类来源（生产对应关系）**：

| 日志类型 | 生产环境打标方式 | 本地模拟 |
|---|---|---|
| 容器 / 应用日志 | K8s namespace、label → 采集器逐条注入 | 按租户分文件，文件名注入 |
| 云主机 / 系统日志 | 采集器实例 tag → CMDB 映射 | 采集器配置固定 `tenant_id` |
| App 直报 | 接入 Token 绑定租户 / OTLP resource | `POST /ingest` 用 `X-Api-Key` |
| 归属不明 | 落 `logs-unknown` 并告警 | `unattributed.log` |

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
