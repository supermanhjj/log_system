"""自监测总览：组件健康、采集链路诊断、全链路拓扑。

数据原则：全部来自真实探活、PG 台账与 OpenSearch 聚合，不做前端假状态。
"""
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from . import db as db_mod
from .audit import write_audit
from .db import pg_query, pg_scalar, to_cst_str
from .security import client_ip, require

router = APIRouter(prefix="/monitor", tags=["monitor"])

CST = timezone(timedelta(hours=8))
STATUS_RANK = {"ok": 0, "warn": 1, "fail": 2, "unknown": 3}
STATUS_LABEL = {"ok": "正常", "warn": "警告", "fail": "异常", "unknown": "未知"}

OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://opensearch:9200")
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "kafka:9092")
CLICKHOUSE_HTTP_URL = os.getenv("CLICKHOUSE_HTTP_URL", "http://clickhouse:8123")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
FLINK_JOBMANAGER = os.getenv("FLINK_JOBMANAGER", "flink-jobmanager:8081")

SERVICES = [
    ("web", "Web 网关", "接入层", "http://web:80/", "nginx", True),
    ("auth", "认证服务", "接入层", "http://auth-service:8000/health", "service", True),
    ("system", "平台管理", "接入层", "http://system-service:8000/health", "service", True),
    ("ingest", "接入网关", "接入层", "http://ingest-service:8000/health", "service", True),
    ("agent", "Agent 管控", "接入层", "http://agent-service:8000/health", "service", True),
    ("search", "检索服务", "查询层", "http://search-service:8000/health", "service", True),
    ("dashboard", "看板服务", "查询层", "http://dashboard-service:8000/health", "service", True),
    ("alarm", "告警服务", "查询层", "http://alarm-service:8000/health", "service", True),
    ("ai", "智能服务", "查询层", "http://ai-service:8000/health", "service", False),
]


def _os_auth() -> Optional[tuple]:
    user = os.getenv("OPENSEARCH_USER", "")
    pwd = os.getenv("OPENSEARCH_PASSWORD", "")
    return (user, pwd) if user and pwd else None


def _os_headers() -> Dict[str, str]:
    return {"Content-Type": "application/json"}


def _http_json(url: str, headers: Optional[Dict[str, str]] = None,
               auth: Optional[tuple] = None, timeout: float = 2.0):
    t0 = time.perf_counter()
    try:
        r = httpx.get(url, headers=headers, auth=auth, timeout=timeout, follow_redirects=False)
        latency = round((time.perf_counter() - t0) * 1000, 1)
        payload = None
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001
            payload = r.text[:120] if r.text else ""
        return {"ok": r.status_code < 500, "code": r.status_code,
                "latencyMs": latency, "payload": payload, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": None,
                "latencyMs": round((time.perf_counter() - t0) * 1000, 1),
                "payload": None, "error": f"{type(e).__name__}: {e}"[:180]}


def _tcp_check(host: str, port: int) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=2):
            pass
        return {"ok": True, "latencyMs": round((time.perf_counter() - t0) * 1000, 1), "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "latencyMs": round((time.perf_counter() - t0) * 1000, 1),
                "error": f"{type(e).__name__}: {e}"[:180]}


def _service_item(item) -> Dict[str, Any]:
    key, name, group, url, kind, required = item
    if key == "system":
        # 自身探活不绕一圈 HTTP，避免自监测接口被自己的请求拖住。
        if db_mod.INIT_DB_ERROR:
            return _item(key, name, group, "fail", f"数据库初始化失败：{db_mod.INIT_DB_ERROR}", 0, kind)
        return _item(key, name, group, "ok", "平台管理服务运行中", 0, kind)
    res = _http_json(url, timeout=2.0)
    if not res["ok"]:
        status = "fail" if required else "warn"
        detail = "探活失败：" + (res['error'] or ('HTTP ' + str(res['code'] or '')))
    elif res["code"] != 200:
        status = "warn"
        detail = f"HTTP {res['code']}"
    else:
        payload = res["payload"] if isinstance(res["payload"], dict) else {}
        status = "warn" if payload.get("status") == "degraded" else "ok"
        detail = payload.get("status") or "服务运行中"
        if payload.get("storageMode"):
            detail += f"；写入模式 {payload['storageMode']}"
    return _item(key, name, group, status, detail, res["latencyMs"], kind)


def _item(key, name, group, status, detail, latency, kind, **extra) -> Dict[str, Any]:
    return {
        "id": key, "name": name, "group": group,
        "status": status, "statusText": STATUS_LABEL.get(status, status),
        "detail": detail, "latencyMs": latency, "kind": kind,
        **extra,
    }


def _data_item(key, name, group, status, detail, latency, **extra) -> Dict[str, Any]:
    return _item(key, name, group, status, detail, latency, "data", **extra)


def _check_pg() -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        pg_query("SELECT 1")
        return _data_item("pg", "PostgreSQL", "数据层", "ok", "连接正常", round((time.perf_counter() - t0) * 1000, 1))
    except Exception as e:  # noqa: BLE001
        return _data_item("pg", "PostgreSQL", "数据层", "fail", f"连接失败：{str(e)[:160]}",
                          round((time.perf_counter() - t0) * 1000, 1))


def _check_os() -> Dict[str, Any]:
    res = _http_json(f"{OPENSEARCH_URL}/_cluster/health", auth=_os_auth(), timeout=2.5)
    if not res["ok"]:
        return _data_item("os", "OpenSearch", "数据层", "fail",
                          f"集群探活失败：{res['error'] or res['code']}", res["latencyMs"])
    payload = res["payload"] if isinstance(res["payload"], dict) else {}
    color = str(payload.get("status") or "").lower()
    status = {"green": "ok", "yellow": "warn", "red": "fail"}.get(color, "warn")
    detail = f"集群 {color or '未知'}，节点 {payload.get('number_of_nodes', '—')}，主分片 {payload.get('active_primary_shards', '—')}"
    return _data_item("os", "OpenSearch", "数据层", status, detail, res["latencyMs"], clusterStatus=color)


def _check_kafka() -> Dict[str, Any]:
    host, _, port = KAFKA_BROKER.partition(":")
    res = _tcp_check(host or "kafka", int(port or 9092))
    status = "ok" if res["ok"] else "fail"
    return _data_item("kafka", "Kafka", "数据层", status,
                      f"{KAFKA_BROKER} {'可达' if res['ok'] else '不可达'}", res["latencyMs"])


def _check_ch() -> Dict[str, Any]:
    user = os.getenv("CLICKHOUSE_USER", "ruilog")
    pwd = os.getenv("CLICKHOUSE_PASSWORD", "")
    res = _http_json(f"{CLICKHOUSE_HTTP_URL}/ping", auth=(user, pwd) if pwd else None, timeout=2.0)
    if not res["ok"]:
        return _data_item("ch", "ClickHouse", "数据层", "fail",
                          f"探活失败：{res['error'] or res['code']}", res["latencyMs"])
    ok = str(res["payload"]).strip().lower().startswith("ok") or res["code"] == 200
    return _data_item("ch", "ClickHouse", "数据层", "ok" if ok else "warn",
                      "HTTP 探活正常" if ok else f"返回 {res['payload']}", res["latencyMs"])


def _check_redis() -> Dict[str, Any]:
    res = _tcp_check(REDIS_HOST, REDIS_PORT)
    return _data_item("redis", "Redis", "数据层", "ok" if res["ok"] else "fail",
                      f"{REDIS_HOST}:{REDIS_PORT} {'可达' if res['ok'] else '不可达'}", res["latencyMs"])


def _check_flink() -> Dict[str, Any]:
    res = _http_json(f"http://{FLINK_JOBMANAGER}/overview", timeout=2.5)
    if not res["ok"]:
        return _data_item("flink", "Flink JobManager", "计算层", "warn",
                          f"探活失败：{res['error'] or res['code']}", res["latencyMs"])
    payload = res["payload"] if isinstance(res["payload"], dict) else {}
    running = payload.get("jobs-running", "—")
    slots = payload.get("slots-available", "—")
    return _data_item("flink", "Flink JobManager", "计算层", "ok",
                      f"运行作业 {running}，可用 slot {slots}", res["latencyMs"])


def collect_health() -> Dict[str, Any]:
    checks = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_service_item, item) for item in SERVICES]
        futures += [
            pool.submit(_check_pg), pool.submit(_check_os), pool.submit(_check_kafka),
            pool.submit(_check_ch), pool.submit(_check_redis), pool.submit(_check_flink),
        ]
        for f in futures:
            try:
                checks.append(f.result())
            except Exception as e:  # noqa: BLE001
                checks.append(_data_item("unknown", "未知组件", "未知", "unknown", str(e)[:160], 0))
    order = {"接入层": 1, "数据层": 2, "计算层": 3, "查询层": 4}
    checks.sort(key=lambda x: (order.get(x["group"], 9), x["name"]))
    summary = {
        "total": len(checks),
        "ok": sum(1 for c in checks if c["status"] == "ok"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "fail": sum(1 for c in checks if c["status"] == "fail"),
        "unknown": sum(1 for c in checks if c["status"] == "unknown"),
    }
    return {
        "items": checks,
        "summary": summary,
        "status": "fail" if summary["fail"] else ("warn" if summary["warn"] else "ok"),
        "updatedAt": to_cst_str(datetime.now(timezone.utc)),
    }


def _scope(tenant: Optional[str], user: Dict[str, Any], *, allow_all: bool = True) -> str:
    current = user.get("tenant_code") or ""
    if not tenant:
        return current
    if tenant == "all":
        if not allow_all or user.get("role_key") != "platform_admin":
            raise HTTPException(status_code=403, detail="只有平台管理员可以查看全部租户")
        return "all"
    if user.get("role_key") != "platform_admin" and tenant != current:
        raise HTTPException(status_code=403, detail="无权查看其他租户")
    return tenant


def _os_index_stats(index: str) -> Dict[str, Any]:
    try:
        r = httpx.get(
            f"{OPENSEARCH_URL}/_cat/indices/{index}",
            params={"format": "json", "h": "index,docs.count,store.size,health,status"},
            auth=_os_auth(), timeout=2.5,
        )
        if r.status_code == 404:
            return {"exists": False, "rows": []}
        r.raise_for_status()
        return {"exists": True, "rows": r.json()}
    except Exception as e:  # noqa: BLE001
        return {"exists": False, "error": str(e)[:160], "rows": []}


def _os_recent_counts(index: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    gte = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    body = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": gte, "lte": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}}},
        "aggs": {
            "hosts": {"terms": {"field": "host", "size": 200}},
            "tenants": {"terms": {"field": "tenant_id", "size": 100}},
        },
    }
    try:
        r = httpx.post(f"{OPENSEARCH_URL}/{index}/_search", json=body,
                       headers=_os_headers(), auth=_os_auth(), timeout=4.0)
        r.raise_for_status()
        data = r.json()
        total = (data.get("hits", {}).get("total") or {}).get("value", 0)
        hosts = {b["key"]: b["doc_count"] for b in data.get("aggregations", {}).get("hosts", {}).get("buckets", [])}
        tenants = {b["key"]: b["doc_count"] for b in data.get("aggregations", {}).get("tenants", {}).get("buckets", [])}
        return {"total": total, "hosts": hosts, "tenants": tenants, "windowMinutes": 15}
    except Exception as e:  # noqa: BLE001
        return {"total": 0, "hosts": {}, "tenants": {}, "windowMinutes": 15, "error": str(e)[:160]}


def _agents(tenant: str) -> List[Dict[str, Any]]:
    try:
        if tenant == "all":
            rows = pg_query(
                """SELECT hostname, tenant_id, version, last_seen, status_override,
                          EXTRACT(EPOCH FROM (now() - last_seen)) AS gap_sec
                     FROM agents ORDER BY tenant_id, hostname"""
            )
        else:
            rows = pg_query(
                """SELECT hostname, tenant_id, version, last_seen, status_override,
                          EXTRACT(EPOCH FROM (now() - last_seen)) AS gap_sec
                     FROM agents WHERE tenant_id=%s ORDER BY hostname""",
                (tenant,),
            )
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        gap = float(r["gap_sec"]) if r.get("gap_sec") is not None else None
        override = r.get("status_override")
        if override:
            status, reason = "warn", f"人工置为「{override}」"
        elif gap is None:
            status, reason = "fail", "从未收到心跳"
        elif gap > 300:
            status, reason = "fail", f"已 {round(gap / 60, 1)} 分钟无心跳"
        elif gap > 120:
            status, reason = "warn", f"{round(gap)} 秒前收到心跳"
        else:
            status, reason = "ok", f"{round(gap)} 秒前收到心跳"
        out.append({
            "hostname": r["hostname"], "tenant": r["tenant_id"],
            "version": r.get("version") or "—",
            "lastSeen": to_cst_str(r.get("last_seen")),
            "gapSec": round(gap, 1) if gap is not None else None,
            "status": status, "reason": reason,
        })
    return out


def _enabled_tenants(tenant: str) -> List[Dict[str, Any]]:
    if tenant == "all":
        rows = pg_query("SELECT code, name FROM tenants WHERE enabled ORDER BY id")
    else:
        rows = pg_query("SELECT code, name FROM tenants WHERE enabled AND code=%s", (tenant,))
    return [dict(r) for r in rows]


def _index_rows(tenant: str) -> Dict[str, Dict[str, Any]]:
    rows = _os_index_stats(f"logs-{tenant}")["rows"] if tenant != "all" else []
    return {r.get("index"): r for r in rows}


def build_topology(tenant: str) -> Dict[str, Any]:
    health = collect_health()
    health_map = {h["id"]: h for h in health["items"]}
    agents = _agents(tenant)
    tenants = _enabled_tenants(tenant)
    recent = _os_recent_counts(f"logs-{tenant}" if tenant != "all" else "logs-*")
    index_map = {t["code"]: _index_rows(t["code"]) for t in tenants}

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    def add_node(node_id, label, layer, category, status, status_text, meta=None):
        nodes.append({
            "id": node_id, "label": label, "layer": layer, "category": category,
            "status": status, "statusText": status_text, "meta": meta or {},
        })

    # 采集器
    for a in agents:
        node_id = f"collector:{a['tenant']}:{a['hostname']}"
        add_node(node_id, a["hostname"], "collector", "采集器", a["status"], a["reason"],
                 {"tenant": a["tenant"], "version": a["version"], "lastSeen": a["lastSeen"],
                  "recent15m": recent["hosts"].get(a["hostname"], 0)})
    # 固定组件节点
    for sid, label, layer, cat in (
        ("agent", "Agent 管控", "control", "管控"),
        ("ingest", "接入网关", "gateway", "接入"),
        ("kafka", "Kafka", "buffer", "缓冲"),
        ("flink", "Flink 解析", "compute", "计算"),
        ("os", "OpenSearch", "storage", "存储"),
        ("ch", "ClickHouse", "storage", "存储"),
        ("pg", "PostgreSQL", "storage", "存储"),
        ("search", "检索服务", "query", "查询"),
        ("dashboard", "看板服务", "query", "查询"),
        ("alarm", "告警服务", "query", "查询"),
        ("system", "平台管理", "platform", "平台"),
        ("auth", "认证服务", "platform", "平台"),
        ("web", "Web 展示", "web", "展示"),
    ):
        h = health_map.get(sid)
        if h:
            add_node(sid, h["name"], layer, cat, h["status"], h["statusText"], {"detail": h["detail"]})
        else:
            add_node(sid, label, layer, cat, "unknown", "未探活", {})

    # 租户与索引
    for t in tenants:
        code = t["code"]
        idx = index_map.get(code, {})
        row = idx.get(f"logs-{code}") or (list(idx.values())[0] if idx else {})
        docs = int(row.get("docs.count", 0) or 0) if row else 0
        idx_status = "ok" if row else "fail"
        idx_health = str(row.get("health") or "").lower()
        if idx_health == "yellow":
            idx_status = "warn"
        add_node(f"tenant:{code}", t["name"], "tenant", "租户", idx_status,
                 f"{docs} 条文档" if row else "索引不存在",
                 {"tenant": code, "index": f"logs-{code}", "docs": docs, "recent15m": recent["tenants"].get(code)})
        add_node(f"index:{code}", f"logs-{code}", "index", "索引", idx_status,
                 row.get("health", "—") if row else "未创建", {"docs": docs})

    # 边：采集器 → 接入 / 管控
    for a in agents:
        cid = f"collector:{a['tenant']}:{a['hostname']}"
        edges.append({"source": cid, "target": "ingest", "label": "日志上报",
                      "status": a["status"], "metric": f"近15分钟 {recent['hosts'].get(a['hostname'], 0)} 条"})
        edges.append({"source": cid, "target": "agent", "label": "心跳",
                      "status": a["status"], "metric": a["reason"]})

    # 数据链路：按 ingest 实际写入模式决定是否经过 Kafka/Flink
    ingest_detail = (health_map.get("ingest") or {}).get("detail") or ""
    storage_mode = "kafka" if "kafka" in ingest_detail.lower() else "direct"
    edge = lambda s, t, label, metric="": edges.append({
        "source": s, "target": t, "label": label,
        "status": "warn" if any((health_map.get(x) or {}).get("status") in ("warn", "unknown") for x in (s, t)) else
                  ("fail" if any((health_map.get(x) or {}).get("status") == "fail" for x in (s, t)) else "ok"),
        "metric": metric,
    })
    if storage_mode == "kafka":
        edge("ingest", "kafka", "写入缓冲", f"近15分钟 {recent['total']} 条")
        edge("kafka", "flink", "流式解析")
        edge("flink", "os", "写入索引")
    else:
        edge("ingest", "os", "直接写入", f"近15分钟 {recent['total']} 条")
    edge("ingest", "pg", "台账/审计")
    edge("agent", "pg", "采集器台账")
    edge("os", "search", "索引查询")
    edge("os", "dashboard", "聚合查询")
    edge("os", "alarm", "规则判定")
    edge("ch", "dashboard", "聚合读取")
    edge("pg", "system", "元数据")
    edge("pg", "auth", "账号数据")
    edge("search", "web", "API")
    edge("dashboard", "web", "API")
    edge("alarm", "web", "API")
    edge("system", "web", "API")
    edge("auth", "web", "API")
    edge("agent", "web", "API")
    edge("ingest", "web", "API")

    for t in tenants:
        code = t["code"]
        # 采集器到租户：按心跳租户归属；多 namespace 场景后续可用映射表细化
        for a in agents:
            if a["tenant"] == code:
                edges.append({"source": f"collector:{code}:{a['hostname']}", "target": f"tenant:{code}",
                              "label": "归属", "status": a["status"],
                              "metric": "心跳租户"})
        edges.append({"source": f"tenant:{code}", "target": f"index:{code}", "label": "写入索引",
                      "status": (health_map.get("os") or {}).get("status", "unknown"),
                      "metric": f"近15分钟 {recent['tenants'].get(code, 0)} 条"})
        edges.append({"source": "os", "target": f"index:{code}", "label": "存储",
                      "status": (health_map.get("os") or {}).get("status", "unknown"), "metric": ""})

    summary = {
        "collectors": len(agents),
        "online": sum(1 for a in agents if a["status"] == "ok"),
        "offline": sum(1 for a in agents if a["status"] == "fail"),
        "tenants": len(tenants),
        "recent15m": recent["total"],
        "nodes": len(nodes),
        "edges": len(edges),
    }
    return {
        "nodes": nodes, "edges": edges, "summary": summary,
        "health": health["summary"], "tenant": tenant,
        "updatedAt": to_cst_str(datetime.now(timezone.utc)),
    }


class DiagnoseIn(BaseModel):
    tenant: Optional[str] = None
    hostname: Optional[str] = None


def _check(key: str, title: str, status: str, detail: str,
           evidence: Optional[Dict[str, Any]] = None, action: str = "") -> Dict[str, Any]:
    return {"key": key, "title": title, "status": status,
            "statusText": STATUS_LABEL.get(status, status),
            "detail": detail, "evidence": evidence or {}, "action": action}


def _recent_iso(minutes: int = 15):
    now = datetime.now(timezone.utc)
    return now, (now - timedelta(minutes=minutes))


def run_diagnose(tenant: str, hostname: Optional[str], authorization: Optional[str]) -> Dict[str, Any]:
    started = time.perf_counter()
    health = collect_health()
    health_map = {h["id"]: h for h in health["items"]}
    agents = _agents(tenant)
    selected = next((a for a in agents if a["hostname"] == hostname), None) if hostname else (agents[0] if agents else None)
    checks: List[Dict[str, Any]] = []

    if not agents:
        checks.append(_check("collector_registered", "采集器注册", "fail",
                             "当前租户没有任何采集器心跳记录",
                             {"tenant": tenant}, "先部署采集器/主机 Agent，并确认心跳地址可达"))
    else:
        checks.append(_check("collector_registered", "采集器注册", "ok",
                             f"发现 {len(agents)} 个采集器心跳", {"collectors": [a["hostname"] for a in agents]}))
    if selected:
        checks.append(_check("collector_heartbeat", "采集器心跳", selected["status"],
                             f"{selected['hostname']}：{selected['reason']}",
                             {"lastSeen": selected["lastSeen"], "version": selected["version"],
                              "gapSec": selected["gapSec"]},
                             "检查采集器 Pod/进程与到 agent-service 的网络"))
    else:
        checks.append(_check("collector_heartbeat", "采集器心跳", "skip",
                             "没有可用的采集器，跳过心跳检查"))

    # 凭证：log_collectors.tenant_allow 是 JSONB 数组
    try:
        cred_count = pg_scalar(
            "SELECT COUNT(*) FROM log_collectors WHERE tenant_allow @> %s::jsonb",
            (json.dumps([tenant], ensure_ascii=False),),
        )
    except Exception:  # noqa: BLE001
        cred_count = None
    checks.append(_check("collector_credential", "采集凭证", "ok" if cred_count else "warn",
                         f"租户可用凭证 {cred_count if cred_count is not None else '查询失败'} 个",
                         {"tenantAllow": tenant},
                         "在 Agent 管理中重新签发接入包"))

    ns_rows = pg_query("SELECT namespace FROM namespace_tenant WHERE tenant=%s ORDER BY namespace", (tenant,))
    ns_list = [r["namespace"] for r in ns_rows]
    checks.append(_check("namespace_mapping", "Namespace 归属映射", "ok" if ns_list else "warn",
                         "已映射：" + "、".join(ns_list) if ns_list else "未配置 Namespace 映射；容器日志会被拒收，主机日志不受影响",
                         {"namespaces": ns_list},
                         "平台管理 → 租户管理 → Namespace 映射"))

    host_count = pg_scalar("SELECT COUNT(*) FROM hosts WHERE tenant=%s", (tenant,))
    checks.append(_check("host_ledger", "主机台账", "ok" if host_count else "warn",
                         f"台账主机 {host_count or 0} 台", {"tenant": tenant},
                         "数据接入 → 主机台账（host-agent 场景必须登记）"))

    ingest = health_map.get("ingest", {})
    checks.append(_check("ingest_gateway", "接入网关", ingest.get("status", "unknown"),
                         ingest.get("detail", "无探活结果"), {"latencyMs": ingest.get("latencyMs")},
                         "检查 ingest-service 与 /ingest/collector"))
    kafka = health_map.get("kafka", {})
    os_health = health_map.get("os", {})
    mode = "kafka" if "kafka" in (ingest.get("detail") or "").lower() else "direct"
    if mode == "kafka":
        checks.append(_check("write_buffer", "Kafka 写入缓冲", kafka.get("status", "unknown"),
                             kafka.get("detail", "无探活结果"), {"storageMode": "kafka"},
                             "检查 Kafka broker 与 topic"))
    else:
        checks.append(_check("write_buffer", "直写存储", os_health.get("status", "unknown"),
                             os_health.get("detail", "无探活结果"), {"storageMode": "direct"},
                             "检查 OpenSearch 集群"))

    idx = _os_index_stats(f"logs-{tenant}")
    checks.append(_check("index_exists", "目标索引", "ok" if idx.get("exists") else "fail",
                         f"logs-{tenant} 已创建" if idx.get("exists") else f"logs-{tenant} 不存在或不可读",
                         {"rows": idx.get("rows", []), "error": idx.get("error")},
                         "确认租户已初始化索引，或首条日志已成功写入"))

    recent = _os_recent_counts(f"logs-{tenant}")
    if recent.get("error"):
        recent_status, recent_detail = "warn", "OpenSearch 聚合失败：" + recent["error"]
    elif recent.get("total", 0) > 0:
        recent_status, recent_detail = "ok", f"近 15 分钟写入 {recent['total']} 条"
    else:
        recent_status, recent_detail = "fail", "近 15 分钟没有新日志写入"
    checks.append(_check("recent_write", "最近写入", recent_status, recent_detail,
                         {"total": recent.get("total", 0), "hosts": recent.get("hosts", {})},
                         "检查采集器输入路径、namespace/host 归属与 ingest 拒收审计"))

    now, frm = _recent_iso(15)
    search_ok, search_detail, search_evidence = False, "检索服务不可达", {}
    if authorization:
        try:
            r = httpx.post(
                "http://search-service:8000/query",
                json={"tenant": tenant, "page": 1, "pageSize": 1,
                      "timeRange": {"from": frm.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                                    "to": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}},
                headers={"Authorization": authorization}, timeout=4.0,
            )
            if r.status_code == 200:
                data = r.json()
                total = data.get("total", 0)
                search_ok = True
                search_detail = f"检索回读正常，命中 {total} 条"
                search_evidence = {"total": total, "index": data.get("index")}
            else:
                search_detail = f"检索接口 HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            search_detail = f"检索接口异常：{str(e)[:120]}"
    checks.append(_check("query_readback", "检索回读", "ok" if search_ok else "warn",
                         search_detail, search_evidence, "检查 search-service 与索引权限"))

    web = health_map.get("web", {})
    checks.append(_check("web_access", "Web 展示", web.get("status", "unknown"),
                         web.get("detail", "无探活结果"), {"latencyMs": web.get("latencyMs")},
                         "检查 web/nginx 与后端 API 路由"))

    summary = {
        "total": len(checks),
        "ok": sum(1 for c in checks if c["status"] == "ok"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "fail": sum(1 for c in checks if c["status"] == "fail"),
        "skip": sum(1 for c in checks if c["status"] == "skip"),
    }
    overall = "fail" if summary["fail"] else ("warn" if summary["warn"] else "ok")
    return {
        "tenant": tenant, "hostname": selected["hostname"] if selected else (hostname or ""),
        "summary": summary, "overall": overall,
        "durationMs": round((time.perf_counter() - started) * 1000, 1),
        "checks": checks,
        "updatedAt": to_cst_str(datetime.now(timezone.utc)),
    }


@router.get("/health")
def monitor_health(user: Dict[str, Any] = Depends(require("monitor", "view"))):
    return collect_health()


@router.get("/topology")
def monitor_topology(tenant: Optional[str] = None,
                     user: Dict[str, Any] = Depends(require("monitor", "view"))):
    effective = _scope(tenant, user)
    return build_topology(effective)


@router.get("/collectors")
def monitor_collectors(tenant: Optional[str] = None,
                       user: Dict[str, Any] = Depends(require("monitor", "view"))):
    effective = _scope(tenant, user)
    return {"tenant": effective, "items": _agents(effective),
            "updatedAt": to_cst_str(datetime.now(timezone.utc))}


@router.post("/diagnose")
def monitor_diagnose(req: DiagnoseIn, request: Request,
                     user: Dict[str, Any] = Depends(require("monitor", "diagnose"))):
    effective = _scope(req.tenant, user, allow_all=False)
    result = run_diagnose(effective, (req.hostname or "").strip() or None,
                          request.headers.get("authorization"))
    write_audit(
        user, "自监测诊断", target=effective,
        detail={"hostname": result["hostname"], "overall": result["overall"],
                "checks": {c["key"]: c["status"] for c in result["checks"]}},
        ip=client_ip(request.headers.get("x-forwarded-for"), request.client.host if request.client else ""),
    )
    return result
