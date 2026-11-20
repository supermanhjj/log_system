"""多类型日志的字段契约与归一化规则。

契约定义见 `docs/log-ingestion-model.md`。本模块只做**纯函数**，不碰网络/数据库，
便于单测。接入网关（main.py）在落库前调用 `normalize()`。

为什么需要这一层：
    系统日志带 facility/severity，中间件带 upstream 状态，数据库带慢查询耗时——
    它们的"级别"和"关键指标"表达方式完全不同（`authpriv.warn` / `#` / `[Warning]`）。
    不归一就直接入库，检索页的 level 维度会变成一团乱麻，告警规则也没法写。
"""
import re
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
LOG_CLASSES = {"system", "middleware", "database", "business", "network", "container"}

# source_type → log_class：采集端已打标，这里作为校验与兜底推导
SOURCE_TYPE_CLASS: Dict[str, str] = {
    "syslog": "system", "auth": "system", "kern": "system", "cron": "system",
    "journald": "system", "dmesg": "system",
    "nginx-access": "middleware", "nginx-error": "middleware", "redis": "middleware",
    "kafka": "middleware", "tomcat": "middleware", "rabbitmq": "middleware",
    "zookeeper": "middleware", "haproxy": "middleware",
    "mysql-error": "database", "mysql-slow": "database", "postgres": "database",
    "oracle-alert": "database", "mssql-error": "database",
    "app": "business",
    "firewall": "network", "switch": "network",
}

# syslog facility → 更精确的 source_type（采集端只能笼统标 syslog）
FACILITY_SOURCE_TYPE: Dict[str, str] = {
    "auth": "auth", "authpriv": "auth", "security": "auth",
    "kern": "kern", "cron": "cron", "dmesg": "kern",
}

# syslog severity 数字（RFC5424 0-7）
SEVERITY_TO_LEVEL: Dict[int, str] = {
    0: "FATAL", 1: "FATAL", 2: "FATAL",
    3: "ERROR", 4: "WARN", 5: "INFO", 6: "INFO", 7: "DEBUG",
}

# 各类日志自带的级别词（小写归一后映射）
LEVEL_WORDS: Dict[str, str] = {
    "emerg": "FATAL", "emergency": "FATAL", "alert": "FATAL", "crit": "FATAL",
    "critical": "FATAL", "panic": "FATAL", "fatal": "FATAL",
    "err": "ERROR", "error": "ERROR", "severe": "ERROR",
    "warn": "WARN", "warning": "WARN",
    "notice": "INFO", "info": "INFO", "log": "INFO", "statement": "INFO",
    "system": "INFO", "note": "INFO", "detail": "INFO",
    "debug": "DEBUG", "trace": "DEBUG",
}

# redis 日志的级别标记字符：* notice / # warning / - info / . debug
REDIS_LEVEL_CHAR: Dict[str, str] = {"#": "WARN", "*": "INFO", "-": "INFO", ".": "DEBUG"}

# 各类型特有指标 → 收进 attrs，避免污染通用 schema
ATTR_KEYS = (
    "query_time", "lock_time", "rows_sent", "rows_examined", "dbuser",
    "remote_addr", "remote_user", "request", "body_bytes", "referer", "user_agent",
    "err_code", "thread", "conn_id", "tid", "role", "duration_ms", "syslog_severity",
)


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------
def infer_log_class(doc: Dict[str, Any]) -> str:
    """日志大类：优先采信采集端打标，非法/缺失时按 source_type 推导，最后归 business。"""
    raw = str(doc.get("log_class") or "").strip().lower()
    if raw in LOG_CLASSES:
        return raw
    st = str(doc.get("source_type") or "").strip().lower()
    if st in SOURCE_TYPE_CLASS:
        return SOURCE_TYPE_CLASS[st]
    return "business"


def infer_source_type(doc: Dict[str, Any], log_class: str) -> str:
    """细分来源：系统日志按 facility 细分（authpriv→auth、kern→kern），其余沿用采集端标值。"""
    st = str(doc.get("source_type") or "").strip().lower()
    facility = str(doc.get("facility") or "").strip().lower()
    if facility in FACILITY_SOURCE_TYPE:
        return FACILITY_SOURCE_TYPE[facility]
    if st in SOURCE_TYPE_CLASS:
        return st
    return {"system": "syslog", "middleware": "middleware", "database": "database"}.get(
        log_class, "app"
    )


# ---------------------------------------------------------------------------
# 级别归一
# ---------------------------------------------------------------------------
def normalize_level(doc: Dict[str, Any]) -> str:
    """把各种表达方式统一成 DEBUG/INFO/WARN/ERROR/FATAL。

    优先级：显式 level > syslog severity 数字 > severity 词 > redis 级别字符 > HTTP 状态码
    """
    lv = str(doc.get("level") or "").strip()
    if lv.upper() in ("DEBUG", "INFO", "WARN", "ERROR", "FATAL"):
        return lv.upper()
    if lv:
        mapped = LEVEL_WORDS.get(lv.lower())
        if mapped:
            return mapped

    sev = doc.get("severity")
    if sev is not None:
        try:
            return SEVERITY_TO_LEVEL.get(int(sev), "UNKNOWN")
        except (TypeError, ValueError):
            pass

    sev_name = str(doc.get("severity_name") or "").strip().lower()
    if sev_name in LEVEL_WORDS:
        return LEVEL_WORDS[sev_name]

    ch = str(doc.get("level_char") or "").strip()
    if ch in REDIS_LEVEL_CHAR:
        return REDIS_LEVEL_CHAR[ch]

    # nginx access：没有级别字段，用状态码推导（5xx=ERROR, 4xx=WARN）
    try:
        status = int(doc.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status >= 500:
        return "ERROR"
    if status >= 400:
        return "WARN"
    # 没有任何级别线索时返回 UNKNOWN，**不能兜底成 INFO**：
    # INFO 是合法级别，兜底会让"解析器没匹配上/上游改了格式"静默沉在信息级里，
    # 表现为"日志有、但级别统计永远很干净"，属于最难发现的一类数据质量问题。
    return "UNKNOWN"


def severity_number(doc: Dict[str, Any]) -> Optional[int]:
    """syslog 原生 severity（0-7），非 syslog 日志返回 None（写 0 会与 emerg 混淆）。"""
    for key in ("severity", "syslog_severity"):
        v = doc.get(key)
        if v is None or v == "":
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= n <= 7:
            return n
    name = str(doc.get("severity_name") or "").strip().lower()
    reverse = {"emerg": 0, "alert": 1, "crit": 2, "err": 3, "warning": 4,
               "notice": 5, "info": 6, "debug": 7}
    return reverse.get(name)


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
# 数据库日志、连接串里出现明文凭证是最常见的数据泄露面（等保三级要求）
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[=:]\s*(['\"]?)([^\s'\"&;,]+)\2"), r"\1=***"),
    (re.compile(r"(?i)(--password[= ]+)([^\s'\"]+)"), r"\1***"),
    (re.compile(r"(?i)\b(authorization)\s*:\s*(bearer|basic)\s+[A-Za-z0-9._\-+/=]+"), r"\1: \2 ***"),
    (re.compile(r"(?i)\b(token|secret|access_key|accesskey|api_key|apikey)\s*[=:]\s*(['\"]?)([^\s'\"&;,]+)\2"), r"\1=***"),
    # URL 里的 user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):([^/\s@]+)@"), r"\1:***@"),
)


def sanitize_text(text: str) -> str:
    """对正文/原文做凭证打码。幂等：已打码的 *** 不会被再次改写。"""
    if not text:
        return text
    out = text
    for pat, rep in _SECRET_PATTERNS:
        out = pat.sub(rep, out)
    return out


# ---------------------------------------------------------------------------
# 整合
# ---------------------------------------------------------------------------
def extract_attrs(doc: Dict[str, Any]) -> Dict[str, str]:
    """把类型特有字段收进 attrs（OpenSearch 用 flat_object，ClickHouse 存 JSON 串）。"""
    attrs: Dict[str, str] = {}
    for k in ATTR_KEYS:
        if k in doc and doc[k] not in (None, ""):
            val = str(doc[k]).strip()
            # 「@」是 PostgreSQL 无会话用户时的 prefix 产物（%u@%d 都为空），
            # 不是有效用户名；留着会让 attrs.dbuser 出现一堆无意义的 "@"
            if k == "dbuser" and val.strip("@ ") == "":
                continue
            if val:
                attrs[k] = val
    # 采集端解析出来的辅助字段，逻辑归属已体现在 level 上，不再单列
    for k in ("severity_name", "level_char", "syslog_tag", "syslog_host"):
        doc.pop(k, None)
    for k in ATTR_KEYS:
        doc.pop(k, None)
    return attrs


def build_message(doc: Dict[str, Any]) -> str:
    """保证 message 可读，且任何一条日志都不能出现"空正文"。

    取值顺序按「原文保真度」排列：
      1. message —— 采集端/解析器已抽好的正文
      2. request —— nginx access 没有正文，用 "方法 路径 状态" 合成
      3. raw     —— 非 K8s 来源（Fluent Bit 文本解析器整体捕获的原始行）
      4. log     —— K8s 容器日志：CRI parser 把正文拆到 log，raw/message 都可能为空。
                    这是上 K8s 后「检索页内容列全空」的直接原因，必须兜到这一层。
    绝不返回空串：空正文让现场无法判断"是没数据还是解析没生效"。
    """
    msg = str(doc.get("message") or "").strip()
    if msg:
        return msg
    request = doc.get("request")
    if request:
        return str(request)
    for key in ("raw", "log"):
        val = str(doc.get(key) or "").strip()
        if val:
            return val
    return ""


def normalize(doc: Dict[str, Any]) -> Dict[str, Any]:
    """对一条记录做分类/级别/字段归一（不处理时间与租户，那是网关的职责）。"""
    out = dict(doc)

    log_class = infer_log_class(out)
    source_type = infer_source_type(out, log_class)
    level = normalize_level(out)
    sev = severity_number(out)

    # 先把特有字段抽走（会从 out 中 pop 掉这些键）
    attrs = extract_attrs(out)

    out["log_class"] = log_class
    out["source_type"] = source_type
    out["level"] = level
    out["message"] = sanitize_text(build_message(out))
    if out.get("raw"):
        out["raw"] = sanitize_text(str(out["raw"]))
    if sev is not None:
        out["severity"] = sev
    if attrs:
        out["attrs"] = attrs

    # pid 必须是整数，解析器给的是字符串；非法值直接丢弃，避免 ES/CH 写失败
    if "pid" in out:
        try:
            out["pid"] = int(out["pid"])
        except (TypeError, ValueError):
            out.pop("pid", None)

    return out
