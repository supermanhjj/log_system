"""查询租户鉴权契约：越权 403、跨租户仅管理员、未知租户 404、写入端不静默兜底。"""
import pytest
from fastapi import HTTPException

from app import authz

ADMIN = {"role_key": "platform_admin", "tenant_code": "log-system"}
NORMAL = {"role_key": "ops", "tenant_code": "tenant-a"}


@pytest.fixture
def tenants(monkeypatch):
    """用一份可控的租户目录替换真实 PG 查询。"""
    def fake_query(sql, params=()):
        if "FROM tenants" in sql:
            code = params[0]
            return [{"?column?": 1}] if code in ("tenant-a", "tenant-b") else []
        return []

    monkeypatch.setattr(authz, "_query", fake_query)


# ---------------- 查询端 ----------------
def test_normal_user_other_tenant_forbidden(tenants):
    with pytest.raises(HTTPException) as err:
        authz.resolve_tenant(NORMAL, "tenant-b")
    assert err.value.status_code == 403


def test_normal_user_all_scope_forbidden(tenants):
    with pytest.raises(HTTPException) as err:
        authz.resolve_tenant(NORMAL, "all")
    assert err.value.status_code == 403


def test_normal_user_own_tenant_allowed(tenants):
    assert authz.resolve_tenant(NORMAL, "tenant-a") == "tenant-a"
    assert authz.resolve_tenant(NORMAL, "") == "tenant-a"


def test_admin_can_cross_tenant(tenants):
    assert authz.resolve_tenant(ADMIN, "all") == "all"
    assert authz.resolve_tenant(ADMIN, "tenant-b") == "tenant-b"


def test_admin_unknown_tenant_is_404(tenants):
    with pytest.raises(HTTPException) as err:
        authz.resolve_tenant(ADMIN, "tenant-c")
    assert err.value.status_code == 404


# ---------------- 写入端 ----------------
def test_ingest_own_tenant_ok(tenants):
    assert authz.resolve_ingest_tenant(NORMAL, "tenant-a") == "tenant-a"


def test_ingest_other_tenant_forbidden(tenants):
    with pytest.raises(HTTPException) as err:
        authz.resolve_ingest_tenant(NORMAL, "tenant-b")
    assert err.value.status_code == 403


def test_ingest_unknown_tenant_returns_empty_not_default(tenants):
    # 判定不出归属必须返回空串，由调用方拒收；不能落到某个默认租户
    assert authz.resolve_ingest_tenant(ADMIN, "tenant-c") == ""
