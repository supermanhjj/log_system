"""服务端解析管线的管理接口：解析器 / 规则 CRUD、试解析、历史重放。

现场适配的核心闭环：
    贴一段真实日志 → 试解析看字段 → 存成规则 → 影子模式验证 → 切 active → 重放补历史

所有管理接口仅平台管理员可用。重放的写回保持 _id 不变（update 而非 index），
因此不会破坏 event_id 幂等语义，也不会在 ReplacingMergeTree 里产生重复行。
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from . import parsers, pipeline, pipeline_store
from .authz import current_identity, is_admin

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


def _admin(identity: Dict[str, Any] = Depends(current_identity)) -> Dict[str, Any]:
    if not is_admin(identity):
        raise HTTPException(status_code=403, detail="解析规则管理仅平台管理员可用")
    return identity


@router.get("/status")
def status(_: Dict[str, Any] = Depends(_admin)) -> Dict[str, Any]:
    rules, pmap = pipeline._load()
    return {
        "mode": pipeline.MODE,
        "cacheTtlSeconds": pipeline.CACHE_TTL,
        "ruleCount": len(rules),
        "parserCount": len(pmap),
        "note": ("shadow：新解析只写 attrs.__shadow，主字段保持老逻辑；"
                 "active：解析结果覆盖契约字段；off：停用"),
    }


class ParserIn(BaseModel):
    code: str
    name: str
    type: str
    definition: Dict[str, Any]
    description: str = ""
    enabled: bool = True


@router.get("/parsers")
def list_parsers(_: Dict[str, Any] = Depends(_admin)) -> List[Dict[str, Any]]:
    return pipeline_store.list_parsers()


@router.post("/parsers")
def save_parser(req: ParserIn, _: Dict[str, Any] = Depends(_admin)) -> Dict[str, Any]:
    d = dict(req.definition or {})
    d["type"] = req.type
    try:
        parsers.validate(d)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    row = pipeline_store.upsert_parser(
        req.code, req.name, req.type, d, req.description, req.enabled)
    pipeline.invalidate()
    return row


@router.delete("/parsers/{code}")
def remove_parser(code: str, _: Dict[str, Any] = Depends(_admin)) -> Dict[str, Any]:
    pipeline_store.delete_parser(code)
    pipeline.invalidate()
    return {"ok": True, "code": code}


class PipelineIn(BaseModel):
    code: str
    name: str
    priority: int = 100
    match: Dict[str, Any] = {}
    parserCode: str
    mapping: Dict[str, Any] = {}
    tenantCode: str = ""
    enabled: bool = True


@router.get("/rules")
def list_rules(_: Dict[str, Any] = Depends(_admin)) -> List[Dict[str, Any]]:
    return pipeline_store.list_pipelines()


@router.post("/rules")
def save_rule(req: PipelineIn, _: Dict[str, Any] = Depends(_admin)) -> Dict[str, Any]:
    row = pipeline_store.upsert_pipeline(
        req.code, req.name, req.priority, req.match, req.parserCode,
        req.mapping, req.tenantCode, req.enabled)
    pipeline.invalidate()
    return row


@router.delete("/rules/{code}")
def remove_rule(code: str, _: Dict[str, Any] = Depends(_admin)) -> Dict[str, Any]:
    pipeline_store.delete_pipeline(code)
    pipeline.invalidate()
    return {"ok": True, "code": code}


class PreviewIn(BaseModel):
    parser: Dict[str, Any]           # 完整解析器定义（含 type），未保存也可试
    sample: str
    mapping: Dict[str, Any] = {}


@router.post("/preview")
def preview(req: PreviewIn, _: Dict[str, Any] = Depends(_admin)) -> Dict[str, Any]:
    """试解析：现场贴一段真实日志，实时看抽出了哪些字段。"""
    try:
        parsers.validate(req.parser)
        return pipeline.preview(req.parser, req.sample, req.mapping)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ReplayIn(BaseModel):
    tenant: str
    days: int = 1
    limit: int = 10000
    dryRun: bool = True              # 默认只预览，不写库


@router.post("/replay")
def replay(req: ReplayIn, _: Dict[str, Any] = Depends(_admin)) -> Dict[str, Any]:
    """按当前规则重放历史日志，验证解析效果并补齐 parse_status。

    dryRun=false 时用 update 写回（_id 不变），不会产生重复数据。
    event_id 始终沿用原值——重算会引入 seq 差异，导致 ReplacingMergeTree 覆盖不掉。
    """
    if req.days < 1 or req.days > 30:
        raise HTTPException(status_code=400, detail="重放窗口限 1-30 天")
    if req.limit < 1 or req.limit > 10000:
        raise HTTPException(status_code=400, detail="单次重放上限 10000 条，更多请分批")

    from .main import index_for, os_client
    from opensearchpy import helpers

    body = {
        "size": req.limit,
        "query": {"range": {"@timestamp": {"gte": f"now-{req.days}d"}}},
        "_source": ["raw", "message", "@timestamp", "level", "source",
                    "host", "log_class", "source_type", "src_file", "parse_status"],
    }
    try:
        resp = os_client.search(index=index_for(req.tenant), body=body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"OpenSearch 查询失败：{e}")

    hits = (resp.get("hits") or {}).get("hits") or []
    rules, pmap = pipeline._load()
    matched = 0
    samples: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []

    for h in hits:
        src = h.get("_source") or {}
        result = pipeline.execute_one(src, rules, pmap)
        if not result["matched"]:
            continue
        matched += 1
        doc: Dict[str, Any] = {"parse_status": "parsed"}
        for key, value in result["mapped"].items():
            if key.startswith("attrs."):
                doc.setdefault("attrs", {})[key[len("attrs."):]] = value
            else:
                doc[key] = value
        if len(samples) < 20:
            samples.append({"id": h.get("_id"), "fields": result["fields"],
                            "mapped": result["mapped"],
                            "previous": src.get("parse_status")})
        if not req.dryRun:
            actions.append({"_op_type": "update", "_index": h.get("_index"),
                            "_id": h.get("_id"), "doc": doc})

    updated = 0
    if actions:
        try:
            helpers.bulk(os_client, actions, raise_on_error=False)
            updated = len(actions)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"写回失败：{e}")

    return {
        "tenant": req.tenant,
        "scanned": len(hits),
        "matched": matched,
        "updated": updated,
        "dryRun": req.dryRun,
        "samples": samples,
    }


# ---- 采集器凭证与租户白名单（E 批次安全加固） ----
class CollectorIn(BaseModel):
    code: str
    name: str = ""
    tenantAllow: List[str] = []
    key: str = ""       # 明文只在此处接受，数据库只存 sha256


@router.get("/collectors")
def list_collectors(_: Dict[str, Any] = Depends(_admin)) -> List[Dict[str, Any]]:
    rows = pipeline_store.list_collectors()
    # 凭证哈希绝不下发到前端
    return [{k: v for k, v in r.items() if k != "key_hash"} for r in rows]


@router.post("/collectors")
def save_collector(req: CollectorIn, _: Dict[str, Any] = Depends(_admin)) -> Dict[str, Any]:
    import hashlib

    if not req.code.strip():
        raise HTTPException(status_code=400, detail="采集器编码不能为空")
    key_hash = hashlib.sha256(req.key.encode("utf-8")).hexdigest() if req.key else ""
    row = pipeline_store.upsert_collector(
        req.code.strip(), req.name, req.tenantAllow, key_hash)
    return {k: v for k, v in row.items() if k != "key_hash"}


@router.get("/security-audit")
def security_audit(limit: int = 100,
                   _: Dict[str, Any] = Depends(_admin)) -> List[Dict[str, Any]]:
    """查询租户注入冲突 / 越界拒收记录，用于追溯谁在伪造归属。"""
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit 限 1-1000")
    from .main import pg_query

    return pg_query(
        "SELECT id, ts, collector_code, action, injected, inferred, final_tenant, "
        "host, src_file FROM ingest_security_audit ORDER BY ts DESC LIMIT %s",
        (limit,),
    )
