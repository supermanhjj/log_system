"""system-service —— 用户/角色/系统参数/审计（骨架占位）。

对应原型 SYS-0601。RBAC 三表、tenants、audit_log、system_params 存 PostgreSQL，
各业务表含 tenant_id 行级隔离（docs 3.2(9)）。
"""
from fastapi import FastAPI

app = FastAPI(title="system-service", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "system-service"}


@app.get("/")
def root():
    return {"service": "system-service", "docs": "/docs"}
