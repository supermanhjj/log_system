"""系统参数与健康检查契约：迁移标记不进配置页、不可被用户改写；建库失败要显示 degraded。"""
import pytest
from fastapi import HTTPException

from app import main

USER = {"id": 1, "name": "管理员", "role_key": "platform_admin", "tenant_code": "log-system"}


def _param(key, label="参数", ptype="str", value="v", options=None):
    return {"pkey": key, "label": label, "pvalue": value, "ptype": ptype,
            "note": "", "options": options, "updated_at": None}


def test_internal_param_prefix_is_reserved():
    assert main.is_internal_param("migration.retire_demo_tenants.v1") is True
    assert main.is_internal_param("search.timeout") is False
    assert main.is_internal_param("") is False


def test_params_listing_hides_migration_keys(monkeypatch):
    monkeypatch.setattr(main, "pg_query", lambda sql, params=(): [
        _param("search.timeout", value="30"),
        _param("migration.retire_demo_tenants.v1", value="done"),
    ])
    items = main.get_params(USER)["items"]
    assert [i["key"] for i in items] == ["search.timeout"]


def test_params_update_rejects_migration_key_without_writing(monkeypatch):
    executed = []
    monkeypatch.setattr(main, "pg_query", lambda sql, params=(): [
        _param("search.timeout"),
        _param("migration.retire_demo_tenants.v1"),
    ])
    monkeypatch.setattr(main, "pg_exec", lambda sql, params=(): executed.append((sql, params)))
    with pytest.raises(HTTPException) as err:
        main.put_params(main.ParamsIn(items={"migration.retire_demo_tenants.v1": "re-run"}), None, USER)
    assert err.value.status_code == 400
    assert "migration" in err.value.detail
    assert executed == []


def test_health_reports_db_init_failure(monkeypatch):
    monkeypatch.setattr(main.db_mod, "INIT_DB_ERROR", "OperationalError: boom")
    degraded = main.health()
    assert degraded["status"] == "degraded"
    assert "boom" in degraded["dbInit"]

    monkeypatch.setattr(main.db_mod, "INIT_DB_ERROR", None)
    assert main.health()["status"] == "ok"
