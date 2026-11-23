"""system-service 鉴权工具：密码哈希、JWT 校验、权限闸门。

与 `services/auth-service/app/security.py` 保持同一套实现（两个容器各自独立打包，
未抽公共包；改动时需同步，JWT_SECRET / 算法必须一致）。

权限不在 Token 里固化，每次请求按 role_key 现查 permissions 表，
所以「改角色权限后实时生效」，不需要重新登录。
"""
import os
from typing import Any, Dict, List, Optional

import bcrypt
from fastapi import Header, HTTPException
from jose import JWTError, jwt

from .db import pg_query

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-not-secret")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

# 密码策略：长度 8-32；bcrypt 对 >72 字节输入会拒算，前端与后端同时限制
PWD_MIN, PWD_MAX = 8, 32
# 中文字符 1 字 = 3 字节，32 个中文字符即 96 字节，超出 bcrypt 上限后会抛 ValueError。
# 策略层必须按字节兜住，让调用方拿到 400 而不是 500。
PWD_MAX_BYTES = 72
PWD_RULE = "密码长度 8-32 位，需同时包含字母与数字"


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), (hashed or "").encode("utf-8"))
    except (ValueError, TypeError):
        return False


def password_error(raw: str) -> Optional[str]:
    """返回不满足策略的原因，None 表示通过。"""
    if not raw or len(raw) < PWD_MIN or len(raw) > PWD_MAX:
        return PWD_RULE
    if len(raw.encode("utf-8")) > PWD_MAX_BYTES:
        return PWD_RULE
    if not any(c.isalpha() for c in raw) or not any(c.isdigit() for c in raw):
        return PWD_RULE
    return None


def decode_token(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="登录态无效或已过期，请重新登录")


def bearer_token(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    return authorization.split(" ", 1)[1].strip()


def current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """解析当前登录用户；用户被停用或角色被删时立即失效。"""
    claims = decode_token(bearer_token(authorization))
    rows = pg_query(
        """SELECT u.id, u.account, u.name, u.role_key, u.enabled,
                  t.code AS tenant_code, t.name AS tenant_name,
                  r.name AS role_name
             FROM users u
             JOIN tenants t ON t.id = u.tenant_id
             JOIN roles r ON r.key = u.role_key
            WHERE u.id = %s""",
        (claims.get("sub"),),
    )
    if not rows:
        raise HTTPException(status_code=401, detail="账号已不存在，请重新登录")
    row = rows[0]
    if not row["enabled"]:
        raise HTTPException(status_code=401, detail="账号已被停用，请联系管理员")
    return row


def perms_for_role(role_key: str) -> list:
    rows = pg_query(
        "SELECT resource, action FROM permissions WHERE role_key=%s ORDER BY resource, action",
        (role_key,),
    )
    return [f"{r['resource']}:{r['action']}" for r in rows]


def client_ip(x_forwarded_for: Optional[str], fallback: str = "") -> str:
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return fallback or ""


def require(resource: str, action: str):
    """依赖工厂：要求当前用户具备 resource:action，否则 403 并如实说明缺哪个权限。"""

    def dependency(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
        user = current_user(authorization)
        if f"{resource}:{action}" not in perms_for_role(user["role_key"]):
            raise HTTPException(
                status_code=403,
                detail=f"当前角色「{user['role_name']}」缺少权限 {resource}:{action}",
            )
        return user

    return dependency


def require_any(pairs: List[tuple]):
    """依赖工厂：任一权限点满足即放行。

    用例：运行总览的「存储容量概览」区块，对有 overview:view 的角色应可读，
    但日志空间管理页本身仍要求 storage:view。
    """

    def dependency(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
        user = current_user(authorization)
        points = set(perms_for_role(user["role_key"]))
        if not any(f"{r}:{a}" in points for r, a in pairs):
            need = " 或 ".join(f"{r}:{a}" for r, a in pairs)
            raise HTTPException(
                status_code=403,
                detail=f"当前角色「{user['role_name']}」缺少权限 {need}",
            )
        return user

    return dependency
