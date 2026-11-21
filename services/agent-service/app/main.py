"""agent-service —— 采集端（Fluent Bit）管控面后端。

对应原型 DAT-0502（prototype/agents.html、specs/P-DAT-0502.json）。
技术路线见 docs/tech-stack-selection.md 3.2(8) / 9.1：业务主机只部署单一 Fluent Bit 进程，
管控客户端是随包脚本（exec 插件调起）；本服务负责主机元数据、心跳超时、指令队列、升级编排。

「按已有实际接入情况开发」的具体含义：
- 当前采集端是容器 fluentbit，用原生 dummy + http 输出每 30s 上报一次心跳（distroless 镜像
  无 exec 插件，见 fluent-bit.conf 注释）。心跳里 ts 是写死的占位串，因此**服务端一律以接收
  时刻为准**，客户端 ts 只作原样留档，不用于状态判定（防止客户端时钟漂移 / 占位值污染）。
- 心跳写 Redis（最近心跳、累计次数、最近间隔）+ PG（自动注册主机元数据）。
- 采集速率 / 采集覆盖不是 Agent 自报（当前心跳不带吞吐），而是由本服务对 OpenSearch 真实日志
  聚合反推，并在返回里明确标注口径；每台 Agent 可配置自己负责的 host/source 集合，上线后
  一机一源、一机多源、多机一源都能对上。
- 重启 / 批量升级是**真实指令队列**：服务端落库并随心跳响应下发，进度按「已下发 / 已确认」
  两段统计。当前采集端不消费 HTTP 响应体、也不回传 ack，因此确认进度会停在 0 —— 这是真实
  状态而非假进度；/commands/{id}/ack 已备好，接入随包管控脚本后自动推进。

字段不写死：按索引真实 mapping 探测 时间/来源/主机 角色，可按 Agent 用 field_map 覆盖。
"""
import os
import json
import hashlib
import secrets
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Literal, Optional

import httpx
import psycopg2
import psycopg2.extras
import redis
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from opensearchpy import OpenSearch
from pydantic import BaseModel, Field

from . import agent_package as pkg
from .authz import current_identity, is_admin, resolve_tenant

# ---------------------------------------------------------------------------
# 连接（惰性；模块导入不触网）
# ---------------------------------------------------------------------------
OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "opensearch")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))
# 认证默认关闭；云上开安全插件时由环境变量打开，不设账号则保持原来的无认证行为。
OPENSEARCH_SCHEME = os.getenv("OPENSEARCH_SCHEME", "http")
OPENSEARCH_USER = os.getenv("OPENSEARCH_USER", "")
OPENSEARCH_PASSWORD = os.getenv("OPENSEARCH_PASSWORD", "")
OPENSEARCH_VERIFY_CERTS = os.getenv("OPENSEARCH_VERIFY_CERTS", "false").lower() == "true"
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
PG_DSN = os.getenv(
    "AGENT_PG_DSN",
    f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
    f"user={os.getenv('POSTGRES_USER', 'loguser')} "
    f"password={os.getenv('POSTGRES_PASSWORD', 'logpass')} "
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')}",
)
FLUENTBIT_VERSION_HINT = os.getenv("FLUENTBIT_IMAGE_VERSION", "5.2.0")
# 接入包生成时要把目标主机写进 hosts 台账（ingest-service 持有该表）。
# 走集群内 service DNS；部署到集群外时用环境变量覆盖。
AGENT_INGEST_URL = os.getenv("AGENT_INGEST_URL", "http://ingest-service:8000").rstrip("/")

def _opensearch_kwargs():
    kw = {
        "hosts": [{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
        "use_ssl": OPENSEARCH_SCHEME == "https",
        "verify_certs": OPENSEARCH_VERIFY_CERTS,
    }
    if OPENSEARCH_USER and OPENSEARCH_PASSWORD:
        kw["http_auth"] = (OPENSEARCH_USER, OPENSEARCH_PASSWORD)
    return kw


os_client = OpenSearch(**_opensearch_kwargs())
rds = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True,
                  socket_connect_timeout=2, socket_timeout=2)

CST = timezone(timedelta(hours=8))
DEFAULT_TENANT = "i6000"            # 心跳未自报租户时的历史默认值（现有采集端均未传）
DEFAULT_TIMEOUT_SEC = 180           # 心跳超过 3 分钟标记离线（原型口径）
STATUS_RANK = {"离线": 0, "异常": 1, "告警中": 2, "维护中": 3, "运行中": 4}
ROLE_CANDIDATES = {
    "time": ["@timestamp", "timestamp", "time", "ts", "event_time", "log_time"],
    "source": ["source", "service", "service_name", "app", "app_name", "logger", "component"],
    "host": ["host", "hostname", "host_name", "node", "instance"],
}

app = FastAPI(title="agent-service", version="0.5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def now_ms() -> int:
    return int(time.time() * 1000)


def to_local(ts) -> Optional[str]:
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


# ---------------------------------------------------------------------------
# PostgreSQL
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


def init_db() -> None:
    try:
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id SERIAL PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'i6000',
                hostname TEXT NOT NULL,
                ip TEXT,                       -- 人工登记值，优先展示
                observed_ip TEXT,              -- 服务端观测到的心跳来源地址（自动，不覆盖人工值）
                version TEXT,
                region TEXT,
                os TEXT,
                status_override TEXT,
                heartbeat_timeout_sec INTEGER,
                host_values JSONB NOT NULL DEFAULT '[]'::jsonb,
                source_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
                field_map JSONB NOT NULL DEFAULT '{}'::jsonb,
                config JSONB NOT NULL DEFAULT '{}'::jsonb,
                tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                extra JSONB NOT NULL DEFAULT '{}'::jsonb,
                note TEXT,
                heartbeat_count BIGINT NOT NULL DEFAULT 0,
                first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS agent_upgrade_tasks (
                id SERIAL PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'i6000',
                target_version TEXT NOT NULL,
                concurrency INTEGER NOT NULL DEFAULT 1,
                created_by TEXT DEFAULT 'system',
                status TEXT NOT NULL DEFAULT 'running',
                note TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ
            )
            """
        )
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS agent_commands (
                id SERIAL PRIMARY KEY,
                tenant_id TEXT NOT NULL DEFAULT 'i6000',
                hostname TEXT NOT NULL,
                type TEXT NOT NULL,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT 'pending',
                task_id INTEGER,
                detail TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                delivered_at TIMESTAMPTZ,
                acked_at TIMESTAMPTZ
            )
            """
        )
        # 老库平滑升级：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列
        pg_exec("ALTER TABLE agents ADD COLUMN IF NOT EXISTS observed_ip TEXT")
        pg_exec("ALTER TABLE agents ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'i6000'")
        pg_exec("ALTER TABLE agent_commands ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'i6000'")
        pg_exec("ALTER TABLE agent_upgrade_tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'i6000'")
        # 采集器凭证台账由 ingest-service 持有；这里确保它存在，接入包签发凭证
        # 不依赖 ingest-service 先启动（两边 DDL 必须保持一致，改表要同步）。
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS log_collectors (
                id           SERIAL PRIMARY KEY,
                code         TEXT NOT NULL UNIQUE,
                name         TEXT NOT NULL DEFAULT '',
                tenant_allow JSONB NOT NULL DEFAULT '[]'::jsonb,
                key_hash     TEXT NOT NULL DEFAULT '',
                last_seen    TIMESTAMPTZ,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # hostname 原来是全局唯一，多租户下必须放开；按约束定义匹配删除，避免硬编码约束名
        pg_exec(
            """
            DO $$
            DECLARE c record;
            BEGIN
                FOR c IN
                    SELECT conname FROM pg_constraint
                     WHERE conrelid = 'agents'::regclass AND contype = 'u'
                       AND pg_get_constraintdef(oid) = 'UNIQUE (hostname)'
                LOOP
                    EXECUTE format('ALTER TABLE agents DROP CONSTRAINT %%I', c.conname);
                END LOOP;
            END $$;
            """
        )
        pg_exec("CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_tenant_hostname ON agents(tenant_id, hostname)")
        pg_exec("CREATE INDEX IF NOT EXISTS idx_agent_commands_tenant_status ON agent_commands(tenant_id, hostname, status)")
        pg_exec("CREATE INDEX IF NOT EXISTS idx_agent_upgrade_tasks_tenant ON agent_upgrade_tasks(tenant_id, id DESC)")
    except Exception as e:  # noqa: BLE001
        print(f"[agent-service] init_db skipped: {e}", flush=True)


def row_to_agent(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "tenant": r.get("tenant_id") or DEFAULT_TENANT,
        "hostname": r["hostname"],
        "ip": r.get("ip") or "—",
        "observedIp": r.get("observed_ip"),
        "version": r.get("version") or "—",
        "region": r.get("region") or "—",
        "os": r.get("os"),
        "statusOverride": r.get("status_override"),
        "heartbeatTimeoutSec": r.get("heartbeat_timeout_sec"),
        "hostValues": r.get("host_values") or [],
        "sourceKeys": r.get("source_keys") or [],
        "fieldMap": r.get("field_map") or {},
        "config": r.get("config") or {},
        "tags": r.get("tags") or [],
        "extra": r.get("extra") or {},
        "note": r.get("note"),
        "heartbeatCount": r.get("heartbeat_count") or 0,
        "firstSeen": to_local(r.get("first_seen")),
        "lastSeen": to_local(r.get("last_seen")),
        "createdAt": to_local(r.get("created_at")),
        "updatedAt": to_local(r.get("updated_at")),
    }


# ---------------------------------------------------------------------------
# 采集器凭证 + 主机台账（接入包生成时使用）
# ---------------------------------------------------------------------------
def issue_collector(hostname: str, tenant: str) -> Dict[str, str]:
    """签发一把采集器凭证，落库时只存 hash。

    返回的明文只在这里出现一次（随下载响应头回给前端），库里查不回来——
    所以「撤销重发」是泄露后的唯一补救路径，不能靠"再查一次凭证"。
    code 带随机后缀，重复给同一台主机下载不会覆盖掉别人正在用的凭证。
    """
    code = f"agent-{pkg.safe_name(hostname).lower()[:40]}-{secrets.token_hex(3)}"
    key = "rk_" + secrets.token_urlsafe(24)
    pg_exec(
        """INSERT INTO log_collectors (code, name, tenant_allow, key_hash)
           VALUES (%s, %s, %s::jsonb, %s)""",
        (code, f"{hostname} 接入包", json.dumps([tenant], ensure_ascii=False),
         hashlib.sha256(key.encode("utf-8")).hexdigest()),
    )
    return {"code": code, "key": key}


def list_collectors() -> List[Dict[str, Any]]:
    rows = pg_query(
        """SELECT id, code, name, tenant_allow, last_seen, created_at
             FROM log_collectors ORDER BY id DESC"""
    )
    return [
        {
            "id": r["id"], "code": r["code"], "name": r["name"],
            "tenantAllow": r.get("tenant_allow") or [],
            "lastSeen": to_local(r.get("last_seen")),
            "createdAt": to_local(r.get("created_at")),
        }
        for r in rows
    ]


def revoke_collector(code: str) -> bool:
    """删除凭证。用 code 而不是明文：明文本来就不该被记录。"""
    rows = pg_query("SELECT 1 FROM log_collectors WHERE code=%s", (code,))
    if not rows:
        return False
    pg_exec("DELETE FROM log_collectors WHERE code=%s", (code,))
    return True


def register_host_ledger(host: str, tenant: str, authorization: Optional[str]) -> str:
    """把目标主机写进 ingest-service 的 hosts 台账。

    返回值只用于 README 与响应头（ASCII）：ok / not_admin / failed / skipped。
    台账是「采得到」到「落得进」之间最容易漏的一环：不登记，host 字段就归属
    不出来，日志会在入口被拒收。
    """
    if not authorization:
        return "failed"
    try:
        r = httpx.post(
            f"{AGENT_INGEST_URL}/hosts",
            json={"host": host, "tenant": tenant, "env": "prod",
                  "note": "由 Agent 管理 · 下载接入配置自动登记"},
            headers={"Authorization": authorization},
            timeout=4.0,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[agent-service] hosts 台账登记失败: {e}", flush=True)
        return "failed"
    if r.status_code == 200:
        return "ok"
    if r.status_code == 403:
        return "not_admin"
    print(f"[agent-service] hosts 台账登记被拒: {r.status_code} {r.text[:120]}", flush=True)
    return "failed"


# ---------------------------------------------------------------------------
# Redis 心跳（失败降级到 PG last_seen，绝不让心跳接口因缓存挂掉而失败）
# ---------------------------------------------------------------------------
def hb_key(tenant: str, hostname: str) -> str:
    # 主机名可跨租户重复，缓存键必须带租户，否则同名主机互相覆盖
    return f"logsys:agent:hb:{tenant}:{hostname}"


def hb_write(tenant: str, hostname: str, body: Dict[str, Any], server_ts: int,
             peer_ip: Optional[str]) -> Dict[str, Any]:
    rec = {
        "ts": server_ts,
        "version": body.get("version"),
        "ip": body.get("ip"),          # 采集端自报（当前 dummy 没有该字段）
        "peerIp": peer_ip,             # 服务端观测到的心跳来源地址
        "clientTs": body.get("ts"),
        "clientStatus": body.get("status"),
    }
    count_key = f"logsys:agent:count:{tenant}:{hostname}"
    recent_key = f"logsys:agent:recent:{tenant}:{hostname}"
    try:
        pipe = rds.pipeline()
        pipe.set(hb_key(tenant, hostname), json.dumps(rec))
        pipe.incr(count_key)
        pipe.lpush(recent_key, server_ts)
        pipe.ltrim(recent_key, 0, 19)
        pipe.expire(hb_key(tenant, hostname), 7 * 86400)
        pipe.expire(recent_key, 7 * 86400)
        pipe.execute()
        rec["redis"] = True
    except Exception as e:  # noqa: BLE001
        rec["redis"] = False
        rec["redisError"] = str(e)[:120]
    return rec


def hb_read(tenant: str, hostname: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"lastTs": None, "count": 0, "intervals": [], "redis": False}
    try:
        raw = rds.get(hb_key(tenant, hostname))
        if raw:
            out.update(json.loads(raw))
            out["lastTs"] = out.get("ts")
        out["count"] = int(rds.get(f"logsys:agent:count:{tenant}:{hostname}") or 0)
        ts = [int(x) for x in rds.lrange(f"logsys:agent:recent:{tenant}:{hostname}", 0, 19)]
        out["intervals"] = [
            round((ts[i] - ts[i + 1]) / 1000, 1) for i in range(min(len(ts) - 1, 9))
        ]
        out["redis"] = True
    except Exception as e:  # noqa: BLE001
        out["redisError"] = str(e)[:120]
    return out


def redis_ok() -> bool:
    try:
        return bool(rds.ping())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# OpenSearch：字段角色自适应 + 真实采集速率 / 覆盖
# ---------------------------------------------------------------------------
_fields_cache: Dict[str, str] = {}
_rate_cache: Dict[str, Dict[str, Any]] = {}


def index_fields() -> Dict[str, str]:
    if not _fields_cache:
        try:
            raw = os_client.indices.get_mapping(index="logs-*")
            for _name, body in (raw or {}).items():
                for f, meta in (((body.get("mappings") or {}).get("properties") or {})).items():
                    t = (meta or {}).get("type")
                    if t:
                        _fields_cache.setdefault(f, t)
                    for sub, smeta in ((meta or {}).get("fields") or {}).items():
                        if (smeta or {}).get("type"):
                            _fields_cache.setdefault(f"{f}.{sub}", smeta["type"])
        except Exception as e:  # noqa: BLE001
            print(f"[agent-service] get_mapping failed: {e}", flush=True)
    return _fields_cache


def roles_for(field_map: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    fields = index_fields()
    ov = field_map or {}
    roles: Dict[str, Optional[str]] = {}
    for role, candidates in ROLE_CANDIDATES.items():
        manual = (ov.get(role) or ov.get(f"{role}_field") or "").strip()
        if manual and manual in fields:
            roles[role] = manual
            continue
        found = next((c for c in candidates if c in fields and
                      (role != "time" or fields[c] == "date")), None)
        if not found and role == "time":
            found = next((f for f, t in fields.items() if t == "date" and "." not in f), None)
        roles[role] = found
    return roles


def agg_field(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    t = _fields_cache.get(name)
    if t is None:
        return None
    if t not in ("keyword", "date", "integer", "long", "ip", "boolean"):
        return f"{name}.keyword" if f"{name}.keyword" in _fields_cache else None
    return name


def log_stats(host_values: List[str], source_keys: List[str],
              field_map: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """按 Agent 负责范围（可空＝全部日志）反推真实速率与采集覆盖。"""
    roles = roles_for(field_map)
    tf = roles.get("time")
    hf = agg_field(roles.get("host"))
    sf = agg_field(roles.get("source"))
    ck = json.dumps([host_values, source_keys, tf, hf, sf], sort_keys=True)
    cached = _rate_cache.get(ck)
    if cached and now_ms() - cached["ts"] < 15_000:
        return cached["data"]
    empty = {"rate": 0.0, "last5min": 0, "last24h": 0, "hosts": {}, "sources": {},
             "note": "无可用的时间字段，无法反推采集速率"}
    if not tf:
        return empty

    def filt(gte: int) -> Dict[str, Any]:
        must: List[Dict[str, Any]] = [{"range": {tf: {"gte": gte, "lte": now_ms()}}}]
        if host_values and hf:
            must.append({"terms": {hf: host_values}})
        if source_keys and sf:
            must.append({"terms": {sf: source_keys}})
        return {"bool": {"must": must}}

    to = now_ms()
    aggs: Dict[str, Any] = {}
    if hf:
        aggs["hosts"] = {"terms": {"field": hf, "size": 20}}
    if sf:
        aggs["sources"] = {"terms": {"field": sf, "size": 20}}
    body = {"size": 0, "query": filt(to - 24 * 3600 * 1000), "aggs": aggs}
    try:
        r24 = os_client.search(index="logs-*", body=body)
        r5 = os_client.count(index="logs-*", body={"query": filt(to - 5 * 60 * 1000)})
        n5 = int(r5.get("count", 0))
        total = r24.get("hits", {}).get("total", {})
        n24 = total.get("value", 0) if isinstance(total, dict) else total or 0
        aggr = r24.get("aggregations", {})
        data = {
            "rate": round(n5 / 300, 2),
            "last5min": n5,
            "last24h": n24,
            "hosts": {b["key"]: b["doc_count"] for b in aggr.get("hosts", {}).get("buckets", [])},
            "sources": {b["key"]: b["doc_count"] for b in aggr.get("sources", {}).get("buckets", [])},
            "timeField": tf, "hostField": hf, "sourceField": sf,
            "scope": "按 host/source 过滤" if (host_values or source_keys) else "当前采集端覆盖全部日志",
            "note": "平台侧按 OpenSearch 近 5 分钟真实日志量 / 300s 反推，非 Agent 自报",
        }
    except Exception as e:  # noqa: BLE001
        data = dict(empty, note=f"OpenSearch 聚合失败：{str(e)[:160]}")
    _rate_cache[ck] = {"ts": now_ms(), "data": data}
    return data


def sample_logs(host_values: List[str], source_keys: List[str],
                field_map: Optional[Dict[str, Any]], size: int = 5) -> List[Dict[str, Any]]:
    roles = roles_for(field_map)
    tf = roles.get("time")
    if not tf:
        return []
    hf, sf = agg_field(roles.get("host")), agg_field(roles.get("source"))
    must: List[Dict[str, Any]] = [{"range": {tf: {"gte": now_ms() - 24 * 3600 * 1000}}}]
    if host_values and hf:
        must.append({"terms": {hf: host_values}})
    if source_keys and sf:
        must.append({"terms": {sf: source_keys}})
    try:
        r = os_client.search(index="logs-*", body={
            "size": size, "query": {"bool": {"must": must}},
            "sort": [{tf: {"order": "desc"}}],
        })
        return [{"time": to_local(h["_source"].get(tf)),
                 "raw": h["_source"].get("raw") or json.dumps(h["_source"], ensure_ascii=False),
                 "source": h["_source"]}
                for h in r.get("hits", {}).get("hits", [])]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 主机状态合并
# ---------------------------------------------------------------------------
def resolve_ip(a: Dict[str, Any], hb: Dict[str, Any]) -> Dict[str, Any]:
    """IP 三级来源：人工登记 > 采集端自报 > 服务端观测。

    人工值永不被自动值覆盖；自动值随每次心跳更新（Redis 实时 + PG observed_ip 持久兜底），
    容器重建 / Agent 换 IP 能自动跟上，无需重新登记。
    """
    manual = a["ip"] if a["ip"] != "—" else None
    reported = hb.get("ip")
    observed = hb.get("peerIp") or a.get("observedIp")
    if manual:
        return {"ip": manual, "ipSource": "人工登记", "observedIp": observed}
    if reported:
        return {"ip": reported, "ipSource": "Agent 自报", "observedIp": observed}
    if observed:
        return {"ip": observed, "ipSource": "服务端观测", "observedIp": observed}
    return {"ip": "—", "ipSource": None, "observedIp": None}


def derive_status(row: Dict[str, Any], hb: Dict[str, Any]) -> Dict[str, Any]:
    a = row_to_agent(row)
    override = a["statusOverride"]
    last_ts = hb.get("lastTs")
    if not last_ts and a["lastSeen"]:
        # Redis 不可用/重启后降级：用 PG 记录近似（转回 epoch ms）
        try:
            last_ts = int(datetime.strptime(a["lastSeen"], "%Y-%m-%d %H:%M:%S")
                          .replace(tzinfo=CST).timestamp() * 1000)
        except Exception:  # noqa: BLE001
            last_ts = None
    gap = (now_ms() - last_ts) / 1000 if last_ts else None
    timeout = a["heartbeatTimeoutSec"] or DEFAULT_TIMEOUT_SEC
    intervals = [x for x in hb.get("intervals", []) if x > 0]
    avg_interval = round(sum(intervals) / len(intervals), 1) if intervals else None

    if override:
        label, reason = ("维护中", f"人工置为「{override}」")
    elif gap is None:
        label, reason = "离线", "从未收到心跳"
    elif gap > timeout:
        label, reason = "离线", f"已 {round(gap / 60, 1)} 分钟无心跳（阈值 {timeout // 60} 分钟）"
    elif avg_interval and avg_interval > 90:
        label, reason = "告警中", f"心跳间隔偏大，近 {len(intervals)} 次平均 {avg_interval}s"
    else:
        label, reason = "运行中", f"{round(gap, 1)} 秒前收到心跳"
    return {"status": label, "reason": reason, "statusReason": reason,
            "gapSec": round(gap, 1) if gap is not None else None,
            "timeoutSec": timeout, "avgIntervalSec": avg_interval,
            "heartbeatCount": hb.get("count") or a["heartbeatCount"]}


def build_agents(tenant: str) -> Dict[str, Any]:
    try:
        if tenant == "all":
            rows = pg_query("SELECT * FROM agents ORDER BY tenant_id, hostname")
        else:
            rows = pg_query("SELECT * FROM agents WHERE tenant_id=%s ORDER BY hostname", (tenant,))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"PostgreSQL 不可用：{e}")
    items: List[Dict[str, Any]] = []
    for r in rows:
        a = row_to_agent(r)
        hb = hb_read(a["tenant"], a["hostname"])
        st = derive_status(r, hb)
        ls = log_stats(a["hostValues"], a["sourceKeys"], a["fieldMap"])
        items.append({
            **a, **st, **resolve_ip(a, hb),
            "lastHeartbeat": to_local(hb.get("lastTs")) or a["lastSeen"],
            "lastHeartbeatTs": hb.get("lastTs"),
            "clientTs": hb.get("clientTs"),
            "redis": hb.get("redis", False),
            "rate": ls["rate"],
            "last5min": ls["last5min"],
            "last24h": ls["last24h"],
            "coverageHosts": ls["hosts"],
            "coverageSources": ls["sources"],
            "rateNote": ls["note"],
            "rateScope": ls["scope"],
            "roles": roles_for(a["fieldMap"]),
        })
    items.sort(key=lambda x: (STATUS_RANK.get(x["status"], 9), x["hostname"]))
    stats = {
        "total": len(items),
        "online": sum(1 for x in items if x["status"] == "运行中"),
        "offline": sum(1 for x in items if x["status"] == "离线"),
        "warning": sum(1 for x in items if x["status"] == "告警中"),
        "maintain": sum(1 for x in items if x["status"] == "维护中"),
        "totalRate": round(sum(x["rate"] for x in items), 2),
    }
    return {"items": items, "stats": stats,
            "redis": redis_ok(),
            "updatedAt": to_local(now_ms()),
            "timeoutDefault": DEFAULT_TIMEOUT_SEC}


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class AgentUpdate(BaseModel):
    ip: Optional[str] = None
    version: Optional[str] = None
    region: Optional[str] = None
    os: Optional[str] = None
    status_override: Optional[str] = None
    heartbeat_timeout_sec: Optional[int] = None
    host_values: List[str] = []
    source_keys: List[str] = []
    field_map: Dict[str, Any] = {}
    config: Dict[str, Any] = {}
    tags: List[str] = []
    extra: Dict[str, Any] = {}
    note: Optional[str] = None


class CommandIn(BaseModel):
    type: str = "restart"       # restart / reload / upgrade
    payload: Dict[str, Any] = {}
    note: Optional[str] = None


class UpgradeIn(BaseModel):
    hostnames: List[str] = []
    target_version: str = Field(..., min_length=1)
    concurrency: int = Field(1, ge=1, le=50)
    created_by: str = "黄金金"
    note: Optional[str] = None
    skip_offline: bool = True


class AckIn(BaseModel):
    status: str = "acked"       # acked / failed
    detail: Optional[str] = None


class PackageIn(BaseModel):
    """接入包生成参数。默认值即「集群内采集器」形态。"""

    hostname: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    variant: Literal["host", "container"] = "host"
    ingest_host: str = "ingest-service"
    ingest_port: int = Field(8000, ge=1, le=65535)
    agent_host: str = "agent-service"
    agent_port: int = Field(8000, ge=1, le=65535)
    ingest_uri: str = "/ingest/collector"
    # Record 值以空格分隔，应用名/集群 ID 里出现空白会把配置拆坏
    app_name: str = Field("", max_length=64, pattern=r"^[^\s\"']*$")
    cluster_id: str = Field("default", max_length=64, pattern=r"^[^\s\"']*$")
    app_log_json: str = "/var/log/app/*.json"
    app_log_text: str = "/var/log/app/*.log"
    syslog_path: str = "/var/log/syslog"
    nginx_access_path: str = "/var/log/nginx/access.log"
    nginx_error_path: str = "/var/log/nginx/error.log"
    container_log_glob: str = "/var/log/containers/*.log"
    multiline: str = "ruilog-java"
    credential: Literal["create", "existing", "none"] = "create"
    collector_key: Optional[str] = None
    register_host: bool = True
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "agent-service"}


@app.get("/")
def root():
    return {"service": "agent-service", "docs": "/docs",
            "endpoints": ["/heartbeat", "/agents", "/agents/{hostname}",
                          "/upgrade-tasks", "/commands/{id}/ack",
                          "/install-package", "/install-config",
                          "/collector-credentials"]}


@app.on_event("startup")
def _startup():
    init_db()


@app.post("/heartbeat")
async def heartbeat(req: Request):
    """采集端定时上报。服务端接收时刻为准；返回待执行指令供管控脚本消费。"""
    body = await req.json()
    if isinstance(body, list):
        body = body[0] if body else {}
    if not isinstance(body, dict):
        body = {}
    hostname = str(body.get("hostname") or "unknown").strip()
    server_ts = now_ms()
    degraded: List[str] = []
    # 采集端无 JWT：租户取心跳 body；未传则用历史默认租户（现有采集端都没传，不能因此拒收）。
    # 显式传了但租户不存在/停用则直接 400，避免静默归到 i6000 掩盖配置错误。
    raw_tenant = str(body.get("tenant") or "").strip()
    tenant = raw_tenant or DEFAULT_TENANT
    if raw_tenant:
        try:
            if not pg_query("SELECT 1 FROM tenants WHERE code=%s AND enabled", (raw_tenant,)):
                raise HTTPException(status_code=400, detail=f"租户「{raw_tenant}」不存在或已停用")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            degraded.append(f"tenant-check: {str(e)[:120]}")
    # 当前 dummy 心跳不带 IP：以服务端真实观测到的连接对端为准，不造数据、无需采集端配置。
    # 经反向代理部署时采信 X-Forwarded-For 首跳；直连时用 TCP peer。
    xff = req.headers.get("x-forwarded-for")
    peer_ip = (xff.split(",")[0].strip() if xff else None) or (req.client.host if req.client else None)
    rec = hb_write(tenant, hostname, body, server_ts, peer_ip)

    try:
        pg_exec(
            """
            INSERT INTO agents (tenant_id, hostname, observed_ip, version, os, first_seen, last_seen, heartbeat_count)
            VALUES (%s, %s, %s, %s, %s, now(), now(), 1)
            ON CONFLICT (tenant_id, hostname) DO NOTHING
            """,
            (tenant, hostname, peer_ip, body.get("version"), body.get("os")),
        )
        pg_exec(
            """
            UPDATE agents SET last_seen=now(), heartbeat_count=heartbeat_count+1,
                observed_ip=COALESCE(%s, observed_ip),
                version=COALESCE(%s, version), os=COALESCE(%s, os),
                updated_at=now()
            WHERE tenant_id=%s AND hostname=%s
            """,
            (peer_ip, body.get("version"), body.get("os"), tenant, hostname),
        )
    except Exception as e:  # noqa: BLE001
        degraded.append(f"pg: {str(e)[:120]}")
    if not rec.get("redis"):
        degraded.append("redis: " + str(rec.get("redisError", "unavailable")))

    commands: List[Dict[str, Any]] = []
    try:
        pend = pg_query(
            "SELECT id, type, payload FROM agent_commands WHERE tenant_id=%s AND hostname=%s AND status IN ('pending','delivered') ORDER BY id LIMIT 5",
            (tenant, hostname),
        )
        commands = [{"id": c["id"], "type": c["type"], "payload": c["payload"] or {}} for c in pend]
        if pend:
            pg_exec("UPDATE agent_commands SET status='delivered', delivered_at=COALESCE(delivered_at, now()) WHERE id = ANY(%s::int[])",
                    ([c["id"] for c in pend],))
    except Exception as e:  # noqa: BLE001
        degraded.append(f"commands: {str(e)[:120]}")

    return {
        "ok": True, "hostname": hostname, "tenant": tenant,
        "serverTime": to_local(server_ts), "serverTs": server_ts,
        "clientTs": body.get("ts"),
        "observedIp": peer_ip,
        "configVersion": int(server_ts // 1000),
        "commands": commands,
        "degraded": degraded,
        "note": "心跳时间以服务端接收时刻为准；客户端 ts 仅留档（当前采集端为固定占位值）",
    }


@app.get("/agents")
def list_agents(
    keyword: str = Query("", description="主机名 / IP 模糊匹配"),
    version: str = Query(""),
    status: str = Query(""),
    region: str = Query(""),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    effective = resolve_tenant(identity, tenant)
    data = build_agents(effective)
    items = data["items"]
    kw = keyword.strip().lower()
    if kw:
        items = [x for x in items if kw in x["hostname"].lower() or kw in x["ip"].lower()]
    if version:
        items = [x for x in items if x["version"] == version]
    if status:
        items = [x for x in items if x["status"] == status]
    if region:
        items = [x for x in items if x["region"] == region]
    total = len(items)
    start = (page - 1) * pageSize
    return {"total": total, "page": page, "pageSize": pageSize,
            "items": items[start:start + pageSize], "tenant": effective,
            "stats": data["stats"], "redis": data["redis"],
            "updatedAt": data["updatedAt"], "timeoutDefault": data["timeoutDefault"],
            "fields": index_fields(), "fieldRoles": roles_for({}),
            "versions": sorted({x["version"] for x in data["items"] if x["version"] != "—"}),
            "regions": sorted({x["region"] for x in data["items"] if x["region"] != "—"})}


@app.get("/agents/{hostname}")
def agent_detail(
    hostname: str,
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    effective = resolve_tenant(identity, tenant)
    if effective == "all":
        raise HTTPException(status_code=400, detail="查看 Agent 详情需指定具体租户")
    rows = pg_query("SELECT * FROM agents WHERE tenant_id=%s AND hostname=%s", (effective, hostname))
    if not rows:
        raise HTTPException(status_code=404, detail="Agent 未注册")
    a = row_to_agent(rows[0])
    hb = hb_read(effective, hostname)
    st = derive_status(rows[0], hb)
    ls = log_stats(a["hostValues"], a["sourceKeys"], a["fieldMap"])
    cmds = pg_query(
        "SELECT * FROM agent_commands WHERE tenant_id=%s AND hostname=%s ORDER BY id DESC LIMIT 20",
        (effective, hostname),
    )
    return {
        "agent": {**a, **st, **resolve_ip(a, hb),
                  "lastHeartbeat": to_local(hb.get("lastTs")) or a["lastSeen"],
                  "clientTs": hb.get("clientTs")},
        "heartbeat": {"lastTs": hb.get("lastTs"), "count": hb.get("count"),
                      "intervals": hb.get("intervals", []), "redis": hb.get("redis"),
                      "clientTs": hb.get("clientTs"),
                      "note": "服务端接收时刻为准；clientTs 为采集端上报原值（当前是固定占位串）"},
        "logStats": ls,
        "samples": sample_logs(a["hostValues"], a["sourceKeys"], a["fieldMap"]),
        "commands": [
            {"id": c["id"], "type": c["type"], "payload": c["payload"], "status": c["status"],
             "taskId": c["task_id"], "detail": c["detail"],
             "createdAt": to_local(c["created_at"]), "deliveredAt": to_local(c["delivered_at"]),
             "ackedAt": to_local(c["acked_at"])}
            for c in cmds
        ],
        "fields": index_fields(),
        "roles": roles_for(a["fieldMap"]),
    }


@app.put("/agents/{hostname}")
def update_agent(
    hostname: str,
    payload: AgentUpdate,
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    effective = resolve_tenant(identity, tenant)
    if effective == "all":
        raise HTTPException(status_code=400, detail="更新 Agent 需指定具体租户")
    rows = pg_query("SELECT id FROM agents WHERE tenant_id=%s AND hostname=%s", (effective, hostname))
    if not rows:
        raise HTTPException(status_code=404, detail="Agent 未注册")
    pg_exec(
        """
        UPDATE agents SET ip=%s, version=%s, region=%s, os=%s, status_override=%s,
            heartbeat_timeout_sec=%s, host_values=%s, source_keys=%s, field_map=%s,
            config=%s, tags=%s, extra=%s, note=%s, updated_at=now()
        WHERE tenant_id=%s AND hostname=%s
        """,
        (payload.ip, payload.version, payload.region, payload.os, payload.status_override,
         payload.heartbeat_timeout_sec, psycopg2.extras.Json(payload.host_values),
         psycopg2.extras.Json(payload.source_keys), psycopg2.extras.Json(payload.field_map),
         psycopg2.extras.Json(payload.config), psycopg2.extras.Json(payload.tags),
         psycopg2.extras.Json(payload.extra), payload.note, effective, hostname),
    )
    _rate_cache.clear()
    return {"ok": True, "hostname": hostname}


@app.post("/agents/{hostname}/command")
def send_command(
    hostname: str,
    req: CommandIn,
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    """下发真实指令（进入指令队列，由下次心跳响应带出）。"""
    effective = resolve_tenant(identity, tenant)
    if effective == "all":
        raise HTTPException(status_code=400, detail="下发指令需指定具体租户")
    rows = pg_query("SELECT id FROM agents WHERE tenant_id=%s AND hostname=%s", (effective, hostname))
    if not rows:
        raise HTTPException(status_code=404, detail="Agent 未注册")
    row = pg_query(
        """
        INSERT INTO agent_commands (tenant_id, hostname, type, payload, detail)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (effective, hostname, req.type, psycopg2.extras.Json(req.payload), req.note),
    )
    return {"ok": True, "commandId": row[0]["id"], "status": "pending",
            "note": "指令已进入队列，将在该 Agent 下一次心跳的响应中下发；确认状态以 Agent 回传 ack 为准"}


@app.post("/agents/{hostname}/status")
def set_status(
    hostname: str,
    payload: Dict[str, Any],
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    effective = resolve_tenant(identity, tenant)
    if effective == "all":
        raise HTTPException(status_code=400, detail="变更 Agent 状态需指定具体租户")
    if not pg_query("SELECT id FROM agents WHERE tenant_id=%s AND hostname=%s", (effective, hostname)):
        raise HTTPException(status_code=404, detail="Agent 未注册")
    val = payload.get("status") or None      # 维护中 / None(恢复正常)
    pg_exec("UPDATE agents SET status_override=%s, updated_at=now() WHERE tenant_id=%s AND hostname=%s",
            (val, effective, hostname))
    return {"ok": True, "hostname": hostname, "status": val or "正常"}


@app.post("/commands/{command_id}/ack")
def ack_command(command_id: int, req: AckIn):
    """供随包管控脚本（或运维确认）回传执行结果。当前采集端尚未接入此接口。"""
    rows = pg_query("SELECT * FROM agent_commands WHERE id=%s", (command_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="指令不存在")
    status = "acked" if req.status == "acked" else "failed"
    pg_exec("UPDATE agent_commands SET status=%s, detail=%s, acked_at=now() WHERE id=%s",
            (status, req.detail, command_id))
    tid = rows[0].get("task_id")
    if tid:
        left = pg_query(
            "SELECT COUNT(*) c FROM agent_commands WHERE task_id=%s AND status NOT IN ('acked','failed')",
            (tid,),
        )
        if left and left[0]["c"] == 0:
            pg_exec("UPDATE agent_upgrade_tasks SET status='completed', finished_at=now() WHERE id=%s", (tid,))
    return {"ok": True, "commandId": command_id, "status": status}


# ------------------------------- 升级任务 -------------------------------
def task_progress(task: Dict[str, Any]) -> Dict[str, Any]:
    cmds = pg_query("SELECT * FROM agent_commands WHERE tenant_id=%s AND task_id=%s ORDER BY id",
                    (task["tenant_id"], task["id"]))
    total = len(cmds)
    delivered = sum(1 for c in cmds if c["status"] in ("delivered", "acked", "failed"))
    acked = sum(1 for c in cmds if c["status"] == "acked")
    failed = sum(1 for c in cmds if c["status"] == "failed")
    return {
        "id": task["id"], "tenant": task["tenant_id"], "targetVersion": task["target_version"],
        "concurrency": task["concurrency"], "createdBy": task.get("created_by"),
        "status": task["status"], "note": task.get("note"),
        "createdAt": to_local(task.get("created_at")),
        "finishedAt": to_local(task.get("finished_at")),
        "total": total, "delivered": delivered, "acked": acked, "failed": failed,
        "dispatchPct": round(delivered / total * 100) if total else 0,
        "ackPct": round(acked / total * 100) if total else 0,
        "commands": [
            {"id": c["id"], "hostname": c["hostname"], "status": c["status"],
             "createdAt": to_local(c["created_at"]), "ackedAt": to_local(c["acked_at"])}
            for c in cmds
        ],
    }


@app.get("/upgrade-tasks")
def list_upgrade_tasks(
    limit: int = Query(10, ge=1, le=100),
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    effective = resolve_tenant(identity, tenant)
    try:
        if effective == "all":
            rows = pg_query("SELECT * FROM agent_upgrade_tasks ORDER BY id DESC LIMIT %s", (limit,))
        else:
            rows = pg_query(
                "SELECT * FROM agent_upgrade_tasks WHERE tenant_id=%s ORDER BY id DESC LIMIT %s",
                (effective, limit),
            )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"PostgreSQL 不可用：{e}")
    tasks = [task_progress(r) for r in rows]
    running = next((t for t in tasks if t["status"] == "running"), None)
    return {"items": tasks, "active": running, "total": len(tasks)}


@app.post("/upgrade-tasks", status_code=201)
def create_upgrade_task(
    req: UpgradeIn,
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    """创建真实滚动升级任务：逐台生成指令，离线主机可跳过。"""
    effective = resolve_tenant(identity, tenant)
    if effective == "all":
        raise HTTPException(status_code=400, detail="创建升级任务需指定具体租户")
    agents = build_agents(effective)["items"]
    selected = [a for a in agents if (not req.hostnames or a["hostname"] in req.hostnames)]
    if req.skip_offline:
        skipped = [a["hostname"] for a in selected if a["status"] == "离线"]
        selected = [a for a in selected if a["status"] != "离线"]
    else:
        skipped = []
    if not selected:
        raise HTTPException(status_code=400, detail="没有可升级的在线 Agent")
    tid = pg_query(
        """
        INSERT INTO agent_upgrade_tasks (tenant_id, target_version, concurrency, created_by, status, note)
        VALUES (%s, %s, %s, %s, 'running', %s) RETURNING id
        """,
        (effective, req.target_version, req.concurrency, req.created_by, req.note),
    )[0]["id"]
    for a in selected:
        pg_exec(
            """
            INSERT INTO agent_commands (tenant_id, hostname, type, payload, task_id, detail)
            VALUES (%s, %s, 'upgrade', %s, %s, %s)
            """,
            (effective, a["hostname"], psycopg2.extras.Json({
                "version": req.target_version, "strategy": "rolling",
                "resume": True, "currentVersion": a["version"],
            }), tid, req.note),
        )
    return {"ok": True, "taskId": tid, "targets": len(selected), "skipped": skipped,
            "note": "指令已入库；下次心跳会带出。当前采集端为原生 dummy 心跳，不回传 ack，确认进度需接入管控脚本后推进"}


@app.post("/upgrade-tasks/{task_id}/cancel")
def cancel_upgrade_task(
    task_id: int,
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    effective = resolve_tenant(identity, tenant)
    if effective == "all":
        raise HTTPException(status_code=400, detail="取消升级任务需指定具体租户")
    rows = pg_query(
        "UPDATE agent_upgrade_tasks SET status='cancelled', finished_at=now() WHERE id=%s AND tenant_id=%s RETURNING id",
        (task_id, effective),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="升级任务不存在")
    pg_exec("UPDATE agent_commands SET status='failed', detail='任务已取消' WHERE tenant_id=%s AND task_id=%s AND status IN ('pending','delivered')",
            (effective, task_id))
    return {"ok": True, "taskId": task_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# 接入包（v1 打包 / v2 双形态 / v3 凭证与台账）
# ---------------------------------------------------------------------------
def _endpoint_host(value: str, field: str) -> str:
    """只接受主机名或 IP。

    Fluent Bit 的 Host 字段放不下 ``http://`` 或路径：整段会被当成主机名，
    症状是 DNS 解析失败且日志里看不出原因。这里前置拦掉，并给出人话提示。
    """
    host = (value or "").strip()
    if not host or "://" in host or any(c in host for c in "/ \t"):
        raise HTTPException(
            status_code=400,
            detail=f"{field} 只填主机名或 IP（不要带 http:// 或路径）",
        )
    return host


def _safe_credential(key: str) -> str:
    """凭证只能由 ASCII 可见字符组成。

    非 ASCII 的 header 会让网关直接报 500（上一版中文占位符踩过）；
    换行则会破坏 agent.env 的一行一值结构。两者都必须前置拦掉。
    """
    key = (key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="使用已有凭证时 collector_key 不能为空")
    if any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise HTTPException(status_code=400, detail="采集器凭证只能是 ASCII 可见字符")
    return key


def _build_params(payload: PackageIn, tenant: str, generated_at: str) -> pkg.PackageParams:
    if payload.variant == "container" and payload.multiline not in (
            "ruilog-java", "ruilog-go", "ruilog-python", "none"):
        raise HTTPException(status_code=400, detail="multiline 只支持 ruilog-java / ruilog-go / ruilog-python / none")
    uri = payload.ingest_uri.strip() or "/ingest/collector"
    if not uri.startswith("/") or " " in uri:
        raise HTTPException(status_code=400, detail="上报路径必须以 / 开头且不含空格")
    return pkg.PackageParams(
        hostname=payload.hostname.strip(),
        tenant=tenant,
        variant=payload.variant,
        ingest_host=_endpoint_host(payload.ingest_host, "平台接入地址"),
        ingest_port=payload.ingest_port,
        agent_host=_endpoint_host(payload.agent_host, "管控心跳地址"),
        agent_port=payload.agent_port,
        ingest_uri=uri,
        app_name=(payload.app_name or "").strip(),
        cluster_id=(payload.cluster_id or "").strip() or "default",
        app_log_json=(payload.app_log_json or "").strip() or "/var/log/app/*.json",
        app_log_text=(payload.app_log_text or "").strip() or "/var/log/app/*.log",
        syslog_path=(payload.syslog_path or "").strip() or "/var/log/syslog",
        nginx_access_path=(payload.nginx_access_path or "").strip() or "/var/log/nginx/access.log",
        nginx_error_path=(payload.nginx_error_path or "").strip() or "/var/log/nginx/error.log",
        container_log_glob=(payload.container_log_glob or "").strip() or "/var/log/containers/*.log",
        multiline=payload.multiline,
        generated_at=generated_at,
        version=FLUENTBIT_VERSION_HINT,
    )


@app.post("/install-package")
def install_package(
    payload: PackageIn,
    request: Request,
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    """生成可落地的接入包（tar.gz），并按需签发凭证、登记主机台账。

    响应头里带本次生成结果（ASCII，前端读取后展示）：
      X-Collector-Code   凭证编号（签发时才有）
      X-Collector-Key    凭证明文（仅本次签发时返回，库里只有 hash）
      X-Host-Ledger      ok / skipped / failed / not_admin
    """
    effective = resolve_tenant(identity, tenant)
    if effective == "all":
        raise HTTPException(status_code=400, detail="生成接入包需指定具体租户")

    hostname = payload.hostname.strip()
    if not hostname or any(c in hostname for c in "\"\\\n\r"):
        raise HTTPException(status_code=400, detail="主机名不能为空，且不能包含引号或换行")

    params = _build_params(payload, effective, to_local(now_ms()))
    warnings: List[str] = []
    code: Optional[str] = None
    key: Optional[str] = None
    issued = False

    if payload.credential == "create":
        cred = issue_collector(hostname, effective)
        code, key, issued = cred["code"], cred["key"], True
    elif payload.credential == "existing":
        key = _safe_credential(payload.collector_key or "")
    else:
        key = "REPLACE_ME_WITH_COLLECTOR_KEY"
        warnings.append("本包未携带有效凭证，必须替换 agent.env 里的 REPLACE_ME_WITH_COLLECTOR_KEY 才能上报")

    ledger = "skipped"
    if payload.variant == "host" and payload.register_host:
        ledger = register_host_ledger(hostname, effective,
                                      request.headers.get("authorization"))
        if ledger == "failed":
            warnings.append("主机台账登记失败：日志仍会被入口拒收，请管理员在数据源页补登记")
        elif ledger == "not_admin":
            warnings.append("当前账号无台账维护权限：请管理员登记该主机，否则日志会被拒收")
    elif payload.variant == "host":
        warnings.append("未登记主机台账：日志文件名必须为 <租户code>.log 才能归属")

    params.collector_key = key or "REPLACE_ME_WITH_COLLECTOR_KEY"
    params.collector_code = code
    params.host_ledger = ledger
    params.extra_warnings = warnings

    try:
        blob = pkg.build_package(params)
    except Exception:
        # 打包失败时把刚签发的凭证撤掉，避免留下没被任何采集器使用的孤儿凭证
        if issued and code:
            revoke_collector(code)
        raise

    headers = {
        "Content-Disposition": f'attachment; filename="{pkg.package_name(params)}"',
        "X-Host-Ledger": ledger,
        "X-Package-Variant": params.variant,
    }
    if code:
        headers["X-Collector-Code"] = code
    if issued and key:
        headers["X-Collector-Key"] = key
    return Response(content=blob, media_type="application/gzip", headers=headers)


@app.get("/install-config")
def install_config(
    hostname: str = Query("new-host"),
    tenant: Optional[str] = Query(None),
    identity: Dict[str, Any] = Depends(current_identity),
):
    """兼容旧调用：返回单文件 Fluent Bit 配置（不含凭证）。

    新流程请用 ``POST /install-package``：单文件不携带 parsers.conf 与凭证，
    在目标机上仍需手工补齐，只适合排障时快速看一版配置。
    """
    effective = resolve_tenant(identity, tenant)
    if effective == "all":
        raise HTTPException(status_code=400, detail="生成安装配置需指定具体租户")
    params = _build_params(
        PackageIn(hostname=hostname, variant="host", credential="none",
                  register_host=False, app_name=""),
        effective, to_local(now_ms()),
    )
    params.extra_warnings = ["单文件模式不含 parsers.conf 与凭证；正式接入请下载接入包"]
    text = pkg.render_conf(params)
    fname = f"fluent-bit-{pkg.safe_name(hostname)}.conf"
    return Response(
        content=text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/collector-credentials")
def collector_credentials(identity: Dict[str, Any] = Depends(current_identity)):
    """凭证台账（只列 hash 之外的信息，明文永远查不回来）。"""
    if not is_admin(identity):
        raise HTTPException(status_code=403, detail="仅平台管理员可查看采集器凭证台账")
    return {"items": list_collectors()}


@app.delete("/collector-credentials/{code}")
def remove_collector_credential(code: str, identity: Dict[str, Any] = Depends(current_identity)):
    if not is_admin(identity):
        raise HTTPException(status_code=403, detail="仅平台管理员可撤销采集器凭证")
    if not revoke_collector(code):
        raise HTTPException(status_code=404, detail="凭证不存在或已撤销")
    return {"ok": True, "code": code, "message": "凭证已撤销，使用它的采集器会立即 401"}
