"""grok 引擎契约：命名捕获只产出命名字段；未命名捕获转 groupN；未命中返回 None。"""
import pytest

from app import grok


def test_named_capture_returns_only_named_fields():
    fields = grok.parse("%{LOGLEVEL:level} %{GREEDYDATA:message}", "ERROR boom")
    assert fields == {"level": "ERROR", "message": "boom"}


def test_unnamed_group_is_kept_as_groupn():
    fields = grok.parse("(ERROR|WARN)", "ERROR")
    assert fields == {"group1": "ERROR"}


def test_no_match_returns_none():
    assert grok.parse("%{LOGLEVEL:level}", "nothing to see") is None


def test_nested_builtin_pattern_expands():
    line = ('10.1.2.3 - - [21/Nov/2026:10:00:00 +0800] '
            '"GET /health HTTP/1.1" 200 12')
    fields = grok.parse("%{COMMONAPACHELOG}", line)
    assert fields["clientip"] == "10.1.2.3"
    assert fields["status"] == "200"


def test_unknown_pattern_raises_grok_error():
    with pytest.raises(grok.GrokError):
        grok.compile_grok("%{NO_SUCH_PATTERN:field}")


def test_empty_pattern_raises_grok_error():
    with pytest.raises(grok.GrokError):
        grok.compile_grok("")


def test_validate_pattern_reports_fields_and_regex():
    result = grok.validate_pattern("%{LOGLEVEL:level} %{GREEDYDATA:message}")
    assert result["fields"] == ["level", "message"]
    assert "(?P<level>" in result["regex"]
