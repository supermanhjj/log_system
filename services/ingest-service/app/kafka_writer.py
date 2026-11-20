"""ingest-service —— Kafka 写入主干（阶段2）。

职责：把归一化后的日志以幂等方式写入 Kafka topic，供下游 Flink 流处理消费。
可靠性（商用级）：
  - confluent-kafka(librdkafka)：enable.idempotence=true + acks=all + retries，
    生产端 exactly-once：网络抖动/leader 切换不产生重复、不丢数据。
    （kafka-python 不支持幂等 producer，故商用链路不用它。）
  - key=tenant_id：同一租户落同一分区 → 单租户内有序，Flink keyBy 后状态对齐；
    跨租户仍并行，兼顾隔离与吞吐。
  - linger.ms + 批量：微批攒包提升吞吐、降低 broker 请求数。
  - 惰性连接：模块导入不触网，首次写入才建 producer（kafka 未就绪也能启动服务）。

写入模式由 INGEST_STORAGE_MODE 控制（见 main.py）：
  direct —— 旧链路，仅写 OpenSearch（回滚用）
  dual   —— 过渡：同时写 OpenSearch 与 Kafka（默认，前端查询不受影响）
  kafka  —— 主干：仅写 Kafka，由 Flink 统一写 OS+CH（稳定后切换）
"""
import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from confluent_kafka import Producer, KafkaException

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "kafka:9092")
TOPIC_RAW = os.getenv("KAFKA_TOPIC_RAW", "ruilog.logs.raw")
STORAGE_MODE = os.getenv("INGEST_STORAGE_MODE", "dual").strip().lower()

_producer: Optional[Producer] = None
_lock = threading.Lock()
_last_error: Optional[str] = None
_delivered_total = 0
_failed_total = 0


def mode() -> str:
    return STORAGE_MODE if STORAGE_MODE in ("direct", "dual", "kafka") else "dual"


def use_kafka() -> bool:
    return mode() in ("dual", "kafka")


def use_opensearch() -> bool:
    return mode() in ("direct", "dual")


def _new_producer() -> Producer:
    # 幂等 + 全 ISR 确认：商用可靠性的底线配置
    conf = {
        "bootstrap.servers": KAFKA_BROKER,
        "enable.idempotence": True,
        "acks": "all",
        "retries": 5,
        "linger.ms": 20,
        "batch.num.messages": 500,
        "compression.type": "gzip",
        "message.timeout.ms": 15000,
        "client.id": "ruilog-ingest",
    }
    return Producer(conf)


def _get_producer() -> Optional[Producer]:
    global _producer, _last_error
    if _producer is not None:
        return _producer
    with _lock:
        if _producer is not None:
            return _producer
        try:
            _producer = _new_producer()
            # 触发一次元数据拉取以尽早发现连接问题（不阻塞主流程）
            _producer.list_topics(timeout=5)
            _last_error = None
        except Exception as e:  # noqa: BLE001
            _last_error = f"{type(e).__name__}: {e}"
            _producer = None
    return _producer


def _event_id(d: Dict[str, Any], seq: int = 0) -> str:
    """确定性事件 ID（幂等去重键）。

    为什么要带 seq 和 raw：
        系统日志里大量行内容完全相同（"Reached target Multi-User System." 每秒都有），
        若只按 tenant|ts|traceId|source|host|message 哈希，同一毫秒内的重复行会撞成
        同一个 event_id，被 ClickHouse 的 ReplacingMergeTree 和 OpenSearch 的 _id
        当成重复数据静默吞掉——表现为"日志量莫名少了一截"。
        seq 取批内序号，既避免批内碰撞，又保证网络重传同一批时 ID 不变（可去重）。
    """
    eid = d.get("_event_id") or d.get("event_id")
    if eid:
        return str(eid)
    raw = "|".join(str(d.get(k, "")) for k in
                   ("tenant_id", "@timestamp", "traceId", "source", "host", "raw", "message"))
    raw = f"{raw}|{seq}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def health() -> Dict[str, Any]:
    return {
        "enabled": use_kafka(),
        "mode": mode(),
        "topic": TOPIC_RAW,
        "broker": KAFKA_BROKER,
        "connected": _producer is not None,
        "lastError": _last_error,
        "delivered": _delivered_total,
        "failed": _failed_total,
    }


def publish(docs: List[Dict[str, Any]], tenant: str) -> int:
    """把一批归一化记录写入 Kafka。返回成功投递条数。

    使用 per-message delivery callback 精确统计成功/失败；flush 等待整批确认。
    acks=all + 幂等 producer 下，callback 无错误即已持久化到所有 ISR。
    """
    global _delivered_total, _failed_total
    if not docs:
        return 0
    p = _get_producer()
    if p is None:
        raise RuntimeError(f"kafka producer unavailable: {_last_error}")

    # 分区 key 必须是明确租户：无租户的记录不允许进入 Kafka（入口已拒收），
    # 否则会以兜底 key 混进主干，下游无法追溯归属。
    if not tenant:
        raise ValueError("Kafka 写入需要明确的租户编码（无法归属的记录应在入口拒收）")
    key = tenant.encode("utf-8")
    results = {"ok": 0, "fail": 0, "err": None}

    def _cb(err, msg):
        if err is not None:
            results["fail"] += 1
            if results["err"] is None:
                results["err"] = str(err)
        else:
            results["ok"] += 1

    for seq, d in enumerate(docs):
        payload = dict(d)
        payload["event_id"] = _event_id(d, seq)
        value = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        p.produce(TOPIC_RAW, key=key, value=value, on_delivery=_cb)
        p.poll(0)

    # 等待整批确认（上限 15s）；超时未回调的按失败处理
    p.flush(timeout=15)
    _delivered_total += results["ok"]
    _failed_total += results["fail"]
    if results["fail"] or results["ok"] < len(docs):
        raise RuntimeError(
            f"kafka publish partial failure: {results['ok']}/{len(docs)} "
            f"fail={results['fail']} err={results['err']}"
        )
    return results["ok"]
