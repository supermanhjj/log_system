"""日志空间管理：容量概览、存储配置、索引管理、租户配额。

数据来源**全部真实**，没有任何模拟值：
- OpenSearch `_cat/indices`   → 索引体积 / 文档数 / health / 创建时间
- OpenSearch `_cluster/stats` → 节点所在文件系统的总量与可用空间
- OpenSearch `date_histogram` → 容量趋势（按天文档分布反推累计占用）
- PostgreSQL                  → storage_config / tenant_quotas / storage_snapshots

口径约定（重要，避免"假数据"式自圆其说）：
1. **总容量是配置值，不是查询值**：`storage_config.quota_gb` 表示上云后实际分配给
   日志平台的总空间（API 对外字段 `totalGB`）。绝不用 OpenSearch 节点的 fs 读数反推
   总容量——那是 Docker 虚拟磁盘大小，既不等于云上获批容量，也不是日志专用空间。
   总容量未配置（0）时如实显示「未配置」，可用与占用率显示「—」。
2. 四张容量卡：`已用 = 日志索引实际 store.size`，`可用 = 总容量 - 已用`，
   `占用率 = 已用 / 总容量`。三项始终闭合，且与趋势图（日志占用）口径一致。
3. 两级配额：租户配额之和不得超过平台总容量；总容量与单租户配额为 0 分别表示
   「未配置总容量」与「该租户不设上限」（后者不计入分配统计）。系统不预置假配额。
4. 趋势优先用 `storage_snapshots` 每日实测快照；快照不足 2 个点时，按真实日志
   文档分布反推并标记 `source=estimated`，前端如实展示"推算/实测"。
5. 清理分两种：`purge` 按保留天数删过期文档（默认，安全）；`drop` 删整个索引，
   且只允许对已转冷的索引执行，活动索引一律拒绝。
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from .audit import write_audit
from .db import pg_exec, pg_query, pg_scalar, to_cst_str
from .security import client_ip, require, require_any

router = APIRouter(prefix="/storage", tags=["storage"])

GB = 1024 ** 3
MB = 1024 ** 2
CST = timezone(timedelta(hours=8))

STRATEGIES = [
    {"value": "hot7d", "label": "热数据 7 天 / 冷数据归档", "hotDays": 7},
    {"value": "hot30d", "label": "热数据 30 天 / 冷数据归档", "hotDays": 30},
    {"value": "allhot", "label": "全量热存储", "hotDays": 0},
]
HOT_DAYS = {s["value"]: s["hotDays"] for s in STRATEGIES}
RANGES = {"近 7 天": 7, "近 30 天": 30, "近 90 天": 90}
INDEX_NAME_RE = r"^logs-[A-Za-z0-9_-]+$"

_os_client = None


# ---------------------------------------------------------------------------
# OpenSearch 访问
# ---------------------------------------------------------------------------
def os_client():
    """惰性创建客户端；开发环境安全插件关闭，因此不强制带认证。"""
    global _os_client
    if _os_client is None:
        from opensearchpy import OpenSearch

        kwargs: Dict[str, Any] = {
            "hosts": [{
                "host": os.getenv("OPENSEARCH_HOST", "opensearch"),
                "port": int(os.getenv("OPENSEARCH_PORT", "9200")),
            }],
            "timeout": 15,
            "max_retries": 1,
            "retry_on_timeout": False,
        }
        user, pwd = os.getenv("OPENSEARCH_USER"), os.getenv("OPENSEARCH_PASSWORD")
        if user and pwd:
            kwargs["http_auth"] = (user, pwd)
        _os_client = OpenSearch(**kwargs)
    return _os_client


def os_call(fn, *args, **kwargs):
    """统一把驱动异常转成 503，前端能显示「OpenSearch 不可用」而不是 500 空错。"""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"OpenSearch 不可用：{e.__class__.__name__} {str(e)[:160]}",
        )


def fetch_indices(tenant: str = "") -> List[Dict[str, Any]]:
    """读取 logs-* 索引的真实统计；tenant 为空或 all 时不按租户过滤。"""
    rows = os_call(
        os_client().cat.indices,
        format="json",
        bytes="b",
        h="index,health,status,docs.count,store.size,creation.date",
    )
    now = datetime.now(CST)
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        name = str(r.get("index", ""))
        if not name.startswith("logs-"):
            continue
        if tenant and tenant != "all" and name != f"logs-{tenant}":
            continue
        created_ms = int(r.get("creation.date") or 0)
        created = datetime.fromtimestamp(created_ms / 1000, CST) if created_ms else None
        out.append({
            "name": name,
            "docs": int(r.get("docs.count") or 0),
            "bytes": int(r.get("store.size") or 0),
            "health": str(r.get("health") or ""),
            "createdAt": to_cst_str(created),
            "ageDays": (now - created).days if created else 0,
        })
    out.sort(key=lambda x: x["name"])
    return out


def cluster_fs() -> Dict[str, int]:
    stats = os_call(os_client().cluster.stats, filter_path="nodes.fs,nodes.count")
    nodes = (stats or {}).get("nodes") or {}
    fs = nodes.get("fs") or {}
    return {
        "total": int(fs.get("total_in_bytes") or 0),
        "available": int(fs.get("available_in_bytes") or 0),
        "free": int(fs.get("free_in_bytes") or 0),
        "nodes": int((nodes.get("count") or {}).get("total") or 1),
    }


def fmt_bytes(n: Any) -> str:
    """自适应单位，避免 19 MB 显示成「0.02 GB」这种读不出量级的写法。"""
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            if unit in ("B", "KB"):
                return f"{v:.0f} {unit}"
            return f"{v:.2f} {unit}"
        v /= 1024
    return f"{v:.2f} TB"


def day_histogram(scope: str, days: int) -> List[Dict[str, Any]]:
    """按天统计日志文档数（用于容量趋势推算）。"""
    index = "logs-*" if (not scope or scope == "all") else f"logs-{scope}"
    body = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": f"now-{days}d/d", "lte": "now"}}},
        "aggs": {
            "by_day": {
                "date_histogram": {
                    "field": "@timestamp",
                    "calendar_interval": "day",
                    "time_zone": "+08:00",
                    "format": "MM-dd",
                }
            }
        },
    }
    res = os_call(os_client().search, index=index, body=body)
    buckets = (((res or {}).get("aggregations") or {}).get("by_day") or {}).get("buckets") or []
    return [{"t": str(b.get("key_as_string") or ""), "docs": int(b.get("doc_count") or 0)}
            for b in buckets]


# ---------------------------------------------------------------------------
# 存储配置 / 快照
# ---------------------------------------------------------------------------
def read_config() -> Dict[str, Any]:
    rows = pg_query("SELECT * FROM storage_config WHERE id=1")
    if not rows:
        pg_exec("INSERT INTO storage_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        rows = pg_query("SELECT * FROM storage_config WHERE id=1")
    r = rows[0]
    total_gb = int(r["quota_gb"])
    return {
        "retainDays": int(r["retain_days"]),
        "rollThreshold": int(r["roll_threshold"]),
        "hotColdStrategy": r["hot_cold_strategy"],
        # 平台总容量：DB 沿用旧列名 quota_gb，语义已明确为「分配给日志平台的总空间」。
        # totalGB 是新口径字段名，quotaGB 保留作为旧前端的兼容别名。
        "totalGB": total_gb,
        "quotaGB": total_gb,
        "strategies": [{"value": s["value"], "label": s["label"]} for s in STRATEGIES],
        "updatedAt": to_cst_str(r["updated_at"]),
    }


def allocated_gb(exclude_tenant: str = "") -> int:
    """已分配给租户的配额之和（GB）。0 表示不设上限，按 0 参与求和只统计显式配额。"""
    rows = pg_query("SELECT tenant_code, quota_gb FROM tenant_quotas")
    return sum(int(r["quota_gb"]) for r in rows
               if r["tenant_code"] != exclude_tenant and int(r["quota_gb"]) > 0)


def tenant_quota(code: str) -> Optional[Dict[str, Any]]:
    """读取单个租户的配额与名称（供租户口径容量使用）。"""
    rows = pg_query(
        """SELECT q.quota_gb, t.name FROM tenant_quotas q
             JOIN tenants t ON t.code = q.tenant_code WHERE q.tenant_code = %s""",
        (code,),
    )
    if not rows:
        return None
    return {"name": rows[0]["name"], "quotaGB": int(rows[0]["quota_gb"])}


def record_snapshot(scope: str, used_bytes: int, docs: int, index_count: int) -> None:
    """每天每个 scope 只留一条，重复调用覆盖为最新实测值。"""
    try:
        pg_exec(
            """INSERT INTO storage_snapshots (snap_date, scope, used_bytes, doc_count, index_count)
               VALUES (CURRENT_DATE, %s, %s, %s, %s)
               ON CONFLICT (snap_date, scope) DO UPDATE
                 SET used_bytes = EXCLUDED.used_bytes,
                     doc_count = EXCLUDED.doc_count,
                     index_count = EXCLUDED.index_count""",
            (scope, used_bytes, docs, index_count),
        )
    except Exception as e:  # noqa: BLE001
        # 快照只是趋势优化的副产品，写失败不影响本次容量查询
        print(f"[system-service] snapshot write failed: {e}", flush=True)


def trend_unit(used_bytes: int) -> Tuple[str, float]:
    if used_bytes >= GB:
        return "GB", float(GB)
    return "MB", float(MB)


def build_trend(scope: str, days: int, used_bytes: int, docs: int) -> Dict[str, Any]:
    unit, div = trend_unit(used_bytes)
    today = datetime.now(CST).date()
    start = today - timedelta(days=days - 1)

    snaps = pg_query(
        """SELECT snap_date, used_bytes FROM storage_snapshots
            WHERE scope=%s AND snap_date >= %s ORDER BY snap_date""",
        (scope, start),
    )
    if len(snaps) >= 2:
        return {
            "unit": unit,
            "source": "snapshot",
            "points": [{"t": s["snap_date"].strftime("%m-%d"),
                        "v": round(int(s["used_bytes"]) / div, 2)} for s in snaps],
            "note": f"近 {days} 天的每日实测快照（索引实际占用）",
        }

    # 快照不足：按真实日志文档分布反推累计占用（可解释的估算，非随机数）
    buckets = day_histogram(scope, days)
    if not buckets or not docs:
        return {
            "unit": unit,
            "source": "estimated",
            "points": [],
            "note": "暂无足够数据绘制趋势（索引内没有日志或快照不足）",
        }
    in_range = sum(b["docs"] for b in buckets)
    before = max(docs - in_range, 0)
    cum = before
    points = []
    for b in buckets:
        cum += b["docs"]
        points.append({"t": b["t"], "v": round(used_bytes * (cum / docs) / div, 2)})
    return {
        "unit": unit,
        "source": "estimated",
        "points": points,
        "note": f"按近 {days} 天真实日志量分布反推（每日快照满 2 天后自动切换为实测）",
    }


def scope_for(user: Dict[str, Any], tenant: str) -> str:
    """非平台管理员一律锁定本租户，防止通过 tenant=all 越权查看他租户容量与索引。"""
    if user["role_key"] != "platform_admin":
        return user["tenant_code"]
    return (tenant or "all").strip() or "all"


# ---------------------------------------------------------------------------
# 容量概览
# ---------------------------------------------------------------------------
@router.get("/capacity")
def capacity(
    rangeLabel: str = Query("近 7 天", alias="range"),
    tenant: str = Query(""),
    user: Dict[str, Any] = Depends(require_any([("storage", "view"), ("overview", "view")])),
):
    scope = scope_for(user, tenant)
    days = RANGES.get(rangeLabel) or RANGES["近 7 天"]

    indexes = fetch_indices(scope)
    fs = cluster_fs()
    cfg = read_config()

    log_bytes = sum(i["bytes"] for i in indexes)
    log_docs = sum(i["docs"] for i in indexes)

    # 容量口径跟着 scope 走（scope 已由 scope_for 按角色锁定，非平台管理员只能是本租户）：
    # - scope=all  → 平台管理员看全局，总量取 storage_config.quota_gb（平台总容量配置值）
    # - scope=具体租户 → 总量取该租户在 tenant_quotas 被分配的配额
    # 已用 log_bytes 本就按 scope 过滤，这样总量/可用/占用率与已用三者始终同一口径。
    # 上云后底层磁盘由云厂商托管，节点 fs 读数既拿不到也不等于获批容量，绝不用于反推总量。
    if scope == "all":
        cap_basis = "platform"
        cap_label = "平台总容量"
        cap_gb = cfg["totalGB"]
        unlimited = False
        cap_source = (f"平台总容量 {cap_gb} GB 为配置值（上云后按实际获批空间填写）"
                      if cap_gb > 0 else "平台总容量尚未配置（请到「存储配置」填写）")
    else:
        tq = tenant_quota(scope)
        cap_gb = tq["quotaGB"] if tq else 0
        tq_name = tq["name"] if tq else scope
        cap_basis = "tenant"
        cap_label = f"租户「{tq_name}」配额"
        unlimited = cap_gb == 0  # 配额 0 = 不设上限（租户主动选择，区别于平台「未配置」）
        cap_source = (f"租户「{tq_name}」配额 {cap_gb} GB（在「租户配额」中分配）"
                      if cap_gb > 0 else f"租户「{tq_name}」未设配额（配额 0 表示不设上限）")

    configured = cap_gb > 0
    scope_label = "全部租户" if scope == "all" else f"租户 {scope}"

    if configured:
        total_bytes = cap_gb * GB
        used_bytes = log_bytes
        avail_bytes = max(total_bytes - used_bytes, 0)
        basis = "configured"
        occupancy = round(used_bytes / total_bytes * 100, 1)
        note = (f"当前口径为{cap_label}：{cap_source}。"
                f"「{scope_label}」日志索引实际占用 {fmt_bytes(log_bytes)}，按此口径计算可用空间与占用率。"
                f"物理卷 {fmt_bytes(fs['total'])}（可用 {fmt_bytes(fs['available'])}）仅为底层磁盘参考，"
                f"不参与容量计算。")
    else:
        total_bytes = 0
        used_bytes = log_bytes
        avail_bytes = 0
        basis = "unconfigured"
        occupancy = 0.0
        if unlimited:
            note = (f"当前口径为{cap_label}：{cap_source}，因此可用空间与占用率不适用；"
                    f"「{scope_label}」日志索引实际占用 {fmt_bytes(log_bytes)}。")
        else:
            note = (f"当前口径为{cap_label}：{cap_source}，无法计算可用空间与占用率。"
                    f"「{scope_label}」日志索引实际占用 {fmt_bytes(log_bytes)}；"
                    f"物理卷 {fmt_bytes(fs['total'])}（可用 {fmt_bytes(fs['available'])}）仅为底层磁盘参考。")

    record_snapshot(scope, log_bytes, log_docs, len(indexes))
    trend = build_trend(scope, days, log_bytes, log_docs)

    return {
        "total": round(total_bytes / GB, 2),
        "used": round(used_bytes / GB, 3),
        "available": round(avail_bytes / GB, 2),
        "occupancy": f"{occupancy}",
        "unit": "GB",
        "configured": configured,
        "totalText": f"{cap_gb} GB" if configured else ("不设上限" if unlimited else "未配置"),
        "usedText": fmt_bytes(used_bytes),
        "availableText": fmt_bytes(avail_bytes) if configured else "—",
        "basis": basis,
        # 总量口径：随 scope 走，供前端动态显示「平台总容量」/「租户配额」
        "capBasis": cap_basis,        # platform | tenant
        "capLabel": cap_label,        # 人类可读口径标签
        "unlimited": unlimited,       # 租户配额 0（不设上限），与平台「未配置」区分
        "totalSourceGB": cfg["totalGB"],   # 平台总容量配置值（非当前口径总量），供对照
        "totalGB": cap_gb,            # 当前口径的总量（平台总容量 or 租户配额）
        "quotaGB": cap_gb,            # 兼容旧前端字段
        "quotaLimited": configured,   # 兼容旧前端字段
        "logBytes": log_bytes,
        "logText": fmt_bytes(log_bytes),
        "docs": log_docs,
        "indexCount": len(indexes),
        "physical": {
            "totalBytes": fs["total"],
            "availableBytes": fs["available"],
            "totalText": fmt_bytes(fs["total"]),
            "availableText": fmt_bytes(fs["available"]),
        },
        "scope": scope,
        "rangeLabel": rangeLabel,
        "trend": trend,
        "updatedAt": to_cst_str(datetime.now(timezone.utc)),
        "note": note,
    }


# ---------------------------------------------------------------------------
# 存储配置
# ---------------------------------------------------------------------------
class ConfigIn(BaseModel):
    retainDays: Optional[int] = None
    rollThreshold: Optional[int] = None
    hotColdStrategy: Optional[str] = None
    totalGB: Optional[int] = None
    quotaGB: Optional[int] = None   # 旧前端字段，等价 totalGB


@router.get("/config")
def get_config(user: Dict[str, Any] = Depends(require("storage", "view"))):
    cfg = read_config()
    cfg["note"] = ("平台总容量是配置值：按上云后实际分配给日志平台的空间填写，0 表示尚未配置；"
                   "各租户配额之和不得超过总容量。物理卷读数仅供参考，不参与容量计算。"
                   "保留天数是日志生命周期的唯一来源，清理动作只删除早于保留天数的日志。")
    return cfg


@router.put("/config")
def put_config(
    req: ConfigIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("storage", "update")),
):
    sets: List[str] = []
    params: List[Any] = []
    if req.retainDays is not None:
        if not 1 <= req.retainDays <= 3650:
            raise HTTPException(status_code=400, detail="保留天数需在 1-3650 之间")
        sets.append("retain_days=%s")
        params.append(req.retainDays)
    if req.rollThreshold is not None:
        if not 50 <= req.rollThreshold <= 95:
            raise HTTPException(status_code=400, detail="滚动阈值需在 50-95 之间")
        sets.append("roll_threshold=%s")
        params.append(req.rollThreshold)
    if req.hotColdStrategy is not None:
        if req.hotColdStrategy not in HOT_DAYS:
            raise HTTPException(status_code=400, detail="热/冷分级策略不存在")
        sets.append("hot_cold_strategy=%s")
        params.append(req.hotColdStrategy)
    # totalGB 为新字段名，quotaGB 是旧前端的等价写法；同时出现以 totalGB 为准。
    new_total = req.totalGB if req.totalGB is not None else req.quotaGB
    if new_total is not None:
        if not 0 <= new_total <= 1048576:  # 上限 1 PB，挡住误输入
            raise HTTPException(status_code=400, detail="平台总容量需在 0-1048576 GB 之间（0 表示尚未配置）")
        if new_total > 0:
            already = allocated_gb()
            if already > new_total:
                rows = pg_query(
                    "SELECT tenant_code, quota_gb FROM tenant_quotas WHERE quota_gb > 0 ORDER BY tenant_code"
                )
                detail = "、".join(f"{r['tenant_code']} {r['quota_gb']}GB" for r in rows)
                raise HTTPException(
                    status_code=400,
                    detail=(f"已分配给租户的配额合计 {already} GB（{detail}），"
                            f"超过新的平台总容量 {new_total} GB。请先下调租户配额再改总容量。"),
                )
        sets.append("quota_gb=%s")
        params.append(new_total)
    if not sets:
        raise HTTPException(status_code=400, detail="没有需要更新的配置项")

    sets.append("updated_at=now()")
    pg_exec(f"UPDATE storage_config SET {', '.join(sets)} WHERE id=1", tuple(params))

    # 保留天数是日志生命周期的唯一来源，不再向 system_params 回写同名参数

    write_audit(user, "修改存储配置", "日志空间管理",
                {"fields": req.model_dump(exclude_none=True)},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "config": read_config(), "message": "存储配置已保存并生效"}


# ---------------------------------------------------------------------------
# 索引管理
# ---------------------------------------------------------------------------
def index_state(ix: Dict[str, Any], cfg: Dict[str, Any],
                single_node: bool = True) -> Tuple[str, str, bool]:
    """返回 (status, reason, expired)。status ∈ healthy / warning / cold。

    判据顺序有讲究：先看「该清理了」和「体积异常」，再看冷热，最后才看分片健康。
    单节点部署下副本无处分配必然 yellow，这不是故障，不应把全部索引刷成告警。
    """
    hot_days = HOT_DAYS.get(cfg["hotColdStrategy"], 7)
    expired = ix["ageDays"] > cfg["retainDays"]
    if expired:
        return "warning", f"已存在 {ix['ageDays']} 天，超出保留期 {cfg['retainDays']} 天，建议清理", True
    if cfg["quotaGB"] > 0 and ix["bytes"] > cfg["quotaGB"] * GB * cfg["rollThreshold"] / 100:
        return "warning", f"体积超过配额的 {cfg['rollThreshold']}%，建议滚动新索引", False
    if hot_days and ix["ageDays"] > hot_days:
        return "cold", f"超出热数据窗口 {hot_days} 天，已转冷存储", False
    if ix["health"] == "red":
        return "warning", "索引健康度 red，存在未分配的主分片", False
    if ix["health"] == "yellow" and not single_node:
        return "warning", "索引健康度 yellow，副本未分配", False
    if ix["health"] == "green":
        return "healthy", "正常", False
    return "healthy", f"单节点部署，副本未分配（health={ix['health']}，属预期）", False


@router.get("/indexes")
def list_indexes(
    tenant: str = Query(""),
    user: Dict[str, Any] = Depends(require("storage", "view")),
):
    scope = scope_for(user, tenant)
    cfg = read_config()
    single_node = cluster_fs()["nodes"] <= 1
    items = []
    for ix in fetch_indices(scope):
        status, reason, expired = index_state(ix, cfg, single_node)
        items.append({
            "name": ix["name"],
            "docs": ix["docs"],
            "bytes": ix["bytes"],
            "sizeGB": round(ix["bytes"] / GB, 3),
            "sizeText": fmt_bytes(ix["bytes"]),
            "health": ix["health"],
            "status": status,
            "reason": reason,
            "expired": expired,
            "ageDays": ix["ageDays"],
            "createdAt": ix["createdAt"],
        })
    return {"items": items, "total": len(items), "scope": scope}


class CleanIn(BaseModel):
    mode: str = "purge"  # purge：按保留期删除过期文档；drop：删除整个冷索引


@router.post("/indexes/{name}/clean")
def clean_index(
    name: str,
    req: CleanIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("storage", "clean")),
):
    import re

    if not re.match(INDEX_NAME_RE, name):
        raise HTTPException(status_code=400, detail="只允许对 logs-* 业务索引执行清理")
    found = {i["name"]: i for i in fetch_indices("")}
    if name not in found:
        raise HTTPException(status_code=404, detail="索引不存在")
    ip = client_ip(request.headers.get("x-forwarded-for"),
                   request.client.host if request.client else "")
    cfg = read_config()

    if req.mode == "drop":
        status, reason, _ = index_state(found[name], cfg, cluster_fs()["nodes"] <= 1)
        if status != "cold":
            raise HTTPException(
                status_code=400,
                detail=f"「{name}」当前状态为 {status}（{reason}），只允许删除已转冷的索引",
            )
        os_call(os_client().indices.delete, index=name)
        write_audit(user, "删除索引", name,
                    {"bytes": found[name]["bytes"], "docs": found[name]["docs"]}, ip)
        return {"ok": True, "mode": "drop", "deleted": 0,
                "message": f"已删除索引 {name}（释放 {fmt_bytes(found[name]['bytes'])}）"}

    if req.mode != "purge":
        raise HTTPException(status_code=400, detail="mode 仅支持 purge 或 drop")

    body = {"query": {"range": {"@timestamp": {"lt": f"now-{cfg['retainDays']}d/d"}}}}
    # 注意：delete_by_query 的 timeout 参数会被 opensearch-py 当作传输层超时（要求数值），
    # 想放宽删除耗时必须用 request_timeout，不能传 "60s" 字符串。踩过一次，503 报错。
    res = os_call(
        os_client().delete_by_query,
        index=name,
        body=body,
        refresh=True,
        conflicts="proceed",
        request_timeout=120,
    )
    deleted = int((res or {}).get("deleted") or 0)
    write_audit(user, "清理过期日志", name,
                {"mode": "purge", "retainDays": cfg["retainDays"], "deleted": deleted}, ip)
    if deleted:
        msg = f"已清理 {name} 中 {deleted} 条早于保留期（{cfg['retainDays']} 天）的日志"
    else:
        msg = f"{name} 中没有早于保留期（{cfg['retainDays']} 天）的日志，无需清理"
    return {"ok": True, "mode": "purge", "deleted": deleted,
            "retainDays": cfg["retainDays"], "message": msg}


# ---------------------------------------------------------------------------
# 租户配额
# ---------------------------------------------------------------------------
class QuotaIn(BaseModel):
    quotaGB: int


@router.get("/quotas")
def list_quotas(user: Dict[str, Any] = Depends(require("storage", "view"))):
    rows = pg_query(
        """SELECT q.tenant_code, t.name, q.quota_gb, q.updated_at
             FROM tenant_quotas q JOIN tenants t ON t.code = q.tenant_code
            ORDER BY t.id"""
    )
    if user["role_key"] != "platform_admin":
        rows = [r for r in rows if r["tenant_code"] == user["tenant_code"]]
    used: Dict[str, int] = {}
    docs: Dict[str, int] = {}
    for ix in fetch_indices(""):
        code = ix["name"][len("logs-"):]
        used[code] = used.get(code, 0) + ix["bytes"]
        docs[code] = docs.get(code, 0) + ix["docs"]

    items = []
    for r in rows:
        code = r["tenant_code"]
        ub = used.get(code, 0)
        quota = int(r["quota_gb"])
        items.append({
            "tenant": code,
            "tenantName": r["name"],
            "quotaGB": quota,
            "usedBytes": ub,
            "usedGB": round(ub / GB, 3),
            "usedText": fmt_bytes(ub),
            "docs": docs.get(code, 0),
            "limited": quota > 0,
            "occupancy": round(ub / (quota * GB) * 100, 2) if quota > 0 else None,
            "updatedAt": to_cst_str(r["updated_at"]),
        })
    total_gb = read_config()["totalGB"]
    allocated = sum(int(r["quota_gb"]) for r in rows) if rows else 0
    return {
        "items": items,
        "totalGB": total_gb,
        "allocatedGB": allocated,
        "availableGB": max(total_gb - allocated, 0) if total_gb > 0 else None,
        "overAllocated": bool(total_gb > 0 and allocated > total_gb),
        "note": (
            (f"平台总容量 {total_gb} GB，已分配 {allocated} GB，剩余可分配 "
             f"{max(total_gb - allocated, 0)} GB。")
            if total_gb > 0 else
            f"平台总容量尚未配置，已分配 {allocated} GB。请先到「存储配置」设置总容量。"
        ) + "配额为 0 表示不设上限（不参与分配统计）；已用容量来自该租户索引的真实 store.size。",
    }


@router.put("/quotas/{tenant}")
def put_quota(
    tenant: str,
    req: QuotaIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("storage", "update")),
):
    if req.quotaGB < 0:
        raise HTTPException(status_code=400, detail="配额不能为负数（0 表示不设上限）")
    if pg_scalar("SELECT 1 FROM tenants WHERE code=%s", (tenant,)) is None:
        raise HTTPException(status_code=404, detail="租户不存在")
    if user["role_key"] != "platform_admin" and tenant != user["tenant_code"]:
        raise HTTPException(status_code=403, detail="只能调整本租户的日志配额")

    # 两级配额约束：租户配额之和不得超过平台总容量。
    # 非平台管理员只改本租户，但校验按全平台口径算，避免从不同租户入口各自突破上限。
    total_gb = read_config()["totalGB"]
    if total_gb > 0:
        others = allocated_gb(exclude_tenant=tenant)
        if others + req.quotaGB > total_gb:
            raise HTTPException(
                status_code=400,
                detail=(f"其他租户已分配 {others} GB，本租户设 {req.quotaGB} GB 后将达 "
                        f"{others + req.quotaGB} GB，超过平台总容量 {total_gb} GB"
                        f"（剩余可分配 {max(total_gb - others, 0)} GB）。"),
            )
    pg_exec(
        """INSERT INTO tenant_quotas (tenant_code, quota_gb, updated_at)
           VALUES (%s, %s, now())
           ON CONFLICT (tenant_code) DO UPDATE
             SET quota_gb = EXCLUDED.quota_gb, updated_at = now()""",
        (tenant, req.quotaGB),
    )
    write_audit(user, "修改租户配额", tenant, {"quotaGB": req.quotaGB},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    text = f"{req.quotaGB} GB" if req.quotaGB > 0 else "不设上限"
    return {"ok": True, "tenant": tenant, "quotaGB": req.quotaGB,
            "message": f"{tenant} 的日志配额已更新为 {text}"}

# ---------------------------------------------------------------------------
# 租户开通/注销时的索引生命周期（供 main.create_tenant / delete_tenant 调用）
#
# 商用口径：租户开通即建 logs-<code>（显式 mapping，避免首条日志把 @timestamp/status
# 推断成错误类型）；注销时删除空索引。这与「按租户动态索引 + 索引模板」的方向一致，
# 本地 OpenSearch 无 index template 管理页，先以显式 mapping 落地。
# ---------------------------------------------------------------------------
TENANT_INDEX_MAPPING: Dict[str, Any] = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "dynamic": True,
        "properties": {
            "@timestamp": {"type": "date"},
            "level": {"type": "keyword"},
            "source": {"type": "keyword"},
            "host": {"type": "keyword"},
            "traceId": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "status": {"type": "integer"},
            "message": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}},
            "raw": {"type": "text"},
        },
    },
}


def ensure_tenant_index(code: str) -> bool:
    """开通租户时建索引；已存在则跳过。失败返回 False（不阻塞租户登记）。"""
    try:
        c = os_client()
        name = f"logs-{code}"
        if c.indices.exists(index=name):
            return True
        c.indices.create(index=name, body=TENANT_INDEX_MAPPING)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[system-service] ensure_tenant_index({code}) failed: {e}", flush=True)
        return False


def drop_tenant_index(code: str) -> bool:
    """删除租户的空索引（有数据的租户在业务层已被拦截）。"""
    try:
        c = os_client()
        name = f"logs-{code}"
        if c.indices.exists(index=name):
            c.indices.delete(index=name)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[system-service] drop_tenant_index({code}) failed: {e}", flush=True)
        return False

