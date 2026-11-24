#!/bin/sh
# 发布镜像构建：一次产出 amd64 + arm64 并推送。
#
# 用法：
#   REGISTRY=registry.cn-hangzhou.aliyuncs.com TAG=0.9.0-test \
#     sh deploy/release/build-images.sh
#
# 镜像名与 K8s/Compose 对齐：
#   ruilog-backend      —— 8 个 FastAPI 服务共用
#   ruilog-web          —— nginx + 前端静态文件
#   ruilog-flink        —— Flink 1.18 + Kafka connector（基础镜像，先构建）
#   ruilog-flink-job    —— ruilog-flink + log-pipeline.jar（作业镜像）
#
# 注意：ruilog-flink 的构建上下文是 flink-jobs/（Dockerfile 里 COPY lib/*.jar），
# 作业镜像的构建上下文是仓库根（Dockerfile 里 COPY flink-jobs/java/target/...）。
set -e
REGISTRY="${REGISTRY:?需要设置 REGISTRY，例如 registry.cn-hangzhou.aliyuncs.com}"
TAG="${TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
HERE=$(cd "$(dirname "$0")/../.." && pwd)
cd "$HERE"

# 构建输入预检：这三个产物不在仓库里（dist / jar 是构建结果），缺了会在 buildx 里
# 报 COPY 失败，提前检查并给出明确命令。
for f in frontend/dist/index.html flink-jobs/java/target/log-pipeline.jar; do
  [ -f "$f" ] || {
    echo "[build] 缺少构建产物 $f"
    echo "  前端：npm --prefix frontend install && npm --prefix frontend run build"
    echo "  Flink：cd flink-jobs/java && mvn -q clean package"
    exit 1
  }
done
ls flink-jobs/lib/flink-connector-kafka-*.jar >/dev/null 2>&1 || {
  echo "[build] 缺少 flink-jobs/lib/flink-connector-kafka-*.jar（见 flink-jobs/README）"
  exit 1
}

build() {
  name="$1"; file="$2"; shift 2
  printf '[build] %s:%s (%s)\n' "$REGISTRY/$name" "$TAG" "$PLATFORMS"
  docker buildx build --platform "$PLATFORMS" "$@" \
    -f "$file" -t "$REGISTRY/$name:$TAG" --push .
}

build_flink_base() {
  printf '[build] %s:%s (%s, context=flink-jobs)\n' "$REGISTRY/ruilog-flink" "$TAG" "$PLATFORMS"
  docker buildx build --platform "$PLATFORMS" \
    -f flink-jobs/Dockerfile -t "$REGISTRY/ruilog-flink:$TAG" --push flink-jobs
}

# 先构建 Flink 基础镜像，作业镜像 FROM 它。
build_flink_base
build ruilog-backend deploy/release/dockerfiles/backend.Dockerfile
build ruilog-web deploy/release/dockerfiles/web.Dockerfile
build ruilog-flink-job deploy/release/dockerfiles/flink-job.Dockerfile \
  --build-arg "FLINK_BASE=$REGISTRY/ruilog-flink:$TAG"
