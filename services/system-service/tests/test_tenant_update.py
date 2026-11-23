"""租户更新契约：先校验后落库（校验失败不得留下半更新）。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main, storage

USER = {"id": 1, "name": "管理员", "role_key": "platform_admin", "tenant_code": "log-system"}
REQ = SimpleNamespace(headers={}, client=None)


@pytest.fixture
def executed(monkeypatch):
    calls = []

    monkeypatch.setattr(main, "pg_query", lambda sql, params=(): [
        {"code": "t1", "name": "旧名", "enabled": True}] if "FROM tenants" in sql else [])
    monkeypatch.setattr(main, "pg_exec", lambda sql, params=(): calls.append((sql, params)))
    monkeypatch.setattr(main, "write_audit", lambda *a, **k: None)
    monkeypatch.setattr(storage, "read_config", lambda: {"totalGB": 10})
    monkeypatch.setattr(storage, "allocated_gb", lambda exclude_tenant="": 0)
    return calls


def test_quota_overflow_leaves_tenant_untouched(executed):
    with pytest.raises(HTTPException) as err:
        main.update_tenant("t1", main.TenantIn(name="改名", quotaGB=999), REQ, USER)
    assert err.value.status_code == 400
    # 校验失败时不能先写了一半：租户名/启停必须保持原值
    assert [c for c in executed if "UPDATE tenants" in c[0]] == []


def test_valid_update_applies_name_and_quota(executed):
    out = main.update_tenant("t1", main.TenantIn(name="新名", quotaGB=5), REQ, USER)
    assert out["ok"] is True
    sqls = [c[0] for c in executed]
    assert any("UPDATE tenants" in s for s in sqls)
    assert any("tenant_quotas" in s for s in sqls)
