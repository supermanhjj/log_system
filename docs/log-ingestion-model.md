# 多类型日志采集模型（系统 / 中间件 / 数据库 / 业务）

> 版本：v1.0（2026-11-19）
> 背景：此前平台只采集了模拟的业务应用日志（`/var/log/app/*.log`，自定义 `app_log` 正则）。
> 系统日志、中间件日志、数据库日志完全没有接入，导致"能查业务日志、查不了主机和数据库"。
> 本文定义扩展后的采集模型、字段契约、归属规则与落地清单。
> 关联：`docs/tech-stack-selection.md` 第 10 章（双引擎 + 流处理）、`docs/service-inventory.md`。

---

## 1. 设计原则

1. **采集器仍然只有一个**。业务主机、数据库主机上只部署一个 Fluent Bit 进程，靠**多个 input + tag 分族**覆盖全部日志类型，不引入第二套 agent。
2. **分类在采集端完成，归属在网关完成**。采集端知道自己采的是"哪台机器的哪个文件"（`log_class`/`source_type`/`host`），但不知道这台机器属于哪个业务租户——租户归属统一由接入网关按主机台账（CMDB）判定。
3. **原始事件必须保留**。任何解析都可能丢信息，`raw` 字段原样存整条（多行合并后）事件，供详情/上下文回显。
4. **单索引不拆**。日志类型作为 `log_class` 字段存进 `logs-<tenant>`，索引数保持 `租户数`，不随类型膨胀（压测后再评估是否按类型拆）。
5. **字段类型必须显式声明**。OpenSearch 动态 mapping 会按"第一个写入值"锁死类型，系统日志的 `pid`/`severity` 一旦被推断成 `text` 就再也改不了，所以走 index template。

---

## 2. 字段契约

### 2.1 通用字段（所有类型都有）

| 字段 | 类型（OS / CH） | 必填 | 说明 |
|------|----------------|------|------|
| `@timestamp` | date / DateTime64(3,'UTC') | ✅ | 事件时间（日志自带），非采集时刻 |
| `tenant_id` | keyword / LowCardinality(String) | ✅ | 租户编码，服务端强制覆盖 |
| `log_class` | keyword / LowCardinality(String) | ✅ | **日志大类**：`system` `middleware` `database` `business` `network` `container` |
| `source_type` | keyword / LowCardinality(String) | ✅ | 细分来源，见 2.2 |
| `app` | keyword / LowCardinality(String) | | 所属组件：nginx / redis / mysql / linux |
| `instance` | keyword / LowCardinality(String) | | 实例标识：`127.0.0.1:3306`、`order-svc-7d9f` |
| `host` | keyword / LowCardinality(String) | ✅ | 主机名（同时也是**归属判定依据**） |
| `level` | keyword / LowCardinality(String) | ✅ | 归一化级别：`DEBUG` `INFO` `WARN` `ERROR` `FATAL` |
| `severity` | integer / UInt8 | | syslog 原生 0-7（emerg…debug） |
| `facility` | keyword / LowCardinality(String) | | syslog facility：kern / authpriv / daemon / local0… |
| `pid` | integer / UInt32 | | 进程号 |
| `source` | keyword / LowCardinality(String) | | 业务来源（沿用现有语义，业务日志用） |
| `traceId` | keyword / String | | 链路追踪 ID |
| `status` | integer / UInt16 | | HTTP/结果状态码，0 表示无 |
| `message` | text / String | ✅ | 语义化正文（解析后） |
| `raw` | text / String | ✅ | 原始整条事件（多行合并后） |
| `attrs` | flattened / String(JSON) | | 类型特有字段，见 2.3 |
| `src_file` | keyword | | 采集来源文件（排障用） |
| `collector_id` | keyword | | 采集器标识，多采集器时定位问题 |

### 2.2 `source_type` 枚举

| log_class | source_type | 典型来源 |
|-----------|-------------|---------|
| system | `syslog` | `/var/log/syslog`、`/var/log/messages` |
| system | `auth` | `/var/log/auth.log`、secure |
| system | `kern` | `/var/log/kern.log`、dmesg |
| system | `cron` | cron/at 执行日志 |
| middleware | `nginx-access` / `nginx-error` | nginx |
| middleware | `redis` | redis-server 日志 |
| middleware | `kafka` | Kafka server.log |
| middleware | `tomcat` | catalina.out（含 Java 堆栈） |
| middleware | `rabbitmq` / `zookeeper` | 后续扩展 |
| database | `mysql-error` / `mysql-slow` | MySQL 错误日志 / 慢查询日志 |
| database | `postgres` | PostgreSQL 日志（statement/error） |
| database | `oracle-alert` | Oracle alert log（后续扩展） |
| business | `app` | 业务应用日志（现有） |
| network | `firewall` / `switch` | 网络设备 syslog（514 转发） |

### 2.3 `attrs` 各类型特有字段

| source_type | attrs 键 |
|-------------|----------|
| `nginx-access` | `method` `uri` `upstream_status` `upstream_time` `bytes` `referer` `ua` |
| `mysql-slow` | `query_time` `lock_time` `rows_sent` `rows_examined` `db` `user` |
| `postgres` | `user` `db` `statement_type` `duration_ms` |
| `syslog` / `auth` | `syslog_tag` `unit` |

---

## 3. 采集端设计（Fluent Bit）

### 3.1 配置结构

采集配置按类型拆分为多个 `conf.d/*.conf`，由主配置 `@INCLUDE` 汇总，新增一类日志只加一个文件：

```
fluentbit/
├── fluent-bit.conf            # SERVICE + @INCLUDE conf.d/*.conf
├── parsers.conf               # 全部解析器 + 多行解析器
├── conf.d/
│   ├── 10-business.conf       # 业务日志（现有）
│   ├── 20-system.conf         # 系统日志
│   ├── 30-middleware.conf     # 中间件日志
│   ├── 40-database.conf       # 数据库日志
│   ├── 50-tagging.conf        # 分类打标（modify/rewrite_tag）
│   └── 90-output.conf         # 统一上报接入网关 + 心跳
└── scripts/
```

### 3.2 tag 命名规范

```
sim.<log_class>.<source_type>       例：sim.database.mysql-slow
```

tag 只用于内部路由与打标，**不承载租户**——租户由 `host` 台账判定，一个采集器可跨多租户。

### 3.3 解析器矩阵

| 文件 | Parser | 多行 | 时间格式 |
|------|--------|------|---------|
| `/var/log/app/*.log` | `app_log`（现有自定义） | 否 | `%Y-%m-%dT%H:%M:%S%z` |
| `/var/log/sim/system/*.log` | `syslog-rfc3164` | 否 | `%b %d %H:%M:%S`（无年份，见 §6） |
| nginx access | `nginx`（内置） | 否 | `%d/%b/%Y:%H:%M:%S %z` |
| nginx error | `nginx_error`（自定义） | 否 | `%Y/%m/%d %H:%M:%S` |
| redis | `redis_log`（自定义） | 否 | `%d %b %Y %H:%M:%S.%L` |
| mysql error | `mysql_error`（自定义） | 否 | `%Y-%m-%dT%H:%M:%S` |
| mysql slow | `mysql_slow`（自定义） | ✅ `mysql_slow` 多行块 | 块首 `# Time:` |
| postgres | `pg_log`（自定义） | ✅ `pg_log`（续行） | `%Y-%m-%d %H:%M:%S.%L` |

### 3.4 打标方式

每个 input 家族用 `[FILTER] modify` 静态补 `log_class` / `source_type` / `app` / `host` / `instance`。
这些是**采集器的部署元数据**（这台机器的角色），不是日志正文属性，与"租户不写正文"的原则一致。

---

## 4. 归属判定（租户从哪来）

系统/中间件/数据库日志正文里**没有租户信息**，只能靠主机台账。判定优先级：

| 优先级 | 依据 | 适用场景 |
|-------|------|---------|
| 1 | 记录里显式带 `tenant_id` | K8s namespace/label 注入、应用 API Key 绑定 |
| 2 | `hosts` 表按 `host` 查（主机 → 租户/环境/角色） | 系统、中间件、数据库日志 |
| 3 | `src_file` 文件名 | 业务日志本地模拟（`<tenant>.log`） |
| 4 | 都判定不出 | 落 `logs-unknown` + 告警，**绝不静默归 default** |

`hosts` 表（由 ingest-service 建表并维护，生产由 CMDB 同步）：

```sql
CREATE TABLE IF NOT EXISTS hosts (
    host        TEXT PRIMARY KEY,      -- 主机名 / 容器 hostname
    tenant      TEXT NOT NULL,         -- 归属租户编码
    env         TEXT DEFAULT 'prod',   -- dev / test / prod
    role        TEXT,                  -- db / web / cache / mq
    note        TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 5. 接入层处理（ingest-service）

`/ingest/collector` 收到记录后按顺序处理：

1. **分类校验**：`log_class` 不在枚举内 → 按 `source_type` 推导；仍推导不出 → 归 `business`（兼容旧数据）并计数。
2. **级别归一**：`severity` 数字（0-7）映射为 `level`；无 severity 时用解析器给出的 `level`；都没有 → `INFO`。
   `0 emerg→FATAL, 1 alert→FATAL, 2 crit→FATAL, 3 err→ERROR, 4 warning→WARN, 5 notice→INFO, 6 info→INFO, 7 debug→DEBUG`
3. **时间归一**：沿用 `TIME_KEYS` 优先级，统一成 UTC ISO 毫秒（现有逻辑）。
4. **脱敏**：`message` 与 `raw` 做正则打码——`password=`、`pwd=`、`--password`、`Authorization: Bearer`、URL 里的 `user:pass@`、以及 MySQL 慢查询里的字符串字面量（可配置开关）。
5. **租户判定**：按 §4 优先级。
6. **落库**：OpenSearch `logs-<tenant>` + Kafka `ruilog.logs.raw`（按租户 key 分区）。

---

## 6. 已知坑（提前规避）

| 坑 | 现象 | 处理 |
|----|------|------|
| **动态 mapping 锁类型** | `pid` 第一条是空字符串 → 被推断成 `text`；后续数值写入报错或被转字符串 | 先建 index template，显式声明字段类型 |
| **RFC3164 无年份** | syslog 时间被解析成"今年"，跨年期间错一年 | 采集端用当前年补齐；跨年窗口内以采集时刻做合理性校正（偏差 >180 天则用当前年） |
| **multiline + tail DB** | 开多行后 offset 语义变化，重启可能重复/丢块 | 单独验证断点续传；`Flush` 与 `multiline flush_timeout` 要匹配 |
| **MySQL 慢查询多行块** | 每行被当成独立日志，`query_time` 抽不到 | 用 `multiline.parser` 按 `# Time:` 切块 |
| **日志量突增** | 系统日志量远大于业务日志，`Mem_Buf_Limit` 打满后丢数据 | 按类型设 `mem_buf_limit`；网关侧限流 + 采样 |
| **敏感信息** | 数据库日志含 SQL 明文、连接串密码 | 网关脱敏（第 5 节第 4 步），存储层持久加密后续演进 |

---

## 7. 本地验证环境（采平台自身组件，只留 1 个系统日志容器）

**设计取舍（2026-11-19 二次调整）**：容器化环境里，平台自己就是一堆真实中间件和数据库 —— nginx、redis、postgres、kafka 全是真进程。为它们各起一个"模拟容器"既重复又占资源（仅 mysql-sim 一个就常驻 370MB），采自己才是真实生产形态（K8s 里采的就是这些组件的日志）。

所以原则是：**能采自己的就采自己，只有采不到的才起容器。**

| 类型 | 采集源 | 日志如何落地 | 卷内路径 |
|------|--------|-------------|---------|
| middleware | `web`（前端/API 网关的 nginx） | `nginx.conf` 显式 `access_log`/`error_log` | `ruilog_logs/nginx-{access,error}.log` |
| middleware | `redis`（平台缓存/会话） | `--logfile /data/redis.log` | `redisdata/redis.log` |
| middleware | `kafka`（日志主干 broker） | 自定义 log4j `RollingFileAppender` | `kafkadata/server.log` |
| database | `postgres`（平台元数据库） | `logging_collector=on` + 相对 `log_directory` | `pgdata/pg_log/*.log` |
| business | `log-generator` | 原生多租户业务日志文件 | `app_logs/<tenant>.log` |
| system | `sys-sim`（**唯一保留的模拟容器**） | busybox `syslogd -O` 真实落盘 | `ruilog_logs/syslog` |

**为什么 system 还得留一个容器**：容器共享宿主内核，读不到宿主机的 syslog/journald，kernel/auth/cron 这类真实系统事件没有任何"顺带"来源。该容器是 alpine + busybox，常驻内存约 0.9MB、无额外磁盘，是验证 system 类采集的最小代价。

**MySQL**：本地不再部署（平台自身没有 MySQL，而 mysql:8.0 常驻 370MB）。解析器与多行规则完整保留在 `parsers.conf`，规则模板 `MySQL 慢查询堆积` / `MySQL 错误日志` 置为未启用，接入真实 MySQL 时取消 `40-database.conf` 里的注释即可。

**收益**：常驻容器 26 → 20，模拟容器内存合计约 410MB → 0.9MB，镜像再省 790MB（删 `mysql:8.0`）。

### 7.1 各组件日志落地的坑（都踩过）

1. **官方镜像把日志软链到 stdout**。`nginx:alpine` 的 `/var/log/nginx/access.log` 是 `-> /dev/stdout` 的符号链接，`docker logs` 能看但容器里没有可采集的文件。必须在 nginx.conf 里显式写 `access_log <路径>`。
2. **非 root 进程写不了共享卷**。命名卷挂到空目录时属主是 `root:root`，而 redis 以 `redis` 用户运行 → `Can't open the log file: Permission denied`，容器反复重启。正解是挂到镜像里**已有且属主正确**的目录（redis 的 `/data`），Docker 会把该目录的属主带进卷。
3. **bitnami Kafka 全是 ConsoleAppender**。`/opt/bitnami/kafka/logs` 也是空的（日志只走 stdout）。需要覆盖 `log4j.properties`：主日志用 `RollingFileAppender` 写数据卷根目录（`/bitnami/kafka` 属主 1001 可写，`config/logs` 属主 root 不可写），专用 appender 仍留 stdout，`docker logs` 不失效。
4. **PG 的 `log_directory` 用相对路径**。相对 `data_directory` 解析，postgres 用户天然可写，不必为它调卷权限。用绝对路径反而要额外 chown。
5. **PG 日志时区默认 UTC**。`log_line_prefix=%m` 输出的时间跟 `log_timezone` 走，默认 `GMT`；解析器按本地 `+0800` 解释会让所有 PG 日志整体偏 8 小时（跨日还会错到前一天）。必须设 `log_timezone=Asia/Shanghai`。
6. **YAML 会削掉 plain scalar 的尾随空格**。`log_line_prefix=%m [%p] %u@%d ` 写成裸标量后末尾空格丢失，日志变成 `loguser@logdbLOG:`（用户名和级别粘在一起）。加引号保留，解析器同时兼容两种形态。
7. **Kafka 启动会把整份配置 dump 成几百行制表符缩进文本**。不合并会让检索页被"无时间戳、无级别"的配置行刷屏。用 `MULTILINE_PARSER kafka_log`（块首 `^\[`、续行 `^\t`）合并成一条。

> 为什么坚持真进程而非脚本伪造：解析器的价值全在边界情况 —— 多行块、无时区标记的本地时间、空会话用户、启动期配置 dump。这些伪造不出来，也只有在真进程上才会暴露。

---

## 8. 落地清单

| 层 | 文件 | 变更 |
|----|------|------|
| 契约 | `docs/log-ingestion-model.md` | 本文 |
| 索引 | `opensearch/index-templates/logs-template.json` | 显式 mapping（新增字段 + `attrs` 用 `flat_object`，OpenSearch 无 `flattened`） |
| 索引 | `scripts/opensearch/apply-logs-template.sh` | 应用模板并对既有索引补 mapping |
| 采集 | `fluentbit/conf.d/*.conf`、`fluentbit/parsers.conf` | 多源 input + 分类打标 + 多行解析 |
| 编排 | `docker-compose.yml`、`compose.override.yml` | 平台自身组件落日志（nginx.conf / redis `--logfile` / kafka log4j / pg `logging_collector`）+ `ruilog_logs`、`redisdata` 卷；仅保留 `sys-sim` 一个模拟容器 |
| 编排 | `tools/kafka/log4j.properties` | 主日志 `RollingFileAppender` 落 `/bitnami/kafka/server.log`，专用 appender 留 stdout |
| 网关 | `services/ingest-service/app/log_schema.py` | 枚举、级别映射、脱敏、分类推导 |
| 网关 | `services/ingest-service/app/main.py` | hosts 表、derive_tenant 扩展、归一化接入 |
| 存储 | `clickhouse/init/01-schema.sql`、`clickhouse/migrations/*.sql` | 新增列 + 物化视图加 `log_class` 维度 |
| 流处理 | `flink-jobs/java/.../LogPipelineJob.java` | LogEvent 字段 + CH sink 映射 |
| 查询 | `services/search-service/app/main.py` | `log_class` / `source_type` / `app` 过滤（支持单值、逗号串、数组）；`_source` 与 `normalize_hit` 输出新字段；`/histogram` 增 `by_log_class` / `by_source_type` |
| 前端 | `frontend/src/views/SearchView.vue` | 「日志类型 / 来源类型 / 应用」列与字段开关；`log_class:` / `source_type:` / `app:` 语法；导出与详情抽屉补新字段 |
| 看板 | `services/dashboard-service/app/main.py` | `logClass` / `sourceType` 维度 + `logClass`/`sourceType` 筛选；总览新增 `logClassDist`；明细表加类型列 |
| 看板前端 | `frontend/src/views/OverviewView.vue`、`DashboardDetailView.vue` | 总览「日志类型分布」卡；编辑面板「日志类型 / 来源类型」筛选 |
| 告警 | `services/alarm-service/app/main.py` | `match_log_class` / `match_source_type` 列 + 匹配；9 条四类日志规则模板（幂等播种；MySQL 两条默认未启用、接入真实库时开启） |
| 告警前端 | `frontend/src/views/AlarmRules.vue`、`api/alarms.js` | 表单与「作用范围」列；判定条件文案 |
| 智能检索 | `services/ai-service/app/{nl2ppl.py,main.py}` | `BASE_FIELDS` 与语法库补分类字段/业务词 |

---

## 9. 查询、看板、告警如何用这个维度

采集打完标只是第一步，**维度要在四条链路上都能用**才有价值：

### 9.1 检索（search-service）

- 过滤入参统一走 `_terms_clause()`：单值 `"system"`、逗号串 `"system,middleware"`、数组 `["system","middleware"]` 三种形态都收敛成合法 DSL（直接 `term` 一个数组会 400）。
- 筛选字段对外用驼峰（`logClass` / `sourceType`），同时兼容下划线写法，前端不必关心物理字段名。
- `/histogram` 额外返回 `by_log_class` / `by_source_type`，检索页的"类型占比"与"候选值"都从这里取，避免前端写死枚举。

### 9.2 看板（dashboard-service）

- 维度字典新增 `logClass`（→ `log_class`）、`sourceType`（→ `source_type`），`bar` / `hbar` / `pie` / `donut` 均可用。
- 类型有**固定配色**（system 蓝 / middleware 橙 / database 红 / business 青），总览、看板、检索页同一类型同色，便于横向对照。
- 总览接口新增 `logClassDist`；明细表 `records` 增加 `logClass` / `sourceType` 列。

### 9.3 告警（alarm-service）

规则表新增两列，判定时与 `match_level` 同级拼接：

```sql
ALTER TABLE alarm_rules ADD COLUMN IF NOT EXISTS match_log_class TEXT;
ALTER TABLE alarm_rules ADD COLUMN IF NOT EXISTS match_source_type TEXT;
```

**为什么按类型圈定而不是 `match_source`**：系统日志的 `source` 是 facility（`daemon`/`authpriv`/`kern`），中间件与数据库的 `source` 随实例变化。用 `match_source` 等于逐台配规则，不可维护。

内置 8 条平台级模板（`tenant='all'`，所有租户生效、普通账号只读），按 `name` 幂等播种，已存在的不会被覆盖：

| 规则 | log_class | source_type | 条件 | 阈值 |
|------|-----------|-------------|------|------|
| 系统日志致命事件 | system | — | level=FATAL | 5min ≥1 |
| 系统认证失败激增 | system | auth | level=WARN | 10min ≥10 |
| MySQL 慢查询堆积 | database | mysql-slow | — | 10min ≥5 |
| MySQL 错误日志 | database | mysql-error | level=ERROR | 5min ≥1 |
| PostgreSQL 错误日志 | database | postgres | level=ERROR | 5min ≥1 |
| 网关 5xx 突增 | middleware | nginx-access | status≥500 | 5min ≥5 |
| 网关错误日志 | middleware | nginx-error | level=ERROR | 10min ≥3 |
| Redis 持久化/内存告警 | middleware | redis | level=WARN | 10min ≥1 |

「查看关联日志」的 PPL 也会带上 `log_class='...' and source_type='...'`，保证关联集合 = 触发集合。

### 9.4 智能检索（ai-service）

`BASE_FIELDS` 不加入 `log_class` 时，模型生成的 `where log_class='system'` 会被当成未知字段剥离，问"数据库慢查询"永远查不出东西。语法库同时补了「日志类型 / 来源类型 / 系统日志 / 中间件日志 / 数据库日志 / 慢查询 / 登录失败」等业务词映射。

---

## 10. 查询侧踩坑（本次新增，务必记住）

| 坑 | 现象 | 根因 | 处理 |
|----|------|------|------|
| **ClickHouse `terms_on` 静默换字段** | 总览「日志类型分布」显示成了**级别分布**（INFO/WARN/ERROR），接口 200、图表有数，结论全错 | 旧实现 `if field not in ("level","source","host","status"): field = "level"`——未知字段悄悄回落 | 改成白名单 `TERMS_FIELDS`，未知字段**抛异常**让调用方回退 OpenSearch。宁可慢一点，不可返回错数据 |
| **OS `terms` 聚合丢弃 missing 文档** | 看板各类之和小于总量，看着"少了几千条" | `terms` 聚合默认不含缺字段文档 | 分类维度聚合显式加 `"missing": ""` 落桶，前端显示为「未分类（改造前）」，与 CH 口径（`log_class=''`）一致 |
| **关联日志 PPL 丢正文列** | 「查看关联日志」只剩维度列，看不到 message | 新增 5 个 keyword 字段后，10 个输出槽位被标量列吃满 | 关键列钉在最前（时间/级别/类型/来源/主机/应用/正文），其余按 mapping 补 |
| **历史数据没有 `log_class`** | 类型分布出现一大块空值 | v2.1 上线前写入的文档没有该字段 | **不猜、不硬塞**：显式标注「未分类（改造前）」。要清就整批清，要补就按 host/source 确定性补，需单独决策 |
| **Flink 作业重提交出现一次性缺口** | 重提交 JAR 前后约 15 分钟窗口，CH 比 OS 少约 1200 条 | 旧作业被 cancel 到新作业从已提交位点续读之间的空档 | 属重启窗口的正常代价；确认后续时段两引擎逐段相等即为健康。需要补齐用 `tools/backfill-to-clickhouse.py` |

