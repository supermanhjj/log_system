"""search-service —— 检索网关：OpenSearch 检索封装（开发期骨架升级版，已接真实检索）。

对应原型 LOG-0201 / DSH-0302。
- /query   : 走 OpenSearch _search（可靠路径，全链路验证主用）
- /query+ppl: 尝试 PPL（opensearch-sql 插件，未装则回退 502 报错，供验证插件是否就绪）
开发期安全插件已关闭，无需账号密码。
"""
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from opensearchpy import OpenSearch

OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "opensearch")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))

# 开发期安全插件关闭，无 auth；连接惰性建立，模块导入不触发网络。
client = OpenSearch(
    hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
    http_auth=None,
    use_ssl=False,
    verify_certs=False,
)

app = FastAPI(title="search-service", version="0.2.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "search-service"}


@app.get("/")
def root():
    return {"service": "search-service", "docs": "/docs", "note": "POST /query 走 _search；附 ppl 字段则尝试 PPL"}


class QueryReq(BaseModel):
    index: str = "logs-default"
    body: Optional[Dict[str, Any]] = None
    ppl: Optional[str] = None


@app.post("/query")
def query(req: QueryReq):
    """检索端点：经 nginx /api/search/query 进入。"""
    try:
        if req.ppl:
            # 尝试 PPL（需 opensearch-sql 插件；未装则返回 404 错误，供验证插件就绪情况）
            resp = client.sql.query(body={"query": req.ppl, "format": "json"})
            return {"engine": "ppl", "result": resp}
        body = req.body or {"query": {"match_all": {}}, "size": 20}
        resp = client.search(index=req.index, body=body)
        return {
            "engine": "opensearch",
            "total": resp["hits"]["total"],
            "took_ms": resp.get("took"),
            "hits": resp["hits"]["hits"],
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"opensearch error: {e}")
