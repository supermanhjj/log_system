-- =============================================================================
-- RuiLog 日志平台 —— ClickHouse 聚合引擎 schema
-- 角色：与 OpenSearch 组成双引擎。OS 负责明细全文检索/PPL/上下文，
--       CH 负责高基数聚合（总览/仪表盘/直方图/级别分布/P95）。
-- 设计对标云智慧 DOLA：列式存储 + 主排序键贴近查询模式 + 预聚合。
-- 幂等：ReplacingMergeTree(ver)，Flink 重放/CK 恢复按 (tenant,event_id) 去重，
--       查询端统一 FINAL（见查询层），保证 exactly-once 语义落地。
-- 分区按天：保留期最小粒度是 1 天，分区粒度必须 ≤ 保留期，否则删 1 天数据
--       要重写整月 part，磁盘 IO 和 CPU 双重爆炸。按天分区后过期直接 drop part。
-- =============================================================================

CREATE DATABASE IF NOT EXISTS ruilog;

-- -----------------------------------------------------------------------------
-- 主明细宽表：一行一条日志，字段与 ingest.normalize_record 输出严格对齐。
-- ORDER BY 前缀 tenant_id：租户过滤是每一次查询的必带条件（行级隔离），
--   放首位可让同一租户数据在物理上聚簇，剪枝效果最好。
-- 时间用 DateTime64(3,'UTC')：毫秒精度，对齐 @timestamp 的 UTC ISO 语义，
--   严禁写入 Fluent Bit 的 date(epoch 秒)，否则时间聚合整体失真。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ruilog.logs
(
    event_id    String,                                   -- 确定性 ID：tenant|ts|traceId|seq，幂等去重键
    tenant_id   LowCardinality(String),                   -- 租户编码（行级隔离维度）
    ts          DateTime64(3, 'UTC'),                     -- 归一化事件时间（毫秒，UTC）
    level       LowCardinality(String),                   -- ERROR/WARN/INFO/...
    source      LowCardinality(String),                   -- 业务来源（order-service 等）
    host        LowCardinality(String),                   -- 主机
    -- 多类型日志维度（系统/中间件/数据库/业务），见 docs/log-ingestion-model.md
    log_class   LowCardinality(String),                   -- system/middleware/database/business
    source_type LowCardinality(String),                   -- syslog/auth/nginx-access/mysql-slow...
    app         LowCardinality(String),                   -- nginx/redis/mysql/linux
    instance    LowCardinality(String),                   -- 实例标识
    facility    LowCardinality(String),                   -- syslog facility
    severity    UInt8 DEFAULT 0,                          -- syslog severity 0-7（0 表示非 syslog）
    trace_id    String,                                   -- 链路追踪 ID
    status      UInt16,                                   -- HTTP 状态码，0 表示无
    message     String,                                   -- 日志正文
    attrs       String,                                   -- 类型特有指标（JSON 串，如慢查询耗时）
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3),    -- 写入时间（观测摄取延迟）
    ver         UInt64   DEFAULT 1,                       -- 去重版本（Flink checkpoint 序号）
    -- 跳数索引：message 上的 LIKE/全文片段加速；tokenbf 对换行分词友好
    INDEX idx_msg   message  TYPE tokenbf_v1(32768, 3, 0) GRANULARITY 4,
    INDEX idx_trace trace_id TYPE bloom_filter(0.01)      GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (tenant_id, ts, event_id)
-- TTL 表达式要求 Date/DateTime，DateTime64 需显式 toDateTime() 转换（CH 24.x 严格校验）
TTL toDateTime(ts) + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192;

-- -----------------------------------------------------------------------------
-- 分钟级预聚合物化视图：把最重的“按时间桶 + 级别 + 来源”计数提前算好。
-- 总览趋势、级别分布、写入量直接查它，避免每次扫明细（PB 级下的关键优化）。
-- SummingMergeTree 对同一 (tenant, minute, level, source) 求和，天然可合并。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ruilog.logs_minute_agg
(
    tenant_id   LowCardinality(String),
    minute      DateTime('UTC'),
    log_class   LowCardinality(String),
    level       LowCardinality(String),
    source      LowCardinality(String),
    cnt         UInt64,
    err_cnt     UInt64,     -- ERROR/FATAL 计数
    warn_cnt    UInt64,
    status_5xx  UInt64,     -- status>=500 计数
    latency_sum UInt64,     -- 检索耗时之和（配合 cnt 求均值/P95 近似）
    latency_max UInt64
)
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMMDD(minute)
ORDER BY (tenant_id, minute, log_class, level, source)
TTL minute + INTERVAL 90 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS ruilog.mv_logs_minute
TO ruilog.logs_minute_agg AS
SELECT
    tenant_id,
    toStartOfMinute(ts)                              AS minute,
    log_class,
    level,
    source,
    count()                                          AS cnt,
    countIf(level IN ('ERROR', 'FATAL'))             AS err_cnt,
    countIf(level = 'WARN')                          AS warn_cnt,
    countIf(status >= 500)                           AS status_5xx,
    toUInt64(0)                                      AS latency_sum,
    toUInt64(0)                                      AS latency_max
FROM ruilog.logs
GROUP BY tenant_id, minute, log_class, level, source;

-- -----------------------------------------------------------------------------
-- 死信表：Flink 解析失败/字段非法的记录落这里，不丢数据、可回溯治理。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ruilog.logs_dlq
(
    ts       DateTime64(3, 'UTC') DEFAULT now64(3),
    tenant_id String,
    raw       String,
    reason    String
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (ts, tenant_id)
TTL toDateTime(ts) + INTERVAL 30 DAY DELETE;
