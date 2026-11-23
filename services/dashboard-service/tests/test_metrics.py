"""运行总览指标契约：P95 插值、环比、目标值重判、时间标签。"""
from app import main


def test_percentile_interpolates():
    assert main.percentile([], 0.95) == 0.0
    assert main.percentile([7], 0.95) == 7.0
    assert main.percentile([10, 20, 30, 40], 0.5) == 25.0
    assert main.percentile([10, 20, 30, 40], 0.95) == 38.5


def test_pct_change():
    assert main.pct_change(120, 100) == 20.0
    assert main.pct_change(80, 100) == -20.0
    # 上一周期为 0 时百分比无定义，返回 0 而不是无穷大
    assert main.pct_change(50, 0) == 0.0


def test_apply_target_rejudges_even_when_cached():
    cached = {"stats": {"searchP95": {"value": 900, "status": "normal", "statusText": "达标"}},
              "latency": {"points": [1, 2]}}
    out = main.apply_target(cached, 800)
    assert out["stats"]["searchP95"]["status"] == "warning"
    assert out["stats"]["searchP95"]["statusText"] == "超标"
    assert out["latency"]["target"] == 800
    # 缓存对象本身不被改写（下一次不同目标值仍可复用原始结果）
    assert cached["stats"]["searchP95"]["status"] == "normal"


def test_apply_target_boundary_is_inclusive():
    out = main.apply_target({"stats": {"searchP95": {"value": 800}}}, 800)
    assert out["stats"]["searchP95"]["status"] == "normal"


def test_apply_target_without_card_is_noop():
    cached = {"stats": {}, "latency": {}}
    assert main.apply_target(cached, 800)["stats"] == {}


def test_classify_level_maps_known_levels():
    assert main.classify_level("ERROR") == "严重"
    assert main.classify_level("fatal") == "严重"
    assert main.classify_level("WARN") == "警告"
    assert main.classify_level("INFO") == "提示"
    assert main.classify_level("debug") == "提示"


def test_ms_to_label_uses_cst():
    ms = 1763680800000  # 2025-11-20T23:20:00Z = 2025-11-21 07:20 CST
    assert main.ms_to_label(ms, "HH:mm") == "07:20"
    assert main.ms_to_label(ms, "MM-DD") == "11-21"


def test_range_key_of_maps_labels_and_falls_back():
    assert main.range_key_of("今日") == "today"
    assert main.range_key_of("1h") == "1h"
    assert main.range_key_of("未知范围") == "1d"


def test_recent_alarm_events_uses_alarm_service(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "items": [
                    {
                        "name": "系统认证失败激增（authpriv）",
                        "level": "警告",
                        "source": "系统日志",
                        "sourceName": "主机登录日志",
                        "firstSeen": "2026-11-23 14:39:49",
                        "status": "告警中",
                        "handler": None,
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "Client", FakeClient)
    rows = main.recent_alarm_events("log-system", "Bearer test-token")

    assert captured["url"].endswith("/events")
    assert captured["params"] == {"tenant": "log-system", "page": 1, "pageSize": 5}
    assert captured["headers"] == {"Authorization": "Bearer test-token"}
    assert rows == [{
        "name": "系统认证失败激增（authpriv）",
        "level": "警告",
        "source": "系统日志",
        "sourceName": "主机登录日志",
        "occurTime": "2026-11-23 14:39:49",
        "status": "告警中",
        "handler": "—",
    }]


def test_recent_alarm_events_returns_empty_when_alarm_service_fails(monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            raise RuntimeError("alarm-service unavailable")

    monkeypatch.setattr(main.httpx, "Client", FailingClient)
    assert main.recent_alarm_events("log-system", "Bearer test-token") == []
