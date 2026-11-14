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
- 数据源健康度 / 在线数  : 各 source 近 1h 接收量 ÷ 近 7d 同时段均值（与高保真原型公式一致）

说明：告警在平台中尚无独立后端（alarm-service 仍为骨架），故“告警”面板由日志严重级别实时推导，
属于“按严重级别的活跃事件视图”；告警事件表仅列出 WARN+（真正需关注的事件）。
"""
import os
import math
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from opensearchpy import OpenSearch

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

app = FastAPI(title="dashboard-service", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 租户 → 索引 路由（与 search-service 保持一致）
# ---------------------------------------------------------------------------
INDEX_BY_TENANT = {"default": "logs-default", "acme": "logs-acme", "gp": "logs-gp"}


def index_for(tenant: Optional[str]) -> str:
    if tenant in ("all", "*", "", None):
        return "logs-*"
    return INDEX_BY_TENANT.get(tenant, f"logs-{tenant}")


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


def count_docs(idx: str, gte: int, lte: int, extra_must: Optional[List[Dict]] = None) -> int:
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
# 端点
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "dashboard-service"}


@app.get("/")
def root():
    return {
        "service": "dashboard-service",
        "docs": "/docs",
        "endpoints": ["/overview (运行总览指标，真实数据分析)"],
    }


@app.get("/overview")
def overview(
    range_in: str = Query("今日", description="时间范围：今日/近 1 小时/近 24 小时/近 7 天/近 30 天 或 today/1h/1d/7d/30d。今日=东八区自然日 00:00→当前"),
    tenant: str = Query("default"),
    force: bool = Query(False, description="强制刷新：绕过缓存重新聚合"),
):
    key = range_key_of(range_in)
    cache_key = f"{tenant}|{key}"
    if not force:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

    idx = index_for(tenant)
    to = now_ms()
    target = 800  # 检索耗时目标（ms）

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

    # 2) 写入趋势（date_histogram）
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
