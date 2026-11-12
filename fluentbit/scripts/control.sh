#!/bin/sh
# 随包管控脚本：由 Fluent Bit exec 输入插件定时调用（业务主机仅单一 Fluent Bit 进程，不另起守护）
# 职责（骨架）：
#   ① 心跳上报 → agent-service（服务端写 Redis 最近心跳时间戳，扫 >3min 无心跳标记离线）
#   ②/③ 配置 / 版本轮询、接收重启 / 升级指令（后续接 agent-service 指令队列；此处占位）
set -e

HOSTNAME=$(hostname)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
VERSION="1.0.0"

# ① 心跳上报（Docker 网络内部用容器端口 8000，非宿主机映射的 8006）
curl -s -X POST http://agent-service:8000/heartbeat \
  -H 'Content-Type: application/json' \
  -d "{\"hostname\":\"${HOSTNAME}\",\"version\":\"${VERSION}\",\"status\":\"online\",\"ts\":\"${TS}\"}" \
  >/dev/null 2>&1 || true

# ②/③ 配置 / 升级轮询占位（后续接 agent-service /control/<hostname> 指令队列）
# curl -s http://agent-service:8006/control/${HOSTNAME} ...

# 输出 JSON 供 Fluent Bit 捕获（exec 插件将 stdout 作为记录）
echo "{\"event\":\"heartbeat\",\"hostname\":\"${HOSTNAME}\",\"version\":\"${VERSION}\",\"ts\":\"${TS}\"}"
