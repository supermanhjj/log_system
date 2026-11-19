"""dashboard-service —— ClickHouse 聚合引擎客户端（阶段4）。

双引擎分工：OS 负责明细全文检索；CH 负责高基数聚合（总览/趋势/级别分布/P95）。
AGG_ENGINE=ch 时，下面几个原语从 OpenSearch 切到 ClickHouse；=os 时保留旧链路（回退开关）。

设计要点：
  - 走 HTTP interface（8123）：无需 native 端口、跨语言通用、云上托管 CH 也支持。
  - 所有查询必带 tenant_id 过滤（行级隔离，与 OS 的 logs-<tenant> 物理隔离对齐）。
  - 明细表用 FINAL 触发 ReplacingMergeTree 去重，保证重放幂等下计数不重复。
  - 时间用 toDateTime64(ts) 与毫秒 epoch 比较，与 ingest 归一化语义一致。
  - 惰性连接 + 异常降级：CH 不可用时抛出，由调用方决定是否回退 OS。
"""
import os
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

CH_URL = os.getenv("CLICKHOUSE_HTTP_URL",
                   f"http://{os.getenv('CLICKHOUSE_HOST', 'clickhouse')}:{os.getenv('CLICKHOUSE_PORT', '8123')}")
CH_DB = os.getenv("CLICKHOUSE_DB", "ruilog")
CH_USER = os.getenv("CLICKHOUSE_USER", "ruilog")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "ruilogpass")
AGG_ENGINE = os.getenv("AGG_ENGINE", "os").strip().lower()


def engine() -> str:
    return "ch" if AGG_ENGINE == "ch" else "os"


def use_ch() -> bool:
    return AGG_ENGINE == "ch"


def _tenant_cond(tenant: Optional[str]) -> str:
    if not tenant or tenant in ("all", "*"):
        return "1=1"
    esc = tenant.replace("'", "''")
    return f"tenant_id = '{esc}'"


def _ts_cond(gte_ms: int, lte_ms: int) -> str:
    """毫秒 epoch → CH DateTime64 比较条件。"""
    frm = datetime.fromtimestamp(gte_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    to = datetime.fromtimestamp(lte_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return f"ts >= toDateTime64('{frm}', 3, 'UTC') AND ts <= toDateTime64('{to}', 3, 'UTC')"


def query(sql: str) -> List[Dict[str, Any]]:
    """执行 SELECT，返回 dict 列表（JSONEachRow）。"""
    headers = {"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASS}
    body = sql + " FORMAT JSONEachRow"
    try:
        with httpx.Client(timeout=20.0) as c:
            resp = c.post(CH_URL, content=body.encode("utf-8"), headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"CH HTTP {resp.status_code}: {resp.text[:300]}")
        out = []
        for line in resp.text.strip().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"clickhouse query error: {e}")


def count_docs(tenant: Optional[str], gte_ms: int, lte_ms: int,
               extra: Optional[str] = None) -> int:
    where = f"{_tenant_cond(tenant)} AND {_ts_cond(gte_ms, lte_ms)}"
    if extra:
        where += f" AND ({extra})"
    rows = query(f"SELECT count() AS c FROM {CH_DB}.logs FINAL WHERE {where}")
    return int(rows[0]["c"]) if rows else 0


# terms 聚合允许的字段白名单：既防 SQL 注入（字段名直接拼进语句），也防"未知字段
# 静默回落"。**绝不可以用 `else: field='level'`**——v2.1 加 log_class 维度时就踩过：
# 总览「日志类型分布」悄悄变成了级别分布，接口 200、图表有数，但结论完全错。
TERMS_FIELDS = {
    "level", "source", "host", "status", "tenant_id",
    "log_class", "source_type", "app", "instance", "facility", "severity",
}


def terms_on(tenant: Optional[str], field: str, gte_ms: int, lte_ms: int,
             size: int = 50) -> Dict[str, int]:
    if field not in TERMS_FIELDS:
        # 抛错让调用方回退 OpenSearch（那里能按任意 keyword 聚合），而不是返回错数据
        raise ValueError(f"unsupported terms field for clickhouse: {field}")
    where = f"{_tenant_cond(tenant)} AND {_ts_cond(gte_ms, lte_ms)}"
    rows = query(
        f"SELECT {field} AS k, count() AS c FROM {CH_DB}.logs FINAL "
        f"WHERE {where} GROUP BY {field} ORDER BY c DESC LIMIT {size}"
    )
    return {str(r["k"]): int(r["c"]) for r in rows}


def histogram(tenant: Optional[str], gte_ms: int, lte_ms: int,
              interval_sec: int, extra: Optional[str] = None) -> List[Dict[str, Any]]:
    """按固定时间桶计数，返回 [{ts: <epoch_ms>, count: n}]。"""
    where = f"{_tenant_cond(tenant)} AND {_ts_cond(gte_ms, lte_ms)}"
    if extra:
        where += f" AND ({extra})"
    step = max(1, interval_sec)
    rows = query(
        f"SELECT toUnixTimestamp(toStartOfInterval(ts, INTERVAL {step} SECOND)) * 1000 AS t, "
        f"count() AS c FROM {CH_DB}.logs FINAL WHERE {where} "
        f"GROUP BY t ORDER BY t ASC"
    )
    return [{"ts": int(r["t"]), "count": int(r["c"])} for r in rows]


def level_histogram(tenant: Optional[str], gte_ms: int, lte_ms: int,
                    interval_sec: int) -> List[Dict[str, Any]]:
    """按时间桶 + 级别分组（总览级别分布趋势）。"""
    where = f"{_tenant_cond(tenant)} AND {_ts_cond(gte_ms, lte_ms)}"
    step = max(1, interval_sec)
    rows = query(
        f"SELECT toUnixTimestamp(toStartOfInterval(ts, INTERVAL {step} SECOND)) * 1000 AS t, "
        f"level AS lv, count() AS c FROM {CH_DB}.logs FINAL WHERE {where} "
        f"GROUP BY t, lv ORDER BY t ASC"
    )
    return [{"ts": int(r["t"]), "level": r["lv"], "count": int(r["c"])} for r in rows]


def health() -> Dict[str, Any]:
    try:
        rows = query("SELECT version() AS v")
        return {"engine": engine(), "chReachable": True, "version": rows[0]["v"] if rows else None}
    except Exception as e:  # noqa: BLE001
        return {"engine": engine(), "chReachable": False, "error": str(e)}
