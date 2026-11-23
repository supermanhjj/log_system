"""system-service —— 平台管理：用户 / 角色权限 / 系统参数 / 审计日志 / 日志空间。

对应原型 SYS-0601（/settings/users）。落点依据 docs/tech-stack-selection.md 3.2(9)：
「FastAPI 系统服务 + PostgreSQL（RBAC 三表、tenants、audit_log、system_params；
各业务表含 tenant_id 行级隔离）」。

边界（务必如实理解）：
- 本服务是九张表的**唯一建表与写入方**（见 db.py）：RBAC 六表 + 日志空间三表；
  auth-service 只读 users/tenants/roles。
- 这里只管「管理面 CRUD」；登录、JWT 签发在 auth-service。两者共用 JWT_SECRET，
  本服务在每个管理接口上自行校验 Token 并现查 permissions，做到改权限后实时生效。
- 不伪造数据：用户列表、角色成员数、审计日志全部来自 PostgreSQL 真实记录；
  日志空间容量/索引/配额来自 OpenSearch 与 PostgreSQL 真实取数（见 storage.py）。
  密码只存 bcrypt 哈希，任何接口都不返回哈希值。
- 系统参数目前是「登记 + 校验」层能力，日志保留天数已与日志空间配置联动，
  其余参数（检索上限等）尚未反向驱动 OpenSearch 与检索网关，属后续接入项。
"""
import csv
import io
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from .audit import write_audit
from . import db as db_mod
from .db import (
    ACTION_LABELS,
    ALL_PERMS,
    PERMISSION_TREE,
    RESERVED_TENANT_CODES,
    init_db,
    pg_exec,
    pg_query,
    pg_scalar,
    to_cst_str,
)
from .security import (
    PWD_RULE,
    client_ip,
    current_user,
    hash_password,
    password_error,
    perms_for_role,
    require,
)
from .storage import router as storage_router

app = FastAPI(title="system-service", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(storage_router)

ACCOUNT_RULE = "账号由小写字母开头，可含小写字母、数字与下划线，长度 3-32"


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def temp_password() -> str:
    """初始/重置密码：满足「字母 + 数字」策略，由管理员一次性转交。"""
    return f"Tmp{secrets.randbelow(9) + 1}{secrets.token_hex(3)}"


def row_user(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "account": r["account"],
        "name": r["name"],
        "roleKey": r["role_key"],
        "roleName": r["role_name"],
        "tenant": r["tenant"],
        "tenantName": r["tenant_name"],
        "phone": r.get("phone") or "",
        "email": r.get("email") or "",
        "enabled": r["enabled"],
        "isBuiltin": r["is_builtin"],
        "lastLogin": to_cst_str(r.get("last_login_at")),
        "lastLoginIp": r.get("last_login_ip") or "",
        "createdAt": to_cst_str(r.get("created_at")),
    }


SELECT_USER = """
    SELECT u.id, u.account, u.name, u.role_key, r.name AS role_name,
           t.code AS tenant, t.name AS tenant_name, u.phone, u.email,
           u.enabled, u.is_builtin, u.last_login_at, u.last_login_ip, u.created_at
      FROM users u
      JOIN roles r ON r.key = u.role_key
      JOIN tenants t ON t.id = u.tenant_id
"""


def role_exists(key: str) -> bool:
    return pg_scalar("SELECT 1 FROM roles WHERE key=%s", (key,)) is not None


def tenant_code_exists(code: str) -> bool:
    return pg_scalar("SELECT 1 FROM tenants WHERE code=%s", (code,)) is not None


# ---------------------------------------------------------------------------
# 健康检查 / 元信息
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    # init_db 失败时服务仍会起来（方便其他服务先启动），但不能让健康检查假装没事：
    # 把原因带出来，探活日志一眼能分清是"表没建成功"还是"服务真挂了"。
    if db_mod.INIT_DB_ERROR:
        return {"status": "degraded", "service": "system-service", "dbInit": db_mod.INIT_DB_ERROR}
    return {"status": "ok", "service": "system-service", "dbInit": "ok"}


@app.get("/")
def root():
    return {
        "service": "system-service",
        "version": "0.3.0",
        "scope": "平台管理：用户 / 角色权限 / 系统参数 / 审计日志 / 日志空间",
        "endpoints": ["/meta", "/users", "/tenants", "/roles", "/params", "/audit",
                      "/storage/capacity", "/storage/config", "/storage/indexes", "/storage/quotas"],
    }


@app.get("/meta")
def meta(user: Dict[str, Any] = Depends(current_user)):
    """页面初始化用：角色、租户、权限矩阵、当前用户与权限。"""
    roles = pg_query("SELECT key, name, scope_desc FROM roles ORDER BY sort_order, key")
    tenants = pg_query("SELECT code, name FROM tenants WHERE enabled ORDER BY id")
    return {
        "roles": [{"key": r["key"], "name": r["name"], "scopeDesc": r["scope_desc"]} for r in roles],
        "tenants": [{"code": t["code"], "name": t["name"]} for t in tenants],
        "permissionTree": PERMISSION_TREE,
        "actionLabels": ACTION_LABELS,
        "me": {
            "id": user["id"],
            "account": user["account"],
            "name": user["name"],
            "roleKey": user["role_key"],
            "roleName": user["role_name"],
            "tenant": user["tenant_code"],
            "tenantName": user["tenant_name"],
        },
        "permissions": perms_for_role(user["role_key"]),
        "accountRule": ACCOUNT_RULE,
        "passwordRule": PWD_RULE,
    }


# ---------------------------------------------------------------------------
# 用户管理
# ---------------------------------------------------------------------------
class UserIn(BaseModel):
    account: Optional[str] = None
    name: Optional[str] = None
    password: Optional[str] = None
    roleKey: Optional[str] = None
    tenant: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    enabled: Optional[bool] = None


class EnabledIn(BaseModel):
    enabled: bool


class BatchEnabledIn(BaseModel):
    ids: List[int]
    enabled: bool


class BatchIdsIn(BaseModel):
    ids: List[int]


def user_filter(keyword: str, role: str, tenant: str, enabled: str,
                user: Dict[str, Any]) -> tuple:
    """列表与导出共用同一套筛选口径，避免两处条件写歪。"""
    where = ["1=1"]
    params: List[Any] = []
    if keyword.strip():
        where.append("(u.name ILIKE %s OR u.account ILIKE %s)")
        kw = f"%{keyword.strip()}%"
        params += [kw, kw]
    if role:
        where.append("u.role_key = %s")
        params.append(role)
    if tenant:
        where.append("t.code = %s")
        params.append(tenant)
    elif user["role_key"] != "platform_admin":
        # 非平台管理员只看自己租户，防跨租户越权（当前角色表中只有管理员有 user:view）
        where.append("t.code = %s")
        params.append(user["tenant_code"])
    if enabled in ("true", "false"):
        where.append("u.enabled = %s")
        params.append(enabled == "true")
    return " AND ".join(where), params


@app.get("/users")
def list_users(
    keyword: str = "",
    role: str = "",
    tenant: str = "",
    enabled: str = "",
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    user: Dict[str, Any] = Depends(require("user", "view")),
):
    cond, params = user_filter(keyword, role, tenant, enabled, user)
    total = pg_scalar(
        f"SELECT count(*) FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE {cond}",
        tuple(params),
    )
    rows = pg_query(
        f"{SELECT_USER} WHERE {cond} ORDER BY u.id LIMIT %s OFFSET %s",
        tuple(params + [pageSize, (page - 1) * pageSize]),
    )
    return {
        "items": [row_user(r) for r in rows],
        "total": int(total or 0),
        "page": page,
        "pageSize": pageSize,
    }


@app.get("/users/export.csv")
def export_users(
    keyword: str = "",
    role: str = "",
    tenant: str = "",
    enabled: str = "",
    user: Dict[str, Any] = Depends(require("user", "export")),
):
    cond, params = user_filter(keyword, role, tenant, enabled, user)
    rows = pg_query(f"{SELECT_USER} WHERE {cond} ORDER BY u.id", tuple(params))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["姓名", "账号", "角色", "所属租户", "启用", "手机号", "最近登录"])
    for r in rows:
        writer.writerow([
            r["name"], r["account"], r["role_name"], r["tenant_name"],
            "是" if r["enabled"] else "否", r.get("phone") or "",
            to_cst_str(r.get("last_login_at")) or "—",
        ])
    write_audit(user, "导出用户", f"{len(rows)} 个用户")
    return Response(
        content=("\ufeff" + buf.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="users.csv"'},
    )


@app.post("/users")
def create_user(
    req: UserIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("user", "create")),
):
    account = (req.account or "").strip().lower()
    name = (req.name or "").strip()
    role_key = (req.roleKey or "").strip()
    tenant = (req.tenant or "").strip()

    if not account or not name:
        raise HTTPException(status_code=400, detail="姓名与账号不能为空")
    if (len(account) < 3 or len(account) > 32 or not account[0].isalpha()
            or not all(c.islower() or c.isdigit() or c == "_" for c in account)):
        raise HTTPException(status_code=400, detail=ACCOUNT_RULE)
    if not role_exists(role_key):
        raise HTTPException(status_code=400, detail="角色不存在")
    if not tenant_code_exists(tenant):
        raise HTTPException(status_code=400, detail="租户不存在")
    if pg_scalar("SELECT 1 FROM users WHERE account=%s", (account,)):
        raise HTTPException(status_code=409, detail="该账号已存在，请更换")

    raw_pwd = (req.password or "").strip() or temp_password()
    err = password_error(raw_pwd)
    if err:
        raise HTTPException(status_code=400, detail=err)

    tenant_id = pg_scalar("SELECT id FROM tenants WHERE code=%s", (tenant,))
    pg_exec(
        """INSERT INTO users (tenant_id, account, name, password_hash, role_key,
                              phone, email, enabled)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (tenant_id, account, name, hash_password(raw_pwd), role_key,
         (req.phone or "").strip(), (req.email or "").strip(),
         True if req.enabled is None else req.enabled),
    )
    write_audit(user, "新建用户", f"{name}（{account}）",
                {"roleKey": role_key, "tenant": tenant},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {
        "ok": True,
        "id": pg_scalar("SELECT id FROM users WHERE account=%s", (account,)),
        "account": account,
        # 初始密码仅在此处返回一次，供管理员转交；库里只有哈希
        "initialPassword": raw_pwd,
        "message": "用户已保存",
    }


@app.post("/users/batch/enabled")
def batch_enabled(
    req: BatchEnabledIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("user", "update")),
):
    if not req.ids:
        raise HTTPException(status_code=400, detail="未选择用户")
    rows = pg_query("SELECT id, account, name, is_builtin FROM users WHERE id = ANY(%s)", (req.ids,))
    skipped: List[str] = []
    target_ids: List[int] = []
    for r in rows:
        if not req.enabled and r["is_builtin"]:
            skipped.append(f"{r['name']}（内置账号不可停用）")
            continue
        if not req.enabled and r["id"] == user["id"]:
            skipped.append(f"{r['name']}（当前登录账号）")
            continue
        target_ids.append(r["id"])
    if target_ids:
        pg_exec("UPDATE users SET enabled=%s, updated_at=now() WHERE id = ANY(%s)",
                (req.enabled, target_ids))
    write_audit(user, "批量启用用户" if req.enabled else "批量停用用户",
                f"{len(target_ids)} 个用户", {"ids": target_ids, "skipped": skipped},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "updated": len(target_ids), "skipped": skipped,
            "message": f"已{'启用' if req.enabled else '停用'} {len(target_ids)} 个用户"}


@app.post("/users/batch/reset-password")
def batch_reset_password(
    req: BatchIdsIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("user", "update")),
):
    if not req.ids:
        raise HTTPException(status_code=400, detail="未选择用户")
    rows = pg_query("SELECT id, account, name FROM users WHERE id = ANY(%s)", (req.ids,))
    results = []
    for r in rows:
        raw = temp_password()
        pg_exec("UPDATE users SET password_hash=%s, updated_at=now() WHERE id=%s",
                (hash_password(raw), r["id"]))
        results.append({"id": r["id"], "account": r["account"],
                        "name": r["name"], "tempPassword": raw})
    write_audit(user, "批量重置密码", f"{len(results)} 个用户",
                {"ids": [r["id"] for r in rows]},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "items": results, "message": f"已重置 {len(results)} 个用户的密码"}


@app.put("/users/{uid}")
def update_user(
    uid: int,
    req: UserIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("user", "update")),
):
    rows = pg_query(
        "SELECT id, account, name, role_key, is_builtin FROM users WHERE id=%s", (uid,))
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    target = rows[0]

    if uid == user["id"] and req.enabled is False:
        raise HTTPException(status_code=400, detail="不能停用当前登录账号")
    if target["is_builtin"]:
        if req.roleKey and req.roleKey != target["role_key"]:
            raise HTTPException(status_code=400, detail="内置管理员的角色不可修改")
        if req.tenant and req.tenant != user["tenant_code"]:
            raise HTTPException(status_code=400, detail="内置管理员的租户不可修改")
    if uid == user["id"] and req.roleKey and req.roleKey != target["role_key"]:
        raise HTTPException(status_code=400, detail="不能修改自己的角色，请让其他管理员操作")
    if req.roleKey and not role_exists(req.roleKey):
        raise HTTPException(status_code=400, detail="角色不存在")
    if req.tenant and not tenant_code_exists(req.tenant):
        raise HTTPException(status_code=400, detail="租户不存在")

    sets, params = [], []
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="姓名不能为空")
        sets.append("name=%s")
        params.append(name)
    if req.roleKey:
        sets.append("role_key=%s")
        params.append(req.roleKey)
    if req.tenant:
        sets.append("tenant_id=(SELECT id FROM tenants WHERE code=%s)")
        params.append(req.tenant)
    if req.phone is not None:
        sets.append("phone=%s")
        params.append(req.phone.strip())
    if req.email is not None:
        sets.append("email=%s")
        params.append(req.email.strip())
    if req.enabled is not None:
        sets.append("enabled=%s")
        params.append(req.enabled)
    if not sets:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    params.append(uid)
    pg_exec(f"UPDATE users SET {', '.join(sets)}, updated_at=now() WHERE id=%s", tuple(params))
    write_audit(user, "修改用户", f"{target['name']}（{target['account']}）",
                {"fields": req.model_dump(exclude_none=True)},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "message": "用户已保存"}


@app.put("/users/{uid}/enabled")
def toggle_user(
    uid: int,
    req: EnabledIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("user", "update")),
):
    rows = pg_query("SELECT id, name, account, is_builtin FROM users WHERE id=%s", (uid,))
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    target = rows[0]
    if uid == user["id"] and not req.enabled:
        raise HTTPException(status_code=400, detail="不能停用当前登录账号")
    if target["is_builtin"] and not req.enabled:
        raise HTTPException(status_code=400, detail="内置账号不可停用")
    pg_exec("UPDATE users SET enabled=%s, updated_at=now() WHERE id=%s", (req.enabled, uid))
    write_audit(user, "启用用户" if req.enabled else "停用用户",
                f"{target['name']}（{target['account']}）", {"enabled": req.enabled},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "enabled": req.enabled,
            "message": f"已{'启用' if req.enabled else '停用'}该用户"}


@app.post("/users/{uid}/reset-password")
def reset_password(
    uid: int,
    request: Request,
    user: Dict[str, Any] = Depends(require("user", "update")),
):
    rows = pg_query("SELECT id, account, name FROM users WHERE id=%s", (uid,))
    if not rows:
        raise HTTPException(status_code=404, detail="用户不存在")
    target = rows[0]
    raw = temp_password()
    pg_exec("UPDATE users SET password_hash=%s, updated_at=now() WHERE id=%s",
            (hash_password(raw), uid))
    write_audit(user, "重置密码", f"{target['name']}（{target['account']}）",
                {}, client_ip(request.headers.get("x-forwarded-for"),
                              request.client.host if request.client else ""))
    return {"ok": True, "tempPassword": raw,
            "message": f"{target['name']} 的密码已重置，临时密码仅显示一次"}


# ---------------------------------------------------------------------------
# 租户管理（日志隔离空间）
#
# 商用口径要点：
# 1. tenants 表是**全系统租户的唯一真源**：前端下拉、各查询服务路由、采集端归属
#    都以它为准；新增租户不再改代码，只需在本页登记。
# 2. 租户编码（code）直接决定 OpenSearch 索引名 logs-<code>，是隔离边界，
#    创建后不可修改（改名只改 display name）。
# 3. 保护性约束：编码唯一且不可用保留字；有用户/有日志数据的租户不可删除；
#    不允许停用或删除当前登录租户（避免把自己锁在门外）。
# 4. 所有变更写审计；配额沿用 tenant_quotas（与「日志空间」共用同一份数据）。
# ---------------------------------------------------------------------------
TENANT_CODE_RULE = "租户编码由小写字母开头，可含小写字母、数字与下划线，长度 2-32"


class TenantIn(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    enabled: Optional[bool] = None
    quotaGB: Optional[int] = None


class TenantEnabledIn(BaseModel):
    enabled: bool


class NamespaceMappingIn(BaseModel):
    namespace: str
    tenant: str
    note: Optional[str] = ""


def tenant_code_valid(code: str) -> bool:
    if not (2 <= len(code) <= 32):
        return False
    if not code[0].isalpha() or not code[0].islower():
        return False
    return all(c.islower() or c.isdigit() or c == "_" for c in code)


def tenant_index_stats() -> Dict[str, Dict[str, int]]:
    """按租户汇总 logs-<code> 的真实体积与文档数（取不到时返回空，不编造）。

    索引名带日期与写别名（logs-i6000-2026.11.20 / logs-i6000-write），
    必须用 index_tenant() 归一出租户编码。早期实现直接切掉 "logs-" 前缀，
    code 变成 "i6000-2026.11.20"，永远匹配不上租户 → 配额页文档数恒为 0。
    """
    try:
        from .storage import fetch_indices, index_tenant

        stats: Dict[str, Dict[str, int]] = {}
        for ix in fetch_indices(""):
            code = index_tenant(ix["name"])
            if not code:          # logs-unknown 之类无法归属的索引不参与配额统计
                continue
            cur = stats.setdefault(code, {"bytes": 0, "docs": 0})
            cur["bytes"] += int(ix.get("bytes") or 0)
            cur["docs"] += int(ix.get("docs") or 0)
        return stats
    except Exception as e:  # noqa: BLE001
        print(f"[system-service] tenant_index_stats skipped: {e}", flush=True)
        return {}


def tenant_row(r: Dict[str, Any], stats: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    code = r["code"]
    st = stats.get(code, {"bytes": 0, "docs": 0})
    return {
        "code": code,
        "name": r["name"],
        "enabled": r["enabled"],
        "userCount": int(r.get("user_count") or 0),
        "quotaGB": int(r.get("quota_gb") or 0),
        "usedBytes": st["bytes"],
        "docs": st["docs"],
        "isReserved": code in RESERVED_TENANT_CODES,
        "index": f"logs-{code}",
        "createdAt": to_cst_str(r.get("created_at")),
        "updatedAt": to_cst_str(r.get("updated_at")),
    }


SELECT_TENANT = """
    SELECT t.id, t.code, t.name, t.enabled, t.created_at,
           (SELECT max(q.updated_at) FROM tenant_quotas q WHERE q.tenant_code = t.code) AS updated_at,
           COALESCE(q.quota_gb, 0) AS quota_gb,
           (SELECT count(*) FROM users u WHERE u.tenant_id = t.id) AS user_count
      FROM tenants t
      LEFT JOIN tenant_quotas q ON q.tenant_code = t.code
"""


@app.get("/tenants")
def list_tenants(
    keyword: str = "",
    enabled: str = "",
    user: Dict[str, Any] = Depends(require("tenant", "view")),
):
    where = ["1=1"]
    params: List[Any] = []
    if keyword.strip():
        where.append("(t.code ILIKE %s OR t.name ILIKE %s)")
        kw = f"%{keyword.strip()}%"
        params += [kw, kw]
    if enabled in ("true", "false"):
        where.append("t.enabled = %s")
        params.append(enabled == "true")
    rows = pg_query(f"{SELECT_TENANT} WHERE {' AND '.join(where)} ORDER BY t.id", tuple(params))
    stats = tenant_index_stats()
    return {
        "items": [tenant_row(r, stats) for r in rows],
        "note": ("有用户或有日志数据的租户不允许删除；配额与「日志空间」共用同一份数据。"),
    }


# ---------------------------------------------------------------------------
# namespace → tenant 归属映射
#
# 采集端只上报 src_file / namespace，服务端按本表判定日志归哪个租户。
# 以前只能手工改库，云上接入真实业务系统时必须能通过 API 维护，否则新 namespace
# 的日志会被 ingest 直接拒收（derive_tenant 不做 logs-unknown 兜底）。
# ---------------------------------------------------------------------------
@app.get("/namespace-mappings")
def list_namespace_mappings(
    user: Dict[str, Any] = Depends(require("tenant", "view")),
):
    rows = pg_query(
        """SELECT m.namespace, m.tenant, m.note, m.updated_at,
                  t.name AS tenant_name, t.enabled AS tenant_enabled
             FROM namespace_tenant m
             LEFT JOIN tenants t ON t.code = m.tenant
            ORDER BY m.namespace"""
    )
    return {
        "items": [
            {
                "namespace": r["namespace"],
                "tenant": r["tenant"],
                "tenantName": r.get("tenant_name") or "",
                "tenantEnabled": bool(r.get("tenant_enabled")),
                "note": r.get("note") or "",
                "updatedAt": to_cst_str(r.get("updated_at")),
            }
            for r in rows
        ],
        "note": "未命中映射的 namespace 会被 ingest 拒收；接入新业务系统时先在这里登记。",
    }


@app.post("/namespace-mappings")
def upsert_namespace_mapping(
    req: NamespaceMappingIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("tenant", "update")),
):
    namespace = (req.namespace or "").strip()
    tenant = (req.tenant or "").strip().lower()
    if not namespace or not tenant:
        raise HTTPException(status_code=400, detail="namespace 与 tenant 不能为空")
    trows = pg_query("SELECT code, name, enabled FROM tenants WHERE code=%s", (tenant,))
    if not trows:
        raise HTTPException(status_code=404, detail=f"租户 {tenant} 不存在")
    if not trows[0]["enabled"]:
        raise HTTPException(status_code=400, detail=f"租户 {tenant} 已停用，不能接收日志")

    pg_exec(
        """INSERT INTO namespace_tenant (namespace, tenant, note, updated_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (namespace) DO UPDATE
              SET tenant=EXCLUDED.tenant, note=EXCLUDED.note, updated_at=now()""",
        (namespace, tenant, (req.note or "").strip()),
    )
    write_audit(
        user, "保存归属映射", f"{namespace} → {tenant}",
        {"namespace": namespace, "tenant": tenant},
        client_ip(request.headers.get("x-forwarded-for"),
                  request.client.host if request.client else ""),
    )
    return {"ok": True, "namespace": namespace, "tenant": tenant,
            "message": f"{namespace} 的日志将归属租户 {tenant}"}


@app.delete("/namespace-mappings/{namespace}")
def delete_namespace_mapping(
    namespace: str,
    request: Request,
    user: Dict[str, Any] = Depends(require("tenant", "update")),
):
    rows = pg_query("SELECT namespace, tenant FROM namespace_tenant WHERE namespace=%s",
                    (namespace,))
    if not rows:
        raise HTTPException(status_code=404, detail="映射不存在")
    pg_exec("DELETE FROM namespace_tenant WHERE namespace=%s", (namespace,))
    write_audit(
        user, "删除归属映射", namespace, {"tenant": rows[0]["tenant"]},
        client_ip(request.headers.get("x-forwarded-for"),
                  request.client.host if request.client else ""),
    )
    return {"ok": True, "message": f"已删除 {namespace} 的归属映射"}


@app.post("/tenants", status_code=201)
def create_tenant(
    req: TenantIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("tenant", "create")),
):
    code = (req.code or "").strip().lower()
    name = (req.name or "").strip()
    if not code or not name:
        raise HTTPException(status_code=400, detail="租户编码与名称不能为空")
    if not tenant_code_valid(code):
        raise HTTPException(status_code=400, detail=TENANT_CODE_RULE)
    if code in RESERVED_TENANT_CODES:
        raise HTTPException(status_code=400, detail=f"「{code}」为系统保留编码，不可使用")
    if tenant_code_exists(code):
        raise HTTPException(status_code=409, detail="该租户编码已存在")

    quota = int(req.quotaGB or 0)
    if quota < 0:
        raise HTTPException(status_code=400, detail="配额不能为负数（0 表示不设上限）")
    # 两级配额：租户配额之和不得超过平台总容量
    if quota > 0:
        from .storage import allocated_gb, read_config

        total = read_config()["totalGB"]
        already = allocated_gb()
        if total > 0 and already + quota > total:
            raise HTTPException(
                status_code=400,
                detail=(f"其他租户已分配 {already} GB，新增该租户 {quota} GB 后将超过"
                        f"平台总容量 {total} GB（剩余可分配 {max(total - already, 0)} GB）。"),
            )

    pg_exec("INSERT INTO tenants (code, name, enabled) VALUES (%s, %s, %s)",
            (code, name, True if req.enabled is None else req.enabled))
    pg_exec(
        """INSERT INTO tenant_quotas (tenant_code, quota_gb) VALUES (%s, %s)
           ON CONFLICT (tenant_code) DO UPDATE SET quota_gb=EXCLUDED.quota_gb, updated_at=now()""",
        (code, quota),
    )
    from .storage import ensure_tenant_index

    index_ok = ensure_tenant_index(code)
    write_audit(user, "新建租户", f"{name}（{code}）", {"quotaGB": quota, "indexCreated": index_ok},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "code": code, "index": f"logs-{code}",
            "message": f"租户「{name}」已创建，日志索引 logs-{code} 已就绪" if index_ok
                       else f"租户「{name}」已创建，但索引 logs-{code} 创建失败，请检查 OpenSearch"}


@app.put("/tenants/{code}")
def update_tenant(
    code: str,
    req: TenantIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("tenant", "update")),
):
    rows = pg_query("SELECT code, name, enabled FROM tenants WHERE code=%s", (code,))
    if not rows:
        raise HTTPException(status_code=404, detail="租户不存在")
    cur = rows[0]

    name = (req.name or "").strip() if req.name is not None else cur["name"]
    if not name:
        raise HTTPException(status_code=400, detail="租户名称不能为空")
    if req.code is not None and req.code.strip().lower() != code:
        raise HTTPException(
            status_code=400,
            detail="租户编码即索引隔离边界，创建后不可修改；如需更换请新建租户并迁移数据",
        )
    if req.enabled is False and code in RESERVED_TENANT_CODES:
        raise HTTPException(status_code=400, detail=f"系统保留租户「{code}」不可停用")
    if req.enabled is False and code == user["tenant_code"]:
        raise HTTPException(status_code=400, detail="不能停用当前登录账号所属的租户")

    enabled = cur["enabled"] if req.enabled is None else req.enabled
    quota = None
    if req.quotaGB is not None:
        quota = int(req.quotaGB)
        if quota < 0:
            raise HTTPException(status_code=400, detail="配额不能为负数（0 表示不设上限）")
        from .storage import allocated_gb, read_config

        total = read_config()["totalGB"]
        others = allocated_gb(exclude_tenant=code)
        if total > 0 and others + quota > total:
            raise HTTPException(
                status_code=400,
                detail=(f"其他租户已分配 {others} GB，本租户设 {quota} GB 后将超过平台总容量 "
                        f"{total} GB（剩余可分配 {max(total - others, 0)} GB）。"),
            )

    # 所有校验都通过后才落库：配额超限直接 400 时不能留下「名字已改、配额没改」的半更新
    pg_exec("UPDATE tenants SET name=%s, enabled=%s WHERE code=%s",
            (name, enabled, code))
    if quota is not None:
        pg_exec(
            """INSERT INTO tenant_quotas (tenant_code, quota_gb, updated_at)
               VALUES (%s, %s, now())
               ON CONFLICT (tenant_code) DO UPDATE SET quota_gb=EXCLUDED.quota_gb, updated_at=now()""",
            (code, quota),
        )
    write_audit(user, "修改租户", code, req.model_dump(exclude_none=True),
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "message": "租户信息已保存"}


@app.put("/tenants/{code}/enabled")
def toggle_tenant(
    code: str,
    req: TenantEnabledIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("tenant", "update")),
):
    rows = pg_query("SELECT code, name FROM tenants WHERE code=%s", (code,))
    if not rows:
        raise HTTPException(status_code=404, detail="租户不存在")
    if not req.enabled and code in RESERVED_TENANT_CODES:
        raise HTTPException(status_code=400, detail=f"系统保留租户「{code}」不可停用")
    if not req.enabled and code == user["tenant_code"]:
        raise HTTPException(status_code=400, detail="不能停用当前登录账号所属的租户")
    if not req.enabled:
        # 停用后该租户账号将无法登录；有启用账号时要求先处理，避免"停租户即锁死一批账号"
        alive = pg_scalar(
            "SELECT count(*) FROM users u JOIN tenants t ON t.id=u.tenant_id "
            "WHERE t.code=%s AND u.enabled", (code,))
        if alive:
            raise HTTPException(
                status_code=400,
                detail=f"该租户下仍有 {int(alive)} 个启用账号，请先停用或迁移这些账号再停用租户",
            )
    pg_exec("UPDATE tenants SET enabled=%s WHERE code=%s", (req.enabled, code))
    write_audit(user, "启用租户" if req.enabled else "停用租户", code, {},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "enabled": req.enabled,
            "message": f"租户「{rows[0]['name']}」已{'启用' if req.enabled else '停用'}"}


@app.delete("/tenants/{code}")
def delete_tenant(
    code: str,
    request: Request,
    user: Dict[str, Any] = Depends(require("tenant", "delete")),
):
    rows = pg_query("SELECT code, name FROM tenants WHERE code=%s", (code,))
    if not rows:
        raise HTTPException(status_code=404, detail="租户不存在")
    if code in RESERVED_TENANT_CODES:
        raise HTTPException(status_code=400, detail=f"系统保留租户「{code}」不可删除")
    if code == user["tenant_code"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号所属的租户")
    users_cnt = int(pg_scalar("SELECT count(*) FROM users WHERE tenant_id=(SELECT id FROM tenants WHERE code=%s)", (code,)) or 0)
    if users_cnt:
        raise HTTPException(status_code=409, detail=f"该租户下仍有 {users_cnt} 个用户，请先迁移后再删除")
    stats = tenant_index_stats().get(code, {"docs": 0})
    if int(stats.get("docs") or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail=(f"该租户仍有 {int(stats['docs'])} 条日志（logs-{code}），"
                    "请先在「日志空间」清理索引后再删除租户"),
        )
    pg_exec("DELETE FROM tenant_quotas WHERE tenant_code=%s", (code,))
    pg_exec("DELETE FROM tenants WHERE code=%s", (code,))
    from .storage import drop_tenant_index

    drop_tenant_index(code)
    write_audit(user, "删除租户", f"{rows[0]['name']}（{code}）", {},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "message": f"租户「{rows[0]['name']}」已删除"}


# ---------------------------------------------------------------------------
# 角色权限
# ---------------------------------------------------------------------------
class RoleIn(BaseModel):
    name: Optional[str] = None
    scopeDesc: Optional[str] = None
    permissions: Optional[List[str]] = None


def role_row(r: Dict[str, Any], with_perms: bool = True) -> Dict[str, Any]:
    data = {
        "key": r["key"],
        "name": r["name"],
        "scopeDesc": r["scope_desc"],
        "isBuiltin": r["is_builtin"],
        "memberCount": int(r.get("member_count") or 0),
        "updatedAt": to_cst_str(r.get("updated_at")),
    }
    if with_perms:
        data["permissions"] = perms_for_role(r["key"])
    return data


@app.get("/roles")
def list_roles(user: Dict[str, Any] = Depends(require("role", "view"))):
    rows = pg_query(
        """SELECT r.*, (SELECT count(*) FROM users u WHERE u.role_key = r.key) AS member_count
             FROM roles r ORDER BY r.sort_order, r.key"""
    )
    return {
        "items": [role_row(r) for r in rows],
        "permissionTree": PERMISSION_TREE,
        "actionLabels": ACTION_LABELS,
        "note": "成员数为用户表实时统计；权限改动即时生效，用户无需重新登录。",
    }


@app.put("/roles/{key}")
def update_role(
    key: str,
    req: RoleIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("role", "update")),
):
    rows = pg_query("SELECT * FROM roles WHERE key=%s", (key,))
    if not rows:
        raise HTTPException(status_code=404, detail="角色不存在")
    role = rows[0]

    if key == "platform_admin" and req.permissions is not None:
        raise HTTPException(status_code=400, detail="平台管理员为内置最高权限角色，权限不可裁剪")
    if (key == user["role_key"] and req.permissions is not None
            and "role:update" not in req.permissions):
        raise HTTPException(
            status_code=400,
            detail="不能移除自己所在角色的「角色权限:修改」，否则将无法再管理权限",
        )
    if req.permissions is not None:
        bad = [p for p in req.permissions if p not in ALL_PERMS]
        if bad:
            raise HTTPException(status_code=400, detail=f"存在未定义的权限点：{', '.join(bad[:3])}")

    sets, params = [], []
    if req.name is not None and req.name.strip():
        sets.append("name=%s")
        params.append(req.name.strip())
    if req.scopeDesc is not None:
        sets.append("scope_desc=%s")
        params.append(req.scopeDesc.strip())
    if sets:
        params.append(key)
        pg_exec(f"UPDATE roles SET {', '.join(sets)}, updated_at=now() WHERE key=%s", tuple(params))

    if req.permissions is not None:
        pg_exec("DELETE FROM permissions WHERE role_key=%s", (key,))
        for perm in sorted(set(req.permissions)):
            resource, action = perm.split(":", 1)
            pg_exec(
                """INSERT INTO permissions (role_key, resource, action)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (key, resource, action),
            )
        pg_exec("UPDATE roles SET updated_at=now() WHERE key=%s", (key,))

    write_audit(user, "修改角色权限", role["name"], {"permissions": req.permissions},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    fresh = pg_query(
        """SELECT r.*, (SELECT count(*) FROM users u WHERE u.role_key=r.key) AS member_count
             FROM roles r WHERE r.key=%s""", (key,))[0]
    return {"ok": True, "role": role_row(fresh), "message": "角色权限已保存并实时生效"}


# ---------------------------------------------------------------------------
# 系统参数
# ---------------------------------------------------------------------------
class ParamsIn(BaseModel):
    items: Dict[str, Any]


# 迁移标记不是业务参数：例如 migration.retire_demo_tenants.v1 只用于控制
# 「废止演示租户」一次性清理是否已经执行。它必须留在库里，但不能出现在系统设置页，
# 更不能让用户改写——写成别的值会让下次启动重新执行清理，误删后来新建的同名租户。
INTERNAL_PARAM_PREFIXES = ("migration.",)


def is_internal_param(key: str) -> bool:
    return str(key or "").startswith(INTERNAL_PARAM_PREFIXES)


@app.get("/params")
def get_params(user: Dict[str, Any] = Depends(require("param", "view"))):
    rows = [r for r in pg_query("SELECT * FROM system_params ORDER BY pkey")
            if not is_internal_param(r["pkey"])]
    return {
        "items": [
            {
                "key": r["pkey"], "label": r["label"], "value": r["pvalue"],
                "type": r["ptype"], "note": r["note"], "options": r["options"] or [],
                "updatedAt": to_cst_str(r["updated_at"]),
            }
            for r in rows
        ],
        "note": ("日志保留天数已归「日志空间管理 · 存储配置」统一设置，此处不再重复；"
                 "检索耗时目标已驱动运行总览的达标判定，单次检索上限仍是登记项。"),
    }


@app.put("/params")
def put_params(
    req: ParamsIn,
    request: Request,
    user: Dict[str, Any] = Depends(require("param", "update")),
):
    if not req.items:
        raise HTTPException(status_code=400, detail="没有需要保存的参数")
    defs = {r["pkey"]: r for r in pg_query("SELECT * FROM system_params")
            if not is_internal_param(r["pkey"])}
    unknown = [k for k in req.items if k not in defs]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知参数：{', '.join(unknown)}")

    for k, v in req.items.items():
        d = defs[k]
        val = str(v).lower() if isinstance(v, bool) else str(v).strip()
        if d["ptype"] == "int":
            if not val.lstrip("-").isdigit():
                raise HTTPException(status_code=400, detail=f"「{d['label']}」必须是整数")
        elif d["ptype"] == "enum":
            options = [str(o) for o in (d["options"] or [])]
            if options and val not in options:
                raise HTTPException(status_code=400, detail=f"「{d['label']}」取值不在可选范围内")
        elif d["ptype"] == "bool":
            if val not in ("true", "false"):
                raise HTTPException(status_code=400, detail=f"「{d['label']}」必须是 true/false")
        pg_exec("UPDATE system_params SET pvalue=%s, updated_at=now() WHERE pkey=%s", (val, k))

    # 日志保留天数只归「日志空间管理 · 存储配置」管（storage_config.retain_days），
    # 系统参数里不再保留同名项，这里自然没有需要回写的地方。
    write_audit(user, "修改系统参数", f"{len(req.items)} 项", {"items": req.items},
                client_ip(request.headers.get("x-forwarded-for"),
                          request.client.host if request.client else ""))
    return {"ok": True, "message": "系统参数已保存"}


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------
def audit_filter(keyword: str, action: str, result: str,
                 date_from: str, date_to: str) -> tuple:
    where = ["1=1"]
    params: List[Any] = []
    if keyword.strip():
        where.append("(user_name ILIKE %s OR account ILIKE %s OR target ILIKE %s OR action ILIKE %s)")
        kw = f"%{keyword.strip()}%"
        params += [kw, kw, kw, kw]
    if action:
        where.append("action = %s")
        params.append(action)
    if result in ("success", "failed"):
        where.append("result = %s")
        params.append(result)
    if date_from:
        where.append("created_at >= %s::date")
        params.append(date_from)
    if date_to:
        # 传入日期按「当天结束」包含，避免用户选同一天查出 0 条
        where.append("created_at < (%s::date + interval '1 day')")
        params.append(date_to)
    return " AND ".join(where), params


@app.get("/audit")
def list_audit(
    keyword: str = "",
    action: str = "",
    result: str = "",
    dateFrom: str = "",
    dateTo: str = "",
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    user: Dict[str, Any] = Depends(require("audit", "view")),
):
    cond, params = audit_filter(keyword, action, result, dateFrom, dateTo)
    total = pg_scalar(f"SELECT count(*) FROM audit_log WHERE {cond}", tuple(params))
    rows = pg_query(
        f"""SELECT * FROM audit_log WHERE {cond}
            ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s""",
        tuple(params + [pageSize, (page - 1) * pageSize]),
    )
    actions = [r["action"] for r in pg_query(
        "SELECT DISTINCT action FROM audit_log ORDER BY action")]
    return {
        "items": [
            {
                "id": r["id"],
                "time": to_cst_str(r["created_at"]),
                "userName": r["user_name"],
                "account": r["account"],
                "action": r["action"],
                "target": r["target"],
                "ip": r["ip"],
                "result": r["result"],
            }
            for r in rows
        ],
        "total": int(total or 0),
        "page": page,
        "pageSize": pageSize,
        "actions": actions,
    }


@app.get("/audit/export.csv")
def export_audit(
    keyword: str = "",
    action: str = "",
    result: str = "",
    dateFrom: str = "",
    dateTo: str = "",
    user: Dict[str, Any] = Depends(require("audit", "export")),
):
    cond, params = audit_filter(keyword, action, result, dateFrom, dateTo)
    rows = pg_query(
        f"SELECT * FROM audit_log WHERE {cond} ORDER BY created_at DESC, id DESC LIMIT 20000",
        tuple(params),
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["操作时间", "操作人", "账号", "操作类型", "操作对象", "来源 IP", "结果"])
    for r in rows:
        writer.writerow([to_cst_str(r["created_at"]), r["user_name"], r["account"],
                         r["action"], r["target"], r["ip"],
                         "成功" if r["result"] == "success" else "失败"])
    write_audit(user, "导出审计日志", f"{len(rows)} 条记录")
    return Response(
        content=("\ufeff" + buf.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )


def _index_rollover_loop() -> None:
    """跨天索引滚动后台任务（每 5 分钟）。

    为什么必须独立存在：稳态切换后 ingest 只写 Kafka，不再触达 OpenSearch，
    写别名就没人维护了。若不滚动，写别名一直指向昨天的索引，ISM 一到保留期
    把昨天索引删掉，Flink 写入立即全部失败且日志无处可去。
    幂等，重跑无副作用；失败只打印，不影响管理服务正常运行。
    """
    from .storage import ensure_all_tenant_indices

    time.sleep(10)  # 等 init_db() 建完 tenants 表，避免首次查询报错
    while True:
        try:
            result = ensure_all_tenant_indices()
            if result["ok"] != result["total"]:
                print(f"[index-rollover] 部分租户索引未就绪：{result}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[index-rollover] failed: {e}", flush=True)
        time.sleep(300)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    threading.Thread(target=_index_rollover_loop, name="index-rollover",
                     daemon=True).start()
