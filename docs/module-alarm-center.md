# 告警中心模块 · 建设思路与开发进展

> 快照日期：2026-11-14（GMT+8）｜基于当前工作区真实代码整理，行号随代码变动可能偏移
> 关联模块：[运行总览](module-overview.md) · [日志分析](module-log-search.md) · [数据接入](module-data-ingestion.md)
> 设计基线：[技术栈选型与方案设计](tech-stack-selection.md) · [服务清单](service-inventory.md)

---

## 0. 本期全局进展快照

| 层次 | 当前状态 |
|---|---|
| 前端 | 已从 `prototype/` 静态高保真 HTML 升级为 **Vue3 + Element Plus + ECharts + Pinia** 单页应用，构建产物 `frontend/dist` 由 nginx 托管；本次提交的是源码，`dist/` 按 `.gitignore` 不入库，clone 后需 `npm run build` |
| 网关 | nginx 统一入口 `http://localhost:8080`，`/api/alarms/ → alarm-service:8000/`（`nginx/nginx.conf:24`） |
| 后端 | 8 个微服务容器内统一 `:8000`，宿主机 `8001–8008` 暴露；PG 存规则与人工状态、OpenSearch 存日志并用于判定 |
| 采集 | `log-generator` → 共享卷 → Fluent Bit → 按租户写入 `logs-default / logs-acme / logs-gp` |
| 运行环境 | **18 个容器 running，8001–8008 `/health` 全部 200**（`docs/service-inventory.md:5,110`） |

---

## 1. 模块定位与建设思路

### 1.1 设计目标（来自原型规格）

原型定义两个页面：`ALM-0401` 告警事件（列表、批量关闭、详情抽屉、导出、状态机「告警中/维护中/已恢复」）与 `ALM-0402` 告警规则（CRUD、启用开关、保存后实时生效）。技术选型原方案是 **Flink 消费 Kafka 日志流 → 匹配规则 → 写 PG 事件表 + 通知**（`docs/tech-stack-selection.md:60-61,99-104`）。

### 1.2 实际落地思路（与设计的偏差必须说清）

| 维度 | 设计稿 | 当前实现 |
|---|---|---|
| 判定引擎 | Flink Job 消费 Kafka 实时判定 | **请求触发的同步判定**：`GET /events` 时遍历 enabled 规则，用 OpenSearch DSL `count ≥ threshold` 实时算，结果缓存 15s（`services/alarm-service/app/main.py:279-349,354,368-405`）；前端 60s 轮询（`AlarmEvents.vue:265`） |
| 事件持久化 | PG 事件主表 + 状态流转 | **不落事件表**：事件按规则实时生成，`event_id = rule:{id}`，每规则仅一条；只把人工状态（关闭/维护）写 PG `alarm_event_states` 覆盖（`main.py:387,389-401,408-420`） |
| 通知 | `notify` 渠道发送 | 字段仅存储/展示，无发送代码（`main.py:58,158,233`） |
| Kafka / Flink | 核心链路 | 容器在编排中运行，但本服务代码不连 Kafka、无 Flink Job（`flink-jobs/README.md:1,10,14` 明确「占位，真实 Job 包待实现」）；`docs/container-deployment.md:70` 记「当前无 topic」 |

这样选型的现实理由：在开发/演示阶段用「读时判定 + 状态覆盖」可以零调度组件就让规则即时生效、避免事件表去重与恢复逻辑的复杂度；代价是没有历史告警、没有真实推送、并发判定依赖 OpenSearch。

### 1.3 三条关键实现取舍

1. **规则 → DSL，而非 PPL**：`_must_for_rule():261-276` 把规则结构化为 OpenSearch DSL（`range @timestamp`、`term level/source`、`range status`、`multi_match message/raw`），`evaluate_rule()` 用 `count ≥ threshold` 判定并顺带取样例、来源、主机。
2. **关联日志自适应生成 PPL**：`build_related_pql():494-581` 先通过 `_index_fields():450-469` 读真实 mapping、`_pick_role():472-483` 探测字段角色，再按可用字段拼 `source=... | where ... | fields ... | head 100`，字段缺失时返回 `skipped/note` 而不是硬编码报错。
3. **结构化规则表单替代 SPL 文本域**：原型 `alarm-rules.html:191-195` 用 SPL 条件文本域，Vue 改为「数据源 / 级别 / 状态码 / 关键词 / 时间窗 / 阈值」结构化字段（`AlarmRules.vue:340-367`），便于后端转 DSL。

数据流：`AlarmEvents/AlarmRules.vue → /api/alarms/* → alarm-service → PG(规则、状态) + OpenSearch(logs-* 判定) → 关联跳转 /search?ppl=...`。

---

## 2. 已落地能力清单

| 能力 | 前端落点 | 后端落点 | 状态 |
|---|---|---|---|
| 告警事件列表 + 5 项筛选 + 分页 | `AlarmEvents.vue:25-31,80-104` | `GET /events`（`main.py:724`） | ✅ |
| 详情抽屉 | `openDetail():127` | `GET /events/{id}`（`:779`） | ✅ |
| 单条 / 批量关闭 | `onClose/doClose/onBatchClose():162-205` | `POST /events/close`、`/events/{id}/close`（`:794,814`） | ✅ 写 PG 状态覆盖 |
| 维护中状态 | — | `POST /events/{id}/maintain`（`:820`） | ✅ |
| CSV 导出（勾选/当前筛选） | `exportCSV/onExport():208-259` | — | ✅ 前端侧 |
| 60s 自动刷新 | `:265` | 15s 服务端缓存 `:354` | ✅ |
| 关联日志跳转 | `viewRelatedLog():134-159` | `GET /events/{id}/related-query`（`:758`）→ `build_related_pql()` | ✅ 跳 `/search` 并回填执行 |
| 规则列表 / 4 项筛选 | `AlarmRules.vue:26,75-94` | `GET /rules`（`:612`） | ✅ |
| 规则 CRUD / 启停 / 去事件页 | `openCreate/openEdit/onSubmit/onDelete/onToggle():113-213` | `POST/GET/PUT/DELETE /rules`、`/rules/{id}/toggle`（`:638-715`） | ✅ 落 PG |
| 判定条件文案 | `conditionText():295` | — | ✅ |
| 通知渠道发送、确认状态、自动恢复 | 无 | 无 | ❌ 未实现 |

---

## 3. 接口与数据结构

**后端 14 个路由**（`services/alarm-service/app/main.py`）：`/health:587`、`/:592`、`GET /rules:612`、`POST /rules:638`、`GET /rules/{id}:667`、`PUT /rules/{id}:675`、`DELETE /rules/{id}:708`、`POST /rules/{id}/toggle:715`、`GET /events:724`、`GET /events/{id}/related-query:758`、`GET /events/{id}:779`、`POST /events/close:794`、`POST /events/{id}/close:814`、`POST /events/{id}/maintain:820`；`startup init_db():605-608`。

**前端封装**：`frontend/src/api/alarms.js:8,43-100`，`baseURL '/api/alarms'`。

| 数据 | 定义 | 说明 |
|---|---|---|
| `RuleIn` | `main.py:224-235` | name / match_source / match_level / match_status_gte / keyword / window_minutes / threshold / level / notify[] / enabled / owner |
| `alarm_rules` | 建表 SQL 内嵌 `main.py:148-165`；种子规则 `:187-218` | 规则主数据，PG |
| `alarm_event_states` | `main.py:168-178` | tenant + event_id 主键，存 status / handler / reason / updated_at；仅人工状态 |
| OpenSearch 索引 | `INDEX_BY_TENANT / index_for:72-79` | 只读 `logs-<tenant>` / `logs-*`，全文无 `os_client.index`，**不写告警索引** |
| 事件 ID | `main.py:387` | `rule:{规则ID}`，每规则一条事件 |

降级边界：PG 不可用服务仍可启动（`:183-184`）；字段缺失在关联 PPL 里跳过（`:431,529,545`）；规则已删除时关联查询降级（`:769`）。

---

## 4. 原型对照与差异

| 原型 | 当前 Vue 实现 |
|---|---|
| 值班组 / 所属区域 / 通知状态筛选、指派处理人（`prototype/alarms.html:36-88,100`） | 未实现 |
| 「30 分钟内同源告警自动合并」（`:23`）、36 条静态假数据（`:105,230`） | 未实现合并；改为按规则实时生成，无历史列表 |
| 「查看关联日志」仅 toast（`:260`） | 真实跳 `/search` 并回填 PPL 执行 |
| 批量关闭自定义 modal（`:265-281`） | 改用 `ElMessageBox.prompt`（`AlarmEvents.vue:193`） |
| 规则状态「运行中/维护中/离线」（`alarm-rules.html:52`） | 「运行中/已停用」（`AlarmRules.vue:270-273`） |
| 判定条件 SPL 文本域（`:191-195`） | 结构化字段表单 |
| 数据源含 host-monitor / db-audit / cert-monitor（`:186`） | `REAL_SOURCES` 仅 5 个业务服务（`api/alarms.js:11-17`） |
| 规则名称唯一校验（`:179`） | 仅 required（`AlarmRules.vue:49`） |

---

## 5. 真实实现 vs 占位 / 模拟

**真实实现**：规则 CRUD 与启停落 PG；OpenSearch 实时判定与聚合取样；状态覆盖；关联 PPL 按真实 mapping 自适应生成；CSV 导出；与检索页的回填联动。

**占位 / 需知悉的简化**：

1. 事件不落库、无历史、无去重合并；「告警中/已恢复/维护中」是 PG 状态覆盖的结果，无「确认」端点、无自动恢复。
2. `notify` 仅存储展示；前端「恢复通知已发送」是提示文案（`AlarmEvents.vue:165`），无真实发送。
3. Flink / Kafka 实时判定为设计稿，`flink-jobs/` 仍是占位（`docs/tech-stack-selection.md:103,201,229`）。
4. 原型里的 `data-toast` 假交互已被真实接口替换，但 `/api/alarms/` 之外仍有通用占位页 `Placeholder.vue`（非告警路由组件）。
5. 编排里 `depends_on: [postgres, opensearch, kafka]`（`docker-compose.yml:152-160`），代码实际不连 Kafka/Redis。

---

## 6. 当前边界与后续待办

| 优先级 | 待办 | 说明 |
|---|---|---|
| P1 | 事件落库与历史 | 建独立事件表，按窗口去重、保留发生/恢复时间，支持历史检索与统计 |
| P1 | 状态机与通知补全 | 确认 / 指派处理人 / 自动恢复；`notify` 渠道真正发送，去掉假提示 |
| P1 | 判定引擎演进 | 若要「保存即实时生效 + 海量日志」，需按设计稿补 Kafka topic + Flink Job；否则明确把「请求时判定」定为正式方案并更新设计文档 |
| P2 | 关联 PPL 字段白名单 | 与检索模块 P0 闸门合并：索引白名单、函数白名单、`head` 上限、tenant 强绑定（`docs/tech-stack-selection.md:187-190`） |
| P2 | 原型差异收口 | 值班组/区域/通知状态筛选、规则名唯一校验、数据源枚举扩充 |
| P2 | 与运行总览打通 | 总览「最近告警」目前由日志 level 推导，应改读告警中心事件 |

---

## 7. 验证记录与运行入口

- 页面：`http://localhost:8080/alarms/events`、`/alarms/rules`；后端 Swagger `http://localhost:8004/docs`。
- 直连示例：`curl 'http://localhost:8004/events?tenant=default&page=1&pageSize=20'`、`curl http://localhost:8004/rules`。
- 本期已验证：规则 CRUD/启停、事件列表与批量关闭、关联 PPL 生成并跳检索页回填执行；18 容器 running、8001–8008 health 200。
- 前端构建：`npm --prefix frontend run build`。

---

## 8. 相关文件索引

| 文件 | 作用 |
|---|---|
| `frontend/src/views/AlarmEvents.vue`、`AlarmRules.vue` | 事件与规则两个页面 |
| `frontend/src/api/alarms.js` | 告警接口封装与数据源枚举 |
| `services/alarm-service/app/main.py` | 规则、判定、事件、状态、关联 PPL（唯一后端文件） |
| `prototype/alarms.html`、`prototype/alarm-rules.html` | 高保真原型 |
| `flink-jobs/README.md`、`docs/tech-stack-selection.md`（3.2 第 5/6 节） | 实时判定设计稿（当前为占位） |
| `nginx/nginx.conf:24` | 网关路由 |
