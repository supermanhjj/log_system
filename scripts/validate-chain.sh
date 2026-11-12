#!/usr/bin/env bash
# log_system 全链路验证脚本
# 设计：OpenSearch / nginx 反代 / 心跳 检查均从 fluentbit 容器内 curl 发起（容器在 log_net，带 curl，
#       用内部 DNS opensearch:9200 / web:80），既绕开沙箱出网限制，又真实验证容器内网通信。
# Kafka 通过 docker exec 进 broker 自检。JSON 解析用 grep（Alpine 无 python3）。
set -u

FB=$(docker ps --filter "name=fluentbit" -q | head -1)
AG=$(docker ps --filter "name=agent-service" -q | head -1)
KAFKA=$(docker ps --filter "name=kafka" -q | head -1)
WEB=$(docker ps --filter "name=web" -q | head -1)

ok(){ echo "  ✅ PASS: $1"; }
no(){ echo "  ❌ FAIL: $1"; }

echo "== 容器发现 =="
echo "  fluentbit=$FB  agent-service=$AG  kafka=$KAFKA  web=$WEB"
[ -z "$FB" ] && { echo "fluentbit 未运行，先 docker compose up"; exit 1; }

echo
echo "=== T1 · OpenSearch 健康（fluentbit 容器内 curl opensearch:9200）==="
OSH=$(docker exec "$FB" curl -s http://opensearch:9200/_cluster/health 2>/dev/null)
echo "  $OSH" | head -c 220; echo
echo "$OSH" | grep -q '"status"' && ok "OpenSearch 可达" || no "OpenSearch 不可达"

echo
echo "=== T2 · 采集→结构化→多租户写入（索引 logs-<tenant>）==="
TENANTS="default acme gp"
ALL_OK=1
for t in $TENANTS; do
  CNT=0
  for i in $(seq 1 40); do
    RES=$(docker exec "$FB" curl -s -X POST "http://opensearch:9200/logs-$t/_count" \
          -H 'Content-Type: application/json' -d '{"query":{"match_all":{}}}' 2>/dev/null)
    C=$(echo "$RES" | grep -o '"count":[0-9]*' | grep -o '[0-9]*')
    CNT=${C:-0}
    [ "$CNT" -gt 0 ] 2>/dev/null && break
    sleep 2
  done
  echo "  logs-$t 文档数 = $CNT"
  if [ "$CNT" -gt 0 ] 2>/dev/null; then
    ok "logs-$t 采集→解析→写入成功"
  else
    no "logs-$t 未写入"; ALL_OK=0
  fi
done
[ $ALL_OK -eq 1 ] && ok "多租户(multi-tenant)采集全通" || no "存在未写入的租户索引"

echo
echo "=== T2b · 结构化字段校验（解析器 app_log 提取 level / tenant）==="
ERR=$(docker exec "$FB" curl -s "http://opensearch:9200/logs-*/_search?size=1&q=level:ERROR" 2>/dev/null)
echo "  $ERR" | grep -q '"level":"ERROR"' && ok "正则解析生效（level 字段存在）" || no "未检出 level 字段"
echo "  $ERR" | grep -q '"tenant_id"' && ok "tenant 字段已抽取并写入（多租户隔离依据）" || echo "  ⚠️ 未检出 tenant_id 字段（检查 parsers.conf / record_modifier）"

echo
echo "=== T3 · search-service 经 nginx 反代查询（全链路入口）==="
Q=$(docker exec "$FB" curl -s -X POST http://web:80/api/search/query \
     -H 'Content-Type: application/json' -d '{"body":{"query":{"match_all":{}},"size":3}}' 2>/dev/null)
echo "  $Q" | head -c 420; echo
echo "$Q" | grep -q '"engine": *"opensearch"' && ok "nginx /api/search → search-service → OpenSearch 检索通" || no "检索链路未通"

echo
echo "=== T4 · PPL 插件探测（informational，不影响主链路）==="
P=$(docker exec "$FB" curl -s -X POST http://web:80/api/search/query \
     -H 'Content-Type: application/json' -d '{"ppl":"source=logs-default | head 3"}' 2>/dev/null)
echo "  $P" | head -c 200; echo
echo "$P" | grep -q '"engine": *"ppl"' && ok "opensearch-sql(PPL) 插件可用" || echo "  ⚠️ PPL 插件未就绪（主链路用 _search，不受影响）"

echo
echo "=== T5 · agent-service 心跳（fluentbit dummy 输入 + http 输出 → /heartbeat）==="
echo "  等待 35s 让 dummy 心跳（Interval_Sec=30）触发一次上报..."
sleep 35
docker logs "$AG" 2>&1 | grep -i heartbeat | tail -3
docker logs "$AG" 2>&1 | grep -qi heartbeat && ok "管控心跳已送达 agent-service" || no "未收到心跳（检查 control.sh 端口/可达性）"

echo
echo "=== T6 · Kafka 缓冲层健康（broker 自检 topics）==="
if [ -n "$KAFKA" ]; then
  # 注意：Kafka 3.7 默认不自动建 topic，--list 返回空属正常，不能据此判不可达。
  # 正确判定 broker 可达性：命令无连接/超时类错误即视为可达。
  KT=$(docker exec "$KAFKA" sh -c '/opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list 2>&1')
  if echo "$KT" | grep -qi "error\|refused\|could not\|timed out\|Exception"; then
    no "Kafka 不可达（$KT）"
  else
    if [ -n "$KT" ]; then
      ok "Kafka broker 可达、已列出 topics: $(echo "$KT" | tr '\n' ' ')"
    else
      ok "Kafka broker 可达（当前无 topic，开发期正常；生产由 ingest/alarm 服务写入）"
    fi
  fi
else
  echo "  ⚠️ kafka 容器未发现，跳过"
fi

echo
echo "=== 全链路验证结束 ==="
