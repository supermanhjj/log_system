-- =============================================================================
-- 迁移：ClickHouse 分区从「按月」改为「按天」
--   2026-11-19  v1
--
-- 为什么必须做：
--   ClickHouse 的删除单位是 part，不能只删分区里的一部分行。按月分区时若
--   保留期设为 1 天，TTL 要每天把整月数据重写一遍（只留下要保留的行），
--   磁盘 IO 与 CPU 双重爆炸。改成按天后，过期数据 = drop 整个 part，
--   元数据操作、毫秒级、零 IO。这是「保留期可配到 1 天」的前提。
--
-- 执行方式（在 clickhouse-client 或 HTTP /?query= 中逐条执行）：
--   clickhouse-client --user default --multiquery < this-file.sql
--
-- 注意：
--   ① 先删物化视图，避免迁移期间新数据写进旧聚合表造成两份数据。
--   ② RENAME 是元数据操作，毫秒级；Flink 写入可能在切换瞬间失败并重试，
--      幂等 producer + ReplacingMergeTree 保证不丢不重。
--   ③ 旧表保留为 *_legacy，不要立即 DROP。观察 24 小时确认查询正常后再删。
--   ④ 新表 TTL 默认 90 天（与聚合保留期配置默认值一致）；改保留期由平台接口 ALTER MODIFY TTL 完成。
-- =============================================================================

-- 1) 停掉物化视图，避免迁移期间双写
DROP TABLE IF EXISTS ruilog.mv_logs_minute;

-- 2) 建按天分区的新明细表
CREATE TABLE IF NOT EXISTS ruilog.logs_daily
(
    event_id    String,
    tenant_id   LowCardinality(String),
    ts          DateTime64(3, 'UTC'),
    level       LowCardinality(String),
    source      LowCardinality(String),
    host        LowCardinality(String),
    log_class   LowCardinality(String),
    source_type LowCardinality(String),
    app         LowCardinality(String),
    instance    LowCardinality(String),
    facility    LowCardinality(String),
    severity    UInt8 DEFAULT 0,
    trace_id    String,
    status      UInt16,
    message     String,
    attrs       String,
    ingested_at DateTime64(3, 'UTC') DEFAULT now64(3),
    ver         UInt64 DEFAULT 1,
    INDEX idx_msg   message  TYPE tokenbf_v1(32768, 3, 0) GRANULARITY 4,
    INDEX idx_trace trace_id TYPE bloom_filter(0.01)      GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ver)
PARTITION BY toYYYYMMDD(ts)
ORDER BY (tenant_id, ts, event_id)
TTL toDateTime(ts) + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192;

-- 3) 历史数据导入（INSERT SELECT 保持 event_id / ver 不变，幂等语义不破坏）
INSERT INTO ruilog.logs_daily SELECT * FROM ruilog.logs;

-- 4) 按天分区的新聚合表
CREATE TABLE IF NOT EXISTS ruilog.logs_minute_agg_daily
(
    tenant_id   LowCardinality(String),
    minute      DateTime('UTC'),
    log_class   LowCardinality(String),
    level       LowCardinality(String),
    source      LowCardinality(String),
    cnt         UInt64,
    err_cnt     UInt64,
    warn_cnt    UInt64,
    status_5xx  UInt64,
    latency_sum UInt64,
    latency_max UInt64
)
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMMDD(minute)
ORDER BY (tenant_id, minute, log_class, level, source)
TTL minute + INTERVAL 90 DAY DELETE;

-- 5) 原子切换表名（不删旧表，留回滚余地）
RENAME TABLE ruilog.logs            TO ruilog.logs_legacy,
             ruilog.logs_daily      TO ruilog.logs,
             ruilog.logs_minute_agg TO ruilog.logs_minute_agg_legacy,
             ruilog.logs_minute_agg_daily TO ruilog.logs_minute_agg;

-- 6) 重建物化视图，指向新表
CREATE MATERIALIZED VIEW ruilog.mv_logs_minute
TO ruilog.logs_minute_agg AS
SELECT
    tenant_id,
    toStartOfMinute(ts)                  AS minute,
    log_class,
    level,
    source,
    count()                              AS cnt,
    countIf(level IN ('ERROR', 'FATAL')) AS err_cnt,
    countIf(level = 'WARN')              AS warn_cnt,
    countIf(status >= 500)               AS status_5xx,
    toUInt64(0)                          AS latency_sum,
    toUInt64(0)                          AS latency_max
FROM ruilog.logs
GROUP BY tenant_id, minute, log_class, level, source;

-- 7) 核对（应有数据且分区键为 toYYYYMMDD）
-- SELECT count() FROM ruilog.logs;
-- SELECT partition FROM system.parts WHERE table='logs' AND active GROUP BY partition;

-- 8) 观察 24 小时无异常后，手工执行清理：
-- DROP TABLE IF EXISTS ruilog.logs_legacy;
-- DROP TABLE IF EXISTS ruilog.logs_minute_agg_legacy;
