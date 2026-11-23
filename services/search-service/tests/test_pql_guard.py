"""/pql 安全闸门契约：索引段由服务端强制、写操作拒绝、租户维度翻译、时间条件注入。"""
import pytest
from fastapi import HTTPException

from app import main

DIRECTORY = [{"code": "i6000", "name": "总部生产"}]
IDENTITY = {"role_key": "ops", "tenant_code": "i6000"}
SENT = {}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def pql_harness(monkeypatch):
    monkeypatch.setattr(main, "tenant_directory", lambda: DIRECTORY)

    def fake_post(url, json=None, timeout=None):
        SENT["stmt"] = json["query"]
        return _FakeResponse({
            "schema": [{"name": "tenant_id"}, {"name": "count"}],
            "datarows": [["i6000", 3]],
            "total": 1,
        })

    monkeypatch.setattr(main.httpx, "post", fake_post)
    return SENT


def test_server_always_sets_source_from_tenant(pql_harness):
    main.pql(main.PqlReq(pql="source=logs-* | where level='ERROR'"), IDENTITY)
    assert pql_harness["stmt"].startswith("source=logs-i6000 |")
    assert "logs-*" not in pql_harness["stmt"]


def test_tenant_dimension_is_translated_before_execution(pql_harness):
    main.pql(main.PqlReq(pql="where tenant = '总部生产' | head 10"), IDENTITY)
    assert "tenant_id = 'i6000'" in pql_harness["stmt"]


def test_response_tenant_column_is_localized(pql_harness):
    data = main.pql(main.PqlReq(pql="where level='ERROR'"), IDENTITY)
    assert data["schema"][0]["name"] == "tenant"
    assert data["datarows"] == [["总部生产", 3]]


def test_time_range_is_injected_as_own_segment(pql_harness):
    main.pql(main.PqlReq(pql="where level='ERROR'",
                         timeRange={"from": "2026-11-21T10:00:00+08:00",
                                    "to": "2026-11-21T11:00:00+08:00"}), IDENTITY)
    stmt = pql_harness["stmt"]
    assert "source=logs-i6000 | where @timestamp >= '2026-11-21T02:00:00.000Z'" in stmt
    assert "where level='ERROR'" in stmt


def test_write_keyword_rejected(pql_harness):
    with pytest.raises(HTTPException) as err:
        main.pql(main.PqlReq(pql="where level='ERROR' | delete"), IDENTITY)
    assert err.value.status_code == 400


def test_write_word_inside_literal_is_searchable(pql_harness):
    main.pql(main.PqlReq(pql="where message = 'delete failed' | head 10"), IDENTITY)
    assert "message = 'delete failed'" in pql_harness["stmt"]
