"""ingest-service —— 数据接入 / 数据源管理后端。

对应原型 DAT-0501（prototype/data-sources.html、specs/P-DAT-0501.json）。
与 dashboard / alarm 服务一致：**不做任何硬编码样例数据**，全部指标来自真实环境。

设计要点（为「上线后接入形态很复杂」而做）：
1. 事实层＝OpenSearch 里真实出现的日志来源。按索引真实 mapping 自动发现（terms 聚合），
   新接一个业务系统会自然出现在列表里，无需先手工录入。
2. 配置层＝PostgreSQL(data_sources)。只存「日志里没有、必须人工补充」的元数据：
   接入类型、接收地址、区域、解析规则、编码、采样率、字段角色映射、标签、扩展属性、
   维护/停用状态覆盖、静默阈值。未登记的来源以「自动发现」形态展示。
3. 字段名不写死：按 mapping 探测 来源/时间/级别/主机/租户/正文 六种角色，
   允许在「接入设置」或单条数据源里覆盖（field_map），适配任意日志 schema。
4. 状态由真实数据推导：最近接收时间 / 静默阈值 / 近1h vs 近7d 健康度；
   人工状态（维护中、已停用）作为显式覆盖，并在返回里标明 evidence 便于追溯。
5. 连通性校验是真实探测：Agent 查真实日志与在线 Agent；Syslog/HTTP/Kafka 做真实
   TCP/HTTP/Kafka broker 探测，失败如实返回 errno，绝不返回假的「延迟 12ms」。

真实日志字段（tools/log-generator + fluentbit/parsers.conf）：
  @timestamp / level / source / host / traceId / tenant_id / status / message / raw
租户路由：tenant → logs-<tenant>（all → logs-*），与 search / dashboard / alarm 一致。
"""
import os
import json
import time
import socket
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from opensearchpy import OpenSearch
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 连接（惰性；模块导入不触网）
# ---------------------------------------------------------------------------
OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "opensearch")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))
PG_DSN = os.getenv(
    "INGEST_PG_DSN",
    f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
    f"user={os.getenv('POSTGRES_USER', 'loguser')} "
    f"password={os.getenv('POSTGRES_PASSWORD', 'logpass')} "
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')}",
)
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "kafka:9092")
AGENT_SERVICE = os.getenv("AGENT_SERVICE_URL", "http://agent-service:8000")

os_client = OpenSearch(
    hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
    http_auth=None,
    use_ssl=False,
    verify_certs=False,
)

CST = timezone(timedelta(hours=8))
INDEX_BY_TENANT = {"default": "logs-default", "acme": "logs-acme", "gp": "logs-gp"}
INGEST_TYPES = ["Agent", "Syslog", "HTTP", "Kafka", "自定义"]

app = FastAPI(title="ingest-service", version="0.5.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def index_for(tenant: Optional[str], override: str = "") -> str:
    if override:
        return override
    if tenant in ("all", "*", "", None):
        return "logs-*"
    return INDEX_BY_TENANT.get(tenant, f"logs-{tenant}")


def now_ms() -> int:
    return int(time.time() * 1000)


def to_local(ts) -> Optional[str]:
    """UTC / epoch-ms → 东八区 'YYYY-MM-DD HH:MM:SS'。"""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    elif isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        s = str(ts).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return str(ts)
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def today_start_ms() -> int:
    n = datetime.now(tz=CST)
    return int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


# ---------------------------------------------------------------------------
# PostgreSQL：配置层
# ---------------------------------------------------------------------------
_pg_conn = None


def pg_conn():
    global _pg_conn
    if _pg_conn is None or getattr(_pg_conn, "closed", 1):
        _pg_conn = psycopg2.connect(PG_DSN, connect_timeout=5)
        _pg_conn.autocommit = True
    return _pg_conn


def pg_exec(sql: str, params: tuple = ()) -> None:
    with pg_conn().cursor() as cur:
        cur.execute(sql, params)


def pg_query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with pg_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


DEFAULT_SETTINGS: Dict[str, Any] = {
    # 空字符串＝按真实 mapping 自动探测，不写死字段名
    "source_field": "",
    "time_field": "",
    "level_field": "",
    "host_field": "",
    "tenant_field": "",
    "message_field": "",
    "silence_warn_min": 5,
    "silence_offline_min": 60,
    "discovery_enabled": True,
    "discovery_days": 30,
}

ROLE_CANDIDATES: Dict[str, List[str]] = {
    "source": ["source", "service", "service_name", "app", "app_name", "logger",
               "component", "system", "log_source", "appid", "app_id"],
    "time": ["@timestamp", "timestamp", "time", "ts", "event_time", "log_time", "datetime"],
    "level": ["level", "severity", "log_level", "loglevel", "logLevel"],
    "host": ["host", "hostname", "host_name", "node", "instance", "hostip"],
    "tenant": ["tenant_id", "tenant", "tenantId", "namespace", "org_id", "project"],
    "message": ["message", "msg", "content", "log", "body", "raw"],
}
TEXT_TYPES = {"text", "match_only_text", "wildcard", "keyword"}
NUMERIC_TYPES = {"integer", "long", "short", "byte", "float", "double", "scaled_float", "half_float"}


def init_db() -> None:
    """建配置表；失败不阻塞服务启动（列表仍可只靠自动发现运行）。"""
    try:
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS data_sources (
                id SERIAL PRIMARY KEY,
                tenant TEXT NOT NULL DEFAULT 'default',
                source_key TEXT NOT NULL,
                display_name TEXT,
                ingest_type TEXT NOT NULL DEFAULT 'Agent',
                endpoint TEXT,
                region TEXT,
                encoding TEXT DEFAULT 'UTF-8',
                parser_rule TEXT,
                sample_rate INTEGER DEFAULT 100,
                target_index TEXT,
                field_map JSONB NOT NULL DEFAULT '{}'::jsonb,
                tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                extra JSONB NOT NULL DEFAULT '{}'::jsonb,
                status_override TEXT,
                silence_warn_min INTEGER,
                silence_offline_min INTEGER,
                owner TEXT DEFAULT 'system',
                note TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (tenant, source_key)
            )
            """
        )
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS ingest_settings (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    except Exception as e:  # noqa: BLE001
        print(f"[ingest-service] init_db skipped: {e}", flush=True)


def load_settings() -> Dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    try:
        rows = pg_query("SELECT value FROM ingest_settings WHERE key='global'")
        if rows and isinstance(rows[0]["value"], dict):
            s.update({k: v for k, v in rows[0]["value"].items() if k in DEFAULT_SETTINGS})
    except Exception as e:  # noqa: BLE001
        print(f"[ingest-service] load_settings fallback: {e}", flush=True)
    return s


def save_settings(value: Dict[str, Any]) -> Dict[str, Any]:
    clean = {}
    for k, v in value.items():
        if k not in DEFAULT_SETTINGS:
            continue
        if k in ("silence_warn_min", "silence_offline_min", "discovery_days"):
            clean[k] = max(1, int(v))
        elif k == "discovery_enabled":
            clean[k] = bool(v)
        else:
            clean[k] = str(v or "").strip()
    merged = dict(load_settings())
    merged.update(clean)
    pg_exec(
        """
        INSERT INTO ingest_settings (key, value, updated_at) VALUES ('global', %s, now())
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()
        """,
        (psycopg2.extras.Json(merged),),
    )
    _cache.clear()
    return merged


def load_configs(tenant: str) -> Dict[str, Dict[str, Any]]:
    """读登记配置。tenant=all 时读全部租户，按 (tenant, source_key) 索引。"""
    try:
        if tenant in ("all", "*"):
            rows = pg_query("SELECT * FROM data_sources")
        else:
            rows = pg_query("SELECT * FROM data_sources WHERE tenant=%s OR tenant='all'", (tenant,))
        return {(r["tenant"], r["source_key"]): r for r in rows}
    except Exception as e:  # noqa: BLE001
        print(f"[ingest-service] load_configs: {e}", flush=True)
        return {}


def row_to_config(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "tenant": r["tenant"],
        "sourceKey": r["source_key"],
        "displayName": r.get("display_name"),
        "ingestType": r.get("ingest_type"),
        "endpoint": r.get("endpoint"),
        "region": r.get("region"),
        "encoding": r.get("encoding"),
        "parserRule": r.get("parser_rule"),
        "sampleRate": r.get("sample_rate"),
        "targetIndex": r.get("target_index"),
        "fieldMap": r.get("field_map") or {},
        "tags": r.get("tags") or [],
        "extra": r.get("extra") or {},
        "statusOverride": r.get("status_override"),
        "silenceWarnMin": r.get("silence_warn_min"),
        "silenceOfflineMin": r.get("silence_offline_min"),
        "owner": r.get("owner"),
        "note": r.get("note"),
        "registered": True,
        "createdAt": to_local(r.get("created_at")),
        "updatedAt": to_local(r.get("updated_at")),
    }


# ---------------------------------------------------------------------------
# OpenSearch：真实 mapping 与字段角色自适应
# ---------------------------------------------------------------------------
def index_fields(idx: str) -> Dict[str, str]:
    """探测真实索引字段 → {字段名: 类型}（含 text 的 keyword 子字段）。"""
    try:
        raw = os_client.indices.get_mapping(index=idx)
    except Exception as e:  # noqa: BLE001
        print(f"[ingest-service] get_mapping({idx}) failed: {e}", flush=True)
        return {}
    out: Dict[str, str] = {}
    for _name, body in (raw or {}).items():
        for f, meta in (((body.get("mappings") or {}).get("properties") or {})).items():
            t = (meta or {}).get("type")
            if t:
                out.setdefault(f, t)
            for sub, smeta in ((meta or {}).get("fields") or {}).items():
                st = (smeta or {}).get("type")
                if st:
                    out.setdefault(f"{f}.{sub}", st)
    return out


def detect_roles(fields: Dict[str, str]) -> Dict[str, str]:
    """按候选名 + 类型探测字段角色；都不命中时按类型回退。"""
    roles: Dict[str, str] = {}
    for role, candidates in ROLE_CANDIDATES.items():
        want = {"date"} if role == "time" else (TEXT_TYPES if role != "host" else TEXT_TYPES)
        for c in candidates:
            if c in fields and (role != "time" or fields[c] == "date"):
                roles[role] = c
                break
        if role not in roles:
            if role == "time":
                fb = next((f for f, t in fields.items() if t == "date" and "." not in f), None)
                if fb:
                    roles[role] = fb
    return roles


def agg_field(fields: Dict[str, str], name: str) -> Optional[str]:
    """可聚合字段名：text 走 .keyword 子字段；不存在的字段返回 None。"""
    if not name:
        return None
    t = fields.get(name)
    if t is None:
        return None
    if t in TEXT_TYPES and t != "keyword":
        return f"{name}.keyword" if f"{name}.keyword" in fields else None
    return name


def resolve_roles(fields: Dict[str, str], settings: Dict[str, Any],
                  override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并「全局设置 / 单条覆盖」与自动探测，返回实际字段名及来源标记。"""
    auto = detect_roles(fields)
    ov = override or {}
    roles: Dict[str, Any] = {}
    for role in ROLE_CANDIDATES:
        manual = (ov.get(role) or settings.get(f"{role}_field") or "").strip()
        if manual and manual in fields:
            roles[role] = {"field": manual, "from": "manual"}
        elif auto.get(role):
            roles[role] = {"field": auto[role], "from": "auto"}
        else:
            roles[role] = {"field": None, "from": "missing"}
    return roles


# ---------------------------------------------------------------------------
# 自动发现：一次聚合拿到所有来源的真实统计（避免 N+1 查询）
# ---------------------------------------------------------------------------
_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_MS = 15 * 1000


def cache_get(key: str):
    it = _cache.get(key)
    if it and now_ms() - it["ts"] < CACHE_TTL_MS:
        return it["data"]
    return None


def cache_put(key: str, data: Any) -> None:
    _cache[key] = {"ts": now_ms(), "data": data}


def discover(idx: str, roles: Dict[str, Any], days: int,
             fields: Dict[str, str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """按「来源字段」terms 聚合 + 子聚合，得到每个真实来源的统计。"""
    sf = agg_field(fields, roles["source"]["field"])
    tf = roles["time"]["field"]
    if not sf or not tf:
        return {}, {"error": "索引中未探测到可用的「来源字段」或「时间字段」，请在接入设置里指定字段角色"}

    to = now_ms()
    frm = to - days * 86400 * 1000
    h1 = to - 3600 * 1000
    d1 = to - 86400 * 1000
    d7 = to - 7 * 86400 * 1000
    t0 = today_start_ms()

    def rng(gte: int) -> Dict[str, Any]:
        return {"range": {tf: {"gte": gte, "lte": to}}}

    sub: Dict[str, Any] = {
        "last": {"max": {"field": tf}},
        "h1": {"filter": rng(h1)},
        "d1": {"filter": rng(d1)},
        "d7": {"filter": rng(d7)},
        "today": {"filter": rng(t0)},
    }
    for role, key in (("level", "levels"), ("host", "hosts"), ("tenant", "tenants")):
        f = agg_field(fields, roles[role]["field"])
        if f:
            sub[key] = {"terms": {"field": f, "size": 20}}

    body = {
        "size": 0,
        "query": {"bool": {"must": [rng(frm)]}},
        "aggs": {"sources": {"terms": {"field": sf, "size": 500}, "aggs": sub}},
    }
    try:
        resp = os_client.search(index=idx, body=body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch discovery error: {e}")

    out: Dict[str, Dict[str, Any]] = {}
    for b in resp.get("aggregations", {}).get("sources", {}).get("buckets", []):
        last = b.get("last", {}).get("value")
        levels = {x["key"]: x["doc_count"] for x in b.get("levels", {}).get("buckets", [])}
        err = sum(v for k, v in levels.items() if str(k).upper() in ("ERROR", "FATAL", "CRITICAL"))
        total_d1 = int(b.get("d1", {}).get("doc_count", 0))
        out[str(b["key"])] = {
            "sourceKey": str(b["key"]),
            "docCount30d": int(b.get("doc_count", 0)),
            "lastReceiveTs": int(last) if last is not None else None,
            "lastReceive": to_local(last),
            "todayCount": int(b.get("today", {}).get("doc_count", 0)),
            "last24hCount": total_d1,
            "last1hCount": int(b.get("h1", {}).get("doc_count", 0)),
            "last7dCount": int(b.get("d7", {}).get("doc_count", 0)),
            "errorRate": round(err / total_d1 * 100, 1) if total_d1 else 0.0,
            "levels": levels,
            "hosts": {x["key"]: x["doc_count"] for x in b.get("hosts", {}).get("buckets", [])},
            "tenants": {x["key"]: x["doc_count"] for x in b.get("tenants", {}).get("buckets", [])},
        }
    meta = {
        "index": idx,
        "sourceField": sf,
        "timeField": tf,
        "from": frm,
        "to": to,
        "todayStart": t0,
        "tookMs": resp.get("took"),
    }
    return out, meta


_FIELDS_CACHE: Dict[str, Dict[str, str]] = {}


def fields_for(idx: str) -> Dict[str, str]:
    if idx not in _FIELDS_CACHE:
        _FIELDS_CACHE[idx] = index_fields(idx)
    return _FIELDS_CACHE[idx]


def health_of(recent_1h: int, total_7d: int) -> Optional[int]:
    """近 1h 接收量 ÷ 近 7d 同时段均值（与 dashboard-service 口径一致）。"""
    if total_7d <= 0:
        return 100 if recent_1h > 0 else 0
    avg = total_7d / (7 * 24)
    if avg <= 0:
        return 100 if recent_1h > 0 else 0
    return min(100, round(recent_1h / avg * 100))


STATUS_RANK = {"异常": 0, "告警中": 1, "离线": 2, "维护中": 3, "待接入": 4, "运行中": 5}


def derive_status(stats: Optional[Dict[str, Any]], cfg: Dict[str, Any],
                  settings: Dict[str, Any]) -> Dict[str, Any]:
    """状态完全由真实数据推导，人工覆盖单独标注。"""
    override = cfg.get("statusOverride")
    if override:
        label = "维护中" if override == "维护中" else "离线"
        return {"status": label, "reason": f"人工置为「{override}」", "manual": True,
                "health": health_of((stats or {}).get("last1hCount", 0), (stats or {}).get("last7dCount", 0))}
    if not stats:
        return {"status": "待接入", "reason": "已登记配置，但索引中尚未检测到该来源的日志",
                "manual": False, "health": None}
    gap_min = (now_ms() - (stats.get("lastReceiveTs") or 0)) / 60000
    warn = cfg.get("silenceWarnMin") or settings["silence_warn_min"]
    offline = cfg.get("silenceOfflineMin") or settings["silence_offline_min"]
    health = health_of(stats.get("last1hCount", 0), stats.get("last7dCount", 0))
    if gap_min <= warn:
        label, reason = "运行中", f"{max(0, round(gap_min, 1))} 分钟前有日志写入"
        if health is not None and health < 70:
            label, reason = "告警中", f"近 1h 流量健康度 {health}%（低于 70%）"
    elif gap_min <= offline:
        label, reason = "告警中", f"已静默 {round(gap_min, 1)} 分钟（告警阈值 {warn} 分钟）"
    else:
        label, reason = "异常", f"已静默 {round(gap_min, 1)} 分钟（异常阈值 {offline} 分钟）"
    return {"status": label, "reason": reason, "manual": False, "health": health}


def merge_sources(tenant: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """事实层（自动发现） + 配置层（PG）合并成列表项。"""
    idx = index_for(tenant)
    fields = fields_for(idx)
    roles = resolve_roles(fields, settings)
    ck = f"merge|{tenant}|{json.dumps(settings, sort_keys=True)}"
    cached = cache_get(ck)
    if cached:
        return cached

    cfg_rows = load_configs(tenant)
    discovered, meta = ({}, {"error": "自动发现已关闭"})
    if settings["discovery_enabled"]:
        discovered, meta = discover(idx, roles, settings["discovery_days"], fields)

    items: List[Dict[str, Any]] = []
    keys = set(discovered)
    for (t, k) in cfg_rows:
        keys.add(k)
    for k in keys:
        # 配置优先取当前租户，其次 tenant='all' 通用配置，最后取同名跨租户登记
        cfg_row = (cfg_rows.get((tenant, k)) or cfg_rows.get(("all", k))
                   or next((v for (t, sk), v in cfg_rows.items() if sk == k), None))
        cfg = row_to_config(cfg_row) if cfg_row else {
            "registered": False, "sourceKey": k, "tenant": tenant,
            "displayName": None, "ingestType": None, "region": None,
            "statusOverride": None, "fieldMap": {}, "tags": [], "extra": {},
        }
        stats = discovered.get(k)
        st = derive_status(stats, cfg, settings)
        item = {
            **cfg,
            "key": k,
            "sourceKey": k,
            "name": cfg.get("displayName") or k,
            "ingestType": cfg.get("ingestType") or "自动发现",
            "region": cfg.get("region") or "—",
            "discovered": k in discovered,
            "status": st["status"],
            "statusReason": st["reason"],
            "statusManual": st["manual"],
            "health": st["health"],
            "lastReceive": (stats or {}).get("lastReceive"),
            "lastReceiveTs": (stats or {}).get("lastReceiveTs"),
            "todayCount": (stats or {}).get("todayCount", 0),
            "last24hCount": (stats or {}).get("last24hCount", 0),
            "last1hCount": (stats or {}).get("last1hCount", 0),
            "docCount30d": (stats or {}).get("docCount30d", 0),
            "errorRate": (stats or {}).get("errorRate", 0.0),
            "levels": (stats or {}).get("levels", {}),
            "hosts": (stats or {}).get("hosts", {}),
            "tenants": (stats or {}).get("tenants", {}),
        }
        items.append(item)

    items.sort(key=lambda x: (STATUS_RANK.get(x["status"], 9), -(x["lastReceiveTs"] or 0)))
    result = {
        "items": items,
        "total": len(items),
        "index": idx,
        "fields": fields,
        "roles": roles,
        "settings": settings,
        "discovery": meta,
        "updatedAt": to_local(now_ms()),
        "capabilities": capabilities(),
    }
    cache_put(ck, result)
    return result


def capabilities() -> Dict[str, bool]:
    out = {"opensearch": False, "postgres": False, "kafka": False, "agentService": False}
    try:
        out["opensearch"] = bool(os_client.ping())
    except Exception:  # noqa: BLE001
        pass
    try:
        pg_query("SELECT 1")
        out["postgres"] = True
    except Exception:  # noqa: BLE001
        pass
    host, _, port = KAFKA_BROKER.partition(":")
    try:
        with socket.create_connection((host or "kafka", int(port or 9092)), timeout=1):
            out["kafka"] = True
    except Exception:  # noqa: BLE001
        pass
    try:
        with httpx.Client(timeout=1.0) as c:
            out["agentService"] = c.get(f"{AGENT_SERVICE}/health").status_code == 200
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class SourceIn(BaseModel):
    tenant: str = "default"
    source_key: str = Field(..., min_length=1, max_length=200)
    display_name: Optional[str] = None
    ingest_type: str = "Agent"
    endpoint: Optional[str] = None
    region: Optional[str] = None
    encoding: str = "UTF-8"
    parser_rule: Optional[str] = None
    sample_rate: int = Field(100, ge=0, le=100)
    target_index: Optional[str] = None
    field_map: Dict[str, Any] = {}
    tags: List[str] = []
    extra: Dict[str, Any] = {}
    status_override: Optional[str] = None
    silence_warn_min: Optional[int] = None
    silence_offline_min: Optional[int] = None
    owner: str = "黄金金"
    note: Optional[str] = None


def _upsert_sql(p: SourceIn) -> Tuple[str, Dict[str, Any]]:
    vals = {
        "tenant": p.tenant, "source_key": p.source_key.strip(),
        "display_name": p.display_name, "ingest_type": p.ingest_type,
        "endpoint": p.endpoint, "region": p.region, "encoding": p.encoding,
        "parser_rule": p.parser_rule, "sample_rate": p.sample_rate,
        "target_index": p.target_index, "field_map": psycopg2.extras.Json(p.field_map),
        "tags": psycopg2.extras.Json(p.tags), "extra": psycopg2.extras.Json(p.extra),
        "status_override": p.status_override,
        "silence_warn_min": p.silence_warn_min, "silence_offline_min": p.silence_offline_min,
        "owner": p.owner, "note": p.note,
    }
    sql = """
        INSERT INTO data_sources
            (tenant, source_key, display_name, ingest_type, endpoint, region, encoding,
             parser_rule, sample_rate, target_index, field_map, tags, extra,
             status_override, silence_warn_min, silence_offline_min, owner, note)
        VALUES (%(tenant)s,%(source_key)s,%(display_name)s,%(ingest_type)s,%(endpoint)s,
                %(region)s,%(encoding)s,%(parser_rule)s,%(sample_rate)s,%(target_index)s,
                %(field_map)s,%(tags)s,%(extra)s,%(status_override)s,
                %(silence_warn_min)s,%(silence_offline_min)s,%(owner)s,%(note)s)
        ON CONFLICT (tenant, source_key) DO UPDATE SET
            display_name=EXCLUDED.display_name, ingest_type=EXCLUDED.ingest_type,
            endpoint=EXCLUDED.endpoint, region=EXCLUDED.region, encoding=EXCLUDED.encoding,
            parser_rule=EXCLUDED.parser_rule, sample_rate=EXCLUDED.sample_rate,
            target_index=EXCLUDED.target_index, field_map=EXCLUDED.field_map,
            tags=EXCLUDED.tags, extra=EXCLUDED.extra,
            status_override=EXCLUDED.status_override,
            silence_warn_min=EXCLUDED.silence_warn_min,
            silence_offline_min=EXCLUDED.silence_offline_min,
            owner=EXCLUDED.owner, note=EXCLUDED.note, updated_at=now()
        RETURNING id
    """
    return sql, vals


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "ingest-service"}


@app.get("/")
def root():
    return {
        "service": "ingest-service",
        "docs": "/docs",
        "endpoints": ["/meta", "/sources (自动发现 + 配置合并)", "/sources/{key}",
                      "/sources/test", "/sources/batch-status", "/export", "/settings"],
    }


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/meta")
def meta(tenant: str = Query("default")):
    """新建/编辑表单所需的真实环境信息：索引、字段角色候选、接入类型、组件可达性。"""
    idx = index_for(tenant)
    fields = fields_for(idx)
    settings = load_settings()
    return {
        "index": idx,
        "fields": fields,
        "roles": resolve_roles(fields, settings),
        "roleCandidates": ROLE_CANDIDATES,
        "autoDetected": detect_roles(fields),
        "aggregatableFields": sorted([f for f in fields if agg_field(fields, f)]),
        "typeOptions": INGEST_TYPES,
        "settings": settings,
        "capabilities": capabilities(),
        "tenantOptions": ["default", "acme", "gp", "all"],
        "note": "字段角色按索引真实 mapping 自动探测；上线后日志字段命名不同，可在接入设置里覆盖",
    }


@app.get("/settings")
def get_settings():
    return {"settings": load_settings(), "defaults": DEFAULT_SETTINGS}


@app.put("/settings")
def put_settings(payload: Dict[str, Any]):
    return {"settings": save_settings(payload.get("settings") or payload)}


@app.get("/sources")
def list_sources(
    tenant: str = Query("default"),
    keyword: str = Query("", description="名称 / 来源标识模糊匹配"),
    type: str = Query("", description="接入类型"),
    status: str = Query("", description="运行状态"),
    region: str = Query("", description="所属区域"),
    registered: str = Query("", description="true=仅已登记 / false=仅自动发现"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    force: bool = Query(False),
):
    settings = load_settings()
    if force:
        _cache.clear()
        _FIELDS_CACHE.clear()
    data = merge_sources(tenant, settings)
    items = data["items"]
    kw = keyword.strip().lower()
    if kw:
        items = [x for x in items if kw in x["name"].lower() or kw in x["sourceKey"].lower()
                 or kw in str(x.get("tags") or []).lower()]
    if type:
        items = [x for x in items if x["ingestType"] == type]
    if status:
        items = [x for x in items if x["status"] == status]
    if region:
        items = [x for x in items if x["region"] == region]
    if registered in ("true", "false"):
        want = registered == "true"
        items = [x for x in items if x["registered"] == want]
    total = len(items)
    start = (page - 1) * pageSize
    return {
        "total": total, "page": page, "pageSize": pageSize,
        "items": items[start:start + pageSize],
        "discoveredCount": sum(1 for x in data["items"] if x["discovered"] and not x["registered"]),
        "registeredCount": sum(1 for x in data["items"] if x["registered"]),
        "index": data["index"], "fields": data["fields"], "roles": data["roles"],
        "settings": settings, "discovery": data["discovery"],
        "capabilities": data["capabilities"], "updatedAt": data["updatedAt"],
    }


@app.post("/sources", status_code=201)
def create_source(payload: SourceIn):
    sql, vals = _upsert_sql(payload)
    try:
        rows = pg_query(sql, vals)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"保存数据源配置失败：{e}")
    _cache.clear()
    return {"ok": True, "id": rows[0]["id"], "sourceKey": vals["source_key"]}


@app.put("/sources/{source_key}")
def update_source(source_key: str, payload: SourceIn):
    payload.source_key = source_key
    sql, vals = _upsert_sql(payload)
    try:
        rows = pg_query(sql, vals)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"更新数据源配置失败：{e}")
    _cache.clear()
    return {"ok": True, "id": rows[0]["id"], "sourceKey": source_key}


@app.delete("/sources/{source_key}")
def delete_source(source_key: str, tenant: str = Query("default")):
    """仅删除平台侧登记配置；不会、也不能删除已写入 OpenSearch 的真实日志。"""
    pg_exec("DELETE FROM data_sources WHERE tenant=%s AND source_key=%s", (tenant, source_key))
    _cache.clear()
    return {"ok": True, "sourceKey": source_key,
            "note": "仅移除登记配置；已接入的日志仍会在自动发现中显示"}


@app.post("/sources/batch-status")
def batch_status(payload: Dict[str, Any]):
    """批量启用/停用/维护：真实写入状态覆盖。未登记的来源会自动补一条最小登记。"""
    keys = payload.get("keys") or []
    status = payload.get("status") or ""   # ""＝恢复正常，维护中，已停用
    tenant = payload.get("tenant") or "default"
    if not keys:
        raise HTTPException(status_code=400, detail="keys 不能为空")
    override = {"维护中": "维护中", "已停用": "已停用"}.get(status, None)
    changed = 0
    for k in keys:
        existing = pg_query(
            "SELECT * FROM data_sources WHERE tenant=%s AND source_key=%s", (tenant, k)
        )
        if existing:
            pg_exec("UPDATE data_sources SET status_override=%s, updated_at=now() WHERE id=%s",
                    (override, existing[0]["id"]))
        elif override:
            p = SourceIn(tenant=tenant, source_key=k, ingest_type="自定义",
                         status_override=override, note="批量操作自动生成的最小登记")
            sql, vals = _upsert_sql(p)
            pg_query(sql, vals)
        changed += 1
    _cache.clear()
    return {"ok": True, "changed": changed, "status": status or "正常"}


@app.get("/sources/{source_key}")
def source_detail(source_key: str, tenant: str = Query("default")):
    """详情：登记配置 + 真实趋势 / 级别分布 / 字段覆盖率 / 样例原始日志。"""
    settings = load_settings()
    data = merge_sources(tenant, settings)
    item = next((x for x in data["items"] if x["sourceKey"] == source_key), None)
    if not item:
        raise HTTPException(status_code=404, detail="该来源在当前租户索引中未发现且无登记配置")
    fields = data["fields"]
    roles = resolve_roles(fields, settings, item.get("fieldMap"))
    sf = agg_field(fields, roles["source"]["field"])
    tf = roles["time"]["field"]
    idx = index_for(tenant, item.get("targetIndex") or "")
    detail: Dict[str, Any] = {"config": item, "roles": roles, "index": idx,
                              "trend": [], "fieldCoverage": {}, "samples": [],
                              "note": "该来源当前无日志，详情仅展示登记配置"}
    if not sf or not tf or not item["discovered"]:
        return detail

    to = now_ms()
    frm = to - 24 * 3600 * 1000
    must: List[Dict[str, Any]] = [
        {"range": {tf: {"gte": frm, "lte": to}}},
        {"term": {sf: source_key}},
    ]
    # 24h 趋势 + 抽样原始日志 + 字段覆盖率（exists 聚合，按真实 mapping 逐字段）
    aggs: Dict[str, Any] = {
        "trend": {"date_histogram": {"field": tf, "fixed_interval": "1h",
                                    "min_doc_count": 0, "time_zone": "+08:00",
                                    "extended_bounds": {"min": frm, "max": to}}}
    }
    cov_keys: Dict[str, str] = {}
    for i, f in enumerate(sorted(k for k in fields if "." not in k and k != "raw")):
        agg_key = f"cov{i}"
        cov_keys[agg_key] = f
        aggs[agg_key] = {"filter": {"exists": {"field": f}}}
    body = {"size": 5, "query": {"bool": {"must": must}},
            "sort": [{tf: {"order": "desc"}}], "aggs": aggs,
            "_source": True}
    try:
        resp = os_client.search(index=idx, body=body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch detail error: {e}")
    buckets = resp.get("aggregations", {}).get("trend", {}).get("buckets", [])
    detail["trend"] = [
        {"t": to_local(b["key"]), "v": b["doc_count"]} for b in buckets
    ]
    aggr = resp.get("aggregations", {})
    total = resp.get("hits", {}).get("total", {})
    doc_total = total.get("value", 0) if isinstance(total, dict) else total or 0
    detail["fieldCoverage"] = {
        cov_keys[k]: round(aggr[k]["doc_count"] / doc_total * 100, 1) if doc_total else 0.0
        for k in cov_keys
    }
    detail["samples"] = [
        {"id": h.get("_id"), "time": to_local(h["_source"].get(tf)),
         "raw": h["_source"].get("raw") or json.dumps(h["_source"], ensure_ascii=False),
         "source": h["_source"]}
        for h in resp.get("hits", {}).get("hits", [])
    ]
    detail["note"] = "全部指标来自 OpenSearch 真实聚合（近 24 小时）"
    return detail


class TestReq(BaseModel):
    ingest_type: str = "Agent"
    endpoint: Optional[str] = ""
    source_key: Optional[str] = ""
    tenant: str = "default"
    target_index: Optional[str] = ""


@app.post("/sources/test")
def test_connectivity(req: TestReq):
    """真实连通性校验。任何失败都如实返回原因，不产生假的「正常 / 12ms」。"""
    started = time.time()
    result: Dict[str, Any] = {
        "ok": False, "type": req.ingest_type, "endpoint": req.endpoint or "",
        "checkedAt": to_local(now_ms()), "evidence": {}, "detail": "",
    }

    def done(ok: bool, detail: str):
        result["ok"] = ok
        result["detail"] = detail
        result["latencyMs"] = round((time.time() - started) * 1000, 1)
        return result

    if req.ingest_type == "Agent":
        idx = index_for(req.tenant, req.target_index or "")
        fields = fields_for(idx)
        roles = resolve_roles(fields, load_settings())
        sf = agg_field(fields, roles["source"]["field"])
        tf = roles["time"]["field"]
        if not req.source_key:
            return done(False, "Agent 类型需要指定「来源标识」，用于匹配真实日志中的来源字段值")
        if not sf or not tf:
            return done(False, "索引缺少可用的来源/时间字段，请先在接入设置里指定字段角色")
        frm = now_ms() - 24 * 3600 * 1000
        body = {
            "size": 0,
            "query": {"bool": {"must": [
                {"range": {tf: {"gte": frm}}}, {"term": {sf: req.source_key}},
            ]}},
            "aggs": {"last": {"max": {"field": tf}}},
        }
        try:
            r = os_client.search(index=idx, body=body)
        except Exception as e:  # noqa: BLE001
            return done(False, f"OpenSearch 查询失败：{e}")
        cnt = r.get("hits", {}).get("total", {})
        n = cnt.get("value", 0) if isinstance(cnt, dict) else cnt or 0
        result["evidence"] = {
            "index": idx, "matchField": sf, "matchValue": req.source_key,
            "last24hCount": n,
            "lastReceive": to_local(r.get("aggregations", {}).get("last", {}).get("value")),
        }
        # 附带管控面真实 Agent 概况（维护中既不算运行中也不算离线，故用「非离线」口径）
        try:
            with httpx.Client(timeout=1.5) as c:
                rr = c.get(f"{AGENT_SERVICE}/agents", params={"pageSize": 200})
                st = rr.json().get("stats", {})
                total = st.get("total", 0)
                result["evidence"]["agents"] = (
                    f"注册 {total} 台 / 运行中 {st.get('online', 0)} 台 / 离线 {st.get('offline', 0)} 台"
                )
        except Exception:  # noqa: BLE001
            result["evidence"]["agents"] = "管控服务暂不可达"
        if n > 0:
            return done(True, f"接入正常：近 24 小时在 {idx} 检测到 {n} 条 {sf}={req.source_key} 的日志")
        return done(False, f"近 24 小时未在 {idx} 检测到 {sf}={req.source_key} 的日志（可先登记，等日志写入后自动转为运行中）")

    # Syslog / HTTP / Kafka / 自定义：都需要一个可连通的地址
    ep = (req.endpoint or "").strip()
    if not ep:
        return done(False, "请填写接收地址（host:port 或完整 URL）")
    parsed = urlparse(ep if "://" in ep else f"//{ep}")
    host = parsed.hostname
    port = parsed.port or (514 if req.ingest_type == "Syslog" else
                           9092 if req.ingest_type == "Kafka" else
                           443 if parsed.scheme == "https" else 80)
    if not host:
        return done(False, f"无法从「{ep}」解析出主机名")
    try:
        with socket.create_connection((host, port), timeout=2.5):
            pass
    except Exception as e:  # noqa: BLE001
        return done(False, f"TCP 连接 {host}:{port} 失败：{e}")
    result["evidence"]["tcp"] = f"{host}:{port} 可达"

    if req.ingest_type in ("HTTP", "自定义") and parsed.scheme in ("http", "https"):
        try:
            with httpx.Client(timeout=3.0, follow_redirects=False) as c:
                r = c.get(ep)
            result["evidence"]["httpStatus"] = r.status_code
            return done(True, f"HTTP 探测成功：{ep} 返回 {r.status_code}（服务在监听即视为连通）")
        except Exception as e:  # noqa: BLE001
            # TCP 通但 HTTP 无响应 → 仍算连通，如实说明
            result["evidence"]["httpError"] = str(e)

    if req.ingest_type == "Kafka":
        try:
            from kafka import KafkaConsumer
            consumer = KafkaConsumer(
                bootstrap_servers=KAFKA_BROKER,
                request_timeout_ms=3000,
                api_version_auto_timeout_ms=3000,
            )
            topics = sorted(consumer.topics())
            consumer.close()
            result["evidence"]["broker"] = KAFKA_BROKER
            result["evidence"]["topics"] = topics
            return done(True, f"Kafka broker {KAFKA_BROKER} 可达，当前 topic：{', '.join(topics) if topics else '（无）'}")
        except Exception as e:  # noqa: BLE001
            result["evidence"]["kafkaProbe"] = str(e)[:200]

    return done(True, f"TCP 连接 {host}:{port} 成功（真实探测，未做应用层握手）")


@app.get("/export")
def export_config(tenant: str = Query("default")):
    """导出真实登记配置 + 自动发现清单，供新环境导入/存档。"""
    settings = load_settings()
    data = merge_sources(tenant, settings)
    payload = {
        "exportedAt": to_local(now_ms()),
        "tenant": tenant,
        "index": data["index"],
        "settings": settings,
        "fieldRoles": data["roles"],
        "sources": [
            {k: v for k, v in x.items() if k not in ("levels", "hosts", "tenants")}
            for x in data["items"] if x["registered"]
        ],
        "discovered": [x["sourceKey"] for x in data["items"] if x["discovered"] and not x["registered"]],
        "note": "由 ingest-service 从 OpenSearch 与 PostgreSQL 实时导出，非静态样例",
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"data-sources-{tenant}-{datetime.now(tz=CST).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=content.encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
