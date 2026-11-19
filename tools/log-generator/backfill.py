#!/usr/bin/env python3
"""backfill.py —— 历史日志回填工具（按指定日期与时段补录模拟日志）。

为什么单独做一个工具，而不是直接往 OpenSearch 塞文档：
  回填必须走**与实时流完全相同的采集链路**（写 `<tenant>.log` → Fluent Bit tail →
  `POST /ingest/collector` → 按来源注入租户 → `logs-<tenant>`）。否则补出来的数据
  字段结构、时间语义、租户归属都可能和真实链路不一致，反而会掩盖链路缺陷。

时间语义：
  每行自带事件时间戳（ISO8601 本地时区，如 2026-11-17T09:03:21+0800），Fluent Bit
  抽为事件时间、接入层归一化为 @timestamp。所以补出来的日志在检索/直方图/趋势图里
  落在**指定日期**，而不是"今天刚采集"的时刻。

密度：
  按小时权重波动（工作时段高、午间与晚间低），避免等间隔的机器生成感；权重仅用于
  在 `--from`/`--to` 区间内调节，不改变区间边界。

用法：
  python backfill.py --date 2026-11-17 --from 09:00 --to 22:00 --per-min 8
  python backfill.py --date 2026-11-16,2026-11-17 --per-min 4
  python backfill.py --date 2026-11-17 --from 09:00 --to 22:00 --per-min 8 --unassigned 0.05

注意：
  - `--date` 只用日期部分，时区为容器本地时区（部署环境 Asia/Shanghai）。
  - `--to` 为右开区间（22:00 表示到 21:59 这一分钟为止），符合"早9点到晚10点"的直觉。
  - 回填是追加写，不修改/删除已有日志；重复执行会产生更多日志（幂等性由调用方保证）。
"""
import argparse
import random
import sys
import uuid
from datetime import datetime, timedelta

from generate import (  # noqa: E402  同目录复用字段池与级别/状态码生成规则
    HOSTS,
    LOG_DIR,
    SOURCES,
    TENANTS,
    UNASSIGNED_RATE,
    fmt_time,
    pick,
    partition_of,
    status_for,
)

# 小时权重：模拟业务负载曲线。工作时段高，午间休整，晚间逐步回落，凌晨最低。
HOUR_WEIGHT = {
    0: 0.35, 1: 0.30, 2: 0.25, 3: 0.25, 4: 0.30, 5: 0.40,
    6: 0.60, 7: 0.90, 8: 1.10, 9: 1.25, 10: 1.35, 11: 1.30,
    12: 0.85, 13: 0.95, 14: 1.30, 15: 1.40, 16: 1.40, 17: 1.25,
    18: 0.90, 19: 0.80, 20: 0.75, 21: 0.60, 22: 0.45, 23: 0.40,
}


def parse_hhmm(text: str) -> tuple[int, int]:
    try:
        hh, mm = text.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 24 and 0 <= m <= 59):
            raise ValueError
        return h, m
    except (ValueError, AttributeError):
        raise SystemExit(f"时间格式应为 HH:MM（00:00-24:00），收到：{text!r}")


def parse_dates(text: str) -> list:
    dates = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            dates.append(datetime.strptime(part, "%Y-%m-%d"))
        except ValueError:
            raise SystemExit(f"日期格式应为 YYYY-MM-DD，收到：{part!r}")
    if not dates:
        raise SystemExit("--date 不能为空")
    return dates


def main() -> None:
    ap = argparse.ArgumentParser(description="按日期与时段回填模拟日志（走真实采集链路）")
    ap.add_argument("--date", required=True, help="日期，YYYY-MM-DD，可逗号分隔多天")
    ap.add_argument("--from", dest="start", default="00:00", help="起始时刻 HH:MM（含），默认 00:00")
    ap.add_argument("--to", dest="end", default="24:00", help="结束时刻 HH:MM（不含），默认 24:00")
    ap.add_argument("--per-min", type=float, default=4.0, help="基准密度：每分钟每租户条数，默认 4")
    ap.add_argument("--tenants", default=",".join(TENANTS), help="租户清单，逗号分隔")
    ap.add_argument("--unassigned", type=float, default=UNASSIGNED_RATE,
                    help=f"未归属日志占比，默认 {UNASSIGNED_RATE}")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（便于复现）")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
    tenants = [t.strip() for t in args.tenants.split(",") if t.strip()]
    if not tenants:
        raise SystemExit("--tenants 不能为空")

    sh, sm = parse_hhmm(args.start)
    eh, em = parse_hhmm(args.end)
    if (eh, em) <= (sh, sm):
        raise SystemExit("--to 必须晚于 --from")

    # 按租户累积，最后一次性落盘（各租户内部仍按事件时间递增，读起来是正常日志流）
    buffers: dict[str, list[str]] = {}
    per_tenant: dict[str, int] = {}
    total = 0
    t_min = None
    t_max = None

    for day in parse_dates(args.date):
        cur = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
        stop = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) \
            if (eh, em) == (24, 0) else day.replace(hour=eh, minute=em, second=0, microsecond=0)
        while cur < stop:
            weight = HOUR_WEIGHT.get(cur.hour, 1.0)
            # per-min 是「每分钟每租户」条数：总条数按租户数放大，再按未归属比例替换来源
            per_tenant_min = max(1, round(args.per_min * weight))
            n = per_tenant_min * len(tenants)
            step = 60.0 / n
            for i in range(n):
                ts = cur + timedelta(seconds=step * i + random.uniform(0, step))
                level, msg = pick()
                tenant = "" if random.random() < args.unassigned else random.choice(tenants)
                status = status_for(level)
                line = (
                    f"[{fmt_time(ts)}] [{level}] {msg} source={random.choice(SOURCES)} "
                    f"host={random.choice(HOSTS)} traceId={uuid.uuid4().hex[:16]} status={status}\n"
                )
                key = partition_of(tenant)
                buffers.setdefault(key, []).append(line)
                per_tenant[key] = per_tenant.get(key, 0) + 1
                total += 1
                t_min = ts if t_min is None or ts < t_min else t_min
                t_max = ts if t_max is None or ts > t_max else t_max
            cur += timedelta(minutes=1)

    for name, lines in buffers.items():
        path = f"{LOG_DIR}/{name}.log"
        with open(path, "a") as f:
            f.writelines(lines)

    print(f"[backfill] 已写入 {LOG_DIR}：{len(buffers)} 个来源 / {total} 条", flush=True)
    for name in sorted(per_tenant):
        print(f"  {name + '.log':<24} {per_tenant[name]:>7} 条", flush=True)
    if t_min and t_max:
        print(f"  事件时间范围：{t_min.strftime('%Y-%m-%d %H:%M:%S')} "
              f"~ {t_max.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("  提示：Fluent Bit 会在 Flush 周期内采集上述新增行并写入 logs-<tenant>。", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
