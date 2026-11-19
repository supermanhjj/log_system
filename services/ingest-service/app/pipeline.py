"""服务端解析管线执行层：匹配 → 抽取 → 映射 → 归属建议。

影子模式（PIPELINE_MODE=shadow，默认）
    老链路的 normalize_record 结果照常入库，新管线结果写进 attrs.__shadow，
    两边可在 OpenSearch 里逐条比对。确认一致后才切 active。
    这是唯一安全的切换方式：解析规则配错了，影子模式只多存几个字段，
    不会把 level / @timestamp 这些公共维度改坏。

active 模式
    解析结果覆盖到契约字段，parse_status=parsed。未匹配仍保留原文并标
    parse_status=unmatched，绝不丢弃——否则现场「配了没生效」无从发现。

规则缓存 30 秒：改规则后最多 30 秒全集群生效，避免每条日志都查 PG。
"""
import fnmatch
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import log_schema, parsers

MODE = os.getenv("PIPELINE_MODE", "shadow").strip().lower()
if MODE not in ("off", "shadow", "active"):
    MODE = "shadow"
CACHE_TTL = int(os.getenv("PIPELINE_CACHE_TTL", "30"))

_cache: Dict[str, Any] = {"at": 0.0, "rules": [], "parsers": {}}


def invalidate() -> None:
    _cache["at"] = 0.0


def _load() -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    now = time.time()
    if _cache["rules"] and now - _cache["at"] < CACHE_TTL:
        return _cache["rules"], _cache["parsers"]
    from . import pipeline_store  # 延迟导入，避免模块初始化时触碰数据库

    rules = pipeline_store.list_pipelines(enabled_only=True)
    pmap = {int(p["id"]): p for p in pipeline_store.list_parsers()}
    _cache.update({"at": now, "rules": rules, "parsers": pmap})
    return rules, pmap


def _iso_utc(value: Any) -> Optional[str]:
    """把 grok 抽出的时间归一为 UTC ISO 毫秒；失败返回 None（不猜）。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # 毫秒
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return None
    s = str(value).strip()
    try:
        f = float(s)
        return _iso_utc(f)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
        except ValueError:
            continue
    return s if "T" in s and s.endswith("Z") else None


def matches(rule: Dict[str, Any], doc: Dict[str, Any]) -> bool:
    """规则匹配：所有条件 AND。列表值表示任一命中。"""
    m = rule.get("match") or {}
    for key in ("log_class", "source_type", "app", "source", "host", "level"):
        want = m.get(key)
        if not want:
            continue
        wanted = want if isinstance(want, list) else [want]
        got = str(doc.get(key) or "")
        if got not in {str(w) for w in wanted}:
            return False
    globs = m.get("path_glob")
    if globs:
        src = str(doc.get("src_file") or "")
        patterns = globs if isinstance(globs, list) else [globs]
        if not any(fnmatch.fnmatch(src, str(g)) for g in patterns):
            return False
    return True


def _apply_mapping(fields: Dict[str, Any], mapping: Dict[str, Any]) -> Dict[str, Any]:
    """映射：目标字段 ← 解析字段。支持 "字段:int" / ":float" 类型转换。"""
    out: Dict[str, Any] = {}
    for target, spec in (mapping or {}).items():
        src = str(spec)
        cast = None
        if ":" in src and src.rsplit(":", 1)[1] in ("int", "float", "str"):
            src, cast = src.rsplit(":", 1)
        value = fields.get(src)
        if value is None:
            value = fields.get(src.split(".")[-1])
        if value is None or value == "":
            continue
        try:
            if cast == "int":
                value = int(float(value))
            elif cast == "float":
                value = float(value)
            elif cast == "str":
                value = str(value)
        except (ValueError, TypeError):
            continue
        out[str(target)] = value
    return out


def execute_one(doc: Dict[str, Any],
                rules: List[Dict[str, Any]],
                pmap: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """按 priority 从小到大找第一条命中的规则并执行。"""
    raw = doc.get("raw") or doc.get("message") or ""
    ordered = sorted(rules, key=lambda r: int(r.get("priority") or 100))
    for rule in ordered:
        if not matches(rule, doc):
            continue
        pdef = pmap.get(int(rule.get("parser_id") or 0))
        if not pdef:
            continue
        try:
            fields = parsers.parse(pdef.get("definition") or {}, raw)
        except Exception as e:  # noqa: BLE001
            # 单条规则定义写错不能阻断整批日志
            continue
        if not fields:
            continue
        mapped = _apply_mapping(fields, rule.get("mapping") or {})
        if mapped.get("@timestamp") is not None:
            iso = _iso_utc(mapped["@timestamp"])
            if iso:
                mapped["@timestamp"] = iso
        if mapped.get("level") is not None:
            mapped["level"] = log_schema.normalize_level({"level": str(mapped["level"])})
        return {
            "matched": True,
            "pipelineId": rule.get("id"),
            "pipelineName": rule.get("name"),
            "parserId": pdef.get("id"),
            "fields": fields,
            "mapped": mapped,
            "tenantCode": rule.get("tenant_code") or "",
        }
    return {"matched": False, "pipelineId": None, "fields": {}, "mapped": {},
            "tenantCode": ""}


def process(docs: List[Dict[str, Any]], tenant: str = "",
            mode: Optional[str] = None) -> List[Dict[str, Any]]:
    """就地处理一批日志。模式 off/shadow/active，见模块说明。"""
    use_mode = (mode or MODE).lower()
    if use_mode == "off" or not docs:
        return docs
    try:
        rules, pmap = _load()
    except Exception as e:  # noqa: BLE001
        # 数据库不可用时不能让日志断流
        print(f"[pipeline] 规则加载失败（本批跳过解析）: {e}", flush=True)
        return docs
    if not rules:
        return docs

    for doc in docs:
        try:
            result = execute_one(doc, rules, pmap)
        except Exception as e:  # noqa: BLE001
            print(f"[pipeline] 解析异常（保留原记录）: {e}", flush=True)
            doc.setdefault("attrs", {})["__parse_error"] = str(e)[:120]
            continue

        if use_mode == "active" and result["matched"]:
            for key, value in result["mapped"].items():
                if key.startswith("attrs."):
                    doc.setdefault("attrs", {})[key[len("attrs."):]] = value
                else:
                    doc[key] = value
            doc["parse_status"] = "parsed"
        else:
            # 影子模式只记录，不动主字段；未匹配也要标出来供治理
            shadow = {
                "pipelineId": result["pipelineId"],
                "fields": result["fields"],
                "mapped": result["mapped"],
            }
            doc.setdefault("attrs", {})["__shadow"] = shadow
            doc["parse_status"] = "parsed" if result["matched"] else "unmatched"
        if result["tenantCode"]:
            # 归属建议只记录，真正归属由 E 批次的安全校验决定，
            # 不允许解析规则直接写租户——那是伪造漏洞的入口。
            doc.setdefault("attrs", {})["__tenant_hint"] = result["tenantCode"]
    return docs


def preview(parser_def: Dict[str, Any], sample: str,
            mapping: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """试解析：贴一段真实日志，实时看抽取和映射结果。现场适配的核心交互。"""
    fields = parsers.parse(parser_def, sample)
    if fields is None:
        return {"matched": False, "fields": {}, "mapped": {}}
    mapped = _apply_mapping(fields, mapping or {})
    if mapped.get("@timestamp"):
        mapped["@timestamp"] = _iso_utc(mapped["@timestamp"]) or mapped["@timestamp"]
    return {"matched": True, "fields": fields, "mapped": mapped}
