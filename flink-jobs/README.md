# Flink 实时流处理 Job（阶段3 · 正式，替换原占位）

## 定位

对标云智慧 DOLA 的 Flink 流处理层。数据主干：

```
Fluent Bit → ingest-service → Kafka(ruilog.logs.raw) → Flink Job →
    ├─ ClickHouse(ruilog.logs)   聚合宽表（ReplacingMergeTree + event_id 幂等）
    ├─ OpenSearch(logs-<tenant>) 明细索引（FLINK_WRITE_OS=true 时启用）
    └─ Kafka(ruilog.logs.dlq)    解析失败死信（不丢数据）
```

实时告警判定仍由 alarm-service 承担（对 CH/OS 聚合查询），Flink 先把数据面打通；
告警判定迁移到 Flink 窗口算子列为后续演进（见 tech-stack-selection.md）。

## 可靠性设计（商用级）

| 机制 | 实现 |
|------|------|
| 消费位置 | KafkaSource from **committed offsets**，earliest 兜底 |
| 容错 | Checkpoint **EXACTLY_ONCE** / 10s，状态写挂载卷 `flinkckpt`（重启不丢） |
| 写入幂等 | CH `ReplacingMergeTree(ver)` + 确定性 `event_id`；OS `_id=event_id` 覆盖写 |
| 死信 | 解析失败旁路写 `ruilog.logs.dlq`，主流不中断 |
| 重启策略 | fixed-delay / 3 次（compose FLINK_PROPERTIES 配置） |
| 反压 | Flink 原生背压 + Kafka 分区缓冲；并行度 2，可在线调 |

## 构建（宿主机无 Maven，直接用本地 Maven 容器）

镜像 `maven:3.9-eclipse-temurin-17` 已在本地，构建不再拉取、也不需要在宿主机装 Maven：

```bash
cd flink-jobs/java
docker run --rm -v "$PWD":/w -v "$HOME/.m2":/root/.m2 -w /w \
  maven:3.9-eclipse-temurin-17 \
  mvn -s /w/settings.xml -q clean package
# 产物：java/target/log-pipeline.jar（fat jar，含 httpclient/jackson/jdbc）
```

> 本机没有该镜像时才需要 `docker pull`（网络受限可加 `docker.m.daocloud.io/` 前缀）。
> `target/` 被 gitignore，换机器或重新 clone 后必须重新构建。
> settings.xml 已配阿里云 mirror。首次构建需下载 Flink/HTTP/Jackson/JDBC 依赖。
> Flink/Kafka connector 依赖声明为 provided（由集群 lib 提供，不重复打进 jar）。

## Kafka connector

Flink 官方镜像**不含** Kafka connector，需把 fat jar 放进集群 `lib/`：

```bash
curl -sSL -o flink-jobs/lib/flink-sql-connector-kafka-1.18.1.jar \
  https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/1.18.1/flink-sql-connector-kafka-1.18.1.jar
# 用阿里云 mirror 备选：https://maven.aliyun.com/repository/public/...
```

compose 已把 `flink-jobs/lib` 挂到 JM/TM 的 `/opt/flink/lib`（见 docker-compose.yml）。
版本必须与 Flink runtime 一致（1.18.1）。

## 部署 + 提交

1. `java/target/log-pipeline.jar` 编译完成，`lib/` 内有 Kafka connector jar。
2. 重启集群让挂载生效（尤其 `mvn clean` 重建过 jar）：`docker compose restart flink-jobmanager flink-taskmanager`
3. 提交：`sh flink-jobs/submit-job.sh`（默认 `FLINK_WRITE_OS=true`，即 kafka 稳态）

| FLINK_WRITE_OS | 阶段 | OS 明细由谁写 |
|----------------|------|--------------|
| `true`（默认） | kafka 稳态 | Flink 统一写 OS+CH，ingest 配 `INGEST_STORAGE_MODE=kafka` |
| `false` | dual / direct 回退 | ingest 直写（Flink 只写 CH） |

## 验证

```bash
docker exec log_system-flink-jobmanager-1 flink list -r          # 作业在跑
docker exec log_system-kafka-1 /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 --describe --group ruilog-pipeline   # 消费位移
curl -s 'http://localhost:8123/?query=SELECT%20count(),tenant_id%20FROM%20ruilog.logs%20GROUP%20BY%20tenant_id%20FORMAT%20TSV'  # CH 数据
```
