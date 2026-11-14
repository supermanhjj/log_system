"""alarm-service —— 告警中心后端（告警规则 + 告警事件）。

对应原型 ALM-0401 / ALM-0402。设计原则与 dashboard-service / search-service 一致：
- 事件主表 / 规则主表 / 状态流转落 PostgreSQL（alarm-service 是平台中唯一约定使用 PG 的服务）；
- 告警事件的「实时判定」完全由本服务对 OpenSearch 日志索引（logs-*）按规则实时聚合得出，
  不依赖任何前端硬编码或样例数据；
- 规则关闭 / 维护中等人工状态通过 Postgres 状态覆盖表（alarm_event_states）持久化，
  与 OpenSearch 中的真实日志解耦，二者在列表接口合并返回。

真实日志字段（见 tools/log-generator 与 Fluent Bit 解析）：
  @timestamp / level(ERROR|WARN|INFO) / source(业务服务) / host(主机) /
  traceId / message / status(HTTP 码) / tenant_id / raw(原始整行)

租户隔离：tenant → logs-<tenant>（all → logs-*），与 search / dashboard 一致。
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from opensearchpy import OpenSearch
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 连接（惰性，模块导入不触网；首次请求才建连）
# ---------------------------------------------------------------------------
OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "opensearch")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))
PG_DSN = os.getenv(
    "ALARM_PG_DSN",
    f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
    f"user={os.getenv('POSTGRES_USER', 'loguser')} "
    f"password={os.getenv('POSTGRES_PASSWORD', 'logpass')} "
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')}",
)

os_client = OpenSearch(
    hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
    http_auth=None,
    use_ssl=False,
    verify_certs=False,
)

# 真实业务数据源（与 log-generator SOURCES 对齐，作为「关联数据源」下拉候选）
REAL_SOURCES = [
    "order-service",
    "user-service",
    "payment-gateway",
    "inventory-svc",
    "api-gateway",
]
NOTIFY_LABELS = {"wecom": "企业微信", "sms": "短信", "email": "邮件"}
LEVELS = ["严重", "警告", "提示"]

app = FastAPI(title="alarm-service", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# 租户 → 索引 + 时区（与 search/dashboard 完全一致）
# ---------------------------------------------------------------------------
INDEX_BY_TENANT = {"default": "logs-default", "acme": "logs-acme", "gp": "logs-gp"}
CST = timezone(timedelta(hours=8))


def index_for(tenant: Optional[str]) -> str:
    if tenant in ("all", "*", "", None):
        return "logs-*"
    return INDEX_BY_TENANT.get(tenant, f"logs-{tenant}")


def to_local(ts):
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


def to_local_ms(ts) -> Optional[int]:
    """返回该本地墙钟时间的 epoch 毫秒，供前端计算「持续时长」。"""
    if not ts:
        return None
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        s = str(ts).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    dt = dt.astimezone(CST)
    return int(dt.timestamp() * 1000)


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Postgres 连接 / 建表 / 种子
# ---------------------------------------------------------------------------
_pg_conn = None


def pg_conn():
    global _pg_conn
    if _pg_conn is None or getattr(_pg_conn, "closed", 1):
        _pg_conn = psycopg2.connect(PG_DSN, connect_timeout=5)
        _pg_conn.autocommit = True
    return _pg_conn


def pg_exec(sql: str, params: tuple = ()):
    conn = pg_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)


def pg_query(sql: str, params: tuple = ()):
    conn = pg_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def init_db() -> None:
    """建表 + 首次种子规则（仅当 rules 为空时）。失败不阻塞服务启动。"""
    try:
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS alarm_rules (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                match_source TEXT,
                match_level TEXT,
                match_status_gte INT,
                keyword TEXT,
                window_minutes INT NOT NULL DEFAULT 5,
                threshold INT NOT NULL DEFAULT 1,
                level TEXT NOT NULL DEFAULT '警告',
                notify TEXT NOT NULL DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                owner TEXT NOT NULL DEFAULT 'system',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS alarm_event_states (
                tenant TEXT NOT NULL DEFAULT 'default',
                event_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '告警中',
                handler TEXT,
                reason TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (tenant, event_id)
            )
            """
        )
        existing = pg_query("SELECT COUNT(*) AS c FROM alarm_rules")
        if not existing or existing[0]["c"] == 0:
            _seed_rules()
    except Exception as e:  # noqa: BLE001
        # 开发期 PG 暂不可用时降级：服务仍可启动，规则/事件接口会返回明确错误
        print(f"[alarm-service] init_db skipped: {e}", flush=True)


def _seed_rules() -> None:
    seeds = [
        dict(name="订单服务错误突增", match_source="order-service", match_level="ERROR",
             match_status_gte=None, keyword=None, window_minutes=5, threshold=1,
             level="严重", notify="wecom,sms", enabled=True, owner="黄金金"),
        dict(name="支付网关 5xx 异常", match_source="payment-gateway", match_level="ERROR",
             match_status_gte=500, keyword=None, window_minutes=5, threshold=1,
             level="严重", notify="wecom", enabled=True, owner="黄金金"),
        dict(name="网关 5xx 比例超阈值", match_source="api-gateway", match_level="ERROR",
             match_status_gte=500, keyword=None, window_minutes=10, threshold=3,
             level="警告", notify="wecom", enabled=True, owner="系统"),
        dict(name="库存服务慢查询告警", match_source="inventory-svc", match_level="WARN",
             match_status_gte=None, keyword="slow query", window_minutes=10, threshold=1,
             level="警告", notify="email", enabled=True, owner="黄金金"),
        dict(name="用户服务告警", match_source="user-service", match_level="WARN",
             match_status_gte=None, keyword=None, window_minutes=15, threshold=1,
             level="提示", notify="wecom", enabled=True, owner="系统"),
        dict(name="全量 ERROR 汇聚告警", match_source=None, match_level="ERROR",
             match_status_gte=None, keyword=None, window_minutes=10, threshold=5,
             level="严重", notify="wecom,sms", enabled=False, owner="系统"),
    ]
    for s in seeds:
        pg_exec(
            """
            INSERT INTO alarm_rules
                (name, match_source, match_level, match_status_gte, keyword,
                 window_minutes, threshold, level, notify, enabled, owner)
            VALUES (%(name)s,%(match_source)s,%(match_level)s,%(match_status_gte)s,%(keyword)s,
                    %(window_minutes)s,%(threshold)s,%(level)s,%(notify)s,%(enabled)s,%(owner)s)
            """,
            s,
        )


# ---------------------------------------------------------------------------
# 规则模型
# ---------------------------------------------------------------------------
class RuleIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    match_source: Optional[str] = None          # 关联数据源；空 = 全部
    match_level: Optional[str] = None           # ERROR / WARN / INFO / 空=全部
    match_status_gte: Optional[int] = None      # HTTP 状态码 ≥
    keyword: Optional[str] = None               # 关键字（message / raw 命中）
    window_minutes: int = 5
    threshold: int = 1
    level: str = "警告"                          # 告警严重级别：严重/警告/提示
    notify: List[str] = []                      # wecom / sms / email
    enabled: bool = True
    owner: str = "system"


def row_to_rule(r: Dict[str, Any]) -> Dict[str, Any]:
    notify = [x for x in (r.get("notify") or "").split(",") if x]
    return {
        "id": r["id"],
        "name": r["name"],
        "matchSource": r.get("match_source"),
        "matchLevel": r.get("match_level"),
        "matchStatusGte": r.get("match_status_gte"),
        "keyword": r.get("keyword"),
        "windowMinutes": r["window_minutes"],
        "threshold": r["threshold"],
        "level": r["level"],
        "notify": notify,
        "enabled": r["enabled"],
        "owner": r.get("owner"),
        "createdAt": to_local(r.get("created_at")),
        "updatedAt": to_local(r.get("updated_at")),
    }


# ---------------------------------------------------------------------------
# OpenSearch 取数原语（事件实时判定）
# ---------------------------------------------------------------------------
def _must_for_rule(rule: Dict[str, Any], gte: int, lte: int) -> List[Dict[str, Any]]:
    must = [{"range": {"@timestamp": {"gte": gte, "lte": lte}}}]
    if rule.get("match_level"):
        must.append({"term": {"level": rule["match_level"]}})
    if rule.get("match_source"):
        must.append({"term": {"source": rule["match_source"]}})
    if rule.get("match_status_gte") is not None:
        must.append({"range": {"status": {"gte": rule["match_status_gte"]}}})
    if rule.get("keyword"):
        must.append({
            "multi_match": {
                "query": rule["keyword"],
                "fields": ["message", "raw"],
            }
        })
    return must


def evaluate_rule(rule: Dict[str, Any], idx: str, now: int) -> Optional[Dict[str, Any]]:
    """对单条规则做实时判定；不触发则返回 None，触发则返回事件雏形（不含人工状态）。"""
    gte = now - rule["window_minutes"] * 60 * 1000
    must = _must_for_rule(rule, gte, now)
    query = {"bool": {"must": must}}

    # 命中数
    try:
        cnt = int(os_client.count(index=idx, body={"query": query}).get("count", 0))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch count error: {e}")

    if cnt < rule["threshold"]:
        return None

    # 首批命中时间 + 样例 raw + 来源/主机分布
    body = {
        "size": 0,
        "query": query,
        "aggs": {
            "first": {"min": {"field": "@timestamp"}},
            "sources": {"terms": {"field": "source", "size": 5}},
            "hosts": {"terms": {"field": "host", "size": 5}},
        },
    }
    # 样例：最近一条原始日志
    sample_body = {
        "size": 1,
        "query": query,
        "sort": [{"@timestamp": {"order": "desc"}}],
        "_source": ["@timestamp", "level", "source", "host", "traceId", "message", "status", "raw"],
    }
    try:
        agg = os_client.search(index=idx, body=body)
        sample = os_client.search(index=idx, body=sample_body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch search error: {e}")

    aggs = agg.get("aggregations", {})
    first_ts = None
    if aggs.get("first", {}).get("value") is not None:
        first_ts = datetime.fromtimestamp(aggs["first"]["value"] / 1000, tz=timezone.utc)
    src = sample["hits"]["hits"][0]["_source"] if sample["hits"]["hits"] else {}
    return {
        "ruleId": rule["id"],
        "name": rule["name"],
        "level": rule["level"],
        "source": rule.get("match_source") or "全部数据源",
        "windowMinutes": rule["window_minutes"],
        "threshold": rule["threshold"],
        # 规则判定条件（供前端「查看关联日志」组织等价 PPL，确保关联日志=触发集合）
        "matchLevel": rule.get("match_level"),
        "matchStatusGte": rule.get("match_status_gte"),
        "keyword": rule.get("keyword"),
        "count": cnt,
        "firstSeen": to_local(first_ts),
        "firstSeenTs": to_local_ms(first_ts),
        "durationSec": max(0, int((now - (to_local_ms(first_ts) or now)) / 1000)),
        "matchedSources": [b["key"] for b in aggs.get("sources", {}).get("buckets", [])],
        "matchedHosts": [b["key"] for b in aggs.get("hosts", {}).get("buckets", [])],
        "sample": {
            "time": to_local(src.get("@timestamp")),
            "level": src.get("level"),
            "source": src.get("source"),
            "host": src.get("host"),
            "traceId": src.get("traceId"),
            "status": src.get("status"),
            "message": src.get("message"),
            "raw": src.get("raw") or "",
        },
    }


# 事件判定结果按租户缓存（避免每次刷新都打满 OpenSearch）
_EVAL_CACHE: Dict[str, Dict[str, Any]] = {}
EVAL_CACHE_TTL = 15 * 1000


def eval_cache_get(tenant: str):
    it = _EVAL_CACHE.get(tenant)
    if it and now_ms() - it["ts"] < EVAL_CACHE_TTL:
        return it["data"]
    return None


def eval_cache_put(tenant: str, data):
    _EVAL_CACHE[tenant] = {"ts": now_ms(), "data": data}


def build_events(tenant: str) -> List[Dict[str, Any]]:
    """对所有 enabled 规则实时判定，合并 Postgres 人工状态覆盖。"""
    cached = eval_cache_get(tenant)
    if cached is not None:
        return cached
    idx = index_for(tenant)
    now = now_ms()
    rules = pg_query("SELECT * FROM alarm_rules WHERE enabled = TRUE ORDER BY id")
    events: List[Dict[str, Any]] = []
    for r in rules:
        try:
            ev = evaluate_rule(r, idx, now)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[alarm-service] rule {r.get('id')} eval error: {e}", flush=True)
            continue
        if not ev:
            continue
        event_id = f"rule:{r['id']}"
        ev["id"] = event_id
        # 人工状态覆盖
        st = pg_query(
            "SELECT status, handler, reason FROM alarm_event_states WHERE tenant=%s AND event_id=%s",
            (tenant, event_id),
        )
        if st:
            ev["status"] = st[0]["status"]
            ev["handler"] = st[0]["handler"]
            ev["closeReason"] = st[0]["reason"]
        else:
            ev["status"] = "告警中"
            ev["handler"] = None
            ev["closeReason"] = None
        events.append(ev)
    events.sort(key=lambda e: (e["firstSeenTs"] or 0), reverse=True)
    eval_cache_put(tenant, events)
    return events


def set_event_state(tenant: str, event_id: str, status: str, handler=None, reason=None):
    pg_exec(
        """
        INSERT INTO alarm_event_states (tenant, event_id, status, handler, reason, updated_at)
        VALUES (%s,%s,%s,%s,%s, now())
        ON CONFLICT (tenant, event_id) DO UPDATE
            SET status=EXCLUDED.status, handler=EXCLUDED.handler,
                reason=EXCLUDED.reason, updated_at=now()
        """,
        (tenant, event_id, status, handler, reason),
    )
    # 状态变更立即可见：清缓存
    _EVAL_CACHE.pop(tenant, None)


# ---------------------------------------------------------------------------
# 关联日志检索语句（自适应生成）
#
# 业务日志字段形态各异、告警规则因人而异，因此不能用固定模板拼 PPL。这里在运行时
# 结合两方面的真实信息做分析：
#   1) 规则完整配置（PG alarm_rules）—— 决定「有哪些条件、窗口多长」；
#   2) 该租户索引的真实 mapping —— 决定「字段叫什么、是什么类型、文本列有哪些」。
# 只对「规则确实配置了」且「索引确实存在该字段」的条件生成子句，字段名与输出列均由
# mapping 推导；缺失字段自动跳过并降级（记入 skipped），保证任何日志形态都能产出可执行的 PPL。
# ---------------------------------------------------------------------------
NUMERIC_TYPES = {"integer", "long", "short", "byte", "float", "double", "scaled_float", "half_float"}
# 正文文本列（关键字匹配用，等价后端 multi_match(message, raw) 的语义）
CONTENT_TYPES = {"text", "match_only_text", "wildcard"}
# 维度/枚举列（仅当日志无 text 正文列时，才降级用于关键字匹配）
DIM_TYPES = {"keyword"}
TEXT_TYPES = CONTENT_TYPES | DIM_TYPES          # 字段角色候选（level/source 常为 keyword）
SCALAR_TYPES = NUMERIC_TYPES | DIM_TYPES | {"boolean", "ip", "date"}

# 字段角色候选名：按优先级探测，避免把字段名写死（不同业务日志命名不同）
ROLE_CANDIDATES = {
    "time": ["@timestamp", "timestamp", "time", "ts", "event_time", "log_time"],
    "level": ["level", "severity", "log_level", "loglevel", "logLevel"],
    "source": ["source", "service", "service_name", "app", "app_name", "logger", "component"],
    "status": ["status", "status_code", "http_status", "response_code", "code"],
}


def _index_fields(idx: str) -> Dict[str, str]:
    """探测索引真实字段 → {字段名: 类型}；logs-* 通配时合并所有索引的字段。"""
    try:
        raw = os_client.indices.get_mapping(index=idx)
    except Exception as e:  # noqa: BLE001
        print(f"[alarm-service] get_mapping({idx}) failed: {e}", flush=True)
        return {}
    fields: Dict[str, str] = {}
    for _name, body in (raw or {}).items():
        props = ((body.get("mappings") or {}).get("properties") or {})
        for f, meta in props.items():
            t = (meta or {}).get("type")
            if t:
                fields.setdefault(f, t)
            # text 的 keyword 子字段等，作为可选匹配列登记
            for sub, smeta in ((meta or {}).get("fields") or {}).items():
                st = (smeta or {}).get("type")
                if st:
                    fields.setdefault(f"{f}.{sub}", st)
    return fields


def _pick_role(fields: Dict[str, str], role: str, types=None) -> Optional[str]:
    for c in ROLE_CANDIDATES[role]:
        if c in fields and (types is None or fields[c] in types):
            return c
    # 候选名都未命中时，按「类型 + 语义」回退到任意同类型字段，避免把字段名写死：
    #   time   → 任意 date 字段；status → 唯一数值字段（如自定义命名 http_code）
    if role == "time":
        return next((f for f, t in fields.items() if t == "date" and "." not in f), None)
    if role == "status":
        nums = [f for f, t in fields.items() if t in NUMERIC_TYPES and "." not in f]
        return nums[0] if len(nums) == 1 else None   # 多个数值字段无法判定时放弃该条件
    return None


def _esc(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace("'", "\\'")


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_related_pql(rule: Dict[str, Any], ev: Dict[str, Any], tenant: str) -> Dict[str, Any]:
    idx = index_for(tenant)
    fields = _index_fields(idx)
    applied: List[str] = []
    skipped: List[str] = []
    conds: List[str] = []

    time_f = _pick_role(fields, "time", {"date"})
    level_f = _pick_role(fields, "level", TEXT_TYPES)
    source_f = _pick_role(fields, "source", TEXT_TYPES)
    status_f = _pick_role(fields, "status", NUMERIC_TYPES)
    # 关键字只匹配「正文文本列」，与后端 multi_match(message, raw) 语义一致；
    # 若该日志没有 text 正文列（字段形态不同），才降级到维度列，保证语句仍可执行。
    text_fs = [f for f, t in fields.items() if t in CONTENT_TYPES and "." not in f]
    if not text_fs:
        text_fs = [f for f, t in fields.items() if t in DIM_TYPES and "." not in f]

    def add(cond, label, field):
        if field:
            conds.append(cond)
            applied.append(label)
        else:
            skipped.append(label)

    # 时间窗口：与后端判定口径（now - window_minutes ~ now）对齐，两端各留 1 分钟余量，
    # 并把事件首次发生时间纳入下界，确保覆盖触发该告警的那批日志。
    window_ms = int(rule.get("window_minutes") or 5) * 60 * 1000
    now = now_ms()
    to_ms = now + 60_000
    from_ms = min(ev.get("firstSeenTs") or (now - window_ms), now - window_ms) - 60_000
    if time_f:
        conds.append(f"{time_f} >= timestamp('{_iso_utc(from_ms)}')")
        conds.append(f"{time_f} <= timestamp('{_iso_utc(to_ms)}')")
        applied.append(f"时间窗口({time_f})")
    else:
        skipped.append("时间窗口(索引无 date 字段)")

    # 结构化条件：仅当规则配置了该条件才尝试，字段不存在/类型不匹配则跳过
    if rule.get("match_source"):
        add(f"{source_f}='{_esc(rule['match_source'])}'", f"来源({source_f or 'source'})", source_f)
    if rule.get("match_level"):
        add(f"{level_f}='{_esc(rule['match_level'])}'", f"级别({level_f or 'level'})", level_f)
    if rule.get("match_status_gte") is not None:
        add(f"{status_f} >= {int(rule['match_status_gte'])}", f"状态码≥{rule['match_status_gte']}({status_f or 'status'})", status_f)
    if rule.get("keyword"):
        kw = rule["keyword"]
        if text_fs:
            likes = [f"like({f}, '%{_esc(kw)}%')" for f in text_fs]
            conds.append(likes[0] if len(likes) == 1 else "(" + " or ".join(likes) + ")")
            applied.append(f"关键字({','.join(text_fs)})")
        else:
            skipped.append("关键字(索引无文本字段)")

    # 输出列：完全由真实 mapping 推导（date 优先 → 标量 → 文本），排除超长 raw，最多 10 列
    cols: List[str] = []
    for pred in (
        lambda f, t: t == "date",
        lambda f, t: t in SCALAR_TYPES and t != "date",
        lambda f, t: t in TEXT_TYPES,
    ):
        for f, t in fields.items():
            if f == "raw" or "." in f or f in cols:
                continue
            if pred(f, t):
                cols.append(f)
    cols = cols[:10]
    if not cols:
        cols = [f for f in fields if "." not in f][:10]

    stmt = f"source={idx}"
    if conds:
        stmt += " | where " + " and ".join(conds)
    if cols:
        stmt += " | fields " + ", ".join(cols)
    stmt += " | head 100"

    return {
        "pql": stmt,
        "from": from_ms,
        "to": to_ms,
        "index": idx,
        "fields": fields,
        "columns": cols,
        "applied": applied,
        "skipped": skipped,
        "note": ("已按规则条件与索引真实字段生成" if not skipped
                 else "部分条件因索引字段缺失/类型不匹配已跳过：" + "；".join(skipped)),
    }


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "alarm-service"}


@app.get("/")
def root():
    return {
        "service": "alarm-service",
        "docs": "/docs",
        "endpoints": [
            "/rules (规则 CRUD)",
            "/events (规则实时判定的告警事件)",
            "/events/close (关闭/批量关闭)",
        ],
    }


@app.on_event("startup")
def _startup():
    # 延后到首次请求再建表，避免 PG 尚未就绪时阻断启动
    init_db()


# ------------------------------- 告警规则 -------------------------------
@app.get("/rules")
def list_rules(
    keyword: str = Query("", description="规则名称模糊匹配"),
    level: str = Query("", description="告警级别：严重/警告/提示"),
    enabled: str = Query("", description="启用状态：true/false/空=全部"),
    owner: str = Query("", description="更新人"),
):
    sql = "SELECT * FROM alarm_rules WHERE 1=1"
    params: List[Any] = []
    if keyword:
        sql += " AND name ILIKE %s"
        params.append(f"%{keyword}%")
    if level:
        sql += " AND level=%s"
        params.append(level)
    if enabled in ("true", "false"):
        sql += " AND enabled=%s"
        params.append(enabled == "true")
    if owner:
        sql += " AND owner ILIKE %s"
        params.append(f"%{owner}%")
    sql += " ORDER BY id"
    rows = pg_query(sql, tuple(params))
    return {"items": [row_to_rule(r) for r in rows], "total": len(rows)}


@app.post("/rules")
def create_rule(payload: RuleIn):
    row = pg_query(
        """
        INSERT INTO alarm_rules
            (name, match_source, match_level, match_status_gte, keyword,
             window_minutes, threshold, level, notify, enabled, owner)
        VALUES (%(name)s,%(match_source)s,%(match_level)s,%(match_status_gte)s,%(keyword)s,
                %(window_minutes)s,%(threshold)s,%(level)s,%(notify)s,%(enabled)s,%(owner)s)
        RETURNING id
        """,
        {
            "name": payload.name,
            "match_source": payload.match_source or None,
            "match_level": payload.match_level or None,
            "match_status_gte": payload.match_status_gte,
            "keyword": payload.keyword or None,
            "window_minutes": payload.window_minutes,
            "threshold": payload.threshold,
            "level": payload.level,
            "notify": ",".join(payload.notify),
            "enabled": payload.enabled,
            "owner": payload.owner,
        },
    )
    _EVAL_CACHE.clear()
    return {"id": row[0]["id"], "ok": True}


@app.get("/rules/{rule_id}")
def get_rule(rule_id: int):
    rows = pg_query("SELECT * FROM alarm_rules WHERE id=%s", (rule_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="规则不存在")
    return row_to_rule(rows[0])


@app.put("/rules/{rule_id}")
def update_rule(rule_id: int, payload: RuleIn):
    rows = pg_query("SELECT id FROM alarm_rules WHERE id=%s", (rule_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="规则不存在")
    pg_exec(
        """
        UPDATE alarm_rules SET
            name=%(name)s, match_source=%(match_source)s, match_level=%(match_level)s,
            match_status_gte=%(match_status_gte)s, keyword=%(keyword)s,
            window_minutes=%(window_minutes)s, threshold=%(threshold)s, level=%(level)s,
            notify=%(notify)s, enabled=%(enabled)s, owner=%(owner)s, updated_at=now()
        WHERE id=%(id)s
        """,
        {
            "id": rule_id,
            "name": payload.name,
            "match_source": payload.match_source or None,
            "match_level": payload.match_level or None,
            "match_status_gte": payload.match_status_gte,
            "keyword": payload.keyword or None,
            "window_minutes": payload.window_minutes,
            "threshold": payload.threshold,
            "level": payload.level,
            "notify": ",".join(payload.notify),
            "enabled": payload.enabled,
            "owner": payload.owner,
        },
    )
    _EVAL_CACHE.clear()
    return {"id": rule_id, "ok": True}


@app.delete("/rules/{rule_id}")
def delete_rule(rule_id: int):
    pg_exec("DELETE FROM alarm_rules WHERE id=%s", (rule_id,))
    _EVAL_CACHE.clear()
    return {"id": rule_id, "ok": True}


@app.post("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int, payload: Dict[str, bool] = {}):
    enabled = bool(payload.get("enabled", True))
    pg_exec("UPDATE alarm_rules SET enabled=%s, updated_at=now() WHERE id=%s", (enabled, rule_id))
    _EVAL_CACHE.clear()
    return {"id": rule_id, "enabled": enabled, "ok": True}


# ------------------------------- 告警事件 -------------------------------
@app.get("/events")
def list_events(
    tenant: str = Query("default"),
    keyword: str = Query("", description="规则名称 / 来源关键字"),
    level: str = Query("", description="告警级别：严重/警告/提示"),
    status: str = Query("", description="处理状态：告警中/已恢复/维护中"),
    source: str = Query("", description="关联数据源"),
    date: str = Query("", description="首次发生日期 YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
):
    events = build_events(tenant)
    # 事件级筛选（在真实判定结果之上）
    kw = keyword.strip().lower()
    if kw:
        events = [e for e in events if kw in (e["name"] or "").lower() or kw in (e["source"] or "").lower()]
    if level:
        events = [e for e in events if e["level"] == level]
    if status:
        events = [e for e in events if e["status"] == status]
    if source:
        events = [e for e in events if source in (e["source"] or "")]
    if date:
        events = [e for e in events if (e["firstSeen"] or "").startswith(date)]
    total = len(events)
    start = (page - 1) * pageSize
    return {
        "total": total,
        "page": page,
        "pageSize": pageSize,
        "items": events[start : start + pageSize],
    }


@app.get("/events/{event_id}/related-query")
def event_related_query(event_id: str, tenant: str = Query("default")):
    """分析「当前规则配置 + 索引真实字段」，生成可直接在日志检索页执行的 PPL。"""
    events = build_events(tenant)
    ev = next((e for e in events if e["id"] == event_id), None)
    if not ev:
        raise HTTPException(status_code=404, detail="事件不存在或当前未触发")
    # ruleId 形如 "rule:3"
    rid = str(ev.get("ruleId") or "").split(":")[-1]
    rows = pg_query("SELECT * FROM alarm_rules WHERE id=%s", (int(rid),)) if rid.isdigit() else []
    rule = dict(rows[0]) if rows else {
        # 规则已被删除时的降级：仅使用事件中残留的条件信息
        "match_source": None if ev.get("source") == "全部数据源" else ev.get("source"),
        "match_level": ev.get("matchLevel") or ev.get("sample", {}).get("level"),
        "match_status_gte": ev.get("matchStatusGte"),
        "keyword": ev.get("keyword"),
        "window_minutes": ev.get("windowMinutes") or 5,
    }
    return build_related_pql(rule, ev, tenant)


@app.get("/events/{event_id}")
def event_detail(event_id: str, tenant: str = Query("default")):
    events = build_events(tenant)
    for e in events:
        if e["id"] == event_id:
            return e
    raise HTTPException(status_code=404, detail="事件不存在或当前未触发")


class CloseReq(BaseModel):
    ids: List[str] = []
    reason: Optional[str] = None
    handler: Optional[str] = None


@app.post("/events/close")
def close_events(req: CloseReq, tenant: str = Query("default")):
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    skipped = 0
    closed = 0
    for eid in req.ids:
        # 已恢复的事件直接跳过（与高保真原型一致）
        st = pg_query(
            "SELECT status FROM alarm_event_states WHERE tenant=%s AND event_id=%s",
            (tenant, eid),
        )
        if st and st[0]["status"] == "已恢复":
            skipped += 1
            continue
        set_event_state(tenant, eid, "已恢复", req.handler, req.reason)
        closed += 1
    return {"ok": True, "closed": closed, "skipped": skipped}


@app.post("/events/{event_id}/close")
def close_one(event_id: str, req: Dict[str, Any] = {}, tenant: str = Query("default")):
    set_event_state(tenant, event_id, "已恢复", req.get("handler"), req.get("reason"))
    return {"ok": True, "eventId": event_id, "status": "已恢复"}


@app.post("/events/{event_id}/maintain")
def maintain_one(event_id: str, req: Dict[str, Any] = {}, tenant: str = Query("default")):
    set_event_state(tenant, event_id, "维护中", req.get("handler"), req.get("reason"))
    return {"ok": True, "eventId": event_id, "status": "维护中"}
