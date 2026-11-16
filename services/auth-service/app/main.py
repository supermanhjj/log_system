"""auth-service —— 登录 / JWT 签发 / 当前用户与权限。

对应原型 AUTH-0100（/login）。职责边界（与规划文档一致）：
- `auth-service`：登录校验、JWT 签发、`/me` 返回当前用户与权限、登出与改密审计。
- `system-service`：用户 / 角色 / 权限 / 参数 / 审计的**管理面 CRUD**，并且是六张表的建表方。

两边共用 `JWT_SECRET` 与 `JWT_ALGORITHM`（.env 已有）。本服务不建表、不写业务表，
只在登录成功时更新 last_login，以及写 audit_log。

失败语义（前端按此区分展示，不糊成一句「登录失败」）：
- 400 账号或密码为空            → 「请输入账号与密码」
- 401 账号不存在 / 密码错误      → 「账号或密码错误，请重新输入」
- 403 账号已停用 / 租户不匹配    → 「该账号未开通平台权限，请联系管理员」
"""
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .db import pg_exec, pg_query, to_cst_str
from .security import (
    EXPIRE_MINUTES,
    PWD_RULE,
    client_ip,
    create_token,
    current_user,
    hash_password,
    password_error,
    perms_for_role,
    verify_password,
)

app = FastAPI(title="auth-service", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TOKEN_TTL_NOTE = f"登录态有效期 {EXPIRE_MINUTES // 60} 小时（ACCESS_TOKEN_EXPIRE_MINUTES）"


class LoginIn(BaseModel):
    account: str
    password: str
    tenant: Optional[str] = None
    remember: bool = False


class ChangePwdIn(BaseModel):
    oldPassword: str
    newPassword: str


def write_audit(user: Optional[Dict[str, Any]], action: str, target: str = "",
                ip: str = "", result: str = "success",
                detail: Optional[Dict[str, Any]] = None,
                account: str = "", name: str = "") -> None:
    """审计写入失败不阻断登录，但要留痕到服务日志。"""
    import json

    try:
        pg_exec(
            """INSERT INTO audit_log (tenant_id, user_id, user_name, account,
                                      action, target, detail, ip, result)
               VALUES ((SELECT id FROM tenants WHERE code=%s), %s, %s, %s, %s, %s, %s, %s, %s)""",
            ((user or {}).get("tenant_code"), (user or {}).get("id"),
             (user or {}).get("name") or name, (user or {}).get("account") or account,
             action, target, json.dumps(detail or {}, ensure_ascii=False), ip, result),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[auth-service] audit write failed: {e}", flush=True)


def user_payload(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user["id"],
        "account": user["account"],
        "name": user["name"],
        "roleKey": user["role_key"],
        "roleName": user["role_name"],
        "tenant": user["tenant_code"],
        "tenantName": user["tenant_name"],
        "lastLogin": to_cst_str(user.get("last_login_at")),
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "auth-service"}


@app.get("/")
def root():
    return {
        "service": "auth-service",
        "version": "0.2.0",
        "scope": "登录 / JWT / 当前用户与权限",
        "endpoints": ["/login", "/me", "/logout", "/password", "/tenants"],
    }


@app.get("/tenants")
def list_tenants():
    """登录页租户下拉：只返回已启用租户，且不暴露内部 id。"""
    rows = pg_query("SELECT code, name FROM tenants WHERE enabled ORDER BY id")
    return {"items": [{"code": r["code"], "name": r["name"]} for r in rows]}


@app.post("/login")
def login(req: LoginIn, request: Request):
    account = (req.account or "").strip().lower()
    password = req.password or ""
    ip = client_ip(request.headers.get("x-forwarded-for"),
                   request.client.host if request.client else "")

    if not account or not password:
        raise HTTPException(status_code=400, detail="请输入账号与密码")

    rows = pg_query(
        """SELECT u.*, t.code AS tenant_code, t.name AS tenant_name, r.name AS role_name
             FROM users u
             JOIN tenants t ON t.id = u.tenant_id
             JOIN roles r ON r.key = u.role_key
            WHERE u.account = %s""",
        (account,),
    )
    if not rows or not verify_password(password, rows[0]["password_hash"]):
        # 账号不存在与密码错误统一为同一句话，避免账号枚举
        write_audit(None, "登录失败", "账号或密码错误", ip,
                    result="failed", account=account, name=account)
        raise HTTPException(status_code=401, detail="账号或密码错误，请重新输入")

    user = rows[0]
    if not user["enabled"]:
        write_audit(user, "登录失败", "账号已停用", ip, result="failed")
        raise HTTPException(status_code=403, detail="该账号未开通平台权限，请联系管理员")

    want_tenant = (req.tenant or "").strip()
    if want_tenant and want_tenant != user["tenant_code"]:
        write_audit(user, "登录失败", f"租户不匹配（选择 {want_tenant}）", ip, result="failed")
        raise HTTPException(
            status_code=403,
            detail=f"该账号不属于所选租户，请联系管理员（账号所属：{user['tenant_name']}）",
        )

    pg_exec(
        "UPDATE users SET last_login_at=now(), last_login_ip=%s WHERE id=%s",
        (ip, user["id"]),
    )
    # 回填本次登录时间，避免响应里返回上一次登录时间（列表页与顶栏口径要一致）
    fresh = pg_query("SELECT last_login_at FROM users WHERE id=%s", (user["id"],))
    user["last_login_at"] = fresh[0]["last_login_at"] if fresh else None
    write_audit(user, "登录", "平台 Web 端", ip)
    token = create_token(user)
    return {
        "ok": True,
        "token": token["token"],
        "tokenType": "bearer",
        "expiresIn": token["expiresIn"],
        "expiresAt": token["expiresAt"],
        "user": user_payload(user),
        "permissions": perms_for_role(user["role_key"]),
        "message": "登录成功，正在进入运行总览",
        "note": TOKEN_TTL_NOTE,
    }


@app.get("/me")
def me(user: Dict[str, Any] = Depends(current_user)):
    return {
        "user": user_payload(user),
        "permissions": perms_for_role(user["role_key"]),
        "passwordRule": PWD_RULE,
    }


@app.post("/logout")
def logout(request: Request, user: Dict[str, Any] = Depends(current_user)):
    ip = client_ip(request.headers.get("x-forwarded-for"),
                   request.client.host if request.client else "")
    write_audit(user, "登出", "平台 Web 端", ip)
    return {"ok": True, "message": "已退出登录"}


@app.put("/password")
def change_password(
    req: ChangePwdIn,
    request: Request,
    user: Dict[str, Any] = Depends(current_user),
):
    ip = client_ip(request.headers.get("x-forwarded-for"),
                   request.client.host if request.client else "")
    if not verify_password(req.oldPassword or "", user["password_hash"]):
        raise HTTPException(status_code=400, detail="原密码不正确")
    err = password_error(req.newPassword or "")
    if err:
        raise HTTPException(status_code=400, detail=err)
    if req.oldPassword == req.newPassword:
        raise HTTPException(status_code=400, detail="新密码不能与原密码相同")
    pg_exec("UPDATE users SET password_hash=%s WHERE id=%s",
            (hash_password(req.newPassword), user["id"]))
    write_audit(user, "修改密码", "本人账号", ip)
    return {"ok": True, "message": "密码已修改，下次登录请使用新密码"}
