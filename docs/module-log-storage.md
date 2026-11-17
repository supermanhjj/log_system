# 日志空间管理模块 · 建设思路与实现

> 快照日期：2026-11-16（GMT+8）｜基于当前工作区真实代码整理，行号随代码变动可能偏移
> 入口：`/settings/storage`（平台管理 → 日志空间管理，四个 Tab）
> 关联文档：[平台管理模块](module-platform-admin.md) · [用户与角色模块（system-service）](service-system.md) · [运行总览](module-overview.md) · [服务清单](service-inventory.md)

---

## 0. 本期进展快照

| 能力 | 状态 | 落点 |
|---|---|---|
| 容量概览（总容量 / 日志已用 / 可用 / 占用率） | ✅ 真实取数，非模拟值 | `services/system-service/app/storage.py` |
| 容量趋势（近 7 / 30 / 90 天） | ✅ 实测快照优先，不足时按真实日志分布推算 | `build_trend()` |
| 存储配置（保留天数 / 滚动阈值 / 冷热策略 / 配额） | ✅ 落 PG，保留天数与系统参数联动 | `PUT /storage/config` |
| 索引管理（体积 / 文档数 / 年龄 / 状态 / 判定依据） | ✅ `_cat/indices` 实时 | `GET /storage/indexes` |
| 手动清理（按保留期删过期文档 / 删冷索引） | ✅ `delete_by_query`；已用探针文档验证真实删除 | `POST /storage/indexes/{name}/clean` |
| 租户配额（配额 / 已用 / 文档数 / 使用率） | ✅ 落 PG，已用来自真实索引体积 | `GET/PUT /storage/quotas` |
| 运行总览「存储容量概览」区块 | ✅ 改为同一套真实接口 | `OverviewView.vue` → `getCapacity()` |
| 独立存储服务 / 归档到对象存储 | ❌ 未做 | 见第 7 节 |

---

## 1. 模块定位与数据口径

这个模块要回答三个问题：**日志现在占多少、还能写多少、哪些该清掉**。三个问题对应四张卡、一张趋势图、一张索引表。

先前的实现是前端写死的一组数（`total 5000 / used 3280 / 7 个点`），改成本次真实取数后，最容易出错的地方不是接口，而是**口径**——四个数字必须自洽，否则用户一眼就发现对不上。最终定下三条：

| 规则 | 说明 |
|---|---|
| **① 四项自洽** | `已用 + 可用 = 总容量`，占用率 = 已用 / 总容量。绝不做「已用按租户、可用按全局」这种混源相加 |
| **② 有配额按配额，无配额按物理卷** | 配置了 `quotaGB > 0`：总容量 = 配额，可用 = 配额 − 已用；未配置：可用取 OpenSearch 节点所在卷的真实剩余空间，总容量 = 已用 + 可用 |
| **③ 已用只算日志** | 已用 = `logs-*` 索引实际 `store.size` 之和，不把镜像、系统索引算进去；物理卷总量单独作为附注展示 |

第 ② 条让「配额」从登记项变成真正驱动页面的参数；第 ③ 条避免把磁盘上其他数据算成"日志占用"。

单租户视角下有一个天然的不对称：磁盘是各租户共用的，「已用」按租户过滤、「可用」只能是全局剩余。这一点在页面附注里写清楚了（`capacity.note`），不藏。

---

## 2. 已落地能力

### 2.1 容量概览

- 四张卡：总容量 / 日志已用 / 可用 / 占用率（含进度条）。
- 附注行：索引数、文档数、物理卷总量与可用、当前口径（配额 or 物理卷）、更新时间。
- 趋势图：ECharts 折线 + 面积，单位自适应（< 1 GB 用 MB，避免「0.02 GB」这种读不出量级的显示）。
- 范围切换：平台管理员可在「全部租户 / 本租户」间切换；其他角色锁定本租户（后端强校验，见第 5 节）。

### 2.2 存储配置

| 项 | 取值范围 | 真实作用 |
|---|---|---|
| 保留天数 | 1–3650 | 决定「清理过期」的删除边界；**日志生命周期的唯一设置项**，系统设置里不再重复一份 |
| 滚动阈值 | 50–95（%） | 单索引体积超过配额的该比例 → 索引状态判为告警 |
| 热/冷分级 | hot7d / hot30d / allhot | 超过热窗口的索引标记为冷数据，才允许「删除索引」 |
| 日志配额 | ≥ 0（GB，0 = 不限制） | 决定容量概览的口径与可用容量 |

### 2.3 索引管理

表格列：索引名 / 大小 / 文档数 / 已存在天数 / 状态 / 判定依据 / 操作。

状态判据（`index_state()`，**按顺序短路**）：

| 顺序 | 条件 | 状态 | 理由文案 |
|---|---|---|---|
| 1 | 年龄 > 保留天数 | 告警 | 超出保留期 N 天，建议清理 |
| 2 | 体积 > 配额 × 滚动阈值 | 告警 | 超过配额比例，建议滚动新索引 |
| 3 | 年龄 > 热窗口 | 冷数据 | 已转冷存储 |
| 4 | health = red | 告警 | 存在未分配的主分片 |
| 5 | health = yellow 且多节点 | 告警 | 副本未分配 |
| 6 | 其余 | 健康 | 单节点下 yellow 属预期，文案里说明 |

第 6 条是本次调过的一处：最初 health ≠ green 一律告警，结果单节点开发环境里**所有索引全红标告警**——把常态当故障，告警就失去意义了。

### 2.4 清理动作

| 模式 | 语义 | 安全约束 |
|---|---|---|
| `purge`（默认，页面按钮「清理过期」） | `delete_by_query` 删除早于保留天数的文档，索引保留 | 需 `storage:clean`；索引名必须匹配 `logs-*`；删除前二次确认并展示保留期 |
| `drop`（页面按钮「删除索引」，仅冷索引可见） | 删除整个索引 | 需 `storage:clean`；**只允许对状态为冷数据的索引执行**，活动索引一律 400 拒绝 |

删除 0 条时文案是「没有早于保留期（N 天）的日志，无需清理」，不假装成功。

### 2.5 租户配额

列出每个租户的配额、真实已用、文档数、使用率。配额 0 显示「不限制」而不是 0%。非平台管理员只能看到、也只能改本租户。

---

## 3. 接口清单

nginx 前缀 `/api/system/storage/*` → `system-service`。所有接口需 `Authorization: Bearer`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/storage/capacity?range=近 7 天&tenant=all` | `storage:view` **或** `overview:view` | 容量四卡 + 趋势；`range` 支持中文标签或纯天数 |
| GET | `/storage/config` | `storage:view` | 配置项 + 冷热策略选项 |
| PUT | `/storage/config` | `storage:update` | 局部更新；保留天数写回系统参数 |
| GET | `/storage/indexes?tenant=` | `storage:view` | 索引列表 + 状态 + 判定依据 |
| POST | `/storage/indexes/{name}/clean` | `storage:clean` | body `{mode: purge\|drop}` |
| GET | `/storage/quotas` | `storage:view` | 各租户配额与真实占用 |
| PUT | `/storage/quotas/{tenant}` | `storage:update` | body `{quotaGB}`，0 = 不限制 |

容量接口放行 `overview:view` 是有意的：运行总览的「存储容量概览」对所有能看概览的角色都应展示，而日志空间管理页本身仍要求 `storage:view`。

---

## 4. 真实数据来源与算式

| 数据 | 来源 | 实现 |
|---|---|---|
| 索引体积 / 文档数 / health / 创建时间 | OpenSearch `_cat/indices?bytes=b&h=index,health,status,docs.count,store.size,creation.date` | `fetch_indices()` |
| 物理卷总量 / 可用 | OpenSearch `_cluster/stats` 的 `nodes.fs.*` 与 `nodes.count` | `cluster_fs()` |
| 趋势（有 ≥2 个快照时） | PostgreSQL `storage_snapshots`（每日每 scope 一条，查询容量时 upsert） | `record_snapshot()` |
| 趋势（快照不足时） | OpenSearch `date_histogram`（按天 `@timestamp`）+ 累计占比 × 当前体积 | `day_histogram()` + `build_trend()` |
| 配置 / 配额 | PostgreSQL `storage_config`（单行）/ `tenant_quotas` | `read_config()` |

趋势推算的算式（可复核）：设范围内某天累计文档数为 `D(t)`、全量文档 `D`、当前体积 `S`，则该天估算体积 `V(t) = S × D(t) / D`。文档分布是真实的，所以曲线形状跟随真实写入节奏；它仍是估算，因此响应里带 `trend.source = 'estimated'`，页面显示「按真实日志量推算」。等每日快照攒到 2 个点，自动切换为 `source = 'snapshot'`（显示「实测快照」）。

---

## 5. 权限与安全边界

权限资源新增 `storage`，动作 `view / update / clean`（`db.py` 的 `PERMISSION_TREE`）：

| 角色 | storage 权限 |
|---|---|
| 平台管理员 | view / update / clean |
| 运维值班 | view |
| 只读用户 | 无（但可经 `overview:view` 读容量概览） |
| 审计员 | 无 |

租户隔离（`scope_for()`）：非平台管理员请求 `tenant=all` 会被强制改写为本租户，配额列表也只剩本租户，改他租户配额直接 403。这条是必要的——否则一个 `storage:view` 就能通过 `?tenant=all` 看到全部租户的容量与索引。

其他防护：索引名正则 `^logs-[A-Za-z0-9_-]+$`（挡住对 `.kibana_1` 等系统索引的操作）；OpenSearch 异常统一转 503 并带简短原因（不吐堆栈）。

---

## 6. 验证记录（2026-11-16 实测）

对手工签发的 Token 与无头浏览器各跑一轮，控制台零报错。

- 容量（全部租户）：日志已用 `54.30 MB`、物理卷 `58.37 GB`、可用 `36.94 GB`、占用率 `0.1%`、索引 3 个、文档 10.1 万条 —— 与 `_cat/indices` 逐项对得上。
- 索引：3 个索引均为 `健康`，判定依据写明「单节点部署，副本未分配（health=yellow，属预期）」；年龄 3 天。
- **清理真删**：向 `logs-gp` 写入 1 条 `@timestamp=2026-09-01` 的探针文档（count 34077 → 34078），`purge` 返回 `deleted: 1`，count 回到 34077。无过期数据时返回「无需清理」而非假成功。
- 状态分支单测：`age 0/green → healthy`、`age 0/red → warning`、`age 10 → cold`、`age 40 → warning + expired`、多节点 yellow → warning。
- 配置：`retainDays=45, quotaGB=2000` 保存后，容量口径切到配额（总容量 1.95 TB = 2000 GB）；`system_params` 中已无 `log_retain_days`（计数 0），保留天数只此一处维护。
- 配额：`default` 改 500 → 已用 18.2 MB、使用率 0%；改 0 → 显示「不限制」；负值 400；不存在的租户 404。
- 权限：运维值班 capacity 200 / 配置与清理 403；只读与审计员 capacity 走 `overview:view` 放行但索引列表 403；非管理员 `?tenant=all` 被锁回本租户（返回 `scope=default`，索引仅 `logs-default`）。
- 界面：四 Tab 渲染正常；索引表 3 行、状态为「健康」；点「清理过期」弹出确认框（含保留期与不可恢复提示），确认后弹出真实结果提示；切到其他 Tab 再切回，趋势图正常重绘（图表实例随 DOM 重建重绑定）。
- 运行总览：「存储容量概览」显示 `36.99 GB / 54.30 MB / 36.94 GB / 0.1%`，脚注「全部租户索引 3 个 · 文档 101,311 条 · 数据来自 OpenSearch 实时统计」。

---

## 7. 已知边界与待办

| 级别 | 项 | 说明 |
|---|---|---|
| **P1** | 趋势前 2 天仍是推算 | 快照表从本次上线才开始记，`source=estimated` 阶段曲线是按日志文档分布反推的（页面已标注）。运行满 2 天后自动变实测 |
| **P1** | 清理无 dry-run | 点确认即执行，只回传实际删除条数。理想是先「试算将删除 N 条」，确认后再删 |
| **P1** | 无定时清理 | 保留期只在手动点「清理过期」时生效，未接 OpenSearch ISM 策略或定时任务 |
| **P2** | 冷索引删除只在有冷索引时可验证 | 冷数据门槛是「年龄 > 热窗口（≥7 天）」，开发环境索引只有 3 天，`drop` 分支用逻辑单测覆盖，未走真实删除 |
| **P2** | 未接对象存储归档 | 原型提「冷数据自动归档」，当前只有冷标记，没有迁移与回读 |
| **P2** | 配额不阻断写入 | 超配额只体现在占用率与告警色，不会拒绝写入或滚动新索引 |
| **P2** | 索引粒度为天/租户维度 | 当前 `logs-<tenant>` 为单索引，没有时间分片，所以「删除整个冷索引」在现网数据上暂时用不到；上时间分片后才真正有意义 |
| **P2** | 快照表会随时间增长 | 每天每 scope 一行，规模可忽略，但未做清理 |

---

## 8. 相关文件索引

| 文件 | 作用 |
|---|---|
| `services/system-service/app/storage.py` | 全部 `/storage` 接口、OpenSearch 取数、趋势与状态判定 |
| `services/system-service/app/db.py` | `storage_config` / `tenant_quotas` / `storage_snapshots` 建表与种子；`storage` 权限资源 |
| `services/system-service/app/audit.py` | 审计写入（管理面与存储共用） |
| `frontend/src/views/LogStorage.vue` | 四个 Tab、趋势图、清理确认与结果反馈 |
| `frontend/src/api/storage.js` | 7 个接口封装 |
| `frontend/src/views/OverviewView.vue` | 运行总览的「存储容量概览」区块 |
