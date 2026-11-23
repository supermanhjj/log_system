"""租户维度翻译契约：字段名收敛、值翻译、结果回显中文名、时间 literal 归一到 UTC。"""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app import main

DIRECTORY = [{"code": "i6000", "name": "总部生产"}, {"code": "pms", "name": "项目管理"}]


def test_index_for_routes_tenant_and_all():
    assert main.index_for("i6000") == "logs-i6000"
    assert main.index_for("all") == "logs-*"
    assert main.index_for(None) == "logs-*"


def test_rewrite_tenant_field_names_and_values():
    assert main.rewrite_tenant_dimension("where tenant = '总部生产' | head 10", DIRECTORY) == (
        "where tenant_id = 'i6000' | head 10")


def test_rewrite_tenant_in_list_values():
    assert main.rewrite_tenant_dimension(
        "where tenant in ('总部生产', 'pms')", DIRECTORY) == (
        "where tenant_id in ('i6000', 'pms')")


def test_rewrite_tenant_leaves_literals_untouched():
    stmt = "where message like '%tenant_id%' and tenant = '总部生产'"
    out = main.rewrite_tenant_dimension(stmt, DIRECTORY)
    assert "'%tenant_id%'" in out
    assert "tenant = 'i6000'" not in out and "tenant_id = 'i6000'" in out


def test_rewrite_tenant_unknown_value_keeps_original():
    assert main.rewrite_tenant_dimension("where tenant = '未知租户'", DIRECTORY) == (
        "where tenant_id = '未知租户'")


def test_localize_tenant_columns_renames_and_maps_back():
    data = {"schema": [{"name": "tenant_id"}, {"name": "count"}],
            "datarows": [["i6000", 3]]}
    out = main.localize_tenant_columns(data, DIRECTORY)
    assert out["schema"][0]["name"] == "tenant"
    assert out["datarows"] == [["总部生产", 3]]


def test_localize_tenant_columns_without_column_is_noop():
    data = {"schema": [{"name": "level"}], "datarows": [["ERROR"]]}
    assert main.localize_tenant_columns(data, DIRECTORY) is data


def test_ppl_time_filter_converts_local_time_to_utc_literal():
    clause = main.ppl_time_filter(
        {"from": "2026-11-21T10:00:00", "to": "2026-11-21T11:00:00+08:00"})
    assert clause == ("where @timestamp >= '2026-11-21T02:00:00.000Z' "
                      "and @timestamp <= '2026-11-21T03:00:00.000Z'")


def test_ppl_time_filter_rejects_invalid_value():
    with pytest.raises(HTTPException) as err:
        main.ppl_time_filter({"from": "昨天上午"})
    assert err.value.status_code == 400


def test_ppl_time_filter_empty_range_returns_none():
    assert main.ppl_time_filter(None) is None
    assert main.ppl_time_filter({}) is None
