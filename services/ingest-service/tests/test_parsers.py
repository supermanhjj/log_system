"""五类解析器契约：命中返回字段字典，不匹配返回 None，定义错误抛 ValueError。"""
import pytest

from app import parsers


# ---------------- json ----------------
def test_json_extract_uses_dotted_path():
    definition = {"type": "json", "extract": {"ts": "@timestamp", "cost": "detail.costMs"}}
    text = '{"@timestamp": "2026-11-21T10:00:00Z", "detail": {"costMs": 12}}'
    assert parsers.parse(definition, text) == {"ts": "2026-11-21T10:00:00Z", "cost": 12}


def test_json_without_extract_keeps_scalar_top_level_only():
    text = '{"a": 1, "b": "x", "nested": {"c": 2}, "flag": true}'
    assert parsers.parse({"type": "json"}, text) == {"a": 1, "b": "x", "flag": True}


def test_json_non_object_payload_is_unmatched():
    assert parsers.parse({"type": "json"}, "[1, 2, 3]") is None


def test_json_invalid_text_is_unmatched():
    assert parsers.parse({"type": "json"}, "not json") is None


# ---------------- regex ----------------
def test_regex_returns_named_groups_only():
    definition = {"type": "regex", "pattern": r"(?P<ts>\S+) (?P<level>ERROR|INFO) (?P<msg>.*)"}
    assert parsers.parse(definition, "2026-11-21T10:00:00Z ERROR boom") == {
        "ts": "2026-11-21T10:00:00Z", "level": "ERROR", "msg": "boom"}


def test_regex_miss_returns_none():
    definition = {"type": "regex", "pattern": r"(?P<level>ERROR)"}
    assert parsers.parse(definition, "INFO ok") is None


def test_regex_without_pattern_raises_value_error():
    with pytest.raises(ValueError):
        parsers.parse({"type": "regex"}, "x")


# ---------------- delimiter ----------------
def test_delimiter_exact_columns():
    definition = {"type": "delimiter", "separator": "|", "fields": ["ts", "level", "msg"]}
    assert parsers.parse(definition, "2026-11-21 10:00:00|ERROR|boom") == {
        "ts": "2026-11-21 10:00:00", "level": "ERROR", "msg": "boom"}


def test_delimiter_extra_columns_is_mismatch():
    definition = {"type": "delimiter", "separator": "|", "fields": ["a", "b"]}
    assert parsers.parse(definition, "1|2|3") is None


def test_delimiter_missing_columns_is_mismatch():
    definition = {"type": "delimiter", "separator": "|", "fields": ["a", "b", "c"]}
    assert parsers.parse(definition, "1|2") is None


def test_delimiter_without_fields_raises_value_error():
    with pytest.raises(ValueError):
        parsers.parse({"type": "delimiter", "separator": "|"}, "1|2")


# ---------------- kv ----------------
def test_kv_filters_to_allowed_fields_and_strips_quotes():
    definition = {"type": "kv", "separator": "=", "split": " ", "fields": ["level"]}
    assert parsers.parse(definition, 'level="ERROR" user=admin') == {"level": "ERROR"}


def test_kv_without_pairs_returns_none():
    assert parsers.parse({"type": "kv", "separator": "=", "split": " "}, "no kv here") is None


# ---------------- dispatcher ----------------
def test_unknown_type_raises_value_error():
    with pytest.raises(ValueError):
        parsers.parse({"type": "xml"}, "<a/>")


def test_validate_lists_fields_for_each_type():
    assert parsers.validate({"type": "delimiter", "fields": ["a", "b"]})["fields"] == ["a", "b"]
    assert parsers.validate({"type": "regex", "pattern": "(?P<x>a)"})["fields"] == ["x"]


def test_validate_invalid_regex_raises_value_error():
    with pytest.raises(ValueError):
        parsers.validate({"type": "regex", "pattern": "(?P<x>a"})
