"""登录与鉴权契约：密码策略、bcrypt 边界、JWT 签发校验、停用账号即时失效。"""
import pytest
from fastapi import HTTPException

from app import security


# ---------------- 密码策略 ----------------
@pytest.mark.parametrize("raw", ["short1a", "abcdefg", "abcd1", "a1" + "x" * 31])
def test_password_policy_rejects_invalid(raw):
    assert security.password_error(raw) == security.PWD_RULE


@pytest.mark.parametrize("raw", ["abcdefg1", "Passw0rd", "a1" + "x" * 30])
def test_password_policy_accepts_valid(raw):
    assert security.password_error(raw) is None


def test_hash_and_verify_roundtrip():
    hashed = security.hash_password("Passw0rd")
    assert security.verify_password("Passw0rd", hashed) is True
    assert security.verify_password("wrongpass1", hashed) is False


def test_verify_password_tolerates_garbage_hash():
    assert security.verify_password("Passw0rd", "") is False
    assert security.verify_password("Passw0rd", "not-a-hash") is False


def test_policy_and_hasher_agree_on_multibyte_passwords():
    # bcrypt 只接受 ≤72 字节：超字节上限必须在策略层拒绝，而不是放过去后让哈希抛 ValueError
    too_long = "密" * 24 + "1"  # 25 字符 / 73 字节
    assert security.password_error(too_long) == security.PWD_RULE
    # 边界内侧：25 字符 / 71 字节，策略通过且真的能哈希
    edge = "密" * 23 + "a1"
    assert security.password_error(edge) is None
    assert security.hash_password(edge)


# ---------------- Token ----------------
def test_create_token_payload_and_expiry():
    user = {"id": 7, "account": "admin", "name": "管理员",
            "role_key": "platform_admin", "tenant_code": "log-system"}
    out = security.create_token(user)
    claims = security.jwt.decode(out["token"], security.JWT_SECRET,
                                 algorithms=[security.JWT_ALGORITHM])
    assert claims["sub"] == "7"
    assert claims["role"] == "platform_admin"
    assert claims["tenant"] == "log-system"
    assert out["expiresIn"] == security.EXPIRE_MINUTES * 60


def test_token_with_other_secret_is_rejected():
    user = {"id": 7, "account": "a", "name": "n", "role_key": "ops", "tenant_code": "t"}
    token = security.create_token(user)["token"]
    with pytest.raises(security.JWTError):
        security.jwt.decode(token, "another-secret", algorithms=[security.JWT_ALGORITHM])


def test_current_user_rejects_missing_and_disabled(monkeypatch):
    monkeypatch.setattr(security, "pg_query", lambda sql, params=(): [])
    with pytest.raises(HTTPException) as err:
        security.current_user("Bearer invalid")
    assert err.value.status_code == 401


def test_current_user_rejects_disabled_account(monkeypatch):
    user = {"id": 7, "account": "a", "name": "n", "role_key": "ops", "tenant_code": "t"}
    token = security.create_token(user)["token"]
    monkeypatch.setattr(security, "pg_query",
                        lambda sql, params=(): [{"id": 7, "enabled": False, "role_key": "ops"}])
    with pytest.raises(HTTPException) as err:
        security.current_user(f"Bearer {token}")
    assert err.value.status_code == 401


def test_bearer_header_validation():
    with pytest.raises(HTTPException):
        security.bearer_token(None)
    with pytest.raises(HTTPException):
        security.bearer_token("Basic abc")
    assert security.bearer_token("Bearer  abc ") == "abc"


def test_client_ip_uses_first_forwarded_entry():
    assert security.client_ip("10.0.0.1, 10.0.0.2", "127.0.0.1") == "10.0.0.1"
    assert security.client_ip("", "127.0.0.1") == "127.0.0.1"
