# Flink 实时告警 Job（占位）

实时判定逻辑：消费 Kafka 日志流 → 匹配 `alarm_rules` 规则 `condition` → 触发 →
写告警事件（PostgreSQL）+ 通知（`notify` 渠道）。对应 docs/tech-stack-mapping.md 3.2(6) / ALM-0402。

## 本骨架状态

- `docker-compose.yml` 已拉起 `flink-jobmanager` + `flink-taskmanager`（官方 `flink:1.18-scala_2.12`，
  Apple Silicon 下以 `platform: linux/amd64` 模拟运行）。
- 真实 Job 包（JAR / PyFlink）待实现后，通过以下方式提交：
  - 命令行：`docker compose exec flink-jobmanager flink run -d /opt/flink/jobs/alarm-job.jar`
  - 或 Web UI：`http://localhost:8081`

## 语言选型待定（见设计文档待拍板项）

- 标准 Flink Job：**Java / Scala**（性能最佳，生态最成熟）。
- 或 **PyFlink**：与本栈 Python 生态统一，但 arm64 模拟环境下踩坑概率略高。
- 选定后在此目录落地 `pom.xml` / `build.gradle` 或 `alarm_job.py` + 提交脚本。
