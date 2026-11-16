"""auth-service 数据访问：只读为主的最小层。

表归属约定（重要）：`users` / `roles` / `permissions` / `tenants` / `audit_log`
由 **system-service 建成并拥有**（见 services/system-service/app/db.py）。
本服务**不建表**，只做查询，以及两处必要写入：
1. 登录成功时更新 users.last_login_at / last_login_ip；
2. 登录成功 / 失败、登出、改密时写 audit_log。

这样避免两个服务各写一份 DDL 互相覆盖；代价是首次部署必须让 system-service 先起来
（compose 中 system-service 的 depends_on postgres，auth-service 亦然；两服务并发启动
时若表还不存在，登录会返回明确错误而不是假装成功）。
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

PG_DSN = os.getenv(
    "AUTH_PG_DSN",
    f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
    f"user={os.getenv('POSTGRES_USER', 'loguser')} "
    f"password={os.getenv('POSTGRES_PASSWORD', 'logpass')} "
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')}",
)
CST = timezone(timedelta(hours=8))

_pg_conn = None


def pg_conn():
    global _pg_conn
    if _pg_conn is None or getattr(_pg_conn, "closed", 1):
        _pg_conn = psycopg2.connect(PG_DSN, connect_timeout=5)
        _pg_conn.autocommit = True
    return _pg_conn


def pg_exec(sql: str, params: tuple = ()) -> None:
    with pg_conn().cursor() as cur:
        cur.execute(sql, params)


def pg_query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with pg_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def to_cst_str(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
