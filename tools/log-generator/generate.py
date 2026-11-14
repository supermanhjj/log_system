#!/usr/bin/env python3
"""log-generator —— 模拟被测业务系统，持续输出多租户日志到 Fluent Bit 采集路径。

设计：
- 每条日志行自带**事件时间戳**（ISO8601 本地时区，如 2026-11-13T15:28:39+0800），由 Fluent Bit
  的 app_log 解析器以 Time_Key=event_time 抽出并写入 @timestamp —— 即"日志自带时间戳"的
  真实语义，而非采集时刻（ingestion time）。这是上下文按时间流分析、直方图按真实事件时间
  聚合的前提。
- 行格式：`[事件时间] [LEVEL] <msg> source=<svc> host=<host> traceId=<id> tenant=<id> status=<code>`。
- 启动时可回填历史（BACKFILL_DAYS / BACKFILL_PER_MIN），使索引具备真实的时间分布，
  近一周 / 近一月检索与上下文相邻日志才有意义；随后进入实时流式。
- source/host/traceId/status 字段支撑 search/overview/dashboard/alarm 页面所需的列与聚合。
- 与 Fluent Bit 共享命名卷 app_logs（生成器写，Fluent Bit tail 读），构成完整"业务系统→采集"链路。
- ERROR/WARN 按概率出现并带 5xx/4xx 状态码，便于测告警触发与 PPL `status>=500`。
"""
import os
import time
import random
import uuid
from datetime import datetime, timedelta

LOG_PATH = os.getenv("LOG_PATH", "/var/log/app/app.log")
INTERVAL = float(os.getenv("LOG_INTERVAL", "1.0"))
TENANTS = [t.strip() for t in os.getenv("TENANTS", "default,acme,gp").split(",") if t.strip()]
ERROR_RATE = float(os.getenv("ERROR_RATE", "0.2"))
WARN_RATE = float(os.getenv("WARN_RATE", "0.25"))
# 启动回填：往前 BACKFILL_DAYS 天、每分钟 BACKFILL_PER_MIN 条（带历史事件时间戳）。
# 设为 0 可关闭回填（仅实时流）。开发期默认 7 天 / 2 条每分钟，使近一周检索与上下文有真实分布。
BACKFILL_DAYS = float(os.getenv("BACKFILL_DAYS", "7"))
BACKFILL_PER_MIN = float(os.getenv("BACKFILL_PER_MIN", "2"))

# 业务服务名与主机名池：让 source/host 列在检索/大盘里有真实分布
SOURCES = ["order-service", "user-service", "payment-gateway", "inventory-svc", "api-gateway"]
HOSTS = ["host-01", "host-02", "host-03", "web-node-7", "web-node-8"]

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


def status_for(level: str) -> int:
    """按级别给一个合理 HTTP 状态码，使 `status>=500` 类 PPL 有真实命中。"""
    if level == "ERROR":
        return random.choice([500, 503, 502, 404])
    if level == "WARN":
        return random.choice([200, 429, 200])
    return 200


def fmt_time(dt: datetime) -> str:
    """事件时间 → ISO8601 本地时区、无冒号的偏移（Fluent Bit %z 兼容，如 +0800）。"""
    return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def emit(dt: datetime, level: str, msg: str, tenant: str, source: str, host: str,
         trace_id: str, status: int) -> None:
    line = (
        f"[{fmt_time(dt)}] [{level}] {msg} source={source} host={host} "
        f"traceId={trace_id} tenant={tenant} status={status}\n"
    )
    with open(LOG_PATH, "a") as f:
        f.write(line)


def backfill() -> None:
    """启动回填历史日志：以真实事件时间戳铺满 BACKFILL_DAYS 天，使索引具备时间分布。

    回填日志带过去的时间戳，Fluent Bit 按事件时间写入 @timestamp，故直方图 / 上下文
    会基于真实事件时间而非采集顺序。回填完成后才进入实时流。
    """
    if BACKFILL_DAYS <= 0 or BACKFILL_PER_MIN <= 0:
        return
    now = datetime.now()
    start = now - timedelta(days=BACKFILL_DAYS)
    step = 60.0 / BACKFILL_PER_MIN  # 相邻回填日志的事件时间间隔（秒）
    t = start
    count = 0
    while t < now:
        level, msg = pick()
        tenant = random.choice(TENANTS)
        source = random.choice(SOURCES)
        host = random.choice(HOSTS)
        trace_id = uuid.uuid4().hex[:16]
        status = status_for(level)
        emit(t, level, msg, tenant, source, host, trace_id, status)
        t += timedelta(seconds=step)
        count += 1
    print(f"[log-generator] 回填完成：{count} 条 / 近 {BACKFILL_DAYS:.0f} 天", flush=True)


def main() -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    print(f"[log-generator] -> {LOG_PATH} tenants={TENANTS} interval={INTERVAL}s "
          f"err={ERROR_RATE} warn={WARN_RATE} backfill={BACKFILL_DAYS}d@{BACKFILL_PER_MIN}/min",
          flush=True)
    backfill()  # 先回填历史，使时间分布真实可检索
    while True:
        level, msg = pick()
        tenant = random.choice(TENANTS)
        source = random.choice(SOURCES)
        host = random.choice(HOSTS)
        trace_id = uuid.uuid4().hex[:16]
        status = status_for(level)
        emit(datetime.now(), level, msg, tenant, source, host, trace_id, status)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
