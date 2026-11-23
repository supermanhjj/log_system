"""系统服务密码策略：与 auth-service 同一实现，多字节密码同样受 bcrypt 72 字节上限约束。"""
import pytest

from app import security


@pytest.mark.parametrize("raw", ["short1a", "abcdefg", "abcd1", "a1" + "x" * 31])
def test_password_policy_rejects_invalid(raw):
    assert security.password_error(raw) == security.PWD_RULE


def test_password_policy_accepts_valid():
    assert security.password_error("Passw0rd") is None


def test_policy_and_hasher_agree_on_multibyte_passwords():
    too_long = "密" * 24 + "1"  # 73 字节，超出 bcrypt 上限
    assert security.password_error(too_long) == security.PWD_RULE
    edge = "密" * 23 + "a1"  # 71 字节，边界内
    assert security.password_error(edge) is None
    assert security.hash_password(edge)
