#!/bin/sh
# =============================================================================
# Kafka topic 初始化（幂等）：在 kafka 容器首次就绪后由一次性 init 容器执行。
# topic 设计：
#   ruilog.logs.raw   —— ingest → Flink 的原始日志主干。分区数是 Flink 消费并行度上限，
#                        按 6 分区（与 6 个种子租户同量级，生产按峰值吞吐 * 副本规划）。
#                        key=tenant_id：保证同一租户日志落同分区 → 单租户内有序、且
#                        Flink keyBy 后可做租户级状态/窗口，跨租户仍并行。
#   ruilog.logs.dlq   —— Flink 解析失败的死信，供治理与重放。
# 生产加固项（本开发 compose 未开，云上托管 Kafka 默认具备）：
#   replication.factor=3 / min.insync.replicas=2 / acks=all / 清理策略与留存按合规设置。
# =============================================================================
set -e
BROKER="${KAFKA_BROKER:-kafka:9092}"
# 单 broker 本地默认 1；云上三 broker 由 ConfigMap 覆盖为 3。
RF="${KAFKA_TOPIC_RF:-1}"
MIN_ISR="${KAFKA_MIN_INSYNC_REPLICAS:-1}"
BIN=/opt/bitnami/kafka/bin/kafka-topics.sh

create() {
  name="$1"; parts="$2"
  if "$BIN" --bootstrap-server "$BROKER" --list 2>/dev/null | grep -qx "$name"; then
    echo "[kafka-init] topic exists: $name (expected RF=$RF; 已有 topic 不会被本脚本改副本数)"
  else
    "$BIN" --bootstrap-server "$BROKER" --create \
      --topic "$name" --partitions "$parts" \
      --replication-factor "$RF" \
      --config "min.insync.replicas=$MIN_ISR"
    echo "[kafka-init] created: $name (partitions=$parts, rf=$RF, min.insync.replicas=$MIN_ISR)"
  fi
}

echo "[kafka-init] waiting for broker $BROKER ..."
i=0
until "$BIN" --bootstrap-server "$BROKER" --list >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -gt 60 ] && { echo "[kafka-init] broker timeout"; exit 1; }
  sleep 2
done

create ruilog.logs.raw 6
create ruilog.logs.dlq 3
echo "[kafka-init] done"
"$BIN" --bootstrap-server "$BROKER" --list
