"""auth-service —— 登录 / JWT / 租户 / RBAC 判定（骨架占位）。

对应原型 AUTH-0100 / SYS-0601。真实实现需接 PostgreSQL(users/tenants/roles) +
JWT 签发 + 多租户 tenant_id 过滤（见 docs/tech-stack-mapping.md 3.2(1)(9)）。
"""
from fastapi import FastAPI

app = FastAPI(title="auth-service", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "auth-service"}


@app.get("/")
def root():
    return {"service": "auth-service", "docs": "/docs"}
