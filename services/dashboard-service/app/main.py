"""dashboard-service —— 看板/图表配置 CRUD、取数编排（骨架占位）。

对应原型 DSH-0301 / DSH-0302。配置存 PostgreSQL，图表取数走 OpenSearch PPL。
"""
from fastapi import FastAPI

app = FastAPI(title="dashboard-service", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "dashboard-service"}


@app.get("/")
def root():
    return {"service": "dashboard-service", "docs": "/docs"}
