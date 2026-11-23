"""namespace → 租户归属映射契约：只允许登记到已启用的真实租户，删除不存在的映射要 404。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main

USER = {"id": 1, "name": "管理员", "role_key": "platform_admin", "tenant_code": "log-system"}
REQ = SimpleNamespace(headers={}, client=None)


@pytest.fixture
def recorder(monkeypatch):
    calls = {"exec": [], "audit": []}
    monkeypatch.setattr(main, "pg_exec",
                        lambda sql, params=(): calls["exec"].append((sql, params)))
    monkeypatch.setattr(main, "write_audit",
                        lambda *a, **k: calls["audit"].append(a))
    return calls


def test_upsert_rejects_blank_namespace_or_tenant(monkeypatch, recorder):
    queries = []
    monkeypatch.setattr(main, "pg_query", lambda *a, **k: queries.append(a) or [])
    with pytest.raises(HTTPException) as err:
        main.upsert_namespace_mapping(
            main.NamespaceMappingIn(namespace="  ", tenant="tenant-a"), REQ, USER)
    assert err.value.status_code == 400
    # 空值必须在查库/写库之前拦住
    assert queries == [] and recorder["exec"] == []


def test_upsert_rejects_unknown_tenant(monkeypatch, recorder):
    monkeypatch.setattr(main, "pg_query", lambda sql, params=(): [])
    with pytest.raises(HTTPException) as err:
        main.upsert_namespace_mapping(
            main.NamespaceMappingIn(namespace="prod-pms", tenant="ghost"), REQ, USER)
    assert err.value.status_code == 404
    assert recorder["exec"] == []


def test_upsert_rejects_disabled_tenant(monkeypatch, recorder):
    monkeypatch.setattr(main, "pg_query", lambda sql, params=(): [
        {"code": "tenant-a", "name": "已停用租户", "enabled": False}])
    with pytest.raises(HTTPException) as err:
        main.upsert_namespace_mapping(
            main.NamespaceMappingIn(namespace="prod-pms", tenant="tenant-a"), REQ, USER)
    assert err.value.status_code == 400
    # 停用租户的映射写入会让 ingest 把日志拒收得莫名其妙，必须连写都不写
    assert recorder["exec"] == []


def test_upsert_writes_normalized_mapping_and_audits(monkeypatch, recorder):
    monkeypatch.setattr(main, "pg_query", lambda sql, params=(): [
        {"code": "tenant-a", "name": "A", "enabled": True}])
    out = main.upsert_namespace_mapping(
        main.NamespaceMappingIn(namespace=" prod-pms ", tenant=" Tenant-A ", note=" 接入备注 "),
        REQ, USER)
    sql, params = recorder["exec"][0]
    assert "namespace_tenant" in sql and "ON CONFLICT" in sql
    assert params == ("prod-pms", "tenant-a", "接入备注")
    assert out == {"ok": True, "namespace": "prod-pms", "tenant": "tenant-a",
                   "message": "prod-pms 的日志将归属租户 tenant-a"}
    assert recorder["audit"][0][1] == "保存归属映射"
    assert recorder["audit"][0][2] == "prod-pms → tenant-a"


def test_delete_missing_mapping_is_404_without_delete(monkeypatch, recorder):
    monkeypatch.setattr(main, "pg_query", lambda sql, params=(): [])
    with pytest.raises(HTTPException) as err:
        main.delete_namespace_mapping("prod-pms", REQ, USER)
    assert err.value.status_code == 404
    assert recorder["exec"] == []


def test_delete_existing_mapping_deletes_and_audits(monkeypatch, recorder):
    monkeypatch.setattr(main, "pg_query", lambda sql, params=(): [
        {"namespace": "prod-pms", "tenant": "tenant-a"}])
    out = main.delete_namespace_mapping("prod-pms", REQ, USER)
    sql, params = recorder["exec"][0]
    assert "DELETE FROM namespace_tenant" in sql and params == ("prod-pms",)
    assert out["ok"] is True
    assert recorder["audit"][0][1] == "删除归属映射"
