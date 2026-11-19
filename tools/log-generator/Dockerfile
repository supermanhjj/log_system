# 测试业务系统模拟器：持续输出多租户日志，供 Fluent Bit 采集。
# 见 docs/tech-stack-mapping.md 9.x 测试 harness。
# backfill.py 为历史回填工具（按日期/时段补数，同样走 Fluent Bit 采集链路）：
#   docker compose exec -T log-generator python /app/backfill.py --date 2026-11-17 --from 09:00 --to 22:00
FROM python:3.11-slim
WORKDIR /app
COPY generate.py /app/generate.py
COPY backfill.py /app/backfill.py
CMD ["python", "/app/generate.py"]
