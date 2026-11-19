#!/usr/bin/env python3
"""历史日志回填：OpenSearch logs-* → ClickHouse ruilog.logs（阶段5）。

用途：Flink 上线前的历史日志、或 CH 数据修复后的补数。
设计要点（与生产语义一致）：
  1. 时间语义：以 @timestamp（UTC ISO 毫秒）为准，严禁退化成采集时刻。
  2. 幂等：event_id 确定性生成（同 ingest），CH ReplacingMergeTree(ver) 按 event_id 去重，
     可重复执行、多次回填不产生重复计数。
  3. 租户隔离：逐索引（logs-<tenant>）回填，tenant_id 从索引名反解，与行级隔离一致。
  4. 分批：scroll + bulk insert，可指定时间范围/租户/批次大小，避免打满内存。
  5. 不经过 ingest/Flink 的裸回填：仅用于历史补数；新增实时数据仍走 Kafka→Flink 主干。

用法（容器内）：
  python tools/backfill-to-clickhouse.py --tenant acme --days 180 --batch 1000
  python tools/backfill-to-clickhouse.py --all --days 180       # 回填全部租户
  python tools/backfill-to-clickhouse.py --tenant acme --dry-run
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    from opensearchpy import OpenSearch
    from clickhouse_driver import Client as CHClient
except ImportError as e:
    print(f"缺少依赖: {e}（需 opensearch-py + clickhouse-driver）", file=sys.stderr)
    sys.exit(1)

OS_HOST = os.getenv("OPENSEARCH_HOST", "opensearch")
OS_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))
CH_HOST = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CH_DB = os.getenv("CLICKHOUSE_DB", "ruilog")
CH_USER = os.getenv("CLICKHOUSE_USER", "ruilog")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "ruilogpass")


def event_id(d: dict, seq: int = 0) -> str:
    """与 ingest 同构的确定性 event_id。

    seq 用于区分"同一秒、同主机、同内容"的重复行（系统日志里非常常见）。
    ingest 侧是批次内序号，回填拿不到原值，因此这里用"相同内容的出现次数"代替：
    只要能把重复行拆开，就不会被 ReplacingMergeTree 静默合并掉。
    """
    raw = "|".join(str(d.get(k, "")) for k in
                   ("tenant_id", "@timestamp", "traceId", "source", "host", "raw", "message")) + f"|{seq}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def to_ch_ts(iso: str):
    """UTC ISO (…Z) → 带时区的 datetime。

    必须返回 datetime 对象而不是字符串：clickhouse-driver 对 DateTime64 列会在
    序列化时读 item.tzinfo，传 str 会直接 AttributeError: 'str' object has no attribute 'tzinfo'。
    """
    try:
        s = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", help="单个租户编码（回填 logs-<tenant>）")
    ap.add_argument("--all", action="store_true", help="回填所有 logs-* 索引")
    ap.add_argument("--days", type=int, default=180, help="回填最近 N 天（默认 180）")
    ap.add_argument("--from", dest="from_ts", help="起始时间（ISO，含），优先于 --days")
    ap.add_argument("--to", dest="to_ts", help="结束时间（ISO，不含）")
    ap.add_argument("--batch", type=int, default=1000, help="每批条数（默认 1000）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = ap.parse_args()

    if not args.all and not args.tenant:
        ap.error("需指定 --tenant 或 --all")

    os_client = OpenSearch(hosts=[{"host": OS_HOST, "port": OS_PORT}],
                           use_ssl=False, verify_certs=False)
    ch = CHClient(host=CH_HOST, database=CH_DB, user=CH_USER, password=CH_PASS)

    # 精确时间窗优先：补数场景通常只缺一小段（如 Flink 重提交/上游断连窗口），
    # 用 --days 会整段重扫，既慢又容易把已有数据再写一遍
    if args.from_ts or args.to_ts:
        frm = int(datetime.fromisoformat(args.from_ts).timestamp() * 1000) if args.from_ts else 0
        to = int(datetime.fromisoformat(args.to_ts).timestamp() * 1000) if args.to_ts else None
    else:
        frm = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)
        to = None
    rng = {"gte": frm}
    if to:
        rng["lt"] = to
    idx = "logs-*" if args.all else f"logs-{args.tenant}"

    query = {"query": {"bool": {"must": [{"range": {"@timestamp": rng}}]}}}
    total = 0
    written = 0
    scroll_id = None
    seen: dict = {}
    insert_sql = (f"INSERT INTO {CH_DB}.logs "
                  "(event_id,tenant_id,ts,log_class,source_type,app,instance,facility,severity,"
                  "level,source,host,trace_id,status,message,attrs,ver) VALUES")

    try:
        resp = os_client.search(index=idx, body=query, scroll="2m", size=args.batch)
        scroll_id = resp.get("_scroll_id")
        while True:
            hits = resp["hits"]["hits"]
            if not hits:
                break
            rows = []
            for h in hits:
                d = h.get("_source", {})
                # 租户：优先记录内 tenant_id，否则从索引名反解
                tenant = d.get("tenant_id") or ""
                if not tenant and h.get("_index", "").startswith("logs-"):
                    tenant = h["_index"][len("logs-"):]
                tenant = tenant or "unknown"
                # 内容相同的重复行用递增序号拆开（跨批次累计），否则会被 ReplacingMergeTree 合并
                key = "|".join(str(d.get(k, "")) for k in
                               ("tenant_id", "@timestamp", "traceId", "source", "host", "raw", "message"))
                n = seen.get(key, 0)
                seen[key] = n + 1
                eid = d.get("event_id") or event_id({**d, "tenant_id": tenant}, n)
                # attrs 在 OS 是 flat_object（取回是 dict），CH 侧存 JSON 串，保持一致
                a = d.get("attrs")
                attrs_json = json.dumps(a, ensure_ascii=False) if isinstance(a, (dict, list)) else str(a or "")
                rows.append((
                    eid, tenant, to_ch_ts(d.get("@timestamp", "")),
                    str(d.get("log_class", "")), str(d.get("source_type", "")),
                    str(d.get("app", "")), str(d.get("instance", "")),
                    str(d.get("facility", "")), int(d.get("severity", 0) or 0),
                    str(d.get("level", "")), str(d.get("source", "")),
                    str(d.get("host", "")), str(d.get("traceId", d.get("trace_id", ""))),
                    int(d.get("status", 0) or 0), str(d.get("message", "")),
                    attrs_json, 1,
                ))
            total += len(hits)
            if not args.dry_run and rows:
                ch.execute(insert_sql, rows)
                written += len(rows)
            resp = os_client.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                os_client.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass

    print(f"回填完成: 索引={idx} 最近{args.days}天 扫描={total} 写入={written} "
          f"{'(dry-run)' if args.dry_run else ''}")


if __name__ == "__main__":
    main()
