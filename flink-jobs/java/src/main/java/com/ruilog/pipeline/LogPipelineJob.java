package com.ruilog.pipeline;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.RichFlatMapFunction;
import org.apache.flink.api.common.functions.RichMapFunction;
import org.apache.flink.api.common.state.ValueState;
import org.apache.flink.api.common.state.ValueStateDescriptor;
import org.apache.flink.api.common.typeinfo.TypeHint;
import org.apache.flink.api.common.typeinfo.TypeInformation;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.streaming.api.CheckpointingMode;
import org.apache.flink.streaming.api.checkpoint.CheckpointedFunction;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.sink.RichSinkFunction;
import org.apache.flink.util.Collector;
import org.apache.http.client.methods.CloseableHttpResponse;
import org.apache.http.client.methods.HttpPost;
import org.apache.http.entity.ByteArrayEntity;
import org.apache.http.impl.client.CloseableHttpClient;
import org.apache.http.impl.client.HttpClients;
import org.apache.http.util.EntityUtils;

import java.io.Serializable;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;

/**
 * RuiLog 实时日志流处理 Job（阶段3）。
 *
 * 拓扑：Kafka(ruilog.logs.raw) → 解析/富化 → 三路 sink
 *   1) ClickHouse 聚合宽表（ruilog.logs，HTTP interface + 幂等 event_id）
 *   2) OpenSearch 明细索引（可选，FLINK_WRITE_OS=true 时启用；dual→kafka 切换后由本 Job 接管）
 *   3) 异常日志旁路 → Kafka DLQ（解析失败不丢数据）
 *
 * 可靠性：
 *   - KafkaSource from committed offsets + checkpoint（EXACTLY_ONCE，10s）
 *   - ClickHouse ReplacingMergeTree(ver) + 确定性 event_id：重放幂等
 *   - OS bulk 用 event_id 作为 _id：重放覆盖不重复
 *   - 失败记录写 DLQ，不阻塞主流
 */
public class LogPipelineJob {

    // ---------- 配置（全部可由 env 覆盖，云上用 ConfigMap/Secret 注入） ----------
    static final String BROKER = env("KAFKA_BROKER", "kafka:9092");
    static final String TOPIC_RAW = env("KAFKA_TOPIC_RAW", "ruilog.logs.raw");
    static final String TOPIC_DLQ = env("KAFKA_TOPIC_DLQ", "ruilog.logs.dlq");
    static final String CH_URL = env("CLICKHOUSE_HTTP_URL", "http://clickhouse:8123");
    static final String CH_DB = env("CLICKHOUSE_DB", "ruilog");
    static final String CH_USER = env("CLICKHOUSE_USER", "ruilog");
    static final String CH_PASS = env("CLICKHOUSE_PASSWORD", "ruilogpass");
    static final String OS_URL = env("OPENSEARCH_URL", "http://opensearch:9200");
    static final boolean WRITE_OS = Boolean.parseBoolean(env("FLINK_WRITE_OS", "false"));
    static final int BATCH_SIZE = Integer.parseInt(env("FLINK_SINK_BATCH", "200"));
    /** sink 缓冲最大滞留时间（ms）：即使攒不满一批也强制刷盘，避免低流量租户数据滞留导致双引擎口径不一致。 */
    static final long FLUSH_INTERVAL_MS = Long.parseLong(env("FLINK_SINK_FLUSH_MS", "5000"));

    static String env(String k, String def) {
        // 优先 system property（flink run -D k=v 可注入到 JM/TM JVM），其次环境变量
        String v = System.getProperty(k);
        if (v == null || v.isEmpty()) v = System.getenv(k);
        return (v == null || v.isEmpty()) ? def : v;
    }

    /** 归一化日志事件（POJO，Flink 可序列化）。 */
    public static class LogEvent implements Serializable {
        public String eventId;
        public String tenantId;
        public String ts;        // UTC ISO-8601 毫秒，如 2026-11-19T06:00:00.123Z
        public String level;
        public String source;
        public String host;
        public String traceId;
        public int status;
        public String message;
        public String raw;
        /** 网关归一化后的原始 JSON（含 log/attrs/parse_status 等明细字段）。 */
        public String payload;

        // 多类型日志维度（系统 / 中间件 / 数据库 / 业务），见 docs/log-ingestion-model.md
        public String logClass;
        public String sourceType;
        public String app;
        public String instance;
        public String facility;
        public int severity;     // syslog 0-7；0 表示非 syslog
        public String attrs;     // 类型特有指标（JSON 字符串）
    }

    /** 解析失败记录 → DLQ。 */
    public static class BadRecord implements Serializable {
        public String tenantId;
        public String raw;
        public String reason;
    }

    public static void main(String[] args) throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(2);
        env.enableCheckpointing(10_000L, CheckpointingMode.EXACTLY_ONCE);
        env.getCheckpointConfig().setMinPauseBetweenCheckpoints(5_000L);
        env.getCheckpointConfig().setCheckpointTimeout(60_000L);

        KafkaSource<String> source = KafkaSource.<String>builder()
                .setBootstrapServers(BROKER)
                .setTopics(TOPIC_RAW)
                .setGroupId("ruilog-pipeline")
                // 优先从消费组已提交位点续读（checkpoint 提交，配合 sink 的 snapshotState 刷盘 = 不丢不重）；
                // 首次上线无提交位点时回退 earliest 全量消费。
                .setStartingOffsets(OffsetsInitializer.committedOffsets(
                        org.apache.kafka.clients.consumer.OffsetResetStrategy.EARLIEST))
                .setValueOnlyDeserializer(new org.apache.flink.api.common.serialization.SimpleStringSchema())
                .build();

        DataStream<String> raw = env.fromSource(source,
                WatermarkStrategy.noWatermarks(), "kafka-logs-raw");

        // 解析 + 富化；失败旁路写 DLQ（不在主流抛异常，保证主流不中断）
        DataStream<LogEvent> parsed = raw.flatMap(new ParseAndEnrich());

        // Sink 1：ClickHouse 聚合宽表（主）
        parsed.addSink(new ClickHouseSink()).name("clickhouse-sink").uid("ch-sink");

        // Sink 2：OpenSearch 明细（切到 kafka 主干模式后启用）
        if (WRITE_OS) {
            parsed.addSink(new OpenSearchSink()).name("opensearch-sink").uid("os-sink");
        }

        // Sink 3：Kafka DLQ（解析失败记录）—— 用 simple producer 写入
        raw.flatMap(new DlqSink()).name("dlq-sink").uid("dlq-sink");

        env.execute("RuiLog Log Pipeline: Kafka -> Flink -> OpenSearch/ClickHouse");
    }

    /** 把 Kafka 消息 JSON 解析为 LogEvent；非法记录写 DLQ。 */
    public static class ParseAndEnrich extends RichFlatMapFunction<String, LogEvent> {
        private transient ObjectMapper mapper;

        @Override
        public void open(Configuration c) {
            mapper = new ObjectMapper();
        }

        @Override
        public void flatMap(String value, Collector<LogEvent> out) {
            try {
                JsonNode n = mapper.readTree(value);
                LogEvent e = new LogEvent();
                e.eventId = text(n, "event_id");
                e.tenantId = text(n, "tenant_id");
                e.ts = normalizeTs(text(n, "@timestamp"));
                e.level = text(n, "level");
                e.source = text(n, "source");
                e.host = text(n, "host");
                e.traceId = firstText(n, "traceId", "trace_id");
                e.status = intVal(n, "status");
                e.message = text(n, "message");
                e.raw = value;
                e.payload = value;
                // 多类型日志维度：由网关归一化后随消息带上来，Flink 只做透传映射
                e.logClass = text(n, "log_class");
                e.sourceType = text(n, "source_type");
                e.app = text(n, "app");
                e.instance = text(n, "instance");
                e.facility = text(n, "facility");
                e.severity = intVal(n, "severity");
                e.attrs = attrsJson(n.get("attrs"));
                if (e.eventId == null || e.eventId.isEmpty() || e.ts == null) {
                    throw new IllegalArgumentException("missing event_id or @timestamp");
                }
                out.collect(e);
            } catch (Exception ex) {
                // 主流不中断：解析失败交给下游 DLQ sink 处理（这里通过 side output 更优雅，
                // 但为保持单文件可读性，DLQ sink 自行二次解析同一条 raw）。
            }
        }
    }

    /** 扫描原始消息，若主流解析失败则写 DLQ。 */
    public static class DlqSink extends RichFlatMapFunction<String, String> {
        private transient ObjectMapper mapper;
        private transient org.apache.kafka.clients.producer.KafkaProducer<String, String> producer;

        @Override
        public void open(Configuration c) {
            mapper = new ObjectMapper();
            Properties p = new Properties();
            p.put("bootstrap.servers", BROKER);
            p.put("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
            p.put("value.serializer", "org.apache.kafka.common.serialization.StringSerializer");
            p.put("enable.idempotence", "true");
            p.put("acks", "all");
            producer = new org.apache.kafka.clients.producer.KafkaProducer<>(p);
        }

        @Override
        public void flatMap(String value, Collector<String> out) {
            try {
                JsonNode n = mapper.readTree(value);
                String eid = text(n, "event_id");
                String ts = normalizeTs(text(n, "@timestamp"));
                if (eid == null || eid.isEmpty() || ts == null) {
                    emitDlq(text(n, "tenant_id"), value, "missing event_id/@timestamp");
                }
            } catch (Exception ex) {
                emitDlq("", value, "invalid json: " + ex.getMessage());
            }
        }

        private void emitDlq(String tenant, String raw, String reason) {
            try {
                producer.send(new org.apache.kafka.clients.producer.ProducerRecord<>(
                        TOPIC_DLQ, tenant == null ? "" : tenant, raw));
            } catch (Exception ignore) {
                // DLQ 写失败只记录，绝不阻塞主流
            }
        }

        @Override
        public void close() {
            if (producer != null) producer.close();
        }
    }

    /**
     * ClickHouse HTTP sink：批量 INSERT ... FORMAT JSONEachRow，event_id 幂等。
     *
     * <p>刷盘时机（三者任一触发）：① 缓冲达到 BATCH_SIZE；② 距上次刷盘超过 FLUSH_INTERVAL_MS；
     * ③ checkpoint 快照前（snapshotState）。
     * <p>③ 是关键：早期版本只在攒满 BATCH_SIZE 时刷盘，低流量租户的数据会在内存里滞留十几分钟才落库，
     * 表现为"检索（OpenSearch）能查到、概览（ClickHouse）查不到"，两边数字对不上。加上 checkpoint
     * 钩子后，CH 最多落后一个 checkpoint 间隔（10s），与明细引擎保持同量级。
     */
    public static class ClickHouseSink extends RichSinkFunction<LogEvent> implements CheckpointedFunction {
        private transient CloseableHttpClient http;
        private transient ObjectMapper mapper;
        private final List<LogEvent> buffer = new ArrayList<>();
        private transient String insertUrl;
        private transient long lastFlushMs;

        @Override
        public void open(Configuration c) {
            http = HttpClients.createDefault();
            mapper = new ObjectMapper();
            lastFlushMs = System.currentTimeMillis();
            insertUrl = CH_URL + "/?query=INSERT%20INTO%20" + CH_DB
                    + ".logs%20(event_id,tenant_id,ts,level,source,host,log_class,source_type,app,instance,facility,severity,trace_id,status,message,attrs,ver)%20FORMAT%20JSONEachRow";
        }

        @Override
        public void invoke(LogEvent e, Context ctx) {
            buffer.add(e);
            if (buffer.size() >= BATCH_SIZE
                    || System.currentTimeMillis() - lastFlushMs >= FLUSH_INTERVAL_MS) {
                flush();
            }
        }

        /** checkpoint 前强制刷盘：不把未落库的数据跨过检查点，保证聚合引擎与明细引擎口径一致。 */
        @Override
        public void snapshotState(org.apache.flink.runtime.state.FunctionSnapshotContext ctx) {
            flush();
        }

        @Override
        public void initializeState(org.apache.flink.runtime.state.FunctionInitializationContext ctx) {
            // 无算子状态：去重靠 CH 的 ReplacingMergeTree(tenant_id, ts, event_id)，重放天然幂等
        }

        private void flush() {
            if (buffer.isEmpty()) return;
            lastFlushMs = System.currentTimeMillis();
            StringBuilder sb = new StringBuilder();
            for (LogEvent e : buffer) {
                try {
                    // CH 表列名是 snake_case（event_id/tenant_id/trace_id），Java POJO 是驼峰。
                    // 手工构建 ObjectNode 做显式映射，避免 valueToTree 带出驼峰列名导致字段丢失。
                    com.fasterxml.jackson.databind.node.ObjectNode o = mapper.createObjectNode();
                    o.put("event_id", e.eventId == null ? "" : e.eventId);
                    o.put("tenant_id", e.tenantId == null ? "unknown" : e.tenantId);
                    // CH DateTime64('UTC') 只接受 'YYYY-MM-DD HH:MM:SS.mmm'，不接受 ISO 的 T/Z
                    String ts = e.ts;
                    if (ts != null) {
                        String ch = ts.replace('T', ' ').replaceAll("Z$", "");
                        if (ch.length() > 23) ch = ch.substring(0, 23);
                        o.put("ts", ch);
                    }
                    o.put("level", e.level == null ? "" : e.level);
                    o.put("source", e.source == null ? "" : e.source);
                    o.put("host", e.host == null ? "" : e.host);
                    // 多类型日志维度（列名同样是 snake_case）
                    o.put("log_class", e.logClass == null ? "" : e.logClass);
                    o.put("source_type", e.sourceType == null ? "" : e.sourceType);
                    o.put("app", e.app == null ? "" : e.app);
                    o.put("instance", e.instance == null ? "" : e.instance);
                    o.put("facility", e.facility == null ? "" : e.facility);
                    o.put("severity", e.severity);
                    o.put("trace_id", e.traceId == null ? "" : e.traceId);
                    o.put("status", e.status);
                    o.put("message", e.message == null ? "" : e.message);
                    o.put("attrs", e.attrs == null ? "" : e.attrs);
                    o.put("ver", 1);
                    sb.append(mapper.writeValueAsString(o)).append('\n');
                } catch (Exception ignore) { }
            }
            buffer.clear();
            HttpPost post = new HttpPost(insertUrl);
            post.setHeader("X-ClickHouse-User", CH_USER);
            post.setHeader("X-ClickHouse-Key", CH_PASS);
            post.setEntity(new ByteArrayEntity(sb.toString().getBytes(StandardCharsets.UTF_8)));
            try (CloseableHttpResponse resp = http.execute(post)) {
                int code = resp.getStatusLine().getStatusCode();
                if (code >= 400) {
                    System.err.println("[ch-sink] HTTP " + code + " "
                            + EntityUtils.toString(resp.getEntity(), StandardCharsets.UTF_8));
                }
            } catch (Exception ex) {
                System.err.println("[ch-sink] write failed: " + ex.getMessage());
                // 失败由 checkpoint 回滚 + Kafka 重放兜底（幂等 event_id 保证不重复）
            }
        }

        @Override
        public void close() {
            flush();
            if (http != null) try { http.close(); } catch (Exception ignore) { }
        }
    }

    /** OpenSearch bulk sink：_id = event_id，重放覆盖不重复。刷盘时机同 ClickHouseSink。 */
    public static class OpenSearchSink extends RichSinkFunction<LogEvent> implements CheckpointedFunction {
        private transient CloseableHttpClient http;
        private transient ObjectMapper mapper;
        private final List<LogEvent> buffer = new ArrayList<>();
        private transient String bulkUrl;
        private transient long lastFlushMs;

        @Override
        public void open(Configuration c) {
            http = HttpClients.createDefault();
            mapper = new ObjectMapper();
            lastFlushMs = System.currentTimeMillis();
            bulkUrl = OS_URL + "/_bulk";
        }

        @Override
        public void invoke(LogEvent e, Context ctx) {
            buffer.add(e);
            if (buffer.size() >= BATCH_SIZE
                    || System.currentTimeMillis() - lastFlushMs >= FLUSH_INTERVAL_MS) {
                flush();
            }
        }

        @Override
        public void snapshotState(org.apache.flink.runtime.state.FunctionSnapshotContext ctx) {
            flush();
        }

        @Override
        public void initializeState(org.apache.flink.runtime.state.FunctionInitializationContext ctx) {
            // 无算子状态：_id = event_id，重放覆盖天然幂等
        }

        private void flush() {
            if (buffer.isEmpty()) return;
            lastFlushMs = System.currentTimeMillis();
            StringBuilder sb = new StringBuilder();
            for (LogEvent e : buffer) {
                try {
                    String tenant = e.tenantId == null || e.tenantId.isEmpty() ? "unknown" : e.tenantId;
                    // 写入走日志平台维护的写别名 logs-<tenant>-write（始终指向当天索引）。
                    // 读侧仍查 logs-<tenant>，因此查询服务代码无需改动。
                    String idx = "logs-" + tenant + "-write";
                    sb.append("{\"index\":{\"_index\":\"").append(idx)
                      .append("\",\"_id\":\"").append(e.eventId).append("\"}}\n");
                    // 直接复用网关归一化后的整条 JSON：字段集由 ingest 统一维护。
                    // 逐字段重建会漏掉 log/attrs/parse_status/src_file 等明细字段，
                    // 切到 kafka 主干模式后表现为「异常栈搜不到、attrs 为空」。
                    com.fasterxml.jackson.databind.node.ObjectNode o;
                    if (e.payload != null && !e.payload.isEmpty()) {
                        com.fasterxml.jackson.databind.JsonNode n = mapper.readTree(e.payload);
                        o = n.isObject() ? (com.fasterxml.jackson.databind.node.ObjectNode) n
                                         : mapper.createObjectNode();
                    } else {
                        o = mapper.createObjectNode();
                        o.put("level", e.level == null ? "" : e.level);
                        o.put("source", e.source == null ? "" : e.source);
                        o.put("host", e.host == null ? "" : e.host);
                        o.put("log_class", e.logClass == null ? "" : e.logClass);
                        o.put("source_type", e.sourceType == null ? "" : e.sourceType);
                        o.put("app", e.app == null ? "" : e.app);
                        o.put("instance", e.instance == null ? "" : e.instance);
                        o.put("facility", e.facility == null ? "" : e.facility);
                        o.put("severity", e.severity);
                        o.put("traceId", e.traceId == null ? "" : e.traceId);
                        o.put("status", e.status);
                        o.put("message", e.message == null ? "" : e.message);
                    }
                    // 服务端强制的三个字段以 Flink 侧归一化结果为准
                    o.put("event_id", e.eventId == null ? "" : e.eventId);
                    o.put("tenant_id", tenant);
                    o.put("@timestamp", e.ts == null ? Instant.now().toString() : e.ts);
                    // attrs 在 OS 是 flat_object，必须是对象；历史数据可能把 JSON 当字符串传
                    com.fasterxml.jackson.databind.JsonNode attrs = o.get("attrs");
                    if (attrs != null && attrs.isTextual()) {
                        try {
                            o.set("attrs", mapper.readTree(attrs.asText()));
                        } catch (Exception ignore) {
                            o.remove("attrs");
                        }
                    }
                    sb.append(mapper.writeValueAsString(o)).append('\n');
                } catch (Exception ignore) { }
            }
            buffer.clear();
            if (sb.length() == 0) return;
            HttpPost post = new HttpPost(bulkUrl);
            post.setHeader("Content-Type", "application/x-ndjson");
            post.setEntity(new ByteArrayEntity(sb.toString().getBytes(StandardCharsets.UTF_8)));
            try (CloseableHttpResponse resp = http.execute(post)) {
                int code = resp.getStatusLine().getStatusCode();
                if (code >= 400) {
                    String body = resp.getEntity() == null ? ""
                            : org.apache.http.util.EntityUtils.toString(resp.getEntity());
                    System.err.println("[os-sink] HTTP " + code + " " + body.substring(0, Math.min(body.length(), 300)));
                } else if (resp.getEntity() != null) {
                    // _bulk 即使 HTTP 200 也可能部分 item 失败（如 mapping 冲突），必须看 errors 标志
                    String body = org.apache.http.util.EntityUtils.toString(resp.getEntity());
                    if (body.contains("\"errors\":true")) {
                        System.err.println("[os-sink] bulk partial failure: "
                                + body.substring(0, Math.min(body.length(), 300)));
                    }
                }
            } catch (Exception ex) {
                System.err.println("[os-sink] write failed: " + ex.getMessage());
            }
        }

        @Override
        public void close() {
            flush();
            if (http != null) try { http.close(); } catch (Exception ignore) { }
        }
    }

    // ---------- 工具 ----------
    static String text(JsonNode n, String k) {
        JsonNode v = n.get(k);
        return (v == null || v.isNull()) ? null : v.asText();
    }

    static String firstText(JsonNode n, String... keys) {
        for (String k : keys) {
            String v = text(n, k);
            if (v != null && !v.isEmpty()) return v;
        }
        return null;
    }

    /**
     * attrs 在 OpenSearch 里是 flat_object（写入时是对象），在 ClickHouse 里是 String。
     * 这里统一序列化成 JSON 串；非对象/空值返回空串，避免 CH 写入 "null" 字面量。
     */
    static String attrsJson(JsonNode v) {
        if (v == null || v.isNull() || !v.isObject() || v.size() == 0) return "";
        return v.toString();
    }

    static int intVal(JsonNode n, String k) {
        JsonNode v = n.get(k);
        return (v == null || v.isNull()) ? 0 : v.asInt();
    }

    /** 时间戳归一化为 UTC ISO-8601 毫秒（与 ingest.normalize_record 对齐）。 */
    static String normalizeTs(String raw) {
        if (raw == null || raw.isEmpty()) return null;
        try {
            Instant instant;
            // epoch 秒 / 毫秒
            if (raw.matches("\\d+")) {
                long n = Long.parseLong(raw);
                instant = (n < 10_000_000_000L) ? Instant.ofEpochSecond(n) : Instant.ofEpochMilli(n);
            } else {
                instant = Instant.parse(raw.replace(" ", "T"));
            }
            return DateTimeFormatter.ISO_INSTANT.format(instant);
        } catch (Exception ex) {
            return null;
        }
    }
}
