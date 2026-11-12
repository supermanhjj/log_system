# log_system

日志系统开发目录（Vibe_Coding 工作区）。

> 初始化于 2026-11-11。

## 说明
本仓库用于存放 log_system 相关开发代码、配置与文档。

## Docker 开发环境（骨架）

技术路线见 `docs/tech-stack-mapping.md`（v1.5：Vue3 + FastAPI + Fluent Bit +
OpenSearch/PPL + Kafka + Flink + PostgreSQL + Redis）。

```bash
# 1. 准备配置（已生成 .env，按需改密码）
cp .env.example .env        # 若 .env 不存在

# 2. 构建并启动全部容器（数据层 + 应用层 + 采集层）
docker compose up -d --build

# 3. 访问
#    前端（静态原型）      http://localhost:8080
#    OpenSearch-Dashboards http://localhost:5601
#    各微服务（直连调试）  http://localhost:8001..8007/health
#    Flink Web UI          http://localhost:8081

# 4. 关闭并清理数据卷（重置开发环境）
docker compose down -v
```

开发期源码经 bind mount 进容器（`compose.override.yml`），改本地文件即热更（uvicorn --reload）。
生产/演示编排见 `docker-compose.prod.yml`（数据层改为云托管）。

> Apple Silicon 注意：OpenSearch 若报架构错误可加 `platform: linux/amd64`；Flink 已默认以 amd64 模拟运行。
> 详见 `docs/tech-stack-mapping.md` 第 7 章与各服务 `services/<name>/app/main.py` 骨架注释。
