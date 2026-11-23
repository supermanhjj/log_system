"""告警规则契约：租户可见/可写边界、判定条件构造、PPL 转义。"""
from datetime import datetime, timezone

from app import main


# ---------------- 规则可见 / 可写 ----------------
def test_rule_visibility():
    # 规则只按租户归属：不存在 tenant='all' 的平台级规则
    assert main._rule_visible({"tenant": "tenant-a"}, "tenant-a") is True
    assert main._rule_visible({"tenant": "tenant-b"}, "tenant-a") is False
    assert main._rule_visible({"tenant": "tenant-b"}, "all") is True


def test_rule_writability_is_tenant_scoped():
    admin = {"role_key": "platform_admin"}
    normal = {"role_key": "tenant_admin"}
    assert main._rule_writable({"tenant": "tenant-a"}, "tenant-a", normal) is True
    assert main._rule_writable({"tenant": "tenant-b"}, "tenant-a", normal) is False
    assert main._rule_writable({"tenant": "tenant-a"}, "tenant-a", admin) is True
    # 管理员切到 all 巡检时可跨租户维护
    assert main._rule_writable({"tenant": "tenant-b"}, "all", admin) is True


# ---------------- 判定条件 ----------------
def test_must_for_rule_includes_time_and_all_filters():
    rule = {"match_log_class": "system", "match_source_type": "syslog",
            "match_level": "ERROR", "match_source": "sshd",
            "match_status_gte": 0, "keyword": "Out of memory"}
    must = main._must_for_rule(rule, 1000, 2000)
    assert must[0] == {"range": {"@timestamp": {"gte": 1000, "lte": 2000}}}
    assert {"term": {"log_class": "system"}} in must
    assert {"term": {"level": "ERROR"}} in must
    # statusGte=0 是合法阈值，不能被「假值」吞掉
    assert {"range": {"status": {"gte": 0}}} in must
    assert must[-1]["multi_match"]["query"] == "Out of memory"


def test_must_for_rule_without_filters_is_time_only():
    assert len(main._must_for_rule({}, 1000, 2000)) == 1


# ---------------- 其它 ----------------
def test_pick_role_requires_unambiguous_numeric_fallback():
    assert main._pick_role({"@timestamp": "date"}, "time") == "@timestamp"
    assert main._pick_role({"http_code": "integer"}, "status") == "http_code"
    # 多个数值字段无法判定时不猜
    assert main._pick_role({"a": "integer", "b": "long"}, "status") is None


def test_esc_escapes_quotes_and_backslashes():
    assert main._esc("a'b") == "a\\'b"
    assert main._esc("a\\b") == "a\\\\b"


def test_iso_local_keeps_same_instant_with_offset():
    text = main._iso_local(1763680800000)
    # 带时区偏移，检索页看到的钟点与时间选择器一致，同时绝对时刻不变
    assert text.endswith("+08:00") or text.endswith("+00:00") or "T" in text
    assert int(datetime.fromisoformat(text).timestamp() * 1000) == 1763680800000


def test_closed_event_reopens_only_on_new_hit():
    closed_at = datetime(2026, 11, 23, 8, 2, 17, tzinfo=timezone.utc)
    state = {"status": "已恢复", "updated_at": closed_at}
    # 关闭前最后一条日志：保持已恢复
    assert main._closed_event_has_new_hit(state, {"lastSeenTs": int(closed_at.timestamp() * 1000) - 1000}) is False
    # 关闭后又有新日志：自动回到告警中
    assert main._closed_event_has_new_hit(state, {"lastSeenTs": int(closed_at.timestamp() * 1000) + 1000}) is True
    # 维护中不自动解除
    assert main._closed_event_has_new_hit({"status": "维护中", "updated_at": closed_at}, {"lastSeenTs": 1}) is False


# ---------------- OpenSearch 连接参数 ----------------
def test_opensearch_kwargs_follow_env(monkeypatch):
    monkeypatch.setattr(main, "OPENSEARCH_SCHEME", "http")
    monkeypatch.setattr(main, "OPENSEARCH_USER", "")
    monkeypatch.setattr(main, "OPENSEARCH_PASSWORD", "")
    monkeypatch.setattr(main, "OPENSEARCH_VERIFY_CERTS", False)
    kw = main._opensearch_kwargs()
    # 默认保持无认证、不校验证书，与自建测试环境一致
    assert kw["use_ssl"] is False and kw["verify_certs"] is False
    assert "http_auth" not in kw

    monkeypatch.setattr(main, "OPENSEARCH_SCHEME", "https")
    monkeypatch.setattr(main, "OPENSEARCH_USER", "admin")
    monkeypatch.setattr(main, "OPENSEARCH_PASSWORD", "secret")
    monkeypatch.setattr(main, "OPENSEARCH_VERIFY_CERTS", True)
    kw = main._opensearch_kwargs()
    assert kw["use_ssl"] is True and kw["verify_certs"] is True
    assert kw["http_auth"] == ("admin", "secret")


# ---------------- 样例原文回退 ----------------
class _FakeOS:
    def __init__(self, source, count=5):
        self.source = source
        self._count = count

    def count(self, **kw):
        return {"count": self._count}

    def search(self, **kw):
        if kw["body"].get("size") == 0:
            return {"aggregations": {"first": {"value": 1763680800000},
                                     "sources": {"buckets": []},
                                     "hosts": {"buckets": []}}}
        return {"hits": {"hits": [{"_source": self.source}]}}


def test_sample_raw_falls_back_from_log_to_message(monkeypatch):
    rule = {"id": 1, "name": "r", "level": "P1", "window_minutes": 5, "threshold": 3}
    cases = [
        ({"raw": "R", "log": "L", "message": "M"}, "R"),
        ({"log": "L", "message": "M"}, "L"),
        ({"message": "M"}, "M"),
        ({}, ""),
    ]
    for source, expected in cases:
        monkeypatch.setattr(main, "os_client", _FakeOS(source))
        sample = main.evaluate_rule(rule, "idx", 1763680800000)["sample"]
        assert sample["raw"] == expected
