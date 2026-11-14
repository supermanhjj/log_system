# code-search.md · search 检索模块实现总结

> 用途：记录 search 模块（检索网关 + 日志检索页）的代码结构与实现进度，作为后续各微服务 `code-<模块>.md` 的模板。
> 最后更新：2026-11-14

---

## 1. 模块定位与目标

`search` 是日志分析平台的**检索网关**，承担前端 ↔ OpenSearch 之间的全部查询封装：

- 文档列表检索（表格 + 详情抽屉）走 **DSL**；
- 管道分析（顶部检索框 `|` 语法、高级统计）走 **PQL**（`/_plugins/_pql`）；
- 直方图、级别分布聚合；
- 多租户索引隔离（`logs-<tenant>`，`all` → `logs-*`）。

对应原型页面：`prototype/search.html`（LOG-0201 / DSH-0302）。

---

## 2. 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI 0.3.0 + opensearch-py 2.x + httpx（PPL 直连） |
| 检索引擎 | OpenSearch 3.9.0（DSL `search` + PPL `/_plugins/_pql`） |
| 前端 | Vue 3 + Vite + Element Plus + ECharts + Pinia + Axios |
| 网关 | nginx（静态托管 + `/api/search/` 反代） |
| 部署 | Docker Compose（源码 bind mount + `uvicorn --reload`） |

---

## 3. 文件清单

```
services/search-service/
└── app/
    ├── __init__.py
    └── main.py            # 全部端点、租户路由、过滤拼装、PQL 安全校验

frontend/
├── src/
│   ├── api/search.js      # axios 封装：searchQuery/Histogram/Pql/Context
│   ├── views/SearchView.vue   # 检索页全部 UI 与交互
│   ├── stores/app.js      # 全局租户状态（default/acme/gp/all）
│   └── public/logo.jpg    # 顶栏 Logo（复制自 prototype/assets/logo.jpg）
└── dist/                  # npm run build 产物，由 web 容器托管

nginx/nginx.conf           # /api/search/ → search-service:8000/（去前缀）
compose.override.yml       # search-service 源码挂载 + 8002:8000 映射
docker-compose.yml         # search-service 服务定义（image: log_system-backend）
```

---

## 4. 后端实现（search-service）

### 4.1 启动与连接
- 入口：`uvicorn app.main:app --host 0.0.0.0 --port 8000`（开发期 `--reload` 热更）。
- OpenSearch 连接惰性建立，模块导入不触发网络；**开发期安全插件关闭，无 auth**。
- 版本：`app = FastAPI(title="search-service", version="0.3.0")`，自动文档 `/docs`。

### 4.2 数据模型（OpenSearch 索引）
索引模板 `logs-template` 字段类型：

| 字段 | 类型 | 说明 |
|---|---|---|
| `@timestamp` | date | **日志事件时间**（由日志行首 ISO8601 时间戳经 Fluent Bit `Time_Key=event_time` 解析写入，**非采集时刻**）；直方图时间桶与上下文相邻判定均基于它 |
| `level` | keyword | DEBUG/INFO/WARN/ERROR/FATAL |
| `source` | keyword | 服务名（order-service 等） |
| `host` | keyword | 主机（host-01 等） |
| `traceId` | keyword | 链路 ID |
| `tenant_id` | keyword | 租户 |
| `status` | integer | HTTP 状态码 |
| `message` | text(+keyword) | 日志内容 |
| `raw` | text(+keyword) | **原始整行日志**（采集端 `app_log` 解析器 `(?<raw>...)` 整体捕获原样保留）。真实业务日志五花八门，结构化字段未必兜得住，详情抽屉的「原始日志」与上下文日志均取此字段原样回显 |

固定索引：`logs-default` / `logs-acme` / `logs-gp`；`tenant=all` → `logs-*`。

### 4.3 API 契约

| 端点 | 方法 | 请求体（关键字段） | 响应 | 说明 |
|---|---|---|---|---|
| `/query` | POST | `tenant, timeRange{from,to}, filters{}, page, pageSize, sort` | `{total, took_ms, page, pageSize, hits[]}` | DSL 列表检索，`hits` 含 `id` 与 `ts`（锚点时间戳，详情抽屉/上下文用） |
| `/context` | POST | `tenant, time, source?, before=2, after=2` | `{before[], after[]}` | 锚点日志上下文：以 `@timestamp` 为界返回前/后各 N 条相邻日志（同租户；传 `source` 则限同来源，**不传即跨来源时间流**）。`before`/`after` 可为任意条数，详情抽屉用 2 条速览，「更多上下文」框用 20+ 条 |
| `/histogram` | POST | `tenant, timeRange, filters, interval` | `{interval, buckets[{time,count}], by_level[{level,count}]}` | 天/时/分级用 `fixed_interval`、月度用 `calendar_interval`；带 `extended_bounds` 铺满所选范围 |
| `/pql` | POST | `pql, tenant` | `{schema[], datarows[], total}` | 原生 PPL，走 `/_plugins/_pql` |
| `/saved-views` | GET | — | `{items:[], note:"stub..."}` | **stub**，未落库 |
| `/saved-views` | POST | 任意 payload | `{status:"received", note:"stub..."}` | **stub**，未落库 |
| `/health` `/` | GET | — | 健康/服务信息 | 探针与自述 |

> 注：前端经 nginx 访问，baseURL=`/api/search`，故实际浏览器路径为 `/api/search/query` 等。

### 4.4 `filters` 过滤能力（`build_must`）
| 字段 | 语义 | DSL |
|---|---|---|
| `level` | 精确 | `term` |
| `source` | 精确 | `term` |
| `host` | 精确 | `term` |
| `traceId` | 精确 | `term` |
| `status` | 精确 | `term` |
| `statusGte` / `statusLte` | 区间 | `range`（如 `status>=500`） |
| `keyword` | 全文 | `multi_match` on `message,source,host,traceId` |

时间过滤：`timeRange.from/to` → `range @timestamp`。

### 4.5 PQL 安全约束
- 必须以 `source=` 开头，否则自动前缀 `source=logs-<tenant>`；
- 正则拒绝 `drop/delete/update/insert/create/alter/grant` 等写操作关键字；
- 仅查 `logs-*`，禁止越权索引。

---

## 5. 前端实现

### 5.1 API 封装（`src/api/search.js`）
`axios.create({ baseURL: '/api/search' })`，导出 `searchQuery / searchHistogram / searchPql / searchContext`，逐一对应后端端点（saved-views 为后端 stub，前端已移除对应入口）。

### 5.2 检索页（`src/views/SearchView.vue`）
对齐 prototype 布局，真实接后端：

- **页头**：面包屑 + 标题 + 描述 + 保存视图 / 导出（占位）；顶栏 Logo（国网标识，复制自原型）。
- **检索栏**：时间范围快捷（近1h/1d/1w/1m/1y）+ 自定义起止时间 + 主检索框 + 查询/重置 + 常用语法 chips。
  - 占位提示：`输入 PPL 或关键字，例如 source=logs-default | where level='ERROR'`
  - **输入含 `|` → 走 PQL 管道分析**；否则按关键字/过滤检索（直方图+字段侧栏+结果表）。
- **直方图卡**：点击柱体缩小到该时段，可重置；X 轴标签按范围自适应（近1时/1天 `HH:MM`、近1周/1月 `MM-DD`、近1年 `YYYY-MM`）。
- **字段侧栏**：已选/可选字段分组，勾选切换表格列显隐 + 覆盖率计数。
- **结果表**：级别彩色徽章、行点击开详情抽屉、批量勾选 → 导出所选(CSV)/复制语句。
- **详情抽屉**：完整字段 + 复制 traceId +「原始日志」整行 + 前后各 2 条上下文速览；仅上方「上下文日志（前 2 条）」标题右侧一个「更多 ›」按钮，打开**更多上下文日志框**（见第 7 节 2026-11-14 记录）。
- **PQL 模式**：动态结果表（按 schema 自动出列）+ "PQL 分析模式"标签 + 空态提示。

### 5.3 状态（Pinia `stores/app.js`）
全局 `tenant`（default/acme/gp/all），切租户即重新检索；租户经 nginx→后端映射索引。

---

## 6. 部署与运行

- **容器**：`search-service` 用 `log_system-backend` 镜像，`working_dir=/app/services/search-service`，`--reload` 热更；`compose.override.yml` 把本地 `services/search-service` bind mount 进容器，并映射 `8002:8000`（本机直连调试用）。
- **网关路由**（`nginx.conf`）：`location /api/search/ { proxy_pass http://search-service:8000/; }` 去掉前缀。
- **前端托管**：`web` 容器把 `./frontend/dist` 挂到 `/usr/share/nginx/html`，浏览器访问 `http://localhost:8080`（WEB_PORT）。
- **构建**：前端在本机（或解除沙盒的原生进程）`cd frontend && npm run build`，dist 即时生效，无需重启 web。

---

## 7. 当前完成度

### ✅ 已实现
- 后端 5 个端点全部可用并经 curl 实测（`/query` 过滤+分页、`/histogram`、`/pql`、saved-views stub）。
- 多租户隔离（`logs-<tenant>` / `logs-*`）实测通过（`tenant=all` 跨索引聚合 2402 条）。
- 前端 `/search` 垂直切片端到端跑通：真实 Vue 产物 → nginx → search-service → OpenSearch。
- 原型视觉/功能对齐：Logo、设计令牌（青绿 `#03857E`/导航 `#005E53`、表头 `#E6FCF8`）、SPL/PQL 混合检索框、直方图、字段侧栏、结果表、详情抽屉、PQL 面板。
- PQL 真正可用：顶部检索框带 `|` 即走 `/_plugins/_pql`（示例 `| where level='ERROR'` 实测 266 条命中）。

### ⚠️ Stub / 未完成
- `saved-views`（保存视图）：GET/POST 仅返回占位，PG 持久化未做（留待统一数据模型阶段）。
- 顶部"导出结果"（上限 5000 条）：仅 `ElMessage.info` 占位。
- 鉴权：租户目前前端透传，未由 auth 中间件强注入（auth 模块待建）。

### ⚠️ 已知限制
- 构建产物主 JS chunk ≈ 2.3MB（gzip 764KB），体积提示非错误，后续可做路由级 code-splitting。
- OpenSearch 单节点 `yellow`（无副本），开发环境正常。
- 详情抽屉的 2 条速览上下文默认按**同一来源（source）**取相邻日志；「更多上下文」日志框默认跨来源时间流，可在框内切回同来源。
- 生成器回填默认 `BACKFILL_DAYS=7`，故"近一月/近一年"里只有最近 7 天有数据，更早日期为 0 高度柱；回填本身为固定速率（120 条/小时）全天均匀，非真实昼夜流量曲线。

### 📝 日志时间戳改造（2026-11-13）
- **问题**：原 `tools/log-generator/generate.py` 行内无时间戳，`parsers.conf` 的 `app_log` 解析器未设 `Time_Key`，导致 OpenSearch `@timestamp` 实为 Fluent Bit **采集时刻**，缺失"日志自带时间戳"的真实语义，上下文/直方图只能基于采集顺序。
- **改造**：
  - `generate.py` 行格式改为 `[事件时间] [LEVEL] <msg> source=... host=... traceId=... tenant=... status=...`，事件时间 `datetime.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")`（本地时区、偏移无冒号，如 `2026-11-06T15:38:43+0800`）。
  - `parsers.conf` `app_log` 增加 `Time_Key event_time` + `Time_Format %Y-%m-%dT%H:%M:%S%z`，抽取行首时间写入 `@timestamp`。
  - `generate.py` 新增**启动回填**：`BACKFILL_DAYS`（默认 7）/ `BACKFILL_PER_MIN`（默认 2）往回铺满历史，使索引具备真实时间分布，近一周/近一月检索与上下文相邻才有意义；随后进入实时流（1 条/秒）。
  - `fluent-bit.conf` 调大缓冲（`tail` 输入 `Buffer_Chunk_Size 1M`/`Buffer_Max_Size 20M`/`Mem_Buf_Limit 50M`；opensearch 输出 `buffer_size 20M`），避免大批量回填的 bulk 单批 > 默认 512KB 被丢弃。
- **验证**：数据时间跨度 `2026-11-06 → 2026-11-13`（7 天事件时间，非采集时刻）；`/context` 锚点前后返回按 `@timestamp` 真实相邻的同来源日志。
- 注：配置经 `compose.override.yml` bind 挂载，改完 `docker compose up -d --force-recreate fluentbit` 生效；重采集需清空 `app_logs` 卷与 `logs-*` 索引以避免重复/混合时间戳。

### 📝 直方图 X 轴修复（2026-11-14）
- **重复日期 / 重复月份**：根因是 X 轴标签粒度与分桶粒度不匹配，多个桶共用同一标签。
  - 近 1 周：`interval` 由 `6h` 改 `1d`（周视图按天聚合，7 个唯一 `MM-DD`）；
  - 近 1 年：`interval` 由 `1d` 改 `1M`（按月聚合，13 个唯一 `YYYY-MM`）；后端对月度改用 `calendar_interval`（`fixed_interval` 不支持日历单位"月"），前端 `intervalMs` 增加月单位以支持点柱体缩放。
- **空日期不展示**：`date_histogram` 虽设 `min_doc_count:0`，但未设 `extended_bounds` 时只会返回数据实际覆盖的区间。现以 `timeRange.from/to` 设 `extended_bounds`，强制铺满所选范围，无数据日期以 0 高度柱显示。
- **实测**：近 1 月返回 31 桶（含 23 个零值桶，10-14~11-13）；近 1 年返回 13 个月桶（2025-11~2026-11，仅当前月有数据）。

### 📝 更多上下文日志框（2026-11-14）
- 详情抽屉保留前后各 2 条速览；**仅上方**「上下文日志（前 2 条）」标题右侧保留一个「更多 ›」按钮（下方"后 2 条"处不设按钮），打开独立 `el-dialog` 日志框。
- 日志框按时间正序渲染 `[更早 ...] → 当前日志(高亮) → [更晚 ...]`，每行整行取 `raw` 原样回显；当前日志用浅青底 + 左侧主色条 +「当前日志」标签高亮，上下文正常显示（不单独高亮时间戳/级别）。
- **滚动加载**：初始前后各 20 条（`MORE_PAGE`），滚到顶部/底部自动追加一页（上限 `MORE_MAX=200`），顶部/底部显示"已到最早/最新日志"或"已到加载上限"；提供「回到当前日志」按钮。
- **为何复用扩大 `before`/`after` 而非游标**：`/context` 以锚点 `@timestamp` 为界取前/后 N 条，扩大 N 是**幂等**的——实测 20→40 条时原 20 条完全不变、新增条时间更早，因此无需游标、也不会因同毫秒日志重复或遗漏。before 段插在列表顶部，加载后需按新增高度补偿 `scrollTop` 才能保持视觉位置不跳；after 段在底部追加，位置不动。后端 `/context` 无需改动。
- 默认取**全局时间流**（不传 `source`），便于跨服务关联排查；框内「仅同来源」复选框可切回抽屉的同来源语义。

---

## 8. 后续计划（参考）
1. 复用本切片模式，推进 `overview / dashboards / alarms` 三个前端切片。
2. `auth` 登录 + 租户强隔离（替换前端透传）。
3. `saved-views` 落地 PG 持久化。
4. Flink 实时告警（ALM-0402）接入 `flink-jobmanager`。
5. 构建体积优化（code-splitting）。
6. 生成器回填扩到 30 天 + 昼夜流量曲线（消除全天均匀、凌晨也有日志的模拟失真）。
