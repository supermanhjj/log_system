"""字段契约（docs/log-ingestion-model.md）：分类 / 级别 / 特有字段 / 脱敏 / 正文兜底。"""
from app import log_schema


# ---------------- 级别归一 ----------------
def test_level_explicit_and_word_forms():
    assert log_schema.normalize_level({"level": "ERROR"}) == "ERROR"
    assert log_schema.normalize_level({"level": "err"}) == "ERROR"
    assert log_schema.normalize_level({"level": "warning"}) == "WARN"
    assert log_schema.normalize_level({"level": "trace"}) == "DEBUG"


def test_level_from_syslog_severity_numbers():
    assert log_schema.normalize_level({"severity": 0}) == "FATAL"
    assert log_schema.normalize_level({"severity": "4"}) == "WARN"
    assert log_schema.normalize_level({"severity": 9}) == "UNKNOWN"


def test_level_from_redis_char_and_http_status():
    assert log_schema.normalize_level({"level_char": "#"}) == "WARN"
    assert log_schema.normalize_level({"status": 503}) == "ERROR"
    assert log_schema.normalize_level({"status": "404"}) == "WARN"
    assert log_schema.normalize_level({"status": 200}) == "UNKNOWN"


def test_level_without_hint_is_unknown_not_info():
    # 兜底成 INFO 会让「解析没生效」静默沉在信息级里，属于最难发现的数据质量问题
    assert log_schema.normalize_level({}) == "UNKNOWN"


def test_severity_number_keeps_zero_distinct_from_missing():
    assert log_schema.severity_number({"severity": 0}) == 0
    assert log_schema.severity_number({"severity": "7"}) == 7
    assert log_schema.severity_number({"severity": 8}) is None
    assert log_schema.severity_number({}) is None
    assert log_schema.severity_number({"severity_name": "err"}) == 3


# ---------------- 分类 / 来源 ----------------
def test_infer_log_class_prefers_explicit_then_source_type():
    assert log_schema.infer_log_class({"log_class": "system"}) == "system"
    assert log_schema.infer_log_class({"log_class": "bogus", "source_type": "mysql-slow"}) == "database"
    assert log_schema.infer_log_class({}) == "business"


def test_infer_source_type_uses_facility_first():
    assert log_schema.infer_source_type({"facility": "authpriv"}, "system") == "auth"
    assert log_schema.infer_source_type({"source_type": "nginx-access"}, "middleware") == "nginx-access"
    assert log_schema.infer_source_type({}, "database") == "database"


# ---------------- 特有字段 ----------------
def test_extract_attrs_moves_and_pops_type_specific_fields():
    doc = {"thread": "t-1", "rows_sent": 10, "dbuser": "@", "level": "INFO"}
    attrs = log_schema.extract_attrs(doc)
    assert attrs == {"thread": "t-1", "rows_sent": "10"}
    assert "thread" not in doc and "rows_sent" not in doc


# ---------------- 脱敏 ----------------
def test_sanitize_masks_credentials_and_is_idempotent():
    assert log_schema.sanitize_text("password=abc123") == "password=***"
    assert log_schema.sanitize_text("Authorization: Bearer abc.def.ghi") == "Authorization: Bearer ***"
    assert log_schema.sanitize_text("connect postgres://user:pwd123@db:5432/app") == (
        "connect postgres://user:***@db:5432/app")
    once = log_schema.sanitize_text("token=abcdef")
    assert once == "token=***"
    assert log_schema.sanitize_text(once) == once


# ---------------- 正文 ----------------
def test_build_message_fallback_order():
    assert log_schema.build_message({"message": "m"}) == "m"
    assert log_schema.build_message({"request": "GET / 500"}) == "GET / 500"
    assert log_schema.build_message({"raw": "r"}) == "r"
    assert log_schema.build_message({"log": "k8s-body"}) == "k8s-body"
    assert log_schema.build_message({}) == ""


def test_normalize_composes_contract_fields():
    doc = {"app": "nginx", "level": "err", "pid": "123", "message": "", "log": "boom"}
    out = log_schema.normalize(doc)
    assert out["level"] == "ERROR"
    assert out["source"] == "nginx"
    assert out["message"] == "boom"
    assert out["pid"] == 123


def test_normalize_drops_invalid_pid_without_losing_record():
    out = log_schema.normalize({"pid": "abc", "message": "x", "level": "INFO"})
    assert "pid" not in out
    assert out["message"] == "x"


def test_normalize_masks_message_and_raw():
    out = log_schema.normalize({"message": "password=hunter2", "raw": "token=abc", "level": "INFO"})
    assert out["message"] == "password=***"
    assert out["raw"] == "token=***"
