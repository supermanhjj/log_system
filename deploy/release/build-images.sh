#!/bin/sh
# 发布镜像构建：一次产出 amd64 + arm64 并推送。
# 用法：REGISTRY=registry.cn-hangzhou.aliyuncs.com/ruilog TAG=1.0.0 sh deploy/release/build-images.sh
set -e
REGISTRY="${REGISTRY:?需要设置 REGISTRY，例如 registry.cn-hangzhou.aliyuncs.com/ruilog}"
TAG="${TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
HERE=$(cd "$(dirname "$0")/../.." && pwd)
cd "$HERE"

build() {
  name="$1"; file="$2"
  printf '[build] %s:%s (%s)\n' "$REGISTRY/$name" "$TAG" "$PLATFORMS"
  docker buildx build --platform "$PLATFORMS" \
    -f "$file" -t "$REGISTRY/$name:$TAG" --push .
}

build ruilog-backend deploy/release/dockerfiles/backend.Dockerfile
build ruilog-web deploy/release/dockerfiles/web.Dockerfile
build ruilog-flink-job deploy/release/dockerfiles/flink-job.Dockerfile
