#!/bin/sh
# =============================================================================
# 提交 Flink 流处理 Job（Docker 部署形态）。
# 用法（宿主机执行）：sh flink-jobs/submit-job.sh
# 前置：
#   - java/target/log-pipeline.jar 已编译（挂载到 JM 容器 /opt/flink/jobs/）
#   - flink-jobmanager / flink-taskmanager 已 up（自定义镜像含 Kafka connector）
# 方式：docker exec 进 JM 用 flink CLI 提交；参数同时用 -e 环境变量和 -D 注入，
# Job 内 env() 优先读 system property，再回退环境变量（-D 在各版本透传行为不完全一致）。
# 幂等：先取消旧作业，再 -d 后台提交新作业。
# 环境变量：
#   FLINK_WRITE_OS    true/false，是否由 Flink 写 OpenSearch 明细（稳态=true）
#   KAFKA_BROKER / CLICKHOUSE_HTTP_URL / OPENSEARCH_URL  下游地址
# =============================================================================
set -e
JM="log_system-flink-jobmanager-1"
JAR="/opt/flink/jobs/log-pipeline.jar"
WRITE_OS="${FLINK_WRITE_OS:-true}"
BROKER="${KAFKA_BROKER:-kafka:9092}"
CH_URL="${CLICKHOUSE_HTTP_URL:-http://clickhouse:8123}"
OS_URL="${OPENSEARCH_URL:-http://opensearch:9200}"

echo "[submit] JM=$JM WRITE_OS=$WRITE_OS"

docker exec "$JM" test -f "$JAR" || { echo "[submit] jar 不存在：$JAR"; exit 1; }

# 取消已有运行作业（幂等）
JOBS=$(docker exec "$JM" flink list -r 2>/dev/null | grep -oE '[0-9a-f]{32}' || true)
for j in $JOBS; do
  echo "[submit] cancel old job $j"
  docker exec "$JM" flink cancel "$j" >/dev/null 2>&1 || true
done
[ -n "$JOBS" ] && sleep 3

# 提交（参数同时走 -e 和 -D；Job 内 env() 两者都认）
echo "[submit] submit..."
docker exec -e FLINK_WRITE_OS="$WRITE_OS" -e KAFKA_BROKER="$BROKER" \
  -e CLICKHOUSE_HTTP_URL="$CH_URL" -e OPENSEARCH_URL="$OS_URL" "$JM" \
  flink run -d \
  -D FLINK_WRITE_OS="$WRITE_OS" \
  -D KAFKA_BROKER="$BROKER" \
  -D CLICKHOUSE_HTTP_URL="$CH_URL" \
  -D OPENSEARCH_URL="$OS_URL" \
  "$JAR" 2>&1 | tail -20

echo "[submit] running jobs:"
docker exec "$JM" flink list -r 2>/dev/null | grep -iE 'running|job' | head -5 || true
echo "[submit] done"
