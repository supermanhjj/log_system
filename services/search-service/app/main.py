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
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from opensearchpy import OpenSearch

from .authz import current_identity, resolve_tenant, tenant_directory

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
#
# 索引命名是**约定**（logs-<租户编码>），不再维护硬编码租户清单：
# tenants 表是唯一真源，新增租户无需改这里。all → logs-*（仅管理员，由 authz 放行）。
# 无法归属的日志在写入入口直接拒收（不再有兜底索引），避免出现归属不明的数据。
# ----------------------------------------------------------------------------
def index_for(tenant: Optional[str]) -> str:
    if tenant in ("all", "*", "", None):
        return "logs-*"
    return f"logs-{tenant}"


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


def ppl_time_filter(time_range: Optional[Dict[str, Any]]) -> Optional[str]:
    """把时间范围转成一段独立的 PPL where 条件（供 /pql 注入）。

    @timestamp 在索引里存的是 UTC 无时区字符串，PPL 比较必须用带 Z 的 ISO 字面量：
    '2026-11-16T09:12:00' 会触发 500，'2026-11-16 09:12:00' 会静默返回空结果。
    这里统一解析后转 UTC 再格式化，避免时区/格式问题。
    """
    if not time_range:
        return None
    conds: List[str] = []
    for key, op in (("from", ">="), ("to", "<=")):
        raw = time_range.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"timeRange.{key} 不是合法的 ISO 时间：{raw}"
            )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        literal = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        conds.append(f"@timestamp {op} '{literal}'")
    if not conds:
        return None
    return "where " + " and ".join(conds)


# ----------------------------------------------------------------------------
# 租户维度：虚拟字段 tenant ↔ 物理字段 tenant_id
#
# 检索层对外的稳定契约是「一个租户维度 tenant」，物理字段名与索引命名不外泄：
# - 请求侧：`tenant` / `tenant_code` / `tenant_name` 一律落到 `tenant_id`（接入端写入的
#   keyword，值即租户编码）；比较右侧写的是租户中文名时换成机器码（'总部生产' → 'default'），
#   否则中文名匹配机器码恒 0 条、甚至把 PPL 打成 500。
# - 结果侧：`tenant_id` 列改名 `tenant` 并换回中文名，表格里直接可读。
# 单租户范围同样适用——索引已物理隔离，这里的翻译只保证「写法能对齐」，不改变可见范围。
# ----------------------------------------------------------------------------
_TENANT_ALIAS = re.compile(
    r"(?<![A-Za-z0-9_.@])(?:tenant_id|tenant_code|tenant_name|tenant)(?![A-Za-z0-9_.@])", re.I)
_TENANT_CMP = re.compile(
    r"(?<![A-Za-z0-9_.@])tenant_id(?![A-Za-z0-9_.@])\s*"
    r"(?:=|!=|<>|\bin\b|\bnot\s+in\b|\blike\b)\s*('(?:[^'\\]|\\.)*'|\([^()]*\))", re.I)
_LITERAL_SPLIT = re.compile(r"('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")")
_QUOTED = re.compile(r"'((?:[^'\\]|\\.)*)'")


def _map_outside_literals(text: str, fn) -> str:
    """只改代码段、跳过字符串字面量，避免误改 message like '%tenant%' 这类正文条件。"""
    segs = _LITERAL_SPLIT.split(text or "")
    return "".join(fn(s) if i % 2 == 0 else s for i, s in enumerate(segs))


def rewrite_tenant_dimension(stmt: str, directory: List[Dict[str, str]]) -> str:
    name2code = {str(t.get("name")): str(t.get("code")) for t in directory if t.get("name")}
    # 1) 字段名归一：tenant / tenant_code / tenant_name → tenant_id
    stmt = _map_outside_literals(stmt, lambda s: _TENANT_ALIAS.sub("tenant_id", s))
    if not name2code:
        return stmt

    # 2) 值归一：只改 tenant_id 比较右侧的字面量，其余位置的字符串一律不动
    def fix(m: "re.Match") -> str:
        whole, value = m.group(0), m.group(1)
        new_value = _QUOTED.sub(
            lambda q: f"'{name2code[q.group(1)]}'" if q.group(1) in name2code else q.group(0),
            value)
        return whole[: len(whole) - len(value)] + new_value

    return _TENANT_CMP.sub(fix, stmt)


def localize_tenant_columns(data: Dict[str, Any],
                            directory: List[Dict[str, str]]) -> Dict[str, Any]:
    """结果中的 tenant_id 列 → tenant，并把租户编码换回中文名（找不到就原样返回）。"""
    schema = data.get("schema") or []
    idx = next((i for i, s in enumerate(schema) if str(s.get("name")) == "tenant_id"), None)
    if idx is None:
        return data
    code2name = {str(t.get("code")): str(t.get("name")) for t in directory if t.get("name")}
    schema[idx] = {**schema[idx], "name": "tenant"}
    for row in data.get("datarows") or []:
        if idx < len(row) and isinstance(row[idx], str) and row[idx] in code2name:
            row[idx] = code2name[row[idx]]
    data["schema"] = schema
    return data


def _terms_clause(field: str, value) -> Optional[Dict[str, Any]]:
    """统一处理单值 / 逗号串 / 数组三种入参形态。

    前端筛选控件可能传 "system"，也可能传多选数组 ["system","middleware"]，
    直接 term 会让数组变成非法 DSL（OpenSearch 400），所以在这里收口。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple, set)):
        vals = [str(v) for v in value if v not in (None, "")]
        if not vals:
            return None
        if len(vals) == 1:
            return {"term": {field: vals[0]}}
        return {"terms": {field: vals}}
    if isinstance(value, str) and "," in value:
        vals = [v.strip() for v in value.split(",") if v.strip()]
        if not vals:
            return None
        if len(vals) == 1:
            return {"term": {field: vals[0]}}
        return {"terms": {field: vals}}
    return {"term": {field: value}}


def build_must(time_range, filters, tenant) -> List[Dict[str, Any]]:
    must: List[Dict[str, Any]] = []
    tr = time_range_clause(time_range)
    if tr:
        must.append(tr)
    if tenant and tenant not in ("all", "*", ""):
        # 跨索引(=all)时按 tenant_id 过滤，保证行级隔离语义一致
        pass  # 固定索引已天然隔离；all 场景如需强隔离可在此附加 term tenant_id
    if filters:
        # 来源分类维度：log_class 先于具体 source_type 收窄，OS 侧两字段都是 keyword
        # （index template 显式声明），命中走倒排，不会退化成扫描。
        for f in ("logClass", "log_class"):
            c = _terms_clause("log_class", filters.get(f))
            if c:
                must.append(c)
                break
        for f in ("sourceType", "source_type"):
            c = _terms_clause("source_type", filters.get(f))
            if c:
                must.append(c)
                break
        c = _terms_clause("app", filters.get("app"))
        if c:
            must.append(c)
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
                        # log：K8s 容器日志的正文（CRI 拆帧结果）。不带上它，
                        # 检索框搜异常栈、搜容器原文永远 0 命中——而这类日志
                        # 的 message 往往只是首行。
                        "fields": ["message", "log", "raw", "source", "host", "traceId"],
                    }
                }
            )
    return must


# ----------------------------------------------------------------------------
# 请求模型
# ----------------------------------------------------------------------------
class QueryReq(BaseModel):
    tenant: Optional[str] = None
    timeRange: Optional[Dict[str, Any]] = None
    filters: Optional[Dict[str, Any]] = None
    page: int = 1
    pageSize: int = 20
    sort: Optional[Dict[str, Any]] = None


class PqlReq(BaseModel):
    pql: str
    tenant: Optional[str] = None
    timeRange: Optional[Dict[str, Any]] = None


class HistReq(BaseModel):
    tenant: Optional[str] = None
    timeRange: Optional[Dict[str, Any]] = None
    filters: Optional[Dict[str, Any]] = None
    interval: Optional[str] = None


class ContextReq(BaseModel):
    tenant: Optional[str] = None
    time: str  # 锚点日志的 @timestamp（ISO，UTC）
    source: Optional[str] = None  # 限定同一来源，取该服务流的相邻日志
    before: int = 2
    after: int = 2


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
LOG_FIELDS = [
    "@timestamp",
    "level",
    "source",
    "host",
    "traceId",
    "message",
    "status",
    "tenant_id",
    "raw",
    # K8s 容器日志正文：详情页/上下文要看完整原始行（含异常栈）时必须带回来
    "log",
    # v2.1 多类型采集新增维度（与 index template 显式字段一一对应）
    "log_class",
    "source_type",
    "app",
    "instance",
    "facility",
    "severity",
    "attrs",
]


def normalize_hit(hit: Dict[str, Any], code2name: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    s = hit.get("_source", {})
    code = s.get("tenant_id")
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
        "tenant_id": code,  # 编码，稳定标识（导出/对账用）
        "tenant": (code2name or {}).get(code, code),  # 中文名，展示用（虚拟维度）
        "raw": s.get("raw"),  # 原始整行日志（采集端 app_log 解析器整体捕获）
        "log": s.get("log"),  # K8s 容器日志原文（CRI 拆帧后），详情/上下文展示用
        # 多类型维度：log_class 是筛选主轴，source_type 是细分来源，attrs 存各类特有字段
        "log_class": s.get("log_class"),
        "source_type": s.get("source_type"),
        "app": s.get("app"),
        "instance": s.get("instance"),
        "facility": s.get("facility"),
        "severity": s.get("severity"),
        "attrs": s.get("attrs"),
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
def query(req: QueryReq, identity: Dict[str, Any] = Depends(current_identity)):
    """文档列表检索：DSL 提供 _id / 分页 / 排序，支撑表格 + 详情抽屉。

    租户由登录态强制解析（请求体里的 tenant 只作为管理员巡检意图，越权 403）。
    """
    tenant = resolve_tenant(identity, req.tenant)
    idx = index_for(tenant)
    must = build_must(req.timeRange, req.filters, tenant)
    body = {
        "from": (req.page - 1) * req.pageSize,
        "size": req.pageSize,
        "query": {"bool": {"must": must}} if must else {"match_all": {}},
        "sort": [req.sort or {"@timestamp": {"order": "desc"}}],
        "_source": LOG_FIELDS,
    }
    try:
        resp = client.search(index=idx, body=body)
        total = resp["hits"]["total"]
        if isinstance(total, dict):
            total = total.get("value")
        code2name = {str(t.get("code")): str(t.get("name")) for t in tenant_directory()}
        return {
            "total": total,
            "took_ms": resp.get("took"),
            "page": req.page,
            "pageSize": req.pageSize,
            "tenant": tenant,
            "index": idx,
            "hits": [normalize_hit(h, code2name) for h in resp["hits"]["hits"]],
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch error: {e}")


@app.post("/pql")
def pql(req: PqlReq, identity: Dict[str, Any] = Depends(current_identity)):
    """原生 PPL 分析查询，走 /_plugins/_ppl（已验证可用）。

    安全约束：
    1. 仅允许查询 logs-*，拒绝 drop/delete/update/insert/create/alter 等写操作关键字。
    2. **source 由服务端强制指定**：用户语句里若自带 `source=...` 会被丢弃（保留其后的
       管道命令），杜绝 `source=logs-*` 绕过租户隔离。
    3. 时间范围作为独立一段 where 注入到 source 之后，不改变用户 where 的 and/or 优先级。
    """
    tenant = resolve_tenant(identity, req.tenant)
    stmt = (req.pql or "").strip()
    if not stmt:
        raise HTTPException(status_code=400, detail="pql 不能为空")
    if re.search(r"\b(drop|delete|update|insert|create|alter|grant)\b", stmt, re.I):
        raise HTTPException(status_code=400, detail="PPL 含不允许的写操作关键字")

    directory = tenant_directory()
    # 租户虚拟维度 → 物理字段：手写 PPL 与智能检索生成结果走同一条翻译，语义一致
    stmt = rewrite_tenant_dimension(stmt, directory)

    # 丢弃用户自带 source 子句，只保留命令段；source 永远由服务端按租户写入
    m = re.match(r"source\s*=\s*\S+\s*(.*)", stmt, re.I | re.S)
    rest = (m.group(1).strip() if m else stmt).lstrip("|").strip()
    stmt = f"source={index_for(tenant)}" + (f" | {rest}" if rest else "")

    time_filter = ppl_time_filter(req.timeRange)
    if time_filter:
        head, sep, tail = stmt.partition("|")
        stmt = f"{head.strip()} | {time_filter} | {tail.strip()}" if sep else f"{stmt} | {time_filter}"

    try:
        # opensearch-py 2.x 未暴露公开 perform_request；PPL 走 /_plugins/_ppl 直连。
        resp = httpx.post(
            f"http://{OPENSEARCH_HOST}:{OPENSEARCH_PORT}/_plugins/_ppl",
            json={"query": stmt},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return localize_tenant_columns({
            "schema": data.get("schema"),
            "datarows": data.get("datarows"),
            "total": data.get("total"),
            "tenant": tenant,
            "index": index_for(tenant),
        }, directory)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"ppl error: {e}")


@app.post("/histogram")
def histogram(req: HistReq, identity: Dict[str, Any] = Depends(current_identity)):
    """日志分布直方图（date_histogram）+ 级别分布（terms）。"""
    tenant = resolve_tenant(identity, req.tenant)
    idx = index_for(tenant)
    must = build_must(req.timeRange, req.filters, tenant)
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
            # 日志类型分布：检索页「按类型筛选」的候选值来源，也让占比一目了然
            "by_log_class": {"terms": {"field": "log_class", "size": 10}},
            "by_source_type": {"terms": {"field": "source_type", "size": 20}},
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
            "by_log_class": [
                {"logClass": b["key"], "count": b["doc_count"]}
                for b in aggs.get("by_log_class", {}).get("buckets", [])
            ],
            "by_source_type": [
                {"sourceType": b["key"], "count": b["doc_count"]}
                for b in aggs.get("by_source_type", {}).get("buckets", [])
            ],
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch error: {e}")


@app.post("/context")
def context(req: ContextReq, identity: Dict[str, Any] = Depends(current_identity)):
    """锚点日志的上下文：返回其前 before 条、后 after 条相邻日志（同租户、可选同来源）。

    用于详情抽屉「原始日志」上/下展示上下文日志；before 按时间倒序取后逆序为时间正序，
    after 按时间正序，二者均按时间升序呈现，符合阅读时序。
    """
    tenant = resolve_tenant(identity, req.tenant)
    idx = index_for(tenant)
    src = LOG_FIELDS
    code2name = {str(t.get("code")): str(t.get("name")) for t in tenant_directory()}
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
        hits = [normalize_hit(h, code2name) for h in resp["hits"]["hits"]]
        if direction == "before":
            hits = list(reversed(hits))  # 逆序为时间正序（早→晚）
        return hits

    try:
        return {"before": fetch("before", req.before), "after": fetch("after", req.after)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch error: {e}")


# saved-views：本切片为 stub（PG 落地留待统一数据模型阶段）
# 落地时必须按 (租户, 用户) 维度存储——视图含查询条件，跨租户可见等同配置泄露。
@app.get("/saved-views")
def list_saved_views(identity: Dict[str, Any] = Depends(current_identity)):
    return {"items": [], "note": "stub: PG 持久化待阶段4统一实现"}


@app.post("/saved-views")
def save_view(
    payload: Dict[str, Any],
    identity: Dict[str, Any] = Depends(current_identity),
):
    return {"status": "received", "note": "stub: 未落库", "payload": payload}
