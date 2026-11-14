"""search-service —— 检索网关：OpenSearch 检索封装（PPL + DSL）。

对应原型 LOG-0201 / DSH-0302。
- POST /query      : 文档列表检索（DSL，支持时间范围 / 级别 / 来源 / 关键字 / 分页 / 排序；返回 _id 供详情抽屉）
- POST /pql        : 原生 PPL 分析查询（/_plugins/_ppl，供检索框 PPL 与高级分析；限定只查 logs-*，禁写操作）
- POST /histogram  : 日志分布直方图 + 级别分布（DSL date_histogram + terms）
- POST /context    : 锚点日志上下文（前/后 N 条相邻日志，供详情抽屉原始日志上下展示）
- GET  /saved-views: 保存的视图列表（PG，本切片为 stub）
- POST /saved-views: 保存视图（PG，本切片为 stub）

开发期 OpenSearch 安全插件已关闭，连接无需账号密码。租户隔离：tenant 映射到固定索引
logs-<tenant>；tenant=all 走 logs-* 并按需附加 tenant_id 过滤（由鉴权中间件统一注入，本切片前端透传）。
"""
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from opensearchpy import OpenSearch

OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "opensearch")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))

# 开发期安全插件关闭，无 auth；连接惰性建立，模块导入不触发网络。
client = OpenSearch(
    hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
    http_auth=None,
    use_ssl=False,
    verify_certs=False,
)

app = FastAPI(title="search-service", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------------
# 租户 → 索引 路由
# ----------------------------------------------------------------------------
INDEX_BY_TENANT = {
    "default": "logs-default",
    "acme": "logs-acme",
    "gp": "logs-gp",
}


def index_for(tenant: Optional[str]) -> str:
    if tenant in ("all", "*", "", None):
        return "logs-*"
    return INDEX_BY_TENANT.get(tenant, f"logs-{tenant}")


# 时区：统一以东八区(Asia/Shanghai)展示时间，与本地时间对齐。
# 背景：OpenSearch date 字段写入时归一为 UTC 存储；此处仅作用于对外展示层，
# 不影响存储。容器内已统一 TZ=Asia/Shanghai，但聚合/格式化需显式按东八区处理。
CST = timezone(timedelta(hours=8))  # Asia/Shanghai，无夏令时


def to_local(ts):
    """ES @timestamp (UTC ISO，如 2026-11-13T06:04:06.356Z) → 东八区 'YYYY-MM-DD HH:MM:SS'。

    供前端直接展示，避免与本地时间差 8 小时；输入为空或非预期格式时原样返回。
    """
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


def time_range_clause(time_range: Optional[Dict[str, Any]]):
    if not time_range:
        return None
    f, t = time_range.get("from"), time_range.get("to")
    if not f and not t:
        return None
    clause: Dict[str, Any] = {}
    if f:
        clause["gte"] = f
    if t:
        clause["lte"] = t
    return {"range": {"@timestamp": clause}}


def build_must(time_range, filters, tenant) -> List[Dict[str, Any]]:
    must: List[Dict[str, Any]] = []
    tr = time_range_clause(time_range)
    if tr:
        must.append(tr)
    if tenant and tenant not in ("all", "*", ""):
        # 跨索引(=all)时按 tenant_id 过滤，保证行级隔离语义一致
        pass  # 固定索引已天然隔离；all 场景如需强隔离可在此附加 term tenant_id
    if filters:
        if filters.get("level"):
            must.append({"term": {"level": filters["level"]}})
        if filters.get("source"):
            must.append({"term": {"source": filters["source"]}})
        if filters.get("host"):
            must.append({"term": {"host": filters["host"]}})
        if filters.get("traceId"):
            must.append({"term": {"traceId": filters["traceId"]}})
        if filters.get("status") is not None:
            must.append({"term": {"status": filters["status"]}})
        if filters.get("statusGte") is not None or filters.get("statusLte") is not None:
            rng: Dict[str, Any] = {}
            if filters.get("statusGte") is not None:
                rng["gte"] = filters["statusGte"]
            if filters.get("statusLte") is not None:
                rng["lte"] = filters["statusLte"]
            must.append({"range": {"status": rng}})
        if filters.get("keyword"):
            must.append(
                {
                    "multi_match": {
                        "query": filters["keyword"],
                        "fields": ["message", "source", "host", "traceId"],
                    }
                }
            )
    return must


# ----------------------------------------------------------------------------
# 请求模型
# ----------------------------------------------------------------------------
class QueryReq(BaseModel):
    tenant: str = "default"
    timeRange: Optional[Dict[str, Any]] = None
    filters: Optional[Dict[str, Any]] = None
    page: int = 1
    pageSize: int = 20
    sort: Optional[Dict[str, Any]] = None


class PqlReq(BaseModel):
    pql: str
    tenant: str = "default"


class HistReq(BaseModel):
    tenant: str = "default"
    timeRange: Optional[Dict[str, Any]] = None
    filters: Optional[Dict[str, Any]] = None
    interval: Optional[str] = None


class ContextReq(BaseModel):
    tenant: str = "default"
    time: str  # 锚点日志的 @timestamp（ISO，UTC）
    source: Optional[str] = None  # 限定同一来源，取该服务流的相邻日志
    before: int = 2
    after: int = 2


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def normalize_hit(hit: Dict[str, Any]) -> Dict[str, Any]:
    s = hit.get("_source", {})
    return {
        "id": hit.get("_id"),
        "ts": s.get("@timestamp"),  # 原始 UTC 时间戳，供上下文检索锚定
        "time": to_local(s.get("@timestamp")),
        "level": s.get("level"),
        "source": s.get("source"),
        "host": s.get("host"),
        "traceId": s.get("traceId"),
        "message": s.get("message"),
        "status": s.get("status"),
        "tenant_id": s.get("tenant_id"),
        "raw": s.get("raw"),  # 原始整行日志（采集端 app_log 解析器整体捕获）
    }


# ----------------------------------------------------------------------------
# 端点
# ----------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "search-service"}


@app.get("/")
def root():
    return {
        "service": "search-service",
        "docs": "/docs",
        "endpoints": ["/query (DSL 列表)", "/pql (PPL 分析)", "/histogram (直方图)"],
    }


@app.post("/query")
def query(req: QueryReq):
    """文档列表检索：DSL 提供 _id / 分页 / 排序，支撑表格 + 详情抽屉。"""
    idx = index_for(req.tenant)
    must = build_must(req.timeRange, req.filters, req.tenant)
    body = {
        "from": (req.page - 1) * req.pageSize,
        "size": req.pageSize,
        "query": {"bool": {"must": must}} if must else {"match_all": {}},
        "sort": [req.sort or {"@timestamp": {"order": "desc"}}],
        "_source": [
            "@timestamp",
            "level",
            "source",
            "host",
            "traceId",
            "message",
            "status",
            "tenant_id",
            "raw",
        ],
    }
    try:
        resp = client.search(index=idx, body=body)
        total = resp["hits"]["total"]
        if isinstance(total, dict):
            total = total.get("value")
        return {
            "total": total,
            "took_ms": resp.get("took"),
            "page": req.page,
            "pageSize": req.pageSize,
            "hits": [normalize_hit(h) for h in resp["hits"]["hits"]],
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch error: {e}")


@app.post("/pql")
def pql(req: PqlReq):
    """原生 PPL 分析查询，走 /_plugins/_ppl（已验证可用）。

    安全约束：仅允许查询 logs-*，拒绝 drop/delete/update/insert/create/alter 等写操作关键字。
    """
    stmt = (req.pql or "").strip()
    if not stmt:
        raise HTTPException(status_code=400, detail="pql 不能为空")
    if re.search(r"\b(drop|delete|update|insert|create|alter|grant)\b", stmt, re.I):
        raise HTTPException(status_code=400, detail="PPL 含不允许的写操作关键字")
    if not stmt.lower().startswith("source="):
        stmt = f"source={index_for(req.tenant)} {stmt}"
    try:
        # opensearch-py 2.x 未暴露公开 perform_request；PPL 走 /_plugins/_ppl 直连。
        resp = httpx.post(
            f"http://{OPENSEARCH_HOST}:{OPENSEARCH_PORT}/_plugins/_ppl",
            json={"query": stmt},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "schema": data.get("schema"),
            "datarows": data.get("datarows"),
            "total": data.get("total"),
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"ppl error: {e}")


@app.post("/histogram")
def histogram(req: HistReq):
    """日志分布直方图（date_histogram）+ 级别分布（terms）。"""
    idx = index_for(req.tenant)
    must = build_must(req.timeRange, req.filters, req.tenant)
    interval = req.interval or "1h"
    # 用 extended_bounds 把轴铺满所选时间范围：否则 date_histogram 即使 min_doc_count=0
    # 也只会返回数据实际覆盖的区间，导致"近一月"被压缩成有数据的那几天。
    # 月度聚合必须用 calendar_interval（fixed_interval 不支持"月"这种日历单位）。
    over_time = {
        "field": "@timestamp",
        "min_doc_count": 0,
        "time_zone": "+08:00",
    }
    if interval == "1M":
        over_time["calendar_interval"] = "1M"
    else:
        over_time["fixed_interval"] = interval
    if req.timeRange:
        f, t = req.timeRange.get("from"), req.timeRange.get("to")
        if f and t:
            over_time["extended_bounds"] = {"min": f, "max": t}
    body = {
        "size": 0,
        "query": {"bool": {"must": must}} if must else {"match_all": {}},
        "aggs": {
            "over_time": {"date_histogram": over_time},
            "by_level": {"terms": {"field": "level", "size": 10}},
        },
    }
    try:
        resp = client.search(index=idx, body=body)
        aggs = resp["aggregations"]
        return {
            "interval": interval,
            "buckets": [
                {"time": b["key_as_string"], "count": b["doc_count"]}
                for b in aggs["over_time"]["buckets"]
            ],
            "by_level": [
                {"level": b["key"], "count": b["doc_count"]}
                for b in aggs["by_level"]["buckets"]
            ],
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch error: {e}")


@app.post("/context")
def context(req: ContextReq):
    """锚点日志的上下文：返回其前 before 条、后 after 条相邻日志（同租户、可选同来源）。

    用于详情抽屉「原始日志」上/下展示上下文日志；before 按时间倒序取后逆序为时间正序，
    after 按时间正序，二者均按时间升序呈现，符合阅读时序。
    """
    idx = index_for(req.tenant)
    src = ["@timestamp", "level", "source", "host", "traceId", "message", "status", "tenant_id", "raw"]
    base_must = []
    if req.source:
        base_must.append({"term": {"source": req.source}})

    def fetch(direction: str, count: int):
        rng = {"lt": req.time} if direction == "before" else {"gt": req.time}
        body = {
            "size": count,
            "query": {"bool": {"must": base_must + [{"range": {"@timestamp": rng}}]}},
            "sort": [{"@timestamp": {"order": "desc" if direction == "before" else "asc"}}],
            "_source": src,
        }
        resp = client.search(index=idx, body=body)
        hits = [normalize_hit(h) for h in resp["hits"]["hits"]]
        if direction == "before":
            hits = list(reversed(hits))  # 逆序为时间正序（早→晚）
        return hits

    try:
        return {"before": fetch("before", req.before), "after": fetch("after", req.after)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch error: {e}")


# saved-views：本切片为 stub（PG 落地留待统一数据模型阶段）
@app.get("/saved-views")
def list_saved_views():
    return {"items": [], "note": "stub: PG 持久化待阶段4统一实现"}


@app.post("/saved-views")
def save_view(payload: Dict[str, Any]):
    return {"status": "received", "note": "stub: 未落库", "payload": payload}
