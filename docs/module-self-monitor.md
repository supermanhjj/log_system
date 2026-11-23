# 自监测总览

面向平台管理员和运维值班的自监测入口，菜单位于「概览 → 自监测总览」，路由 `/monitor`。

## 权限

| 权限点 | 用途 | 默认角色 |
|---|---|---|
| `monitor:view` | 组件健康总览、全链路拓扑、采集器清单 | 平台管理员、运维值班、只读用户 |
| `monitor:diagnose` | 执行采集链路一键诊断（会写审计） | 平台管理员、运维值班 |

权限仍由 system-service 的 `permissions` 表现查；角色权限变更后下一次请求即生效。

## 接口

### `GET /monitor/health`

并发展活 15 个组件，返回 `items / summary / status / updatedAt`。覆盖：

- 接入层：Web 网关、认证、平台管理、接入网关、Agent 管控
- 数据层：PostgreSQL、OpenSearch、Kafka、ClickHouse、Redis
- 计算层：Flink JobManager
- 查询层：检索、看板、告警、智能服务

每项带状态（`ok/warn/fail/unknown`）、耗时、详情。`warn` 不算故障，例如 OpenSearch 集群 yellow、可选服务不可用。

### `GET /monitor/collectors?tenant=`

返回当前可见租户的采集器心跳台账：主机名、租户、版本、最近心跳、心跳间隔、状态与原因。

### `GET /monitor/topology?tenant=`

返回全链路节点和边。节点按固定分层：采集器 → 接入/管控 → Kafka → Flink → 存储 → 租户索引 → 查询/平台 → Web。边状态由两端节点状态推导；最近 15 分钟流量来自 OpenSearch `logs-*` 按 `host` / `tenant_id` 聚合。

第一版多采集器的边流量使用 host/tenant 近似。要精确区分采集器集群，需要给心跳补 `cluster_id` / `collector_code`，或在 ingest 写日志时补 `collector_id`。

### `POST /monitor/diagnose`

真实执行 11 个检查步骤：

1. 采集器注册
2. 采集器心跳
3. 采集凭证
4. Namespace 归属映射
5. 主机台账
6. 接入网关
7. Kafka 缓冲 / 直写存储
8. 目标索引
9. 最近 15 分钟写入
10. 检索回读（带当前登录态调用 `search-service /query`）
11. Web 展示

每步返回 `ok/warn/fail/skip`、详情、证据和处理建议。执行结果写审计日志。

## 前端

- `frontend/src/views/SelfMonitor.vue`：页头下方保留 4 个指标卡（组件健康 / 采集器在线 / 近 15 分钟写入 / 链路节点）；随后展示全链路状态图，组件健康总览紧接在下方；采集链路一键诊断放在页头右上角，点击后以弹窗展示诊断过程。页面每 30 秒静默刷新一次拓扑与健康数据，页面隐藏时暂停
- 拓扑使用原生 SVG 自适应宽度，按容器宽度铺满卡片；节点名称完整展示，警告/异常组件在图上和健康列表中都给出具体原因
- `frontend/src/api/monitor.js`：`/api/system/monitor` 客户端
- 拓扑使用原生 SVG 固定分层（采集器 / 接入管控 / 缓冲 / 计算 / 存储 / 租户 / 索引 / 查询平台 / Web），不使用力导向，避免每次刷新节点乱跳
- 节点颜色表示健康状态，连线颜色表示边界状态；连线箭头表示数据或调用方向；Flink 同时写入 OpenSearch 明细与 ClickHouse 聚合宽表

## 已知边界

- 组件探活是进程/端口级，不等价于业务功能自检
- 采集凭证统计按 `log_collectors.tenant_allow`，历史 legacy 采集器可能显示 0 个凭证但仍可上报
- 主机台账只对 host-agent 场景强制；容器日志优先按 `namespace_tenant` 判定
