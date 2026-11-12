# ---- builder：安装 Python 依赖到独立 venv（依赖与源码分离，便于 dev bind mount 复用） ----
FROM python:3.11-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    PIP_TIMEOUT=120 \
    PIP_RETRIES=10
WORKDIR /build
COPY services/requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---- runtime：精简运行 ----
FROM python:3.11-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY services /app/services
# 非 root 运行
RUN useradd -m -s /bin/bash appuser \
 && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
# 每个服务在 compose 中通过 command 指定各自 uvicorn 入口与端口；
# 开发期源码 bind mount 覆盖 /app/services，依赖仍用 /opt/venv
CMD ["sh", "-c", "echo 'override command per service in compose'"]
