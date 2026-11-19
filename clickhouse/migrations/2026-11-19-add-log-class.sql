-- =============================================================================
-- 迁移：为多类型日志（系统/中间件/数据库）扩展字段
--   2026-11-19  v1
--
-- 背景：原 logs 表只有业务日志需要的 10 个字段；系统日志的 facility/severity、
-- 中间件/数据库的类型特有指标（慢查询耗时、upstream 状态）无处存放。
-- 说明：
--   ① ClickHouse 的 ADD COLUMN 是在线操作，不重写历史数据，已有行新列为默认值。
--   ② LowCardinality 用于低基数字典列（log_class/source_type/app/facility），
--      相比普通 String 可显著压缩并加速 GROUP BY。
--   ③ attrs 存 JSON 字符串：类型特有指标差异太大，建宽表不现实；
--      需要聚合时用 JSONExtract 现算（如 JSONExtractFloat(attrs,'query_time')）。
-- =============================================================================

ALTER TABLE ruilog.logs
    ADD COLUMN IF NOT EXISTS log_class   LowCardinality(String) AFTER host,
    ADD COLUMN IF NOT EXISTS source_type LowCardinality(String) AFTER log_class,
    ADD COLUMN IF NOT EXISTS app         LowCardinality(String) AFTER source_type,
    ADD COLUMN IF NOT EXISTS instance    LowCardinality(String) AFTER app,
    ADD COLUMN IF NOT EXISTS facility    LowCardinality(String) AFTER instance,
    ADD COLUMN IF NOT EXISTS severity    UInt8 DEFAULT 0 AFTER facility,
    ADD COLUMN IF NOT EXISTS attrs       String AFTER message;

-- -----------------------------------------------------------------------------
-- 分钟聚合表重建：增加 log_class 维度（ORDER BY 变更必须重建，CH 不支持改排序键）
-- 该表由物化视图维护、无外部写入，重建只丢"预聚合缓存"，明细仍在 logs 表。
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS ruilog.mv_logs_minute;
DROP TABLE IF EXISTS ruilog.logs_minute_agg;

CREATE TABLE ruilog.logs_minute_agg
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
PARTITION BY toYYYYMM(minute)
ORDER BY (tenant_id, minute, log_class, level, source)
TTL minute + INTERVAL 180 DAY DELETE;

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
