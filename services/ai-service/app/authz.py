"""租户鉴权：服务端强制隔离（与 auth-service / system-service 共用同一套 JWT）。

设计原则（商用口径）：
1. **租户归属一律取自登录态**：JWT → 现查 users/tenants，绝不相信请求体/查询参数里的
   tenant。前端传参只作为"管理员巡检时想切到哪个租户"的意图，是否放行由本模块判定。
2. 普通账号：只能访问自己所属租户；请求其他租户 → 403。
3. 平台管理员：可巡检任意已启用租户；`all` 表示跨租户，仅管理员可用。
4. 账号被停用 / 被删除时立即失效（每次请求现查，不依赖 Token 内容）。
5. 未知租户不会静默落到某个默认租户：查询直接 404；写入端判定不出归属就直接拒收（无兜底桶）。

该文件在 search / dashboard / alarm / ingest 四个服务中各存一份（与项目既有
security.py 的做法一致：各服务独立打包，未抽公共包）。改动时需同步保持行为一致。
"""
import os
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from fastapi import Header, HTTPException
from jose import JWTError, jwt

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-not-secret")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
PG_DSN = os.getenv(
    "PG_DSN",
    f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
    f"user={os.getenv('POSTGRES_USER', 'loguser')} "
    f"password={os.getenv('POSTGRES_PASSWORD', 'logpass')} "
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')}",
)

ADMIN_ROLE = "platform_admin"
ALL_SCOPES = ("all", "*")

_pg_conn = None


def _conn():
    global _pg_conn
    if _pg_conn is None or getattr(_pg_conn, "closed", 1):
        _pg_conn = psycopg2.connect(PG_DSN, connect_timeout=5)
        _pg_conn.autocommit = True
    return _pg_conn


def _query(sql: str, params: tuple = ()):
    with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def decode_bearer(authorization: Optional[str]) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="登录态无效或已过期，请重新登录")


def current_identity(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """解析并校验当前登录身份（每次现查库，停用/删除立即生效）。"""
    claims = decode_bearer(authorization)
    rows = _query(
        """SELECT u.id, u.account, u.name, u.role_key, u.enabled,
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


def is_admin(identity: Dict[str, Any]) -> bool:
    return identity.get("role_key") == ADMIN_ROLE


def resolve_tenant(identity: Dict[str, Any], requested: Optional[str]) -> str:
    """计算本次请求实际生效的租户；越权直接 403，未知租户 404。"""
    req = (requested or "").strip()
    own = identity["tenant_code"]

    if req in ALL_SCOPES:
        if not is_admin(identity):
            raise HTTPException(status_code=403, detail="无权跨租户查询全部租户日志")
        return "all"

    if not is_admin(identity):
        if req and req != own:
            raise HTTPException(status_code=403, detail="无权访问其他租户的日志")
        return own

    if not req:
        return own
    rows = _query("SELECT 1 FROM tenants WHERE code=%s AND enabled", (req,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"租户「{req}」不存在或已停用")
    return req


def tenant_directory() -> List[Dict[str, str]]:
    """已启用租户的编码/名称清单（search-service 专用）。

    检索层需要它做「虚拟字段 tenant ↔ 物理字段 tenant_id」的双向翻译：
    用户按中文名过滤 → 换成机器码下推；结果里的机器码 → 换回中文名展示。
    查不到时返回空表，翻译退化为原样透传，不影响检索主流程。
    """
    try:
        return _query("SELECT code, name FROM tenants WHERE enabled ORDER BY id")
    except Exception:  # noqa: BLE001
        return []


def resolve_ingest_tenant(identity: Dict[str, Any], requested: Optional[str]) -> str:
    """写入端租户解析：与查询端一致，但允许管理员显式指定任意已启用租户。

    返回空字符串表示"无法归属"（调用方应拒收并计数），绝不静默归到某个默认租户。
    """
    req = (requested or "").strip()
    own = identity["tenant_code"]
    target = req or own
    rows = _query("SELECT 1 FROM tenants WHERE code=%s AND enabled", (target,))
    if not rows:
        return ""
    if not is_admin(identity) and target != own:
        raise HTTPException(status_code=403, detail="无权向其他租户写入日志")
    return target
