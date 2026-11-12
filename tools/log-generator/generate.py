#!/usr/bin/env python3
"""log-generator —— 模拟被测业务系统，持续输出多租户日志到 Fluent Bit 采集路径。

设计：
- 单机无真实业务，但持续吐 `[LEVEL] <msg> tenant=<id>` 格式日志，租户/级别/频率可配。
- 与 Fluent Bit 共享命名卷 app_logs（生成器写，Fluent Bit tail 读），构成完整"业务系统→采集"链路。
- ERROR/WARN 按概率出现，便于后续测告警触发。
- 见 docs/tech-stack-mapping.md 3.2(9) 多租户隔离 / 9.x 测试 harness。
"""
import os
import time
import random
from datetime import datetime

LOG_PATH = os.getenv("LOG_PATH", "/var/log/app/app.log")
INTERVAL = float(os.getenv("LOG_INTERVAL", "1.0"))
TENANTS = [t.strip() for t in os.getenv("TENANTS", "default,acme,gp").split(",") if t.strip()]
ERROR_RATE = float(os.getenv("ERROR_RATE", "0.2"))
WARN_RATE = float(os.getenv("WARN_RATE", "0.25"))

INFO_MSGS = [
    "user login success",
    "order processed",
    "request handled in 45ms",
    "cache hit for key user:profile",
    "session created",
    "config reloaded",
]
WARN_MSGS = [
    "slow query detected 1200ms on table orders",
    "high latency 860ms on /api/checkout",
    "memory usage 78% on heap",
    "retry attempt 2 for upstream payment",
    "connection pool near limit 92/100",
]
ERROR_MSGS = [
    "failed to connect to upstream db: timeout after 5000ms",
    "disk usage 92% on /var/log",
    "null pointer exception in OrderHandler.process",
    "payment gateway returned 503",
    "kafka produce failed: leader not available",
]


def pick() -> tuple[str, str]:
    r = random.random()
    if r < ERROR_RATE:
        return "ERROR", random.choice(ERROR_MSGS)
    if r < ERROR_RATE + WARN_RATE:
        return "WARN", random.choice(WARN_MSGS)
    return "INFO", random.choice(INFO_MSGS)


def main() -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    print(f"[log-generator] -> {LOG_PATH} tenants={TENANTS} interval={INTERVAL}s "
          f"err={ERROR_RATE} warn={WARN_RATE}", flush=True)
    while True:
        level, msg = pick()
        tenant = random.choice(TENANTS)
        line = f"[{level}] {msg} tenant={tenant}\n"
        with open(LOG_PATH, "a") as f:
            f.write(line)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
