"""五类解析器：json / grok / regex / delimiter / kv。

统一契约：parse(definition, text) -> Dict[str, Any] | None
    None 表示「这条日志不匹配该解析器」，调用方据此标 parse_status=unmatched，
    日志本身仍要入库——静默丢弃会让现场「配了没生效」无从发现。

definition 是数据库里 log_parsers.definition 的 JSON，按 type 不同结构不同：
    json       {"extract": {"ts": "@timestamp", "msg": "message"}}  # 可选，点号路径
    grok       {"pattern": "%{TIMESTAMP_ISO8601:ts} %{LOGLEVEL:level} %{GREEDYDATA:msg}"}
    regex      {"pattern": "(?P<ts>\\S+) (?P<level>ERROR|INFO) (?P<msg>.*)"}
    delimiter  {"separator": "|", "fields": ["ts", "level", "msg"]}
    kv         {"separator": "=", "split": " ", "fields": ["ts", "level"]}  # fields 可选过滤
"""
import csv
import io
import json
import re
from typing import Any, Dict, Optional

from . import grok

PARSER_TYPES = ("json", "grok", "regex", "delimiter", "kv")


def _get_path(obj: Any, path: str) -> Any:
    """按点号路径取值，如 detail.costMs。"""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _parse_json(definition: Dict[str, Any], text: Any) -> Optional[Dict[str, Any]]:
    if isinstance(text, dict):
        obj = text
    else:
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return None
    if not isinstance(obj, dict):
        return None
    extract = definition.get("extract")
    if isinstance(extract, dict) and extract:
        return {k: v for k, v in
                ((k, _get_path(obj, str(p))) for k, p in extract.items())
                if v is not None}
    # 未指定 extract 时取全部标量顶层字段；嵌套对象留给 attrs
    return {k: v for k, v in obj.items()
            if isinstance(v, (str, int, float, bool))}


def _parse_regex(definition: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    pattern = definition.get("pattern")
    if not pattern:
        raise ValueError("regex 解析器缺少 pattern")
    m = re.compile(pattern).search(text or "")
    if not m:
        return None
    return {k: v for k, v in m.groupdict().items() if v is not None}


def _parse_delimiter(definition: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    sep = definition.get("separator", ",")
    fields = definition.get("fields") or []
    if not fields:
        raise ValueError("delimiter 解析器缺少 fields")
    row = next(csv.reader(io.StringIO(text or ""), delimiter=sep), None)
    if not row:
        return None
    out = {str(f): v.strip() for f, v in zip(fields, row)}
    # 列数不符视为格式不符，宁可标 unmatched，也不要悄悄丢列
    return out if len(out) == len(fields) else None


def _parse_kv(definition: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    sep = definition.get("separator", "=")
    split = definition.get("split", " ")
    allowed = set(definition.get("fields") or [])
    out: Dict[str, Any] = {}
    for item in re.split(split, text or ""):
        if not item or sep not in item:
            continue
        k, v = item.split(sep, 1)
        k = k.strip()
        if allowed and k not in allowed:
            continue
        out[k] = v.strip().strip('"').strip("'")
    return out or None


def parse(definition: Dict[str, Any], text: Any) -> Optional[Dict[str, Any]]:
    """按解析器类型抽取字段。未匹配返回 None；定义错误抛 ValueError。"""
    if not isinstance(definition, dict):
        raise ValueError("解析器定义必须是 JSON 对象")
    kind = str(definition.get("type") or "").lower()
    if kind not in PARSER_TYPES:
        raise ValueError(f"不支持的解析器类型：{kind}（可选 {'/'.join(PARSER_TYPES)}）")
    if kind == "json":
        return _parse_json(definition, text)
    if kind == "grok":
        return grok.parse(str(definition.get("pattern") or ""), text if isinstance(text, str) else str(text))
    if kind == "regex":
        return _parse_regex(definition, text if isinstance(text, str) else str(text))
    if kind == "delimiter":
        return _parse_delimiter(definition, text if isinstance(text, str) else str(text))
    return _parse_kv(definition, text if isinstance(text, str) else str(text))


def validate(definition: Dict[str, Any]) -> Dict[str, Any]:
    """保存解析器前的校验：编译一次，返回字段列表供 UI 提示。"""
    kind = str((definition or {}).get("type") or "").lower()
    if kind not in PARSER_TYPES:
        raise ValueError(f"不支持的解析器类型：{kind}")
    if kind == "grok":
        return grok.validate_pattern(str(definition.get("pattern") or ""))
    if kind == "regex":
        rx = re.compile(str(definition.get("pattern") or ""))
        return {"fields": sorted(rx.groupindex.keys())}
    if kind == "delimiter":
        fields = definition.get("fields") or []
        if not fields:
            raise ValueError("delimiter 解析器需要 fields")
        return {"fields": [str(f) for f in fields]}
    if kind == "kv":
        return {"fields": [str(f) for f in (definition.get("fields") or [])]}
    return {"fields": sorted((definition.get("extract") or {}).keys())}
