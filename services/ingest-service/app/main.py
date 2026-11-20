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
import hashlib
import time
import socket
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
import psycopg2
import psycopg2.extras
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from opensearchpy import OpenSearch, helpers
from pydantic import BaseModel, Field

from .authz import current_identity, is_admin, resolve_ingest_tenant, resolve_tenant
from . import api_pipelines
from . import index_manager
from . import kafka_writer
from . import log_schema
from . import pipeline
from . import pipeline_store

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
INGEST_TYPES = ["Agent", "Syslog", "HTTP", "Kafka", "自定义"]

app = FastAPI(title="ingest-service", version="0.5.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_pipelines.router)


def index_for(tenant: Optional[str], override: str = "") -> str:
    """索引命名是约定（logs-<租户编码>）；租户清单以 tenants 表为唯一真源。

    空租户 → logs-unknown：采集/接入端无法归属时集中落这里等待治理，
    绝不静默写入 default（跨租户污染比查不到更危险）。
    """
    if override:
        return override
    if tenant in ("all", "*"):
        return "logs-*"
    if not tenant:
        return "logs-unknown"
    return f"logs-{tenant}"


def tenant_from_index(idx: str) -> str:
    """从索引/别名反解租户编码（logs-<tenant>[-write|-YYYY.MM.DD] → <tenant>）。

    写入侧已改为按天索引 + 写别名，这里必须同时识别三种后缀，否则 Kafka key
    会变成 "acme-write"，同一租户被拆到不同分区，Flink keyBy 后状态对不上。
    """
    if not idx.startswith("logs-"):
        return ""
    tail = idx[len("logs-"):]
    if tail.endswith("-write"):
        return tail[: -len("-write")]
    # 日期后缀 YYYY.MM.DD（长度 10，形如 2026.11.19）
    # "-2026.11.19" 共 11 字符：[-11]='-'，[-6] 与 [-3] 是两个点号
    if len(tail) > 11 and tail[-11] == "-" and tail[-6] == "." and tail[-3] == ".":
        return tail[:-11]
    return tail


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
        # ------------------------------------------------------------------
        # 主机台账：系统/中间件/数据库日志的**唯一**归属依据。
        # 这类日志正文里没有业务租户信息，只能靠"这台机器属于谁"来判定。
        # 生产环境应由 CMDB 同步写入（主机名 / 租户 / 环境 / 角色），
        # 本地开发用种子数据模拟。
        # ------------------------------------------------------------------
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS hosts (
                host       TEXT PRIMARY KEY,
                tenant     TEXT NOT NULL,
                env        TEXT NOT NULL DEFAULT 'prod',
                role       TEXT,
                note       TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # 平台自身组件即采集源（真实进程、真实格式），host 由采集端静态打标。
        # 租户按业务系统划分：这些是"日志平台自己"的基础设施，统一归 log-system 租户。
        # 业务系统（如 i6000）不在这里，走 namespace_tenant 映射判定。
        seed = [
            ("web",      "log-system", "dev", "web",   "平台前端/API 网关 nginx"),
            ("redis",    "log-system", "dev", "cache", "平台缓存/会话 redis"),
            ("postgres", "log-system", "dev", "db",    "平台元数据库 PostgreSQL"),
            ("kafka",    "log-system", "dev", "mq",    "日志主干 Kafka broker"),
        ]
        for host, tenant, env, role, note in seed:
            pg_exec(
                """
                INSERT INTO hosts (host, tenant, env, role, note)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (host) DO NOTHING
                """,
                (host, tenant, env, role, note),
            )
        # 修正历史映射：ON CONFLICT DO NOTHING 不会更新已存在行，旧库里 host→default
        # 的映射会残留。这里把平台自身组件的归属显式纠正到 log-system，并清掉已停用的
        # 模拟主机台账。幂等，每次启动执行。
        for host in ("web", "redis", "postgres", "kafka"):
            pg_exec("UPDATE hosts SET tenant=%s WHERE host=%s", ("log-system", host))
        pg_exec("DELETE FROM hosts WHERE host LIKE '%-sim'")
        # 服务端解析管线的规则表（解析器 / 管线 / 采集器凭证）
        pipeline_store.init_tables()
        pipeline_store.seed_defaults()
        # 归属冲突 / 越界审计：谁在伪造租户必须可追溯
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS ingest_security_audit (
                id             SERIAL PRIMARY KEY,
                ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
                collector_code TEXT NOT NULL DEFAULT '',
                action         TEXT NOT NULL,
                injected       TEXT NOT NULL DEFAULT '',
                inferred       TEXT NOT NULL DEFAULT '',
                final_tenant   TEXT NOT NULL DEFAULT '',
                host           TEXT NOT NULL DEFAULT '',
                src_file       TEXT NOT NULL DEFAULT '',
                sample         TEXT NOT NULL DEFAULT ''
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
    tenant: str = "i6000"
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
    return {
        "status": "ok",
        "service": "ingest-service",
        "storageMode": kafka_writer.mode(),
        "kafkaWriter": kafka_writer.health(),
    }


@app.get("/")
def root():
    return {
        "service": "ingest-service",
        "docs": "/docs",
        "endpoints": ["/ingest (通用日志写入)", "/meta", "/sources (自动发现 + 配置合并)",
                      "/sources/{key}", "/sources/test", "/sources/batch-status", "/export", "/settings"],
    }


# ---------------------------------------------------------------------------
# 数据面：统一日志写入（各类系统接入的唯一入口）
#
# 为什么需要它：Fluent Bit 适合主机/容器侧采集，但数据库、网络设备、第三方 SaaS、
# App 直报等无法部署采集器的系统，需要一个通用 HTTP 入口。租户归属在这里确定：
# - 机器接入：X-Api-Key → 映射到租户（租户绑在凭证上，与日志正文无关）
# - 控制台/调试：Authorization: Bearer <JWT> → 用登录身份所属租户
# - 无法归属：写 logs-unknown 并告警，绝不静默归 default
# ---------------------------------------------------------------------------
INGEST_API_KEYS = os.getenv("INGEST_API_KEYS", "")
MAX_BATCH = int(os.getenv("INGEST_MAX_BATCH", "1000"))
MAX_PAYLOAD = int(os.getenv("INGEST_MAX_PAYLOAD", "50000"))


def tenant_by_api_key(key: str) -> Optional[str]:
    for pair in INGEST_API_KEYS.split(","):
        k, _, t = pair.partition("=")
        if k.strip() and t.strip() and secrets.compare_digest(k.strip(), key.strip()):
            return t.strip()
    return None


def tenant_enabled(code: str) -> bool:
    try:
        return bool(pg_query("SELECT 1 FROM tenants WHERE code=%s AND enabled", (code,)))
    except Exception:  # noqa: BLE001
        return False


# 来源系统/采集器可能用各种键名与取值形态表达「事件时间」。接入层负责统一归一化，
# 而不是强求每个来源都改成同一种格式——这正是商用平台"兼容各类系统接入"的要求：
#   - @timestamp / time / timestamp / ts / event_time / date 等键名
#   - ISO8601（带 Z / 带 ±HH:MM / 带 ±HHMM）、"YYYY-MM-DD HH:MM:SS"、epoch 秒/毫秒
TIME_KEYS = ("@timestamp", "time", "timestamp", "ts", "event_time", "date")
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
)


def _iso_utc(dt: datetime) -> str:
    """统一成 OpenSearch date 类型可直接吃的 UTC ISO8601（毫秒 + Z）。"""
    if dt.tzinfo is None:
        # 无时区信息按接入侧本地时区解释（部署在 Asia/Shanghai，与来源主机一致）
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_iso_utc(value: Any) -> Optional[str]:
    """把来源时间值归一化为 UTC ISO8601；无法识别返回 None（调用方用采集时刻兜底）。

    epoch 启发式：> 1e12 视为毫秒，> 1e9 视为秒；小于 1e9 的多半不是时间戳。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if num > 1e12:
            num /= 1000.0
        if num > 1e9:
            try:
                return _iso_utc(datetime.fromtimestamp(num, tz=timezone.utc))
            except (OverflowError, OSError, ValueError):
                return None
        return None
    text = str(value).strip()
    if not text:
        return None
    # 纯数字字符串（epoch）
    if text.replace(".", "", 1).isdigit():
        return to_iso_utc(float(text))
    # RFC3339 的 +08:00 在 %z 下可用；再补一份无冒号形态（Python 3.7+ %z 均支持）
    normalized = text.replace("Z", "+0000") if text.endswith("Z") else text
    for fmt in _TIME_FORMATS:
        try:
            return _iso_utc(datetime.strptime(normalized, fmt))
        except ValueError:
            continue
    try:  # 兜底：交给 fromisoformat 处理少见形态
        return _iso_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def normalize_record(rec: Dict[str, Any], tenant: str) -> Dict[str, Any]:
    """统一记录形态：时间归一化 + 租户服务端强制覆盖。

    时间语义严格按「来源自带的事件时间」优先，采集时刻只作最后兜底。
    Fluent Bit 等采集器把事件时间放在 date（epoch 秒）字段，若只认 @timestamp
    就会全部退化成采集时刻——历史日志会整体挤到当天，按时间聚合失真。
    """
    doc = dict(rec or {})
    raw_ts = next((doc[k] for k in TIME_KEYS if doc.get(k) not in (None, "")), None)
    iso = to_iso_utc(raw_ts)
    if iso is None:
        iso = _iso_utc(datetime.now(timezone.utc))
    doc["@timestamp"] = iso
    # 采集器的时间键已归一化进 @timestamp，不重复落库（避免 date/time 多套时间字段并存）
    for k in TIME_KEYS:
        if k != "@timestamp":
            doc.pop(k, None)
    # 分类/级别/特有字段归一 + 敏感信息脱敏（契约见 docs/log-ingestion-model.md）。
    # 放在租户覆盖之前：这一步只改日志自身语义，不应影响归属结果。
    try:
        doc = log_schema.normalize(doc)
    except Exception as e:  # noqa: BLE001
        # 归一化失败不能丢日志：保留原始形态入库，后续按 source_type 治理
        print(f"[ingest] 日志归一化失败（保留原始记录）: {e}", flush=True)
    # 服务端强制覆盖：正文里的 tenant 字段不可信，一律以入口判定结果为准
    doc["tenant_id"] = tenant or "unknown"
    return doc


@app.post("/ingest")
def ingest(
    payload: Dict[str, Any],
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
):
    """通用日志写入：POST /ingest

    body: {"tenant": "acme", "records": [{...}, ...]}
      或单条 {"tenant": "acme", "message": "...", "level": "ERROR"}
    鉴权二选一：
      - X-Api-Key: <key>   （机器接入，租户由 Key 绑定，body.tenant 被忽略）
      - Authorization: Bearer <JWT>（控制台/调试，租户取登录身份）
    """
    records = payload.get("records")
    if records is None:
        records = payload.get("logs")
    if records is None:
        records = [payload] if any(k in payload for k in ("message", "msg", "raw")) else []
    if not isinstance(records, list) or not records:
        raise HTTPException(status_code=400, detail="records 不能为空（需为非空数组或单条日志对象）")
    if len(records) > MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"单批最多 {MAX_BATCH} 条，请拆分后再发送")

    if x_api_key:
        key_tenant = tenant_by_api_key(x_api_key)
        if key_tenant is None:
            raise HTTPException(status_code=401, detail="API Key 无效")
        tenant = key_tenant if tenant_enabled(key_tenant) else ""
    else:
        identity = current_identity(authorization)
        tenant = resolve_ingest_tenant(identity, payload.get("tenant"))

    docs = [normalize_record(r, tenant) for r in records if isinstance(r, dict)]
    if not docs:
        raise HTTPException(status_code=400, detail="records 中没有有效的日志对象")
    pipeline.process(docs, tenant)

    written = {"opensearch": False, "kafka": False}
    errors = []
    # 双引擎写入链路（阶段2）：按 STORAGE_MODE 决定落点。
    #  - Kafka 失败不丢数据语义：dual 模式下 OS 仍写，Kafka 报错如实返回 502 让采集端重试；
    #  - kafka 模式下由 Flink 统一消费后写 OS+CH。
    if kafka_writer.use_opensearch():
        try:
            # 写别名始终指向当天索引；查询侧仍查 logs-<tenant> 读别名，代码不变。
            idx = index_manager.ensure_write_target(os_client, tenant)
            helpers.bulk(os_client, [{"_index": idx, "_source": d} for d in docs])
            written["opensearch"] = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"opensearch: {e}")
    if kafka_writer.use_kafka():
        try:
            kafka_writer.publish(docs, tenant)
            written["kafka"] = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"kafka: {e}")
    if errors and not any(written.values()):
        raise HTTPException(status_code=502, detail="; ".join(errors))

    if not tenant:
        print(f"[ingest-service] 未归属日志 {len(docs)} 条已写入 logs-unknown，请检查接入端租户绑定",
              flush=True)
    return {
        "ok": any(written.values()),
        "accepted": len(docs),
        "tenant": tenant or "unknown",
        "index": idx,
        "written": written,
        "storageMode": kafka_writer.mode(),
        "unassigned": not bool(tenant),
        "message": ("已写入 " + idx) if tenant else
                   "无法判定租户，已写入 logs-unknown 待治理（请为接入端绑定租户或 API Key）",
    }


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/meta")
def meta(
    tenant: str = Query("i6000"),
    identity: Dict[str, Any] = Depends(current_identity),
):
    """新建/编辑表单所需的真实环境信息：索引、字段角色候选、接入类型、组件可达性。

    租户由服务端按登录身份解析，前端传入值只作管理员选择用，普通账号无法越权探测他租户索引结构。
    """
    tenant = resolve_tenant(identity, tenant)
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
        "tenantOptions": (
            [r["code"] for r in pg_query(
                "SELECT code FROM tenants WHERE enabled ORDER BY id")] + ["all"]
            if is_admin(identity) else [tenant]
        ),
        "note": "字段角色按索引真实 mapping 自动探测；上线后日志字段命名不同，可在接入设置里覆盖",
        "tenant": tenant,
    }


@app.get("/settings")
def get_settings(identity: Dict[str, Any] = Depends(current_identity)):
    """平台级接入设置：需登录可读。"""
    return {"settings": load_settings(), "defaults": DEFAULT_SETTINGS}


@app.put("/settings")
def put_settings(
    payload: Dict[str, Any],
    identity: Dict[str, Any] = Depends(current_identity),
):
    """平台级接入设置：全局生效，仅平台管理员可改（避免单租户改坏他租户字段映射）。"""
    if not is_admin(identity):
        raise HTTPException(status_code=403, detail="仅平台管理员可修改接入设置")
    return {"settings": save_settings(payload.get("settings") or payload)}


@app.get("/sources")
def list_sources(
    tenant: str = Query("i6000"),
    keyword: str = Query("", description="名称 / 来源标识模糊匹配"),
    type: str = Query("", description="接入类型"),
    status: str = Query("", description="运行状态"),
    region: str = Query("", description="所属区域"),
    registered: str = Query("", description="true=仅已登记 / false=仅自动发现"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    force: bool = Query(False),
    identity: Dict[str, Any] = Depends(current_identity),
):
    tenant = resolve_tenant(identity, tenant)
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
def create_source(payload: SourceIn, identity: Dict[str, Any] = Depends(current_identity)):
    payload.tenant = resolve_tenant(identity, payload.tenant)
    sql, vals = _upsert_sql(payload)
    try:
        rows = pg_query(sql, vals)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"保存数据源配置失败：{e}")
    _cache.clear()
    return {"ok": True, "id": rows[0]["id"], "sourceKey": vals["source_key"]}


@app.put("/sources/{source_key}")
def update_source(source_key: str, payload: SourceIn,
                  identity: Dict[str, Any] = Depends(current_identity)):
    payload.source_key = source_key
    payload.tenant = resolve_tenant(identity, payload.tenant)
    sql, vals = _upsert_sql(payload)
    try:
        rows = pg_query(sql, vals)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"更新数据源配置失败：{e}")
    _cache.clear()
    return {"ok": True, "id": rows[0]["id"], "sourceKey": source_key}


@app.delete("/sources/{source_key}")
def delete_source(source_key: str, tenant: str = Query("i6000"),
                  identity: Dict[str, Any] = Depends(current_identity)):
    """仅删除平台侧登记配置；不会、也不能删除已写入 OpenSearch 的真实日志。"""
    tenant = resolve_tenant(identity, tenant)
    pg_exec("DELETE FROM data_sources WHERE tenant=%s AND source_key=%s", (tenant, source_key))
    _cache.clear()
    return {"ok": True, "sourceKey": source_key,
            "note": "仅移除登记配置；已接入的日志仍会在自动发现中显示"}


@app.post("/sources/batch-status")
def batch_status(payload: Dict[str, Any],
                 identity: Dict[str, Any] = Depends(current_identity)):
    """批量启用/停用/维护：真实写入状态覆盖。未登记的来源会自动补一条最小登记。"""
    keys = payload.get("keys") or []
    status = payload.get("status") or ""   # ""＝恢复正常，维护中，已停用
    tenant = resolve_tenant(identity, payload.get("tenant"))
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
def source_detail(source_key: str, tenant: str = Query("i6000"),
                  identity: Dict[str, Any] = Depends(current_identity)):
    tenant = resolve_tenant(identity, tenant)
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
    tenant: str = "i6000"
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
def export_config(tenant: str = Query("i6000"),
                  identity: Dict[str, Any] = Depends(current_identity)):
    tenant = resolve_tenant(identity, tenant)
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


# ---- 采集器通道：受信任的采集端按来源打标后批量上报 ----
# 鉴权用独立的 COLLECTOR_KEY（基础设施凭证），与"应用 API Key"区分开：
# 采集器是受信基础设施，允许每条记录带不同租户（一个采集器可跨多租户）；
# 应用直报则必须用绑定租户的 API Key，不允许自报租户。
COLLECTOR_KEYS = os.getenv("INGEST_COLLECTOR_KEYS", "")


def collector_authorized(key: Optional[str]) -> Optional[Dict[str, Any]]:
    """按 key_hash 查采集器台账，返回含 tenant_allow 的凭证主体。

    兼容旧环境变量：表中查不到但环境变量命中时返回 legacy 主体（不做白名单限制），
    保证现有演示链路不中断；但每次命中都打 warning，推动迁移到台账。
    """
    if not key:
        return None
    digest = hashlib.sha256(key.strip().encode("utf-8")).hexdigest()
    try:
        rows = pipeline_store.pg_query(
            "SELECT code, name, tenant_allow FROM log_collectors WHERE key_hash=%s",
            (digest,),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[ingest] 采集器台账查询失败（沿用环境变量凭证）: {e}", flush=True)
        rows = []
    if rows:
        r = rows[0]
        allow = r.get("tenant_allow")
        if isinstance(allow, str):
            try:
                allow = json.loads(allow)
            except ValueError:
                allow = []
        r["tenant_allow"] = [str(x) for x in (allow or [])]
        r["legacy"] = False
        return r
    for k in COLLECTOR_KEYS.split(","):
        if k.strip() and secrets.compare_digest(k.strip(), key.strip()):
            print("[ingest] 仍在使用环境变量采集器凭证，请迁移到采集器台账", flush=True)
            return {"code": "env-legacy", "name": "环境变量凭证",
                    "tenant_allow": [], "legacy": True}
    return None


def require_collector(
    x_collector_key: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """采集器通道鉴权依赖，返回凭证主体供归属判定使用。

    写成依赖而不是函数体内判断，是为了让鉴权先于 body 解析执行：否则未携带凭证的请求
    会先被 body schema 拦成 422，既是无效信息暴露，也不符合「未授权一律 401」的语义。
    """
    collector = collector_authorized(x_collector_key)
    if not collector:
        raise HTTPException(status_code=401, detail="采集器凭证无效")
    return collector


_HOST_CACHE: Dict[str, Any] = {"ts": 0.0, "map": {}}
_HOST_CACHE_TTL = 30.0

_NS_CACHE: Dict[str, Any] = {"ts": 0.0, "map": {}}
_NS_CACHE_TTL = 30.0


def namespace_tenant_map() -> Dict[str, str]:
    """K8s namespace → 租户(code) 映射，带 30s 缓存（与 hosts 台账同构）。

    租户按业务系统划分后，K8s 场景最可靠的归属来源是容器日志文件名里的 namespace：
    采集器不注入 tenant_id，服务端用 namespace 查这张映射表定租户。新增业务系统
    只需在 namespace_tenant 表加一行，不改采集器配置与服务端规则。
    """
    now = time.time()
    if now - _NS_CACHE["ts"] > _NS_CACHE_TTL:
        try:
            rows = pg_query("SELECT namespace, tenant FROM namespace_tenant WHERE tenant <> ''")
            _NS_CACHE["map"] = {str(r["namespace"]): str(r["tenant"]) for r in rows}
            _NS_CACHE["ts"] = now
        except Exception as e:  # noqa: BLE001
            # 查库失败不缓存空结果，避免一次抖动导致整批 K8s 日志落 unknown
            print(f"[ingest] namespace_tenant 台账查询失败（沿用旧缓存）: {e}", flush=True)
    return _NS_CACHE["map"]


def k8s_namespace_from_src(src: str) -> str:
    """从 K8s 容器日志路径解析 namespace。

    Docker/CRI 容器日志文件名格式：<pod>_<namespace>_<container>.log
    pod / namespace / container 名都不含下划线（K8s DNS 命名规则），所以按 '_'
    切分取倒数第二段即 namespace。非 K8s 路径返回空串，由调用方回退其他判定。
    例：/var/log/containers/order-7d9f_i6000_order-1a2b.log -> i6000
    """
    base = os.path.basename(str(src or ""))
    if not base.endswith(".log"):
        return ""
    parts = base[: -len(".log")].split("_")
    if len(parts) < 3:
        return ""
    return parts[-2]


def host_tenant_map() -> Dict[str, str]:
    """主机台账缓存（30s）：系统/中间件/数据库日志的归属依据。

    为什么缓存：采集器一次上报几百条，逐条查库会把 PostgreSQL 打满；
    而台账变更频率极低（CMDB 同步），30s 的陈旧度完全可接受。
    查库失败时**不缓存空结果**，避免一次抖动导致整批日志全落 unknown。
    """
    now = time.time()
    if now - _HOST_CACHE["ts"] > _HOST_CACHE_TTL:
        try:
            rows = pg_query("SELECT host, tenant FROM hosts WHERE tenant <> ''")
            _HOST_CACHE["map"] = {str(r["host"]): str(r["tenant"]) for r in rows}
            _HOST_CACHE["ts"] = now
        except Exception as e:  # noqa: BLE001
            print(f"[ingest] hosts 台账查询失败（沿用旧缓存）: {e}", flush=True)
    return _HOST_CACHE["map"]


def audit_tenant_decision(action: str, collector: Optional[Dict[str, Any]],
                          injected: str, inferred: str, final: str,
                          rec: Dict[str, Any]) -> None:
    """记录归属冲突 / 越界，便于事后追溯是谁在伪造租户。"""
    try:
        pg_exec(
            """INSERT INTO ingest_security_audit
               (collector_code, action, injected, inferred, final_tenant, host, src_file, sample)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                str((collector or {}).get("code") or ""), action,
                injected[:128], inferred[:128], final[:128],
                str(rec.get("host") or "")[:128],
                str(rec.get("src_file") or "")[:256],
                json.dumps(rec, ensure_ascii=False)[:1000],
            ),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[ingest] 安全审计写入失败: {e}", flush=True)


def derive_tenant(rec: Dict[str, Any],
                  collector: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """归属判定：服务端推导优先，采集端注入仅作补充，越界拒收。

    旧实现把采集端注入的 tenant_id 放第一优先级——任何持有 collector key 的人
    都能往任意租户写数据，索引隔离与行级过滤在这个口子下全部失效。现在改为：

      推导值 = K8s namespace 映射 > 主机台账(hosts) > 来源文件名
      注入值与推导值冲突 → 以推导为准 + 审计
      最终值不在 collector.tenant_allow 白名单 → 拒收该条 + 审计

    租户按业务系统划分：K8s 日志最可靠的来源是容器文件名里的 namespace（部署
    元数据，非正文），查 namespace_tenant 映射表定租户；平台自身组件日志靠 host
    台账（CMDB 雏形）归到 log-system；本地构造日志走文件名兜底（兼容 demo）。
    """
    injected = str(rec.get("tenant_id") or "").strip()
    inferred = ""
    src = str(rec.get("src_file") or "").strip()

    # 1) K8s 场景：从容器日志文件名解析 namespace，查映射表（最高优先级）
    ns = k8s_namespace_from_src(src)
    if ns:
        inferred = namespace_tenant_map().get(ns, "")

    # 2) 平台自身组件日志：host → hosts 台账（如 web/redis/postgres/kafka → log-system）
    host = str(rec.get("host") or "").strip()
    if not inferred and host:
        inferred = host_tenant_map().get(host, "")

    # 3) 兜底：来源文件名（本地 demo 的 acme.log / 非 K8s 单文件日志）
    if not inferred and src:
        base = os.path.basename(src)
        if base.endswith(".log"):
            inferred = base[: -len(".log")]

    final = injected or inferred
    action = "ok"
    if injected and inferred and injected != inferred:
        final, action = inferred, "mismatch"

    allow = set(str(x) for x in ((collector or {}).get("tenant_allow") or []))
    if allow and final and final not in allow:
        audit_tenant_decision("out_of_scope", collector, injected, inferred, "", rec)
        return "", "out_of_scope"
    if action == "mismatch":
        audit_tenant_decision("mismatch", collector, injected, inferred, final, rec)
    return final, action


@app.post("/ingest/collector")
def ingest_collector(
    records: List[Dict[str, Any]] = Body(...),
    collector: Dict[str, Any] = Depends(require_collector),
):
    """采集器批量上报（Fluent Bit 等）。每条记录按来源元数据判定租户。

    无法判定或租户不存在的记录统一落 logs-unknown，并在响应里如实计数，
    绝不静默归 default。鉴权由 require_collector 依赖前置完成。
    """
    if not isinstance(records, list) or not records:
        raise HTTPException(status_code=400, detail="records 不能为空")
    if len(records) > MAX_PAYLOAD:
        raise HTTPException(status_code=413, detail=f"单次上报超过 {MAX_PAYLOAD} 条，请减小采集批大小")

    known = {r["code"] for r in pg_query("SELECT code FROM tenants WHERE enabled")}
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    rejected = 0      # 越白名单，拒收且不入库（已写审计）
    mismatches = 0    # 注入值与推导冲突，已按推导纠正
    for rec in records:
        if not isinstance(rec, dict):
            rejected += 1
            continue
        tenant, action = derive_tenant(rec, collector)
        if action == "out_of_scope":
            rejected += 1
            continue
        if action == "mismatch":
            mismatches += 1
        if tenant not in known:
            tenant = ""   # 未归属 → logs-unknown
        # 按租户分桶而不是按索引名分桶：写入目标已改为按天索引 + 写别名，
        # 同一租户的不同日期索引不能拆成不同 Kafka key。
        buckets.setdefault(tenant, []).append(normalize_record(rec, tenant))

    # 服务端解析管线：默认 shadow 模式，只把新解析结果写进 attrs.__shadow，
    # 主字段仍走老链路。比对确认无误后再把 PIPELINE_MODE 切成 active。
    for tenant, docs in buckets.items():
        pipeline.process(docs, tenant)

    accepted = 0
    kafka_ok = 0
    os_ok = 0
    errors = []
    # 逐个租户写：OS 走该租户的写别名，Kafka 按租户 key 分区。
    for tenant, docs in buckets.items():
        kafka_tenant = tenant or "unknown"
        # 采集器一次 flush 可能上千条：按 MAX_BATCH 分片，避免整个 chunk 因单批过大被拒后
        # 无限重试（采集链路上最常见的数据丢失点）。
        for i in range(0, len(docs), MAX_BATCH):
            part = docs[i:i + MAX_BATCH]
            if kafka_writer.use_opensearch():
                try:
                    idx = index_manager.ensure_write_target(os_client, tenant)
                    helpers.bulk(os_client, [{"_index": idx, "_source": d} for d in part])
                    os_ok += len(part)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"opensearch[{kafka_tenant}]: {e}")
            if kafka_writer.use_kafka():
                try:
                    kafka_ok += kafka_writer.publish(part, kafka_tenant)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"kafka[{kafka_tenant}]: {e}")
            accepted += len(part)

    if errors and os_ok == 0 and kafka_ok == 0:
        raise HTTPException(status_code=502, detail="; ".join(errors[:5]))

    # buckets 已改为按租户编码分桶，未归属的 key 是空串（写入时才映射 logs-unknown）
    unassigned = len(buckets.get("", []))
    if unassigned:
        print(f"[ingest-service] 采集器上报中 {unassigned} 条无法归属，已写入 logs-unknown", flush=True)
    return {
        "ok": os_ok > 0 or kafka_ok > 0,
        "accepted": accepted,
        "rejected": rejected,
        "tenantMismatches": mismatches,
        "written": {"opensearch": os_ok, "kafka": kafka_ok},
        "storageMode": kafka_writer.mode(),
        "byTenant": {k or "unknown": len(v) for k, v in buckets.items()},
        "unassigned": unassigned,
        "message": (
            "已按服务端推导的租户写入"
            + (f"，其中 {unassigned} 条未归属进 logs-unknown" if unassigned else "")
            + (f"，{mismatches} 条注入租户与推导冲突已纠正" if mismatches else "")
            + (f"，{rejected} 条越出采集器租户白名单被拒收" if rejected else "")
        ),
    }
