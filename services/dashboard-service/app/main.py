"""dashboard-service —— 运行总览取数编排（真实数据分析）。

对应原型 OVW-0101 / 规格 P-OVW-0101。
本服务不做任何硬编码指标：所有数字均来自对 OpenSearch 日志索引（logs-*）的实时聚合分析，
与 search-service 共用同一索引路由与连接参数（租户 → logs-<tenant>；all → logs-*）。

聚合来源：
- 写入量 / 写入趋势      : count + date_histogram（@timestamp）
- 告警级别分布 / 活动告警 : terms(level) 枚举全部原始级别（致命/错误/警告/信息/调试…）各自配色统计；活动告警 = WARN 及以上（ERROR/FATAL+WARN）
- 最近告警              : 近期 WARN+ 事件（ERROR/FATAL/WARN），按时间倒序取前 5
- 检索耗时 P95 / 分布    : OpenSearch 查询洞察索引（top_queries-*）中真实检索记录的
                          measurements.latency.number，按固定时间桶做 P95 聚合；
                          记录一经写入不再变化，故历史柱值不会随刷新波动。
                          前置条件：集群需启用 query insights（top_n_queries）；若索引不存在或无记录，
                          返回空柱（0）而非用实时探测值伪造历史。
                          目标值不写死：读 system_params.search_p95_target（系统设置 → 检索耗时目标，
                          默认 800ms），改完 10 秒内生效，且对 30 秒结果缓存同样生效。
- 数据源健康度 / 在线数  : 各 source 近 1h 接收量 ÷ 近 7d 同时段均值（与高保真原型公式一致）

说明：告警在平台中尚无独立后端（alarm-service 仍为骨架），故“告警”面板由日志严重级别实时推导，
属于“按严重级别的活跃事件视图”；告警事件表仅列出 WARN+（真正需关注的事件）。
"""
import os
import json
import math
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import Depends, FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from opensearchpy import OpenSearch

from .authz import current_identity, resolve_tenant
from . import ch_client

OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "opensearch")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))
# OpenSearch query insights 写入的检索记录索引（滚动按天建，通配覆盖全部历史）。
QUERY_INSIGHTS_INDEX = os.getenv("QUERY_INSIGHTS_INDEX", "top_queries-*")

# 开发期安全插件关闭，无 auth；惰性建立连接。
client = OpenSearch(
    hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
    http_auth=None,
    use_ssl=False,
    verify_certs=False,
)

app = FastAPI(title="dashboard-service", version="0.4.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# PostgreSQL 只读：仅用于读取「系统设置」下发到 system_params 的阈值类参数。
PG_DSN = os.getenv(
    "SYSTEM_PG_DSN",
    f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
    f"user={os.getenv('POSTGRES_USER', 'loguser')} "
    f"password={os.getenv('POSTGRES_PASSWORD', 'logpass')} "
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')}",
)
_pg_conn = None


def pg_conn():
    """惰性连接 + autocommit。库不可达时抛异常，由调用方兜底，不影响 OpenSearch 取数。"""
    global _pg_conn
    if _pg_conn is None or getattr(_pg_conn, "closed", 1):
        _pg_conn = psycopg2.connect(PG_DSN, connect_timeout=3)
        _pg_conn.autocommit = True
    return _pg_conn

# ---------------------------------------------------------------------------
# 租户 → 索引 路由（与 search-service 保持一致）
# ---------------------------------------------------------------------------
def index_for(tenant: Optional[str]) -> str:
    """索引命名是约定（logs-<租户编码>）：租户清单以 tenants 表为唯一真源，不在此硬编码。"""
    if tenant in ("all", "*", "", None):
        return "logs-*"
    return f"logs-{tenant}"


# 时区：统一以东八区(Asia/Shanghai)展示时间，与本地时间对齐。
# 背景：OpenSearch date 字段写入时归一为 UTC 存储；此处仅作用于对外展示层，不影响存储。
CST = timezone(timedelta(hours=8))  # Asia/Shanghai，无夏令时


def to_local(ts):
    """ES @timestamp (UTC ISO，如 2026-11-13T06:04:06.356Z) → 东八区 'YYYY-MM-DD HH:MM:SS'。"""
    if not ts:
        return ts
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        s = str(ts).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return ts
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 时间范围
# ---------------------------------------------------------------------------
RANGE_LABEL_TO_KEY = {
    "今日": "today",
    "近 1 小时": "1h",
    "近 24 小时": "1d",
    "近 7 天": "7d",
    "近 30 天": "30d",
}
RANGE_DURATION_MS = {
    "1h": 3600 * 1000,
    "1d": 86400 * 1000,
    "7d": 7 * 86400 * 1000,
    "30d": 30 * 86400 * 1000,
}
HIST_INTERVAL = {"1h": "5m", "1d": "1h", "7d": "1d", "30d": "1d", "today": "1h"}
HIST_FORMAT = {"1h": "HH:mm", "1d": "HH:mm", "7d": "MM-dd", "30d": "MM-dd", "today": "HH:mm"}

# 各维度下“今日写入日志”卡片的标题（今日=自然日口径，其余=滚动窗口口径）
WRITE_LABEL = {
    "today": "今日写入日志",
    "1h": "近1小时写入",
    "1d": "近24小时写入",
    "7d": "近7天写入",
    "30d": "近30天写入",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def range_key_of(range_in: str) -> str:
    if range_in in ("today", "今日"):
        return "today"
    return RANGE_LABEL_TO_KEY.get(range_in, range_in if range_in in RANGE_DURATION_MS else "1d")


def ms_to_label(ms: int, fmt: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=CST).strftime(
        "%H:%M" if fmt == "HH:mm" else "%m-%d"
    )


# ---------------------------------------------------------------------------
# OpenSearch 取数原语
# ---------------------------------------------------------------------------
def _time_must(gte: int, lte: int) -> Dict[str, Any]:
    return {"range": {"@timestamp": {"gte": gte, "lte": lte}}}


def _tenant_of(idx: str) -> Optional[str]:
    """从索引名反解租户（logs-<tenant> → tenant；logs-* → None=全部）。"""
    if idx and idx.startswith("logs-") and idx != "logs-*":
        return idx[len("logs-"):]
    return None


def count_docs(idx: str, gte: int, lte: int, extra_must: Optional[List[Dict]] = None) -> int:
    # 双引擎：AGG_ENGINE=ch 时走 ClickHouse 聚合（失败回退 OS，保证可用性）
    if ch_client.use_ch():
        try:
            extra_sql = None
            if extra_must:
                # 仅支持把常见 term/terms 条件翻译成 CH 谓词；复杂条件回退 OS
                parts = []
                for m in extra_must:
                    if "term" in m:
                        for k, v in m["term"].items():
                            parts.append(f"{k} = '{str(v).replace(chr(39), chr(39)*2)}'")
                    elif "terms" in m:
                        for k, vs in m["terms"].items():
                            inside = ",".join(f"'{str(v).replace(chr(39), chr(39)*2)}'" for v in vs)
                            parts.append(f"{k} IN ({inside})")
                    elif "range" in m:
                        for k, rng in m["range"].items():
                            if "gte" in rng:
                                parts.append(f"{k} >= {rng['gte']}")
                extra_sql = " AND ".join(parts) if parts else None
            return ch_client.count_docs(_tenant_of(idx), gte, lte, extra_sql)
        except Exception as ce:  # noqa: BLE001
            print(f"[dashboard] CH count failed, fallback OS: {ce}", flush=True)
    must = [_time_must(gte, lte)]
    if extra_must:
        must.extend(extra_must)
    body = {"query": {"bool": {"must": must}}}
    try:
        resp = client.count(index=idx, body=body)
        return int(resp.get("count", 0))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch count error: {e}")


def agg_search(idx: str, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        resp = client.search(index=idx, body=body)
        return resp
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch search error: {e}")


def terms_on(idx: str, field: str, gte: int, lte: int, size: int = 50) -> Dict[str, int]:
    if ch_client.use_ch():
        try:
            return ch_client.terms_on(_tenant_of(idx), field, gte, lte, size)
        except Exception as ce:  # noqa: BLE001
            print(f"[dashboard] CH terms_on failed, fallback OS: {ce}", flush=True)
    body = {
        "size": 0,
        "query": {"bool": {"must": [_time_must(gte, lte)]}},
        "aggs": {"t": {"terms": {"field": field, "size": size}}},
    }
    resp = agg_search(idx, body)
    return {b["key"]: b["doc_count"] for b in resp["aggregations"]["t"]["buckets"]}


def percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


# ---------------------------------------------------------------------------
# 严重级别归类
# ---------------------------------------------------------------------------
SEV_CRITICAL = {"ERROR", "FATAL"}
SEV_WARNING = {"WARN"}
SEV_INFO = {"INFO", "DEBUG"}


def classify_level(level: Optional[str]) -> str:
    lv = (level or "").upper()
    if lv in SEV_CRITICAL:
        return "严重"
    if lv in SEV_WARNING:
        return "警告"
    return "提示"


def pct_change(cur: float, prev: float) -> float:
    if prev <= 0:
        return 0.0
    return round((cur - prev) / prev * 100, 1)


# ---------------------------------------------------------------------------
# 检索耗时：取 OpenSearch 查询洞察里已落盘的真实检索记录，按时间桶聚合 P95
# ---------------------------------------------------------------------------
def latency_buckets(idx: str, frm: int, to: int, interval: str, fmt: str) -> Dict[str, Any]:
    """按固定时间桶统计真实检索 P95。

    与「每次刷新重新对历史窗口发起查询、拿本次 took 当历史耗时」不同，这里的每条样本都是
    检索当时由 OpenSearch 写入的历史记录，桶一旦过去就不再变化；只有当前桶会随新发生的
    检索缓慢增长。无记录的时段返回 0 柱并带上 count=0，不用探测值补空。

    租户口径：具体租户只统计命中所选索引与跨租户通配（logs-*）的检索；all 统计全部 logs-*。
    本查询自身落在 top_queries-* 上，不会被计入统计。
    """
    if idx == "logs-*":
        idx_filter: Dict[str, Any] = {"prefix": {"indices": "logs-"}}
    else:
        idx_filter = {"terms": {"indices": sorted({idx, "logs-*"})}}
    body = {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    idx_filter,
                    {"range": {"timestamp": {"gte": frm, "lte": to}}},
                ]
            }
        },
        "aggs": {
            "over_time": {
                "date_histogram": {
                    "field": "timestamp",
                    "fixed_interval": interval,
                    "min_doc_count": 0,
                    "format": fmt,
                    "time_zone": "+08:00",
                    "extended_bounds": {"min": frm, "max": to},
                },
                "aggs": {
                    "p95": {
                        "percentiles": {
                            "field": "measurements.latency.number",
                            "percents": [95],
                        }
                    }
                },
            },
            "overall_p95": {
                "percentiles": {"field": "measurements.latency.number", "percents": [95]}
            },
        },
    }
    try:
        resp = client.search(index=QUERY_INSIGHTS_INDEX, body=body)
    except Exception:  # noqa: BLE001  集群未启用 query insights 时不做伪造，返回空柱
        return {"points": [], "p95": 0.0, "count": 0, "source": "unavailable"}

    aggs = resp.get("aggregations", {})
    points = []
    for b in aggs.get("over_time", {}).get("buckets", []):
        val = b.get("p95", {}).get("values", {}).get("95.0")
        points.append(
            {
                "t": b.get("key_as_string") or ms_to_label(b["key"], fmt),
                "v": round(val, 1) if val is not None else 0,
                "count": int(b.get("doc_count", 0)),
            }
        )
    overall = aggs.get("overall_p95", {}).get("values", {}).get("95.0")
    total = resp.get("hits", {}).get("total", 0)
    if isinstance(total, dict):
        total = total.get("value", 0)
    return {
        "points": points,
        "p95": round(overall or 0.0, 1),
        "count": int(total or 0),
        "source": "opensearch-query-insights" if total else "no-records",
    }


# ---------------------------------------------------------------------------
# 缓存（避免自动刷新/多用户重复打满 OpenSearch）
# ---------------------------------------------------------------------------
_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_MS = 30 * 1000


def cache_get(key: str) -> Optional[Dict]:
    item = _CACHE.get(key)
    if item and now_ms() - item["ts"] < CACHE_TTL_MS:
        return item["data"]
    return None


def cache_put(key: str, data: Dict) -> None:
    _CACHE[key] = {"ts": now_ms(), "data": data}


# ---------------------------------------------------------------------------
# 检索耗时目标（系统设置 → 系统参数 search_p95_target）
# ---------------------------------------------------------------------------
DEFAULT_P95_TARGET = 800          # 参数缺失或库不可达时的兜底值（与系统参数默认值一致）
P95_TARGET_MIN, P95_TARGET_MAX = 50, 60000
_PARAM_CACHE: Dict[str, Any] = {"ts": 0, "value": None}
PARAM_TTL_MS = 10 * 1000          # 10 秒即可，保证「系统设置」改完近乎立即生效


def p95_target() -> int:
    """读系统参数 search_p95_target。读不到、值非法、库连不上都回落 800，绝不因此让首页 500。"""
    now = now_ms()
    if _PARAM_CACHE["value"] is not None and now - _PARAM_CACHE["ts"] < PARAM_TTL_MS:
        return _PARAM_CACHE["value"]
    value = None
    try:
        conn = pg_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT pvalue FROM system_params WHERE pkey = 'search_p95_target'")
            row = cur.fetchone()
        if row and str(row[0]).strip():
            n = int(str(row[0]).strip())
            if P95_TARGET_MIN <= n <= P95_TARGET_MAX:
                value = n
    except Exception as e:  # noqa: BLE001
        print(f"[dashboard-service] read search_p95_target failed: {e}", flush=True)
    if value is None:
        value = DEFAULT_P95_TARGET
    _PARAM_CACHE.update(ts=now, value=value)
    return value


def apply_target(data: Dict[str, Any], target: int) -> Dict[str, Any]:
    """把缓存里的结果按当前目标值重新判定达标/超标。

    缓存按 (租户, 时间范围) 存 30 秒，目标值是「设置项」，改完要立刻生效，
    所以判定相关的字段一律在返回前用最新目标值重算，不受缓存年龄影响。
    """
    out = dict(data)
    stats = dict(out.get("stats") or {})
    card = stats.get("searchP95")
    if isinstance(card, dict):
        card = dict(card)
        p95 = card.get("value") or 0
        ok = p95 <= target
        card["sub"] = f"目标 ≤ {target}ms"
        card["status"] = "normal" if ok else "warning"
        card["statusText"] = "达标" if ok else "超标"
        stats["searchP95"] = card
    out["stats"] = stats
    latency = dict(out.get("latency") or {})
    latency["target"] = target
    out["latency"] = latency
    return out


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "dashboard-service", "aggEngine": ch_client.engine(),
            "clickhouse": ch_client.health()}


@app.get("/")
def root():
    return {
        "service": "dashboard-service",
        "docs": "/docs",
        "endpoints": [
            "/overview (运行总览指标，真实数据分析)",
            "/dashboards (仪表盘配置 CRUD，PG 持久化)",
            "/query (配置驱动的图表聚合，OpenSearch 真实取数)",
            "/meta (可配置的图表类型 / 维度 / 指标字典)",
        ],
    }


@app.get("/overview")
def overview(
    range_in: str = Query("今日", description="时间范围：今日/近 1 小时/近 24 小时/近 7 天/近 30 天 或 today/1h/1d/7d/30d。今日=东八区自然日 00:00→当前"),
    tenant: str = Query("i6000"),
    force: bool = Query(False, description="强制刷新：绕过缓存重新聚合"),
    identity: Dict[str, Any] = Depends(current_identity),
):
    tenant = resolve_tenant(identity, tenant)
    key = range_key_of(range_in)
    cache_key = f"{tenant}|{key}"
    target = p95_target()  # 系统设置 → 系统参数 search_p95_target，取不到回落 800
    if not force:
        cached = cache_get(cache_key)
        if cached is not None:
            return apply_target(cached, target)

    idx = index_for(tenant)
    to = now_ms()

    if key == "today":
        # 今日：东八区当天 00:00:00 → 当前时刻（自然日口径，区别于“近24小时”滚动窗口）
        now_local = datetime.now(tz=CST)
        start_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        frm = int(start_today.timestamp() * 1000)
        dur = to - frm
        # 环比：昨日同时段（相同自然日长度，整体前移 24h）
        prev_frm = frm - 86400 * 1000
        prev_to = to - 86400 * 1000
    else:
        dur = RANGE_DURATION_MS[key]
        frm = to - dur
        prev_frm = frm - dur
        prev_to = frm

    fmt = HIST_FORMAT[key]
    interval = HIST_INTERVAL[key]

    # 1) 写入量 + 环比
    write_cur = count_docs(idx, frm, to)
    write_prev = count_docs(idx, prev_frm, prev_to)
    write_trend_pct = pct_change(write_cur, write_prev)
    write_sub = "较昨日同期" if key == "today" else "较上一周期"

    # 2) 写入趋势（date_histogram）—— CH 引擎下按固定秒级桶聚合，再按东八区格式化
    write_points = None
    if ch_client.use_ch():
        try:
            # interval 形如 "5m"/"1h"/"1d"，转换为秒
            unit = interval[-1]
            num = int(interval[:-1])
            sec = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 60) * num
            ch_hist = ch_client.histogram(_tenant_of(idx), frm, to, sec)
            write_points = [
                {"t": ms_to_label(p["ts"], fmt), "v": p["count"]} for p in ch_hist
            ]
        except Exception as ce:  # noqa: BLE001
            print(f"[dashboard] CH histogram failed, fallback OS: {ce}", flush=True)
    if write_points is None:
        hist_body = {
            "size": 0,
            "query": {"bool": {"must": [_time_must(frm, to)]}},
            "aggs": {
                "over_time": {
                    "date_histogram": {
                        "field": "@timestamp",
                        "fixed_interval": interval,
                        "min_doc_count": 0,
                        "format": fmt,
                        "time_zone": "+08:00",
                    }
                }
            },
        }
        hist_resp = agg_search(idx, hist_body)
        write_points = [
            {"t": b.get("key_as_string") or ms_to_label(b["key"], fmt), "v": b["doc_count"]}
            for b in hist_resp["aggregations"]["over_time"]["buckets"]
        ]

    # 3) 级别分布（terms on level）—— 展示全部级别，而非仅严重/警告
    level_counts = terms_on(idx, "level", frm, to, size=50)
    # 日志类型分布：四类采集上线后，总览必须能回答"这些日志来自哪类系统"
    log_class_counts = terms_on(idx, "log_class", frm, to, size=10)
    # 严重级别归类仍用于“活动告警”KPI（WARN 及以上视为告警；INFO/DEBUG 为正常日志不计入）
    sev = {"严重": 0, "警告": 0, "提示": 0}
    for lv, c in level_counts.items():
        sev[classify_level(lv)] += c
    active_total = sev["严重"] + sev["警告"]

    # 各原始级别 → 中文名 + 配色；按数量降序，保证图例与扇区稳定、重点级别靠前
    LEVEL_META = {
        "FATAL": ("致命", "#B71C1C"),
        "ERROR": ("错误", "#E53935"),
        "WARN": ("警告", "#EA720F"),
        "INFO": ("信息", "#03857E"),
        "DEBUG": ("调试", "#757575"),
        "NOTICE": ("通知", "#8E24AA"),
        "TRACE": ("追踪", "#90A4AE"),
    }
    DEFAULT_META = ("其他", "#CFD8DC")
    alarm_dist = [
        {
            "raw": lv,
            "name": LEVEL_META.get(lv.upper(), DEFAULT_META)[0],
            "value": c,
            "color": LEVEL_META.get(lv.upper(), DEFAULT_META)[1],
        }
        for lv, c in sorted(level_counts.items(), key=lambda kv: -kv[1])
    ]

    # 日志类型分布：固定配色 + 中文名，占比口径与级别分布一致（同一时间窗、同一租户索引）
    # 空 log_class 是 v2.1 采集模型上线前写入的历史日志（分类字段当时还不存在），
    # 单独标注为「未分类（改造前）」而不是悄悄归到某一类——宁可显示"不知道"也不伪造口径。
    log_class_dist = [
        {
            "raw": k,
            "name": LOG_CLASS_LABELS.get(k, k or "未分类（改造前）"),
            "value": c,
            "color": LOG_CLASS_COLORS.get(k, "#CFD8DC"),
        }
        for k, c in sorted(log_class_counts.items(), key=lambda kv: -kv[1])
    ]

    # 4) 最近告警（WARN+ 事件，按时间倒序）
    recent_body = {
        "size": 5,
        "query": {
            "bool": {
                "must": [
                    _time_must(frm, to),
                    {"terms": {"level": ["ERROR", "FATAL", "WARN"]}},
                ]
            }
        },
        "sort": [{"@timestamp": {"order": "desc"}}],
        "_source": ["@timestamp", "level", "source", "message"],
    }
    recent_resp = agg_search(idx, recent_body)
    recent_alarms = []
    for h in recent_resp["hits"]["hits"]:
        s = h.get("_source", {})
        msg = (s.get("message") or "")[:48]
        recent_alarms.append(
            {
                "name": msg or s.get("source", "—"),
                "level": classify_level(s.get("level")),
                "source": s.get("source", "—"),
                "occurTime": to_local(s.get("@timestamp")),
                "status": "告警中",
                "handler": "—",
            }
        )

    # 5) 检索耗时分布：按固定时间桶聚合查询洞察里已落盘的真实检索记录
    #    历史桶的样本是当时写入的，刷新不会重测，因此历史柱值恒定；只有当前桶随新检索增长。
    lat = latency_buckets(idx, frm, to, interval, fmt)
    lat_samples = lat["points"]
    p95 = lat["p95"]
    # 趋势：有检索记录的桶按时间前后各半分别取 P95 再比较；无记录时段不参与，避免把 0 算进来
    lat_valid = [p["v"] for p in lat_samples if p["count"] > 0]
    half = len(lat_valid) // 2
    lat_first = percentile(sorted(lat_valid[:half]), 0.95)
    lat_second = percentile(sorted(lat_valid[half:]), 0.95)
    p95_trend_pct = pct_change(lat_second, lat_first)  # 后段 vs 前段（升高=变慢）

    # 6) 数据源健康度（近 1h ÷ 近 7d 同时段均值）
    h1_now = now_ms()
    h1_from = h1_now - 3600 * 1000
    d7_from = h1_now - 7 * 86400 * 1000
    recent_src = terms_on(idx, "source", h1_from, h1_now, size=100)
    base_src = terms_on(idx, "source", d7_from, h1_now, size=100)
    all_src = list(dict.fromkeys(list(recent_src.keys()) + list(base_src.keys())))
    sources = []
    abnormal = 0
    offline = 0
    for name in all_src:
        total7 = base_src.get(name, 0)
        avg_per_1h = total7 / (7 * 24) if total7 > 0 else 0
        recent = recent_src.get(name, 0)
        if avg_per_1h <= 0:
            health = 100 if recent > 0 else 0
        else:
            health = min(100, round(recent / avg_per_1h * 100))
        if recent == 0 and total7 > 0:
            offline += 1
        if health < 70:
            abnormal += 1
        if health < 70:
            status, tag = ("fault", "异常")
        elif health < 85:
            status, tag = ("warning", "告警中")
        else:
            status, tag = ("normal", "正常")
        rate_per_day = round(total7 / 7)
        sources.append(
            {
                "name": name,
                "rate": f"{rate_per_day:,} 条/日" if rate_per_day > 0 else "",
                "health": health,
                "status": status,
                "tag": tag,
            }
        )
    sources.sort(key=lambda x: x["health"])  # 健康度低（异常）优先展示

    data = {
        "updatedAt": datetime.fromtimestamp(to / 1000, tz=CST).isoformat(),
        "range": key,
        "stats": {
            "writeVolume": {
                "label": WRITE_LABEL.get(key, "周期内写入日志"),
                "value": write_cur,
                "unit": "条",
                "trend": "up" if write_trend_pct >= 0 else "down",
                "trendText": f"{'↑' if write_trend_pct >= 0 else '↓'} {abs(write_trend_pct)}%",
                "sub": write_sub,
                "status": "normal",
                "statusText": "正常",
            },
            "activeAlarms": {
                "label": "活动告警",
                "value": active_total,
                "unit": "条",
                "badge": sev["严重"],
                "sub": f"严重 {sev['严重']} · 警告 {sev['警告']}",
            },
            "searchP95": {
                "label": "检索耗时 P95",
                "value": p95,
                "unit": "ms",
                "trend": "up" if p95_trend_pct >= 0 else "down",
                "trendText": f"{'↑' if p95_trend_pct >= 0 else '↓'} {abs(p95_trend_pct)}%",
                "sub": f"目标 ≤ {target}ms",
                "status": "normal" if p95 <= target else "warning",
                "statusText": "达标" if p95 <= target else "超标",
            },
            "sourceCount": {
                "label": "在线数据源",
                "value": len(all_src) - abnormal,
                "total": len(all_src),
                "unit": "个",
                "status": "warning" if abnormal > 0 else "normal",
                "statusText": f"{abnormal} 个异常" if abnormal > 0 else "全部正常",
                "sub": f"离线 {offline} · 异常 {abnormal}",
            },
        },
        "writeTrend": {"unit": "条", "points": write_points},
        "alarmDist": alarm_dist,
        "logClassDist": log_class_dist,
        "latency": {
            "unit": "ms",
            "target": target,
            "points": lat_samples,
            "source": lat["source"],
            "sampleCount": lat["count"],
        },
        "sources": sources,
        "recentAlarms": recent_alarms,
    }
    cache_put(cache_key, data)
    return data

# ===========================================================================
# 仪表盘（对应原型 P-DSH-0301 列表 / P-DSH-0302 详情）
# ---------------------------------------------------------------------------
# 设计取舍（如实记录）：
# - 配置（名称/负责人/图表布局/每图表的维度与筛选）存 PG 表 dashboards，
#   一条记录一个看板，widgets 为 JSONB 数组，支持拖拽后的布局持久化。
# - 图表数据不在保存时固化：每次打开看板由 POST /query 按每个 widget 的维度配置
#   实时对 OpenSearch(logs-*) 聚合，与运行总览共用同一取数原语与租户路由。
# - 不新增定时任务、不落指标快照：演示环境数据量小，实时聚合即可；30s 结果缓存复用。
# ===========================================================================

# 可配置维度字典：前端编辑面板与后端聚合共用，避免两边口径漂移。
# field 为 OpenSearch 字段名；label 为中文展示名；支持与否取决于日志 mapping。
DIMENSIONS = {
    "none":     {"label": "总量（单值）", "field": None,       "kind": "scalar"},
    "time":     {"label": "时间趋势",     "field": "@timestamp", "kind": "time"},
    # 多类型采集后新增的两个分类维度：先把「系统/中间件/数据库/业务」分开看，
    # 再下钻到 source_type，否则四类日志混在一个总量里，看板看不出是哪类在涨。
    "logClass": {"label": "日志类型",     "field": "log_class", "kind": "terms"},
    "sourceType": {"label": "来源类型",   "field": "source_type", "kind": "terms"},
    "level":    {"label": "日志级别",     "field": "level",    "kind": "terms"},
    "source":   {"label": "来源服务",     "field": "source",   "kind": "terms"},
    "host":     {"label": "主机",         "field": "host",     "kind": "terms"},
    "status":   {"label": "状态码",       "field": "status",   "kind": "terms"},
    "traceId":  {"label": "追踪 ID",      "field": "traceId",  "kind": "terms"},
    "tenant":   {"label": "租户",         "field": "tenant_id","kind": "terms"},
    "records":  {"label": "日志明细",     "field": None,        "kind": "records"},
}

# 图表类型字典：type 决定前端 ECharts 渲染形态，也约束可用维度。
CHART_TYPES = {
    "kpi":       {"label": "指标卡",   "dims": ["none"]},
    "line":      {"label": "折线图",   "dims": ["time"]},
    "area":      {"label": "面积图",   "dims": ["time"]},
    "bar":       {"label": "柱状图",   "dims": ["logClass", "sourceType", "level", "source", "host", "status", "traceId", "tenant"]},
    "hbar":      {"label": "横向排行", "dims": ["logClass", "sourceType", "level", "source", "host", "status", "traceId", "tenant"]},
    "pie":       {"label": "占比饼图", "dims": ["logClass", "sourceType", "level", "source", "host", "status", "tenant"]},
    "donut":     {"label": "环形占比", "dims": ["logClass", "sourceType", "level", "source", "host", "status", "tenant"]},
    "table":     {"label": "日志明细表", "dims": ["records"]},
}

# 日志级别配色（与运行总览一致）
LEVEL_COLORS = {
    "FATAL": "#B71C1C", "ERROR": "#E53935", "WARN": "#EA720F",
    "INFO": "#03857E", "DEBUG": "#757575", "NOTICE": "#8E24AA", "TRACE": "#90A4AE",
}
# 日志类型固定配色：同一类型在总览/看板/检索页保持同色，便于横向对照
LOG_CLASS_COLORS = {
    "system": "#2178D8", "middleware": "#EA720F", "database": "#E53935",
    "business": "#03857E", "network": "#5C6BC0", "container": "#8E24AA",
}
LOG_CLASS_LABELS = {
    "system": "系统日志", "middleware": "中间件日志", "database": "数据库日志",
    "business": "业务日志", "network": "网络日志", "container": "容器日志",
}
PALETTE = ["#03857E", "#19A5A0", "#2178D8", "#EA720F", "#8E24AA", "#E53935", "#757575", "#00A9A3", "#5C6BC0", "#26A69A"]


def _color_for(dim: str, key: Any, idx: int) -> str:
    if dim == "level":
        return LEVEL_COLORS.get(str(key).upper(), "#CFD8DC")
    if dim in ("logClass", "log_class"):
        return LOG_CLASS_COLORS.get(str(key).lower(), "#CFD8DC")
    return PALETTE[idx % len(PALETTE)]


def init_dashboard_table() -> None:
    """建 dashboards 表 + 种子看板。失败不阻塞服务启动。"""
    try:
        conn = pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS dashboards (
                    id SERIAL PRIMARY KEY,
                    tenant TEXT NOT NULL DEFAULT 'default',
                    name TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '系统',
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    default_range TEXT NOT NULL DEFAULT '近 24 小时',
                    widgets JSONB NOT NULL DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            # 迁移：租户隔离列。存量看板归 default，避免升级后越权可见。
            cur.execute("ALTER TABLE dashboards ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default'")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dashboards_tenant ON dashboards (tenant)")
            # 迁移：早期版本含 folder（所属目录）列，现按需求移除；IF EXISTS 保证幂等。
            cur.execute("ALTER TABLE dashboards DROP COLUMN IF EXISTS folder")
            cur.execute("SELECT COUNT(*) FROM dashboards")
            if (cur.fetchone() or [0])[0] == 0:
                seed = _seed_dashboards()
                for d in seed:
                    cur.execute(
                        """INSERT INTO dashboards (tenant, name, owner, description, status, default_range, widgets)
                           VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                        (d.get("tenant") or "i6000", d["name"], d["owner"], d["description"],
                         d["status"], d["default_range"], json.dumps(d["widgets"], ensure_ascii=False)),
                    )
    except Exception as e:  # noqa: BLE001
        print(f"[dashboard-service] init dashboards table failed: {e}", flush=True)


def _seed_dashboards() -> List[Dict[str, Any]]:
    """两条种子：对齐原型「核心业务日志看板」的 4 图 + 指标卡。"""
    core_widgets = [
        {"id": "w1", "type": "kpi", "title": "日志总量", "dimension": "none", "span": 1, "filter": {}, "limit": 10},
        {"id": "w2", "type": "kpi", "title": "错误日志", "dimension": "none", "span": 1,
         "filter": {"level": ["ERROR", "FATAL"]}, "limit": 10},
        {"id": "w3", "type": "kpi", "title": "影响主机", "dimension": "none", "span": 1,
         "filter": {}, "metric": "distinct_host", "limit": 10},
        {"id": "w4", "type": "kpi", "title": "来源服务", "dimension": "none", "span": 1,
         "filter": {}, "metric": "distinct_source", "limit": 10},
        {"id": "w5", "type": "line", "title": "日志量趋势", "dimension": "time", "span": 2, "filter": {}, "limit": 10},
        {"id": "w6", "type": "donut", "title": "日志级别分布", "dimension": "level", "span": 1, "filter": {}, "limit": 10},
        {"id": "w7", "type": "hbar", "title": "Top10 来源服务", "dimension": "source", "span": 1, "filter": {}, "limit": 10},
        {"id": "w8", "type": "bar", "title": "Top10 异常主机", "dimension": "host", "span": 2,
         "filter": {"level": ["ERROR", "FATAL"]}, "limit": 10},
        {"id": "w9", "type": "hbar", "title": "状态码分布", "dimension": "status", "span": 1, "filter": {}, "limit": 10},
        {"id": "w10", "type": "line", "title": "错误级别趋势", "dimension": "time", "span": 2,
         "filter": {"level": ["ERROR", "FATAL"]}, "limit": 10},
        {"id": "w11", "type": "table", "title": "异常日志明细", "dimension": "records", "span": 4,
         "filter": {"level": ["ERROR", "FATAL", "WARN"]}, "limit": 6},
        {"id": "w12", "type": "donut", "title": "日志类型分布", "dimension": "logClass", "span": 1,
         "filter": {}, "limit": 10},
    ]
    return [
        {
            "name": "核心业务日志看板", "owner": "黄金金",
            "description": "订单/支付链路的日志总量、级别分布、异常主机与趋势",
            "status": "running", "default_range": "近 24 小时", "widgets": core_widgets,
        },
        {
            "name": "网关访问质量", "owner": "黄金金",
            "description": "网关状态码与来源服务分布",
            "status": "running", "default_range": "近 7 天",
            "widgets": [
                {"id": "g1", "type": "kpi", "title": "请求日志量", "dimension": "none", "span": 1, "filter": {}, "limit": 10},
                {"id": "g2", "type": "donut", "title": "状态码占比", "dimension": "status", "span": 1, "filter": {}, "limit": 10},
                {"id": "g3", "type": "bar", "title": "来源服务请求量", "dimension": "source", "span": 2, "filter": {}, "limit": 10},
                {"id": "g4", "type": "line", "title": "请求量趋势", "dimension": "time", "span": 2, "filter": {}, "limit": 10},
            ],
        },
    ]


def _row_to_dashboard(row) -> Dict[str, Any]:
    d = {
        "id": row["id"], "name": row["name"], "owner": row["owner"],
        "tenant": row.get("tenant") or "i6000",
        "description": row["description"], "status": row["status"],
        "defaultRange": row["default_range"],
        "widgets": row["widgets"] if isinstance(row["widgets"], list) else json.loads(row["widgets"] or "[]"),
        "createdAt": to_local(row["created_at"]) if row["created_at"] else "",
        "updatedAt": to_local(row["updated_at"]) if row["updated_at"] else "",
    }
    d["chartCount"] = len(d["widgets"])
    return d


def _load_dashboard(dash_id: int, tenant: str):
    """按 id 取看板并校验租户归属。跨租户统一按 404 处理，不泄露他租户资产是否存在。"""
    with pg_conn().cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM dashboards WHERE id = %s", (dash_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="仪表盘不存在")
    if tenant != "all" and (row.get("tenant") or "i6000") != tenant:
        raise HTTPException(status_code=404, detail="仪表盘不存在")
    return row


@app.on_event("startup")
def _startup_dashboards():
    init_dashboard_table()


@app.get("/dashboards")
def list_dashboards(
    tenant: str = Query("i6000"),
    identity: Dict[str, Any] = Depends(current_identity),
):
    """看板为租户级资产：普通账号只看本租户，管理员可用 tenant=all 巡检全平台。"""
    tenant = resolve_tenant(identity, tenant)
    try:
        with pg_conn().cursor(cursor_factory=RealDictCursor) as cur:
            if tenant == "all":
                cur.execute("SELECT * FROM dashboards ORDER BY updated_at DESC")
            else:
                cur.execute(
                    "SELECT * FROM dashboards WHERE tenant = %s ORDER BY updated_at DESC",
                    (tenant,),
                )
            rows = cur.fetchall()
        return {"items": [_row_to_dashboard(r) for r in rows], "tenant": tenant}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"list dashboards error: {e}")


@app.get("/dashboards/meta")
def dashboard_meta(identity: Dict[str, Any] = Depends(current_identity)):
    """编辑面板可用字典：图表类型、可配置维度以及时间范围。需登录。"""
    return {
        "chartTypes": [{"value": k, **v} for k, v in CHART_TYPES.items()],
        "dimensions": [{"value": k, **v} for k, v in DIMENSIONS.items()],
        "ranges": list(RANGE_LABEL_TO_KEY.keys()),
        "defaultLayout": {"span1": 1, "span2": 2, "cols": 4},
    }


@app.get("/dashboards/{dash_id}")
def get_dashboard(
    dash_id: int,
    tenant: str = Query("i6000"),
    identity: Dict[str, Any] = Depends(current_identity),
):
    tenant = resolve_tenant(identity, tenant)
    try:
        return _row_to_dashboard(_load_dashboard(dash_id, tenant))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"get dashboard error: {e}")


@app.post("/dashboards")
def create_dashboard(
    payload: Dict[str, Any] = Body(...),
    tenant: str = Query("i6000"),
    identity: Dict[str, Any] = Depends(current_identity),
):
    tenant = resolve_tenant(identity, tenant)
    if tenant == "all":
        raise HTTPException(status_code=400, detail="新建看板需指定具体租户，不能是 all")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    if len(name) > 24:
        raise HTTPException(status_code=400, detail="名称不超过 24 字")
    widgets = payload.get("widgets") or []
    try:
        with pg_conn().cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO dashboards (tenant, name, owner, description, status, default_range, widgets)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",
                (tenant, name, payload.get("owner") or "系统",
                 payload.get("description") or "", payload.get("status") or "running",
                 payload.get("defaultRange") or "近 24 小时",
                 json.dumps(widgets, ensure_ascii=False)),
            )
            row = cur.fetchone()
        return _row_to_dashboard(row)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"create dashboard error: {e}")


@app.put("/dashboards/{dash_id}")
def update_dashboard(
    dash_id: int,
    payload: Dict[str, Any] = Body(...),
    tenant: str = Query("i6000"),
    identity: Dict[str, Any] = Depends(current_identity),
):
    tenant = resolve_tenant(identity, tenant)
    try:
        _load_dashboard(dash_id, tenant)
        with pg_conn().cursor(cursor_factory=RealDictCursor) as cur:
            fields, values = [], []
            mapping = {
                "name": "name", "owner": "owner",
                "description": "description", "status": "status", "defaultRange": "default_range",
            }
            for k, col in mapping.items():
                if k in payload:
                    fields.append(f"{col} = %s")
                    values.append(payload[k])
            if "widgets" in payload:
                fields.append("widgets = %s::jsonb")
                values.append(json.dumps(payload["widgets"] or [], ensure_ascii=False))
            fields.append("updated_at = now()")
            values.append(dash_id)
            cur.execute(f"UPDATE dashboards SET {', '.join(fields)} WHERE id = %s RETURNING *", values)
            row = cur.fetchone()
        return _row_to_dashboard(row)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"update dashboard error: {e}")


@app.delete("/dashboards/{dash_id}")
def delete_dashboard(
    dash_id: int,
    tenant: str = Query("i6000"),
    identity: Dict[str, Any] = Depends(current_identity),
):
    tenant = resolve_tenant(identity, tenant)
    try:
        _load_dashboard(dash_id, tenant)
        with pg_conn().cursor() as cur:
            cur.execute("DELETE FROM dashboards WHERE id = %s", (dash_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="仪表盘不存在")
        return {"deleted": dash_id}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"delete dashboard error: {e}")


# ---------------------------------------------------------------------------
# 配置驱动的图表聚合：一次请求按多个 widget 的维度配置并发取数
# ---------------------------------------------------------------------------
def _window_of(range_in: str):
    key = range_key_of(range_in)
    to = now_ms()
    if key == "today":
        now_local = datetime.now(tz=CST)
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        frm = int(start.timestamp() * 1000)
    else:
        frm = to - RANGE_DURATION_MS[key]
    return key, frm, to, HIST_INTERVAL[key], HIST_FORMAT[key]


def _must_for(frm: int, to: int, flt: Dict[str, Any]) -> List[Dict[str, Any]]:
    must = [_time_must(frm, to)]
    if not flt:
        return must
    # 级别 / 日志类型 / 来源类型天然是多选语义（"看系统+中间件"很常见），统一走 terms。
    for src_key, field in (("level", "level"), ("logClass", "log_class"),
                           ("log_class", "log_class"), ("sourceType", "source_type"),
                           ("source_type", "source_type")):
        val = flt.get(src_key)
        if not val:
            continue
        vals = val if isinstance(val, list) else [v.strip() for v in str(val).split(",") if v.strip()]
        if vals:
            must.append({"terms": {field: vals}})
    for key in ("source", "host", "status", "traceId", "tenant_id", "app", "instance"):
        val = flt.get(key)
        if val not in (None, "", "all"):
            must.append({"term": {key: val}})
    return must


def _metric_aggs(metric: str) -> Dict[str, Any]:
    """按指标类型生成子聚合：distinct_* 走 cardinality，error_rate 走 filter 比例。"""
    if metric == "distinct_host":
        return {"uniq": {"cardinality": {"field": "host"}}}
    if metric == "distinct_source":
        return {"uniq": {"cardinality": {"field": "source"}}}
    if metric == "error_rate":
        return {"bad": {"filter": {"terms": {"level": ["ERROR", "FATAL"]}}}}
    return {}


def _bucket_value(bucket: Dict[str, Any], metric: str) -> float:
    if metric == "error_rate":
        total = bucket.get("doc_count", 0)
        bad = (bucket.get("bad") or {}).get("doc_count", 0)
        return round(bad / total * 100, 2) if total else 0.0
    if metric in ("distinct_host", "distinct_source"):
        return int((bucket.get("uniq") or {}).get("value", 0) or 0)
    return int(bucket.get("doc_count", 0))


def aggregate_widget(w: Dict[str, Any], idx: str, frm: int, to: int, interval: str, fmt: str) -> Dict[str, Any]:
    dim = w.get("dimension") or "none"
    metric = w.get("metric") or "count"
    limit = int(w.get("limit") or 10)
    limit = max(1, min(limit, 50))
    flt = w.get("filter") or {}
    must = _must_for(frm, to, flt)
    sub = _metric_aggs(metric)
    unit = "%" if metric == "error_rate" else ("个" if metric.startswith("distinct_") else "条")

    # 维度 records：最近日志明细表（按时间倒序取 N 条）
    if dim == "records":
        body = {
            "size": limit,
            "query": {"bool": {"must": must}},
            "sort": [{"@timestamp": {"order": "desc"}}],
            "_source": ["@timestamp", "log_class", "source_type", "level", "source", "host", "message", "status"],
        }
        resp = agg_search(idx, body)
        rows = []
        for h in resp.get("hits", {}).get("hits", []):
            s = h.get("_source", {})
            rows.append({
                "time": to_local(s.get("@timestamp")),
                "logClass": s.get("log_class", ""),
                "sourceType": s.get("source_type", ""),
                "level": s.get("level", "—"),
                "source": s.get("source", "—"),
                "host": s.get("host", "—"),
                "message": s.get("message", ""),
                "status": s.get("status", ""),
            })
        return {"id": w.get("id"), "dimension": dim, "unit": unit, "rows": rows}

    # 维度为 none：单值（count / distinct / error_rate）
    if dim == "none":
        # track_total_hits=True：OpenSearch 默认 hits.total 封顶 10000（relation=gte），
        # 总量类指标卡必须关闭截断，否则近 7 天等大数据量会被低估。error_rate 的分母同源。
        body = {"size": 0, "track_total_hits": True, "query": {"bool": {"must": must}}, "aggs": sub}
        resp = agg_search(idx, body)
        total = resp.get("hits", {}).get("total", 0)
        if isinstance(total, dict):
            total = total.get("value", 0)
        aggs = resp.get("aggregations", {})
        if metric == "error_rate":
            bad = (aggs.get("bad") or {}).get("doc_count", 0)
            value = round(bad / total * 100, 2) if total else 0.0
        elif metric in ("distinct_host", "distinct_source"):
            value = int((aggs.get("uniq") or {}).get("value", 0) or 0)
        else:
            value = int(total or 0)
        return {"id": w.get("id"), "dimension": dim, "metric": metric, "unit": unit, "value": value}

    dmeta = DIMENSIONS.get(dim)
    if not dmeta or not dmeta.get("field"):
        return {"id": w.get("id"), "dimension": dim, "unit": unit, "value": 0, "error": "unsupported dimension"}

    if dmeta["kind"] == "time":
        agg_node = {
            "date_histogram": {
                "field": "@timestamp", "fixed_interval": interval, "min_doc_count": 0,
                "format": fmt, "time_zone": "+08:00",
            }
        }
        if sub:
            agg_node["aggs"] = sub
        body = {"size": 0, "query": {"bool": {"must": must}}, "aggs": {"over_time": agg_node}}
        resp = agg_search(idx, body)
        points = []
        for b in resp["aggregations"]["over_time"]["buckets"]:
            points.append({
                "t": b.get("key_as_string") or ms_to_label(b["key"], fmt),
                "v": _bucket_value(b, metric),
            })
        return {"id": w.get("id"), "dimension": dim, "metric": metric, "unit": unit, "points": points}

    # terms 维度
    field = dmeta["field"]
    terms_node: Dict[str, Any] = {"terms": {"field": field, "size": limit}}
    # 分类维度必须把"缺字段"的文档显式收敛到一个桶：OpenSearch 的 terms 聚合默认
    # 直接丢弃 missing 文档，会让图里各类之和小于总量（看起来"少了几千条"）。
    # 用 missing="" 落桶后，前端把它显示为「未分类（改造前）」，口径与 ClickHouse 一致
    # （CH 里这些历史行 log_class 就是空串）。
    if field in ("log_class", "source_type", "app", "instance", "facility"):
        terms_node["terms"]["missing"] = ""
    if sub:
        terms_node["aggs"] = sub
    body = {"size": 0, "query": {"bool": {"must": must}}, "aggs": {"t": terms_node}}
    resp = agg_search(idx, body)
    items = []
    level_label = {"FATAL": "致命", "ERROR": "错误", "WARN": "警告", "INFO": "信息",
                   "DEBUG": "调试", "NOTICE": "通知", "TRACE": "追踪"}
    for i, b in enumerate(resp["aggregations"]["t"]["buckets"]):
        name = str(b["key"])
        if dim == "level":
            name = level_label.get(name.upper(), name)
        elif dim == "logClass":
            # 机器码（system/middleware）翻中文，图例直接可读；空值=改造前历史数据
            name = LOG_CLASS_LABELS.get(name, name or "未分类（改造前）")
        items.append({
            "name": name, "value": _bucket_value(b, metric),
            "color": _color_for(dim, b["key"], i),
        })
    return {"id": w.get("id"), "dimension": dim, "metric": metric, "unit": unit, "items": items}


@app.post("/query")
def query_widgets(payload: Dict[str, Any] = Body(...),
                  identity: Dict[str, Any] = Depends(current_identity)):
    """按 widget 配置聚合。body: {range_in, tenant, widgets:[...], force?}

    租户由登录态强制解析，payload.tenant 仅作管理员巡检意图。
    """
    widgets = payload.get("widgets") or []
    if not isinstance(widgets, list):
        raise HTTPException(status_code=400, detail="widgets 必须为数组")
    tenant = resolve_tenant(identity, payload.get("tenant"))
    key, frm, to, interval, fmt = _window_of(payload.get("range_in") or "今日")
    idx = index_for(tenant)
    force = bool(payload.get("force"))

    results = []
    errors = []
    for w in widgets:
        if not isinstance(w, dict):
            continue
        try:
            results.append(aggregate_widget(w, idx, frm, to, interval, fmt))
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001  单个图表失败不影响整板
            errors.append({"id": w.get("id"), "error": str(e)})
            results.append({"id": w.get("id"), "dimension": w.get("dimension"), "unit": "条",
                            "value": 0, "items": [], "points": [], "degraded": True})
    return {
        "range": key, "tenant": tenant,
        "updatedAt": datetime.fromtimestamp(to / 1000, tz=CST).isoformat(),
        "results": results, "errors": errors,
    }
