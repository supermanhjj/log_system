# 日志接入管线设计（v4.2 · 公有云 VM + 国网私有云 · 仅主机与业务日志）

> 状态：方案待评审
> 决策输入：① 解析放服务端 ② 部署载体为公有云 VM（阿里/华为）+ 国网私有云（VM 与私有 K8s 两种）③ 允许部署 Fluent Bit 采集器 ④ 租户逻辑隔离 ⑤ **本期只做主机日志与业务日志，云 API（RDS/SLB/OSS/SLS）通道暂不纳入**
> 典型接入对象：SG-I6000 传统版（VM 裸跑 SpringBoot）与微服务版（国网私有云 K8s），见第 2 节
> 影响范围：ingest-service 重构、采集包与配置下发、pipeline/parser 管理、交付包

---

## 0. 本期范围与三条关键判断

### 本期做什么

```
ECS 主机
  ├ 业务应用日志      /opt/*/logs/*.log、/home/*/app/logs/*
  ├ 系统日志          /var/log/messages、secure、cron、kern.log
  ├ 自建中间件日志    nginx、redis、kafka、tomcat
  └ 自建数据库日志    自建 mysql / postgresql（非 RDS）
        │
        ▼  HTTP（出方向）
  ingest-service 统一解析管线 → Kafka → Flink → OpenSearch / ClickHouse
```

一条通道，三类来源（业务 / 系统 / 自建中间件与数据库），全部是文件采集。

### 本期不做，但预留

托管服务日志（RDS 慢日志、SLB 访问日志、OSS 归档、SLS/LTS）不在本期。原因：这些需要云 API 与 AK/SK 管理，是独立的一块工程，且客户当前痛点集中在自建应用与主机日志。

预留方式：`data_sources.channel_type` 保留枚举值，UI 置灰，代码留适配层接口，后续接入不改数据结构。

### 判断一：配置下发是本期 P0

云主机逐台差异（日志路径、要采的服务、运行用户、轮转策略都不同），几十台机器就是几十份配置。

下发必须是 **pull（采集器主动拉）**，不能 push：云上 VPC、安全组、NAT、无固定公网 IP，push 的失败面太大且重试语义差；pull 天然容忍网络抖动，采集器离线再上线会自动收敛到最新配置。

### 判断二：多行合并必须留采集端

服务端收到的是打散的单行流，无法判断某行是否属于上一条（Java 异常栈、Python traceback、Go panic）。这是服务端解析的硬边界。

多行属于「分帧」，不属于「字段抽取」——与「解析放服务端」不矛盾。采集端只保留这一项解析职责。

### 判断三：逻辑隔离有伪造漏洞，必须补

当前 `derive_tenant()` 优先信任采集端注入的 `tenant_id`。任何持有 collector key 的人都能往任意租户写数据，索引前缀与行级过滤在此前提下形同虚设。

必须改为「凭证绑定允许范围 + 越界拒收 + 审计告警」。这条不补，租户隔离只是名义上的。

---

## 1. 架构分层

```
┌──────────────── 采集端（每台 ECS）────────────────┐
│  Fluent Bit（单二进制，systemd 托管）              │
│    ├ 文件 tail（多路径、通配、DB 断点续读）         │
│    ├ multiline 分帧（唯一保留的解析能力）           │
│    ├ filesystem storage（网关不可用不丢）           │
│    └ 原文 + 来源提示上行，不做字段抽取               │
│                                                    │
│  ruilog-sync（配置同步器，systemd timer）          │
│    └ 每 30s GET 配置 → 变更则写 conf.d → SIGHUP    │
└───────────────────────────────────────────────┘
                    │ HTTPS 出方向
                    ▼
┌──────────────── 接入网关（ingest-service）────────┐
│  1 鉴权        collector key → 查表 → 白名单       │
│  2 匹配        pipeline 按来源提示选解析器          │
│  3 解析        json / grok / regex / delimiter / kv │
│  4 映射        抽取字段 → 8 契约字段 + attrs        │
│  5 归属        规则绑定 → 凭证绑定 → hosts 兜底      │
│  6 归一        级别 / 分类 / 脱敏                   │
│  7 标识        event_id = sha256(tenant+ts+raw)    │
│  8 写入        按租户分片：OS logs-<tenant> + Kafka │
└───────────────────────────────────────────────┘
                    │
                    ▼
        Kafka ──► Flink ──► OpenSearch / ClickHouse
```

核心性质：**改解析规则不碰任何主机**。新增一类日志 = 平台加 parser + pipeline，实时生效，可对历史 raw 重放。

---

## 2. 数据源类型（本期三类）

| 类型 | channel_type | 场景 | 归属方式 |
|---|---|---|---|
| 主机文件 | `agent` | 业务应用、系统日志、自建中间件/数据库 | 采集规则绑定 / 凭证绑定 |
| Syslog 接收 | `syslog` | 网络设备、无法装 agent 的老旧系统 | 源 IP → hosts 台账 |
| HTTP 直报 | `http_api` | 业务系统主动 POST（既有能力） | API Key 绑定租户 |

后两类是低成本兜底，保留即可，不是本期重点。

**已预留但置灰**：`cloud_api`（RDS/SLB）、`object_storage`（OSS/OBS）、`log_service`（SLS/LTS）。

### 典型接入对象：SG-I6000 的两代形态

I6000 是国内网现网主流的运维管控平台，但它**不是公有云负载**：不走阿里云 ACK / 华为云 CCE，而是跑在国网私有云（国网云）底座上，分两代形态。这两代恰好对应两种完全不同的采集方式，是本次接入设计必须同时覆盖的样本。

| 维度 | 传统版 I6000 | 微服务版 SG-I6000 2.0 |
|---|---|---|
| 运行载体 | 国网云虚拟机 / 物理机 | 国网私有云 K8s 集群（非公有云托管） |
| 应用形态 | SpringBoot jar / 进程，无编排 | 容器镜像 + Helm 包，SpringCloud 微服务 |
| 日志落点 | 文件（logback / log4j 输出） | Pod stdout（CRI 格式）或容器内文件 |
| 采集方式 | Fluent Bit 文件采集（systemd） | Fluent Bit DaemonSet（每节点一个） |
| 配置来源 | 按主机渲染 | 按节点渲染（各节点配置一致） |
| 归属方式 | 采集规则绑定（一条路径 → 一租户） | 按 namespace / 规则绑定 |

**两套采集形态在平台侧共用同一套解析管线**——logback 格式的 grok 只写一次，传统版和微服务版都用它。差异只在采集端怎么把原文取出来。

#### 传统版：logback 文件 + grok

典型路径 `/opt/i6000/*/logs/*.log`，典型格式：

```
2026-11-19 18:02:11.334 [http-nio-8080-exec-3] INFO  c.s.i.order.OrderController - 开始处理订单
```

对应 grok：

```
%{TIMESTAMP_ISO8601:ts}\s+\[%{DATA:thread}\]\s+%{LOGLEVEL:level}\s+%{JAVACLASS:logger}\s+-\s+%{GREEDYDATA:message}
```

字段映射：`ts → @timestamp`、`level → level`、`message → message`、`logger → source`、`thread → attrs.thread`。

多行：Java 异常栈用 `java` 分帧规则（首行 `^\d{4}-\d{2}-\d{2}`），否则一个 SQL 异常会产生 20 条无意义的裸行。

#### 微服务版：国网云 K8s 的真实约束

私有云 K8s 与公有云托管集群的管理方式不同，落地时有三处会卡：

| 约束 | 影响 | 应对 |
|---|---|---|
| 镜像准入 | 镜像需推入国网云 Harbor 并通过安全扫描，不是 `docker push` 即可 | 交付物提供「镜像 + 扫描说明 + 部署申请单」 |
| 权限收紧 | 可能禁止 `privileged`、限制 `hostPath` 挂载 | 先按「只读挂载 `/var/log/containers`」申请；被拒则降级方案 |
| 安全加固 | 准入控制器可能拦截未登记的工作负载 | 提前让平台方在白名单登记 DaemonSet |

若 DaemonSet 无法落地，降级顺序（按侵入性从小到大）：

1. **日志落共享存储**（NFS / 国网云共享卷）→ 集中节点采集，不动集群权限
2. **应用侧 logback 加 HTTP appender** 直报 `/ingest`，完全不碰集群，但要改应用配置并重启
3. **Sidecar** 每 Pod 一个，不占节点权限但资源开销大，最后选

这三个方案的解析规则完全相同，只是取原文的位置不同。**这是把解析放服务端带来的实际收益**——换采集方式不用重写解析。

#### 但有一个前置问题必须先确认：安全分区

I6000 承载调度监控、运检监控等业务，有 I/II 区安全要求。日志分析平台通常部署在 III 区（管理信息大区）。**I/II 区与 III 区之间有正/反向物理隔离装置，HTTP 不通。**

这直接决定架构成立与否：

| 场景 | 采集链路 | 实时性 |
|---|---|---|
| 同区部署 | Fluent Bit → HTTPS → ingest | 秒级 |
| 跨区部署 | Fluent Bit 落本地文件 → 隔离装置摆渡 → III 区采集器读文件 | 取决于摆渡周期（分钟级） |

跨区时「采集器直连网关」不成立，必须增加「落文件 + 摆渡 + 二次采集」环节。这会让实时告警的能力大幅下降，需要在方案阶段就跟客户对齐可接受度，不能等实施时才发现。

---

## 3. 租户归属（不依赖云 API）

不做云 API 意味着拿不到实例标签，所以归属改为**配置驱动**，三级：

```
1. 采集规则绑定      collect_rules.tenant_code —— 一条规则（= 一组路径 + 应用名）归属某租户
2. 采集器凭证绑定    log_collectors.tenant_allow —— 整机/整批机器归属某租户
3. hosts 台账        cmdb_instances —— 手工导入或 CMDB 同步，兜底
4. 都不命中          logs-unknown + 告警（禁止静默归 default）
```

### 为什么第一级是规则而不是主机

一台 ECS 上可能跑多个应用，甚至（少见但存在）不同应用属于不同部门。规则级绑定比主机级更贴近现实：

```jsonc
{
  "rule_id": "in-order-app",
  "paths": ["/opt/order/logs/*.log"],
  "tenant_code": "acme",          // 这条规则采到的日志归 acme
  "source_hint": { "app": "order-service", "type": "app" }
}
```

系统日志这类"整机性质"的日志，用凭证绑定（第 2 级）更省事——一台机器只属于一个租户时，配一次即可。

### 归属不可伪造

```
租户 = 规则绑定 ∩ collector.tenant_allow
越界 → 401 + 审计 + 告警
采集端注入的 tenant 与推导不一致 → 以推导为准，记 warning（不丢弃，便于发现配置错）
```

---

## 4. 解析器

### 六种类型（本期实现五种，syslog 已有）

| type | 适用 | config 结构 |
|---|---|---|
| `json` | 应用输出 JSON（Spring Boot、Go、Node 常见） | `{"json_path": ""}` |
| `grok` | 绝大多数文本日志（主力） | `{"patterns": ["%{TIMESTAMP_ISO8601:ts} %{LOGLEVEL:level} %{GREEDYDATA:message}"]}` |
| `regex` | grok 表达不了的 | `{"regex": "(?P<ts>...)"}` |
| `delimiter` | 固定分隔符 / CSV / TSV | `{"delimiter": "\\t", "fields": [...]}` |
| `kv` | `a=1 b=2` 形式（Java 应用日志常见） | `{"kv_delimiter": "=", "pair_delimiter": " "}` |
| `syslog` | RFC3164 / RFC5424 | 已有 |

**grok 是主力**，需要内置模式库约 40 条（`TIMESTAMP_ISO8601`、`LOGLEVEL`、`IP`、`IPORHOST`、`NUMBER`、`WORD`、`DATA`、`GREEDYDATA`、`UUID`、`JAVACLASS`、`JAVATHREAD` 等）。

选择 grok 而非裸 regex 的理由：现场运维与技术支持改 grok 模板的准确率远高于改正则；日志易、Logstash 生态均用 grok，学习成本最低。

### 试解析（现场适配的核心交互）

`POST /parsers/test` 传解析器定义 + 一段真实日志，返回：

```json
{
  "ok": true,
  "extracted": { "ts": "2026-11-19 18:02:11,334", "level": "ERROR", "thread": "http-nio-8080-exec-3", "message": "..." },
  "mapped": { "@timestamp": "...", "level": "ERROR", "attrs": { "thread": "http-nio-8080-exec-3" } },
  "unmapped": ["logger_name"],
  "warnings": ["ts 未映射到契约字段，时间将退化为采集时刻"],
  "elapsed_ms": 3
}
```

`warnings` 必须反映真实风险。时间未映射是最常见也最难发现的问题——表现为「历史日志全挤在当天」。

---

## 5. 数据契约

### 固定契约字段（八个，不允许现场改）

`@timestamp / tenant_id / level / host / source / message / log_class / source_type`

检索、告警、看板、PPL 的公共语言。允许现场重命名 `level` 会让所有上层功能散掉。

### 扩展字段

其余全部进 `attrs`。OpenSearch 侧已是 `flat_object`，加子字段无需改 mapping。ClickHouse 侧当前为 `String`（JSON 串），建议迁 `Map(String,String)`，属 P2。

### 新增溯源字段

| 字段 | 用途 |
|---|---|
| `collector_id` / `rule_id` | 哪个采集器、哪条规则上报 |
| `pipeline_code` | 命中哪条管线（排查「配了没生效」的第一入口） |
| `parse_status` | `ok` / `unmatched` / `failed` |
| `parser_version` | 解析器版本，重放后区分数据来源 |

**「不丢」原则**：未匹配 pipeline 的日志必须入库并标 `unmatched`，不得丢弃。否则现场无法发现漏配，是数据质量问题最隐蔽的一种。

---

## 6. event_id 必须同期改

当前 `event_id` 依赖 Flink 侧 `seq` 序号。重放同一条原始日志时 seq 变化 → 变成两条，`ReplacingMergeTree(ver)` 覆盖不掉，重复计数。

改内容哈希：

```
event_id = sha256(tenant_id + "@timestamp" + raw)
```

同一行原文无论解析几次，ID 恒定。这是「改规则可重放历史」的前提，**必须与 P0 一起改**。

副作用：同秒同内容的完全重复行会被去重。多数场景是收益（重复打印属噪声），如需保留可在 raw 后附加采集器序列号。

---

## 7. 采集器（Fluent Bit + 配置同步器）

### 为什么继续用 Fluent Bit，不自研

Fluent Bit 是 C 静态编译的单二进制（约 20MB），无运行时依赖，原生满足主机采集的全部硬要求：

| 要求 | Fluent Bit 原生能力 | 缺了会怎样 |
|---|---|---|
| 断点续读 | tail 的 `DB` 文件（inode + offset） | 每次重启重复入库 |
| 磁盘缓冲 | `storage.type filesystem` + `storage.path` | 网关抖动即丢数据 |
| 多行分帧 | `multiline.parser`（块首 `start_state`） | 异常栈被拆成几十条 |
| 轮转跟踪 | tail 原生支持 rotate / truncate | 轮转后丢日志或重复 |
| 热加载 | `SIGHUP` 重载，不清 DB 断点 | 改配置要重启，中断采集 |
| 系统集成 | 官方 systemd unit；也有官方镜像可跑 DaemonSet | — |

自研等同于重写这些能力，收益为零、风险很高。**不写自研 agent。**

### 唯一要补的短板：不会主动拉配置

Fluent Bit 配置是静态文件，没有内置「从远端拉配置」的能力。补法很轻：

```
ruilog-sync（约 200 行，shell 即可，不必 Go）
  systemd timer 每 30s 触发
  → GET /agent/v1/config?v=<本地 hash>（带 collector key）
  → hash 有变：原子写 /etc/ruilog/conf.d/*.conf
  → kill -HUP $(pidof fluent-bit)
```

不需要常驻守护进程，不需要重写采集器。timer + curl + 一次 SIGHUP 就够。

### 自更新

同样交给 `ruilog-sync`：GET `/agent/v1/version` 比对 → 下载 tar.gz → 校验 sha256 → 解包替换二进制 → `systemctl restart ruilog-fluentbit`。DB 断点在 `/var/lib/ruilog/db`，重启不丢位点。

### 资源与限流

| 项 | 建议值 |
|---|---|
| 内存缓冲上限 | `Mem_Buf_Limit 64M`（按机器日志量调） |
| 磁盘缓冲 | 512MB，环形覆盖最旧 |
| 大文件保护 | `Buffer_Max_Size` + 读限速，防单文件打满 |
| 输出重试 | `Retry_Limit False`（无限重试，配合磁盘缓冲不丢） |

### bootstrap 配置（唯一手工编辑的文件）

```yaml
# /etc/ruilog/agent.conf
server: https://ruilog.example.com
collector_key: ${RUILOG_COLLECTOR_KEY}   # 也可写在同目录 0600 权限文件
buffer_dir: /var/lib/ruilog/buffer
```

其余全部由服务端渲染下发。

### 安装方式（三选一）

| 方式 | 适用 | 说明 |
|---|---|---|
| 一键脚本 | 机器可出公网 | `curl -fsSL <platform>/install/agent.sh \| sudo bash -s -- --key=... --server=...` |
| **离线包** | 内网机器（常见） | `ruilog-agent-1.0.0-linux-x86_64.tar.gz`，自包含二进制 + unit + 卸载脚本 |
| 云厂商批量执行 | 批量下发 | 阿里云 OOS 扩展程序 / 华为云 COC 批量命令，用离线包执行 |

**离线包是刚需**：客户内网 ECS 普遍不能出公网，curl 脚本会直接失败。

### 网络方向

**只要求出方向** HTTPS（443）到接入网关。云上开入方向需要审批，出方向通常放行，部署阻力最小。采集器不开监听端口（除本地 health 端口 127.0.0.1）。

---

## 8. 配置下发模型

服务端按「该采集器的规则集合」渲染完整配置，采集器只比对 hash：

```yaml
# 服务端渲染产物，采集器不手工编辑
version: 8f3a2c1e                    # 内容 hash
collector_id: ecs-prod-01
ingest: { url: ..., key_ref: env }
buffer: { path: /var/lib/ruilog/buffer, max_size: 512MB }
inputs:
  - id: in-order-app
    type: file
    paths: ["/opt/order/logs/*.log"]
    exclude: ["*.gz", "*.1", "*.2026*"]
    multiline: java
    tenant_code: acme                 # 规则级归属
    source_hint: { app: order-service, type: app }
  - id: in-syslog
    type: file
    paths: ["/var/log/messages", "/var/log/secure"]
    multiline: none
    source_hint: { type: syslog }
```

`source_hint` 仅作提示，`log_class` / `source_type` 最终由服务端 pipeline 按真实内容判定——采集端填错不该污染数据，服务端有更完整上下文。

### 渲染时机

- 规则增删改 → 立即重算该采集器的 `config_version`（内容 hash）
- `ruilog-sync` 每 30s 拉取，hash 不一致则原子写入 conf.d 并 `SIGHUP`
- UI 点「立即下发」= 置空服务端缓存强制重渲染，最长 30s 生效

---

## 9. 多行分帧规则

```yaml
multiline_rules:
  java:    { firstline: '^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}', maxlines: 500 }
  python:  { firstline: '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]\d+ ' }
  go:      { firstline: '^time="' }
  generic: { firstline: '^\S+\s+\d+\s+\d{2}:\d{2}:\d{2}' }
```

踩坑固化（来自既有实践，写入实施清单）：
- **块首规则只能放 `start_state`，`cont` 只认块内其余行**，放错会多块粘连成一条
- `maxlines` 必须设上限，否则一行格式错乱会把整个文件吞成一条，表现为「日志量突然归零」
- 分帧规则改错的影响是灾难性的（整段日志变一条），必须提供「试分帧」预览

---

## 10. 表结构

```sql
-- 解析器
CREATE TABLE IF NOT EXISTS log_parsers (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                     -- json|grok|regex|delimiter|kv|syslog
    config JSONB NOT NULL DEFAULT '{}',
    sample TEXT NOT NULL DEFAULT '',
    builtin BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 管线
CREATE TABLE IF NOT EXISTS log_pipelines (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    match JSONB NOT NULL DEFAULT '{}',
    parser_code TEXT REFERENCES log_parsers(code) ON DELETE SET NULL,
    field_map JSONB NOT NULL DEFAULT '{}',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 采集器
CREATE TABLE IF NOT EXISTS log_collectors (
    collector_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    cloud TEXT NOT NULL DEFAULT '',          -- aliyun|huawei|""（自建机房）
    key_hash TEXT NOT NULL,                  -- sha256，不存明文
    tenant_allow JSONB NOT NULL DEFAULT '[]',
    config_version TEXT NOT NULL DEFAULT '',
    agent_version TEXT NOT NULL DEFAULT '',
    last_seen TIMESTAMPTZ,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 采集规则（配置渲染的来源）
CREATE TABLE IF NOT EXISTS collect_rules (
    id SERIAL PRIMARY KEY,
    collector_id TEXT NOT NULL REFERENCES log_collectors(collector_id) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    input_type TEXT NOT NULL DEFAULT 'file',
    paths JSONB NOT NULL DEFAULT '[]',
    exclude JSONB NOT NULL DEFAULT '[]',
    multiline TEXT NOT NULL DEFAULT 'none',
    tenant_code TEXT NOT NULL DEFAULT '',    -- 规则级归属
    source_hint JSONB NOT NULL DEFAULT '{}',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (collector_id, rule_id)
);

-- 主机台账（兜底归属，本期手工导入 / 后续 CMDB 同步）
CREATE TABLE IF NOT EXISTS cmdb_instances (
    instance_id TEXT PRIMARY KEY,
    cloud TEXT NOT NULL DEFAULT '',
    hostname TEXT NOT NULL DEFAULT '',
    private_ip TEXT NOT NULL DEFAULT '',
    tenant_code TEXT NOT NULL DEFAULT '',
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`match` 结构（云主机场景）：

```jsonc
{
  "channel": "agent",                  // agent | syslog | http_api
  "app_regex": "order-.*",
  "path_regex": "/opt/order/logs/.*",
  "source_type_in": ["app"],
  "host_regex": ""
}
```

语义：非空条件取 AND；`priority` 小者优先；无命中落 `unmatched`。

---

## 11. API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST/PUT/DELETE | `/parsers`、`/parsers/{code}` | CRUD，内置不可删、被引用不可删 |
| POST | `/parsers/test` | 试解析 |
| POST | `/multiline/test` | 试分帧（返回合并后的条目数与预览） |
| GET/POST/PUT/DELETE | `/pipelines`、`/pipelines/{code}` | CRUD |
| GET | `/pipelines/{code}/preview?minutes=30` | 用近 30 分钟真实日志试跑，看命中数与解析成功率 |
| POST | `/pipelines/{code}/replay` | 重放 `{from, to, dryRun}` |
| POST/GET/PUT | `/collectors`、`/collectors/{id}` | 注册（返回一次性 key）/ 列表 / 改白名单 |
| GET/POST/PUT/DELETE | `/collectors/{id}/rules` | 采集规则 CRUD |
| GET | `/agent/v1/config?v=N` | **采集器拉配置**（key 鉴权，返回渲染结果 + version） |
| POST | `/agent/v1/heartbeat` | 心跳（版本、上报量、丢弃量、积压） |
| GET | `/agent/v1/version` | 检查采集器新版本 |

---

## 12. 文件级改动清单

### 新增

| 文件 | 职责 | 规模 |
|---|---|---|
| `services/ingest-service/app/grok.py` | grok 编译 + 内置模式库 | ~250 行 |
| `services/ingest-service/app/parsers.py` | 五类解析器，统一接口 `parse(raw, config) -> dict` | ~300 行 |
| `services/ingest-service/app/pipeline.py` | 匹配 / 映射 / 归属 / 编排 | ~350 行 |
| `services/ingest-service/app/registry.py` | parser/pipeline/collector 的 DB 读写 + 缓存 | ~250 行 |
| `services/ingest-service/app/collectors.py` | 注册、鉴权、白名单、配置渲染 | ~300 行 |
| `services/ingest-service/app/replay.py` | 读 raw → 重跑 → 回写 | ~200 行 |
| `services/ingest-service/app/api_pipelines.py` | 上述路由（拆文件，避免 main.py 膨胀） | ~300 行 |
| `agent/config-sync/` | 配置同步器（shell 即可）+ 自更新逻辑 | ~200 行 |
| `agent/install.sh`、`agent/ruilog-fluentbit.service` | 安装、systemd 托管、卸载 | ~250 行 |
| `agent/deploy/docker/fluentbit/*.conf.tpl` | 采集配置模板（tail / multiline / output） | ~150 行 |
| `agent/k8s/daemonset.yaml` | 国网云 K8s 采集形态（含权限申请说明） | ~120 行 |
| `deploy/pack/build-agent-pkg.sh` | 打离线包（x86_64 + aarch64） | ~120 行 |
| `docs/cloud-deployment.md` | 云上部署手册 | — |

### 修改

| 文件 | 改什么 | 风险 |
|---|---|---|
| `services/ingest-service/app/main.py` | `/ingest/collector` 改走 `pipeline.process()`；`derive_tenant` 移出并加强校验 | 高：主链路 |
| `services/ingest-service/app/log_schema.py` | 枚举改「代码默认 + DB 覆盖」 | 中：影响级别归一 |
| `services/ingest-service/app/kafka_writer.py` | 加 `pipeline_code`/`parser_version`；event_id 改 sha256 | 中：影响 Flink 去重 |
| `flink-jobs/.../LogRecord.java` | 新字段映射 | 中 |
| `deploy/shared/opensearch/index-templates/logs-template.json` | 加三个溯源 keyword | 低 |
| `deploy/shared/clickhouse/init/01-schema.sql` | 同步加列 | 中：需 migration |
| `deploy/docker/fluentbit/*` | 保留作本地开发环境，解析器砍到 multiline + 基础 | 低 |
| 前端数据源页 | 改为「采集器 → 规则 → 管线」三级 + 试解析 | 高：交互重构 |

### 保留不动

双引擎分工、单索引 + `log_class` 分类、`raw` 完整保留、无法归属落 `logs-unknown` 并告警。

---

## 13. 交付形态

### 平台侧

```
docker compose（单机 PoC / 小规模客户）或客户已有 K8s（复用 Helm 思路）
数据层：自建容器 或 云托管（阿里云 ES / 华为云 CSS / RDS / 消息队列）
```

**跨云第一问**：平台装在哪朵云？要采另一朵云的机器，专线/VPN/公网白名单怎么走？这决定可行性与成本，是现场踏勘第一件事。

### 交付包

```
ruilog-platform-1.4.0/
├── images.tar
├── compose.yml
├── .env.template
├── config/                  # nginx、索引模板、CH schema
├── sql/                     # 建表 + 内置 parser/pipeline 种子
├── scripts/{install,upgrade,doctor,uninstall}.sh
├── agent-packages/          # x86_64 / aarch64 离线包
├── VERSION
├── SHA256SUMS
└── DEPLOY.md                # 前置检查、安装、验收、常见问题
```

`doctor.sh` 必做：一键查各组件连通、版本、配置差异。现场排障第一个动作，能把「是不是没装好」和「是不是配置错」分开。

---

## 14. 迁移路径（不破坏现有演示链路）

**Step 1 · 并行**：建表、灌内置 parser、给现有 6 类日志配 pipeline；`ingest_collector` 加影子模式，新管线跑一遍只记录差异不切换。
**Step 2 · 切换**：`PIPELINE_ENABLED=true`；旧路径保留一版本作兜底（环境变量回切）。验收：级别分布、条数、时间分布与切换前一致。
**Step 3 · 瘦身上云**：Fluent Bit 砍解析器；collector key 改查表；上线 agent；本地 compose 保留作开发环境。

---

## 15. 分阶段任务

| 阶段 | 内容 | 验收 |
|---|---|---|
| A | grok + 五类 parser + 试解析 API | 现网 6 类日志样例，字段抽取人工核对 100% |
| B | pipeline 模型 + 匹配 + 映射 + 影子模式 | 影子比对差异 = 0 |
| C | 租户安全加固（collector 表 + 白名单 + 归属不可伪造） | 伪造 tenant_id 被拒并告警 |
| D | event_id 改 sha256 + 重放 | 重放后 OS 条数不变，字段按新规则更新 |
| E | Fluent Bit 采集包（tail + DB 断点 + filesystem 缓冲 + 多行）+ ruilog-sync 配置同步 | 3 台主机上线，改配置 30s 内生效，无需登录机器 |
| E2 | I6000 传统版接入验证（logback grok + 异常栈分帧） | 真实 I6000 日志解析成功率 ≥ 99% |
| F | 离线包 + 批量部署模板（OOS/COC） | 10 台机器 30 分钟内装完 |
| G | 交付包 + install/doctor | 全新环境 2 小时装完并通过冒烟 |
| H | 前端采集器/规则/管线管理 + 试解析 | 技术支持独立完成「新接一类日志」 |

A~E 是 P0，F~H 是 P1。

---

## 16. 待确认

1. **安全分区（最优先，决定架构成立与否）**：I6000 在 I/II 区，日志分析平台部署在哪个区？同区直连，跨区必须经隔离装置摆渡（实时性从秒级降到分钟级）。这一条不定，后面所有设计都可能白做。
2. **I6000 现网版本**：只接传统版（VM 裸跑），还是微服务版（国网云 K8s），还是两代并存都要接？决定是否需要同时出 systemd 包与 DaemonSet 包。
3. **国网云 K8s 权限边界**：能否部署 DaemonSet？能否只读挂载 `/var/log/containers`？准入控制器是否需要提前登记白名单？若都不行，走共享存储或应用直报。
4. **规模**：主机多少台、Pod 多少个、日均日志量多少 GB？决定采集并发、缓冲容量与平台容量规划。
5. **操作系统与 CPU 架构**：麒麟 / 统信 / CentOS？x86_64 还是 aarch64（鲲鹏）？决定离线包构建矩阵，信创环境需单独验证。
6. **是否要求采集端脱敏**：现在脱敏在服务端。等保三级若要求「敏感数据不出节点」，需双层脱敏，Fluent Bit 侧加 filter。
7. **日志轮转方式**：logrotate 还是应用自切（logback 的 `RollingFileAppender`）？切分命名规则决定 exclude 表达式，配错会重复采集或多采 .gz。
