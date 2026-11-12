"""ingest-service —— 数据源管理、连通性校验、Kafka topic 管理（骨架占位）。

对应原型 DAT-0501。实际采集由 Fluent Bit 承担（docs 3.2(7) / 9.1）。
"""
from fastapi import FastAPI

app = FastAPI(title="ingest-service", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "ingest-service"}


@app.get("/")
def root():
    return {"service": "ingest-service", "docs": "/docs"}
