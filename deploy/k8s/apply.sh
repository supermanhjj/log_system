#!/bin/sh
# 用法：sh deploy/k8s/apply.sh local|cloud|cloud-ha|offline
#
# 为什么不是直接 kubectl apply -k：
# base 的 configMapGenerator 引用 deploy/shared 下的脚本/模板，路径在 base 目录之外；
# kubectl apply -k 没有 --load-restrictor 参数，默认会拒绝加载。
# 这里用 kubectl kustomize 显式放开加载限制，再管道 apply。
set -eu

OVERLAY="${1:-local}"
HERE=$(cd "$(dirname "$0")" && pwd)

case "$OVERLAY" in
  local|cloud|cloud-ha|offline) ;;
  *)
    echo "用法：sh deploy/k8s/apply.sh local|cloud|cloud-ha|offline" >&2
    exit 2
    ;;
esac

echo "[apply] overlay=$OVERLAY"

# 一次性 init Job 的 podTemplate 不可变：已存在时再 apply 会被 apiserver 拒绝
# （spec.template: field is immutable），整条命令非 0 退出，重复部署会被误判成失败。
# 这两个 Job 的脚本本身幂等（topic 存在就跳过、索引模板 PUT 覆盖），所以先删后建。
kubectl -n ruilog-system delete job kafka-init opensearch-init --ignore-not-found >/dev/null 2>&1 || true

kubectl kustomize --load-restrictor LoadRestrictionsNone \
  "$HERE/platform/overlays/$OVERLAY" | kubectl apply -f -
