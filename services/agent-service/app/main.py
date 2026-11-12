"""agent-service —— Agent 主机元数据、心跳超时、升级编排（骨架占位，一阶段纳入）。

对应原型 DAT-0502。业务主机仅部署单一 Fluent Bit 进程，其随包管控脚本通过 exec 插件
定时调用本服务的 /heartbeat（写 Redis 最近心跳时间戳，扫 >3min 无心跳标记离线）。
见 docs/tech-stack-mapping.md 3.2(8) / 9.1。
"""
from fastapi import FastAPI, Request

app = FastAPI(title="agent-service", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent-service"}


@app.post("/heartbeat")
async def heartbeat(req: Request):
    # 开发期骨架：仅回显并记录；正式实现写 Redis 最近心跳时间戳。
    # fluent-bit http 输出以 JSON 数组批量发送，故兼容 list / dict 两种形态。
    body = await req.json()
    if isinstance(body, list):
        body = body[0] if body else {}
    print("[agent-service] heartbeat:", body)
    return {"status": "received", "hostname": body.get("hostname")}


@app.get("/")
def root():
    return {"service": "agent-service", "docs": "/docs"}
