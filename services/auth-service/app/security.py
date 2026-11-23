"""auth-service 鉴权工具：密码哈希、JWT 签发与校验、当前用户解析。

与 `services/system-service/app/security.py` 保持同一套实现（两个容器各自独立打包，
未抽公共包；改动时需同步，JWT_SECRET / 算法必须一致，否则两边解析不了对方的 Token）。
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
from fastapi import Header, HTTPException
from jose import JWTError, jwt

from .db import pg_query

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-not-secret")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

PWD_MIN, PWD_MAX = 8, 32
# bcrypt 对 >72 字节的输入直接拒算（抛 ValueError），而中文字符 1 字 = 3 字节，
# 32 个中文字符就是 96 字节。必须在这里按字节兜住，否则接口会 500 而不是 400。
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
    if not raw or len(raw) < PWD_MIN or len(raw) > PWD_MAX:
        return PWD_RULE
    if len(raw.encode("utf-8")) > PWD_MAX_BYTES:
        return PWD_RULE
    if not any(c.isalpha() for c in raw) or not any(c.isdigit() for c in raw):
        return PWD_RULE
    return None


def create_token(user: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=EXPIRE_MINUTES)
    payload = {
        "sub": str(user["id"]),
        "account": user["account"],
        "name": user["name"],
        "role": user["role_key"],
        "tenant": user["tenant_code"],
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return {
        "token": jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM),
        "expiresIn": EXPIRE_MINUTES * 60,
        "expiresAt": exp.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
    }


def bearer_token(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    return authorization.split(" ", 1)[1].strip()


def current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """按 Token 里的 sub 现查库：账号被停用或删除时，旧 Token 立即失效。"""
    try:
        claims = jwt.decode(bearer_token(authorization), JWT_SECRET,
                            algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="登录态无效或已过期，请重新登录")
    rows = pg_query(
        """SELECT u.id, u.account, u.name, u.role_key, u.enabled, u.password_hash,
                  u.last_login_at, u.last_login_ip,
                  t.code AS tenant_code, t.name AS tenant_name, r.name AS role_name
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
