"""alarm-service —— 告警事件管理、状态机、通知（骨架占位）。

对应原型 ALM-0401。事件主表/状态流转存 PostgreSQL，原始日志溯源走 OpenSearch。
实时判定由 Flink Job 消费 Kafka 触发（docs 3.2(6)）。
"""
from fastapi import FastAPI

app = FastAPI(title="alarm-service", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "alarm-service"}


@app.get("/")
def root():
    return {"service": "alarm-service", "docs": "/docs"}
