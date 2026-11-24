#!/bin/sh
# 把本地构建/已有的镜像导入 Docker Desktop 节点的 containerd。
# Docker Desktop 的 K8s 与 docker 镜像存储是隔离的：不导入会 ImagePullBackOff。
# 云上不需要这一步，改用 buildx 构建并 push 到 ACR/SWR。
set -e
NODE="${NODE:-desktop-control-plane}"
IMAGES="${*:-ruilog-backend:k8s ruilog-web:k8s ruilog-flink-job:k8s \
postgres:16-alpine redis:7-alpine bitnami/kafka:3.7 opensearchproject/opensearch:3.9.0 \
clickhouse/clickhouse-server:24.3 ruilog-flink:1.18}"
for img in $IMAGES; do
  printf '[load] %s ... ' "$img"
  docker save "$img" | docker exec -i "$NODE" ctr -n k8s.io images import - >/dev/null
  echo ok
done
