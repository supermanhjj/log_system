# 测试业务系统模拟器：持续输出多租户日志，供 Fluent Bit 采集。
# 见 docs/tech-stack-mapping.md 9.x 测试 harness。
FROM python:3.11-slim
WORKDIR /app
COPY generate.py /app/generate.py
CMD ["python", "/app/generate.py"]
