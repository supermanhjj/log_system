"""默认 seed 的解析规则契约：I6000 订单日志的 grok 规则要能解析行尾 status，并映射成整数。"""
import json

from app import parsers, pipeline, pipeline_store

LINE = ("2026-11-21 10:00:00.123 [http-nio-8080-exec-1] INFO com.sgcc.order.OrderController"
        " - 创建订单成功 orderId=123 status=200")


def _seeded(recorder, code):
    for sql, params in recorder:
        if params and params[0] == code:
            return sql, params
    raise AssertionError(f"seed 里没有 {code}")


def test_seeded_i6000_parser_extracts_status_and_mapping_casts_int(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline_store, "pg_exec",
                        lambda sql, params=(): calls.append((sql, params)))

    pipeline_store.seed_defaults()

    _, parser_params = _seeded(calls, "java-logback-http-status")
    definition = json.loads(parser_params[3])
    fields = parsers.parse(definition, LINE)
    assert fields == {"ts": "2026-11-21 10:00:00.123", "thread": "http-nio-8080-exec-1",
                      "level": "INFO", "logger": "com.sgcc.order.OrderController",
                      "message": "创建订单成功 orderId=123", "status": "200"}

    _, rule_params = _seeded(calls, "java-logback-i6000-status")
    mapping = json.loads(rule_params[4])
    assert rule_params[6] == "java-logback-http-status"
    assert rule_params[5] == "i6000"
    mapped = pipeline._apply_mapping(fields, mapping)
    assert mapped["status"] == 200
    assert mapped["source"] == "com.sgcc.order.OrderController"
    assert mapped["level"] == "INFO"
    assert mapped["attrs.thread"] == "http-nio-8080-exec-1"
