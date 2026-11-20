#!/bin/sh
# 交付包：把部署所需的全部文件打成 tar.gz，输出到 dist/（不入库）。
# 用法：VERSION=1.0.0 sh deploy/release/package.sh
set -e
VERSION="${VERSION:-dev}"
HERE=$(cd "$(dirname "$0")/../.." && pwd)
cd "$HERE"
mkdir -p dist
OUT="dist/ruilog-deploy-$VERSION.tar.gz"
tar czf "$OUT" \
  deploy \
  docker-compose.yml compose.override.yml docker-compose.prod.yml \
  .env.example \
  README.md
printf '[package] %s (%s)\n' "$OUT" "$(du -h "$OUT" | cut -f1)"
