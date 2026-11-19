"""轻量 grok 引擎：%{PATTERN:field} → 命名捕获组。

为什么不引入 grok / pygrok 包：
    现场是国网内网，连 pip 源都没有；新增依赖意味着交付包要多带 wheel，
    还可能在信创架构上没有对应版本。grok 本质是「命名的正则片段集合」，
    用标准库 re 实现即可，模式表开放给用户扩展，能力不打折。

内置模式覆盖 Java logback / syslog / nginx / PostgreSQL 等常见格式。
字段名只允许 [A-Za-z_][A-Za-z0-9_]*（Python 命名组限制）；需要写进 attrs
的点号字段通过 pipeline 的 mapping 完成，例如 attrs.thread ← thread。
"""
import re
from typing import Any, Dict, Optional

PATTERNS: Dict[str, str] = {
    # ---- 通用 ----
    "WORD": r"\w+",
    "DATA": r".*?",
    "GREEDYDATA": r".*",
    "NOTSPACE": r"\S+",
    "SPACE": r"\s*",
    "INT": r"[+-]?\d+",
    "NUMBER": r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)",
    "BASE10NUM": r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)",
    "QUOTEDSTRING": r'"(?:[^"\\]|\\.)*"',
    "UUID": r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}",
    "IP": r"(?:\d{1,3}\.){3}\d{1,3}",
    "HOSTNAME": r"[A-Za-z0-9][A-Za-z0-9_.-]*",
    "PATH": r"(?:/[\w._+~-]+)+/?",
    "MAC": r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}",
    # ---- 时间 ----
    "YEAR": r"\d{4}",
    "MONTHNUM": r"(?:0?[1-9]|1[0-2])",
    "MONTHDAY": r"(?:0?[1-9]|[12]\d|3[01])",
    "HOUR": r"(?:[01]\d|2[0-3])",
    "MINUTE": r"[0-5]\d",
    "SECOND": r"(?:[0-5]\d|60)",
    "TIMESTAMP_ISO8601": (
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    ),
    "SYSLOGTIMESTAMP": r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}",
    "HTTPDATE": r"\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}\s[+-]\d{4}",
    # ---- 级别 / 语言 ----
    "LOGLEVEL": (
        r"(?:TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|SEVERE)"
    ),
    "JAVACLASS": r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*",
    "JAVATHREAD": r"[^\]]+",
    "PYTHONFUNC": r"[\w.]+",
    # ---- 网络设备 / Web ----
    "SYSLOG5424PRI": r"<(?:\d{1,3})>",
    "HTTPSTATUS": r"[1-5]\d{2}",
    "HTTPMETHOD": r"(?:GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH|TRACE|CONNECT)",
    "URIPATH": r"/\S*",
    "COMMONAPACHELOG": (
        r"%{IP:clientip}\s+%{NOTSPACE:ident}\s+%{NOTSPACE:auth}\s+"
        r"\[%{HTTPDATE:ts}\]\s+\"%{HTTPMETHOD:method}\s+%{URIPATH:request}\s+%{NOTSPACE:proto}\"\s+"
        r"%{HTTPSTATUS:status}\s+%{INT:bytes}"
    ),
}

_TOKEN = re.compile(r"%\{(\w+)(?::([A-Za-z_]\w*))?\}")
_FIELD = re.compile(r"^[A-Za-z_]\w*$")


class GrokError(ValueError):
    pass


def _expand(pattern: str, depth: int = 0) -> str:
    """展开模式里对内置模式的嵌套引用（如 COMMONAPACHELOG 引用 IP）。"""
    if depth > 5:
        raise GrokError("grok 模式嵌套超过 5 层，可能存在循环引用")
    if "%{" not in pattern:
        return pattern

    def repl(m: re.Match) -> str:
        name, field = m.group(1), m.group(2)
        inner = PATTERNS.get(name)
        if inner is None:
            raise GrokError(f"未知 grok 模式：{name}")
        # 只展开无别名的引用；带别名的必须保留为捕获组
        if field:
            return f"(?P<{field}>{_expand(inner, depth + 1)})"
        return f"(?:{_expand(inner, depth + 1)})"

    return _TOKEN.sub(repl, pattern)


def compile_grok(pattern: str) -> re.Pattern:
    """把 grok 表达式编译成正则。带别名的 token 成为命名捕获组。"""
    if not pattern or not isinstance(pattern, str):
        raise GrokError("grok pattern 不能为空")
    expanded = _expand(pattern)
    try:
        return re.compile(expanded)
    except re.error as e:
        raise GrokError(f"grok 表达式不是合法正则：{e}") from e


def parse(pattern: str, text: str) -> Optional[Dict[str, Any]]:
    """解析单行文本，成功返回命中的字段字典；未命中返回 None。"""
    rx = compile_grok(pattern)
    m = rx.search(text or "")
    if not m:
        return None
    out: Dict[str, Any] = {}
    for k, v in m.groupdict().items():
        if v is not None:
            out[k] = v
    # 无名捕获组也保留（用 group1.. 命名），供纯模式匹配场景
    for i, v in enumerate(m.groups(), 1):
        if v is not None and m.re.groupindex.get(str(i)) is None:
            out.setdefault(f"group{i}", v)
    return out


def validate_pattern(pattern: str) -> Dict[str, Any]:
    """保存解析器前的校验：返回捕获字段列表，前端可即时提示。"""
    rx = compile_grok(pattern)
    return {"fields": sorted(rx.groupindex.keys()), "regex": rx.pattern}
