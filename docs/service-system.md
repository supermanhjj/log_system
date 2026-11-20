# 用户、角色与日志空间（system-service）开发记录

> 快照日期：2026-11-19（GMT+8）｜基于当前工作区真实代码整理，行号随代码变动可能偏移
> 模块范围：**用户**（增删改查 / 启停 / 重置密码 / 批量 / 导出）、**角色**（权限矩阵）、**日志空间**（容量 / 配置 / 索引 / 配额），以及依附其上的**系统参数**与**审计日志**
> 关联文档：[平台管理模块](module-platform-admin.md) · [登录与鉴权模块（auth-service）](service-auth.md) · [日志空间管理](module-log-storage.md) · [服务清单](service-inventory.md) · [技术栈选型](tech-stack-selection.md)
> 服务 / 端口：`log_system-system-service-1`，宿主机 `8007` → 容器 `8000`；网关前缀 `/api/system/`（`deploy/docker/nginx/nginx.conf:27`）

---

## 0. 本期进展快照

| 能力 | 状态 | 落点 |
|---|---|---|
| 用户管理（筛选 / CRUD / 启停 / 重置密码 / 批量 / 导出 / 分页） | ✅ 真实读写 PostgreSQL | `app/main.py:215-475` |
| 角色权限（成员数 / 矩阵编辑 / 实时生效） | ✅ | `app/main.py:501-568` |
| 系统参数（登记 + 类型校验） | ✅ `search_p95_target` 已驱动运行总览 P95 达标判定；`log_retain_days` 已下线 | `app/main.py`、`dashboard-service/app/main.py` |
| 审计日志（筛选 / 分页 / 导出） | ✅ | `app/main.py:655-722` |
| RBAC 六表 + 日志空间三表建表与种子 | ✅ 启动时幂等执行 | `app/db.py` 的 `init_db()` |
| 日志空间管理（容量 / 配置 / 索引 / 配额 / 清理） | ✅ 总容量来自配置，已用来自 OpenSearch 真实取数，配额之和受总量约束；**容量口径按角色**（管理员平台口径 / 租户配额口径） | `app/storage.py` |
| 鉴权闸门 `require(resource, action)` | ✅ 每请求现查权限 | `app/security.py:94-119` |
| 删除用户 / 角色增删 | ❌ 未做，用停用代替 | 见第 10 节 |
| 租户管理（CRUD / 配额 / 启停 / 删除保护） | ✅ 已落地，与日志空间共用配额数据 | `app/main.py` `/tenants` 路由 |

代码规模：`main.py` 约 730 行、`db.py` 约 380 行、`security.py` 约 120 行、`storage.py` 约 480 行、`audit.py` 40 行。**共 24 个路由**（22 个业务 + `/health`、`/`），其中 7 个属 `/storage`。库内实测：`tenants 3 / roles 4 / permissions 48 / params 5 / storage_config 1 / tenant_quotas 3`。

---

## 1. 模块定位与开发边界

**一句话**：本服务是平台管理的数据面——**九张表的唯一建表方与唯一写入方**（RBAC 六表 + 日志空间三表），管人、管角色、管参数、管审计、管日志空间。

| | 内容 |
|---|---|
| 做 | 建表 + 种子、用户 CRUD/启停/重置/批量/导出、角色权限矩阵读写、系统参数读写、审计查询与导出、日志空间容量/配置/索引/两级配额、页面元信息 `/meta` |
| 不做 | 登录与 JWT 签发（`auth-service`）、各业务模块（检索/告警/接入/Agent/仪表盘/AI）的取数逻辑 |
| 表所有权 | RBAC 六表（`tenants` / `roles` / `permissions` / `users` / `audit_log` / `system_params`）+ 日志空间三表（`storage_config` / `tenant_quotas` / `storage_snapshots`）——**只在本服务 `app/db.py` 建**，`auth-service` 只读不建 |
| 鉴权方式 | 自行校验 Token + 每个接口按「资源:操作」现查 `permissions`，改权限后实时生效 |

`main.py` 按「通用 → 元信息 → 用户 → 角色 → 参数 → 审计」分段注释，**没有 service 层**：每个接口就是「校验 → 一条 SQL → 写审计 → 组装响应」。这是刻意保持直白——管理面逻辑不值得为分层再套一层，读代码的人能一眼看到 SQL。

---

## 2. 数据层：六表与种子（实现方式）

### 2.1 建表

全部在 `db.py:146-284` 的 `init_db()` 里 `CREATE TABLE IF NOT EXISTS`，由启动钩子调用（`main.py:725-727`）。**建表失败不阻塞进程**：捕获异常后打印 `init_db skipped: ...` 继续启动，避免 PG 抖动导致整个服务不可用。

| 表 | 主要字段 | 约束 / 索引 | 说明 |
|---|---|---|---|
| `tenants` | `code` / `name` / `enabled` | `code` UNIQUE | 编码与 OpenSearch 索引对齐：`default` 总部生产、`acme` 省侧运维、`gp` 测试租户 |
| `roles` | `key` / `name` / `scope_desc` / `is_builtin` / `sort_order` | `key` 主键 | `key` 即 Token 里的 `role` 声明 |
| `permissions` | `role_key` / `resource` / `action` | `UNIQUE(role_key, resource, action)`、外键级联删除 | 权限点粒度「资源:操作」 |
| `users` | `tenant_id` / `account` / `name` / `password_hash` / `role_key` / `phone` / `email` / `enabled` / `last_login_at` / `last_login_ip` / `is_builtin` | `account` UNIQUE、`idx_users_tenant` | 账号规则：小写字母开头，可含小写字母/数字/下划线，3-32 位 |
| `audit_log` | `tenant_id` / `user_id` / `user_name` / `account` / `action` / `target` / `detail`(jsonb) / `ip` / `result` | `idx_audit_created`（`created_at DESC`） | 追加写；姓名与账号**冗余存储**，用户改名后历史仍可读 |
| `system_params` | `pkey` / `pvalue` / `ptype`(int/enum/bool) / `label` / `note` / `options`(jsonb) | `pkey` 主键 | 保存时按 `ptype` 校验类型与枚举范围 |
| `storage_config` | `id`(恒为 1) / `retain_days` / `roll_threshold` / `hot_cold_strategy` / `quota_gb` | `id` 主键 + `CHECK (id = 1)` | 单行表，多行无意义；`quota_gb = 0` 表示不限制 |
| `tenant_quotas` | `tenant_code` / `quota_gb` | `tenant_code` 主键，外键指向 `tenants(code)` | 每租户日志配额，0 = 不设上限；之和受 `storage_config.quota_gb`（平台总容量）约束 |
| `storage_snapshots` | `snap_date` / `scope` / `used_bytes` / `doc_count` / `index_count` | 主键 `(snap_date, scope)` | 每日容量实测快照，供趋势图从「推算」转「实测」 |

### 2.2 种子策略

全部 `ON CONFLICT DO NOTHING`；用户**仅在账号不存在时创建**（`db.py:260-263`）。这条看似不起眼的规则决定了运维行为：`docker compose restart` **不会**把已改的密码刷回默认、**不会**把停用的账号重新启用、**不会**改名。代价是 admin 密码忘了只能走其他管理员重置或改库。

| 账号 | 姓名 | 角色 | 租户 | 手机号 | 说明 |
|---|---|---|---|---|---|
| `admin` | 系统管理员 | 平台管理员 | 总部生产 | — | **内置账号**，口令取 `AUTH_SEED_ADMIN_PASSWORD` |
| `ops_chenli` | 陈立 | 平台管理员 | 总部生产 | 13800000001 | 其余账号口令取 `AUTH_SEED_DEMO_PASSWORD` |
| `ops_zhouhang` | 周航 | 运维值班 | 总部生产 | 13800000002 | |
| `ops_gaoyuan` | 高远 | 只读用户 | 省侧运维 | 13800000003 | 跨租户样本，验证越权拦截 |
| `audit_linjing` | 林静 | 审计员 | 总部生产 | 13800000004 | 只有审计权限，验证菜单过滤 |
| `ops_zhaolei` | 赵磊 | 运维值班 | 总部生产 | 13800000005 | **初始即停用**，验证停用后登录被拒 |

参数种子 5 项：`search_limit`(int 5000)、`search_p95_target`(int 800，检索耗时目标 ms)、`timezone`(enum)、`session_timeout_min`(int 30)、`audit_enabled`(bool)。

**参数归属的一次收敛**：`log_retain_days` 已从本表下线（写入 `RETIRED_PARAMS`，`init_db()` 每次启动幂等 `DELETE` 残留行）。日志保留天数只有一个权威来源——`storage_config.retain_days`，由「日志空间管理 · 存储配置」维护，避免同一语义在两个页面各存一份、显示不一致。

---

## 3. 用户模块

### 3.1 开发内容

| 功能点 | 实现细节 |
|---|---|
| 列表与筛选 | 关键词（姓名 / 账号）、角色、租户、启用状态四个条件 + 分页；`pageSize` 上限 200（`main.py:222`） |
| 新建 | 校验账号规则 / 角色存在 / 租户存在 / 账号唯一（409）；密码留空则生成临时密码 |
| 临时密码 | `Tmp` + 1 位数字 + 6 位 hex（`main.py:78-80`），共 10 位，恰好满足「字母 + 数字」策略；**只在创建/重置的响应里出现一次** |
| 编辑 | 部分更新，只改传入字段；空姓名 400；无字段可改 400 |
| 单条启停 | 独立接口，与编辑分开便于表格开关直接调用 |
| 重置密码 | 生成新临时密码并返回一次，落审计但**审计里不含密码本身** |
| 批量启停 | 自动跳过内置账号与当前登录账号，并把跳过原因回传给前端展示 |
| 批量重置密码 | 逐条生成临时密码，返回 `[{id, account, name, tempPassword}]` |
| 导出 CSV | 带 UTF-8 BOM，Excel 直开不乱码；导出动作本身也留痕 |
| 权限控制 | 全部接口经 `require("user", ...)`；列表/导出对**非平台管理员强制限定本租户**（`main.py:205-208`） |

### 3.2 实现方式：筛选口径只写一份

列表与导出**共用 `user_filter()`**（`main.py:190-212`）。这是刻意的：`WHERE` 条件写两处，早晚会出现「列表看到 6 条、导出 8 条」的经典错位。返回 `(cond_sql, params)` 后，列表再拼 `LIMIT/OFFSET`，导出直接全量。

租户隔离的写法值得单独看：

```python
if tenant:
    where.append("t.code = %s"); params.append(tenant)
elif user["role_key"] != "platform_admin":
    # 非平台管理员只看自己租户，防跨租户越权
    where.append("t.code = %s"); params.append(user["tenant_code"])
```

即「显式指定了租户就按指定的来，没指定且你不是平台管理员就锁死本租户」。

### 3.3 保护性约束（防止把自己锁在门外）

这些不是通用校验，而是业务护栏：

| 约束 | 触发接口 | 报错 |
|---|---|---|
| 不能停用当前登录账号 | 编辑、单条停用、批量停用 | 不能停用当前登录账号 |
| 内置账号不可停用 | 单条停用、批量停用 | 内置账号不可停用 |
| 内置账号角色不可改 | 编辑 | 内置管理员的角色不可修改 |
| 内置账号租户不可改 | 编辑 | 内置管理员的租户不可修改 |
| 不能改自己的角色 | 编辑 | 不能修改自己的角色，请让其他管理员操作 |
| 角色 / 租户必须存在 | 新建、编辑 | 角色不存在 / 租户不存在 |
| 账号唯一 | 新建 | 该账号已存在，请更换（409） |

### 3.4 子问题与解法

**批量停用把"跳过"说清楚。** 初版批量接口静默跳过内置账号，前端只显示「已停用 2 个用户」，管理员不知道第 3 个为什么没停。现在返回 `{updated, skipped: ["陈立（内置账号不可停用）", ...]}`，前端把 `skipped` 直接展示出来（`main.py:329-347`）。

**临时密码不能落审计。** 重置密码时审计记 `target` 与操作人，但 `detail` 里不放 `tempPassword`——否则审计表本身成了明文口令库，有 `audit:view` 的审计员就能看到所有人的临时密码。

---

## 4. 角色模块

### 4.1 开发内容：13 资源 / 28 权限点

权限树定义在 `db.py` 的 `PERMISSION_TREE` + `ALL_PERMS`，动作标签统一走 `ACTION_LABELS`：`view` 查看 / `create` 新建 / `update` 修改 / `delete` 删除 / `export` 导出 / `clean` 清理。

| 资源 | 动作 | 平台管理员 | 运维值班 | 只读用户 | 审计员 |
|---|---|:--:|:--:|:--:|:--:|
| overview 运行总览 | view | ✓ | ✓ | ✓ | ✓ |
| search 日志检索 | view / export | ✓ | ✓/✓ | ✓ | ✓ |
| dashboards 仪表盘 | view / update | ✓ | ✓ | ✓ | |
| alarms 告警中心 | view / update | ✓ | ✓/✓ | ✓ | |
| data 数据接入 | view / update | ✓ | ✓ | ✓ | |
| agents Agent 管理 | view / update | ✓ | ✓ | ✓ | |
| ai 智能管理 | view / update | ✓ | ✓ | | |
| settings 平台管理 | view | ✓ | | | |
| user 用户管理 | view/create/update/delete/export | ✓ | | | |
| role 角色权限 | view / update | ✓ | | | |
| param 系统参数 | view / update | ✓ | | | |
| audit 审计日志 | view / export | ✓ | | | ✓/✓ |
| storage 日志空间 | view / update / clean | ✓ | ✓ | | |

合计：平台管理员 **28**、运维值班 **10**、只读用户 **6**、审计员 **4**，落库 48 行。`storage` 只给运维值班 `view`（值班需要知道磁盘快满），清理与改配置仅平台管理员。

两个设计取舍：**审计员是原型角色列表里的第四类**，与文档已确认的三级模型并存；**角色数据驱动但无管理入口**——`roles` 表增删角色不需要改代码，但增了还得补前端菜单与路由，所以暂未开放 CRUD。

### 4.2 实现方式：整体替换 + 实时生效

`PUT /roles/{key}` 的权限更新是**先删后插整体替换**（`main.py:551-560`）：

```python
pg_exec("DELETE FROM permissions WHERE role_key=%s", (key,))
for perm in sorted(set(req.permissions)):
    resource, action = perm.split(":", 1)
    pg_exec("INSERT INTO permissions ... ON CONFLICT DO NOTHING", ...)
```

语义清晰（前端把整棵矩阵提交上来，后端不用 diff），但**不是事务**——autocommit 连接下若中途失败，该角色会短暂处于权限不全状态。当前并发极低未处理，已记入待办。

「实时生效」不是靠推送，而是靠**每次请求现查 `permissions`**：用户改完角色权限，该角色下所有用户的下一个请求就是新权限，**无需重新登录**。这也是选「权限不写进 Token」的直接收益。

成员数用子查询实时统计（`main.py:504`），不做冗余计数，避免「改了用户角色但成员数没跟上」。

### 4.3 子问题与解法

**① 权限矩阵弹窗超出屏幕。** 12 组资源单列堆叠，1440×900 下需要滚动才能看到「审计日志」组，保存按钮被挤出视口。改为两列 grid（`.perm-grid`）并收紧分组间距，一屏容纳 12 组。

**② 审计员进不了平台管理页（真实权限设计漏洞）。** 平台管理入口原按 `settings:view` 判定，但审计员只有 `audit:view`、没有 `settings:view`，于是「有审计权限却看不到审计日志页」。这是设计层面的 bug，不是实现 bug。
解法：路由增加 `meta.permAny`，`/settings/users` 在 `settings/user/role/param/audit` **任一** `view` 下放行；`App.vue` 菜单项由 `resource` 改为 `resources` 数组。实测审计员现在能进页面，且页内只剩「审计日志」一个 Tab。

---

## 5. 鉴权闸门

`require(resource, action)`（`security.py:94-106`）是全服务唯一的权限判定入口，作为 FastAPI 依赖注入到每个业务接口：

```
解析 Authorization → current_user() 按 sub 现查库（不存在/停用 → 401）
  → perms_for_role(role_key) 现查权限点
    → 缺 resource:action → 403「当前角色「只读用户」缺少权限 user:view」
```

403 文案**如实带出缺哪个权限点**，排障时不用翻代码猜。

---

## 6. 系统参数与审计日志（依附模块）

**系统参数**（`main.py` 的 `/params`）：保存时按 `ptype` 校验——`int` 必须整数、`enum` 必须命中 `options`、`bool` 必须 `true/false`；未知 `pkey` 直接 400 并列出参数名。**边界要说清**：`search_p95_target` 已真实驱动运行总览（`dashboard-service` 每 10 秒读一次，见 `module-overview.md`）；`search_limit` / `audit_enabled` / `session_timeout_min` / `timezone` 仍是「登记 + 校验」层能力，尚未反向驱动检索网关上限、审计开关与 JWT 过期时间。

**审计日志**（`main.py:655-722`）：`write_audit()` 是全服务唯一入口，覆盖新建用户、修改用户、启停、重置密码、批量启停、批量重置、修改角色权限、修改系统参数、导出用户、导出审计 10 类管理动作。

- 失败**不阻断业务**：捕获所有异常只打日志，避免审计表故障拖垮管理功能；代价是审计可能静默缺失。
- 日期右端按「当天结束」处理：`created_at < (%s::date + interval '1 day')`（`main.py:649-651`），否则用户选同一天会查出 0 条。
- `/audit` 同时返回去重后的 `actions` 列表，前端筛选下拉不用硬编码。
- 导出上限 20000 行（`main.py:707`），带 BOM，导出动作本身也留痕。

---

## 7. 接口清单（24 个路由）

业务接口全部要求 `Authorization: Bearer`，权限列即所需权限点。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/health`、`/` | 公开 | 存活检查与服务自述 |
| GET | `/meta` | 登录即可 | 角色、租户、权限树、动作标签、当前用户与权限、账号/密码规则 |
| GET | `/users` | `user:view` | 筛选 + 分页；非平台管理员强制本租户 |
| GET | `/users/export.csv` | `user:export` | 与列表共用筛选口径 |
| POST | `/users` | `user:create` | 返回 `initialPassword`（仅此一次） |
| PUT | `/users/{uid}` | `user:update` | 部分更新，受第 3.3 节约束 |
| PUT | `/users/{uid}/enabled` | `user:update` | 单条启停 |
| POST | `/users/{uid}/reset-password` | `user:update` | 返回 `tempPassword`（仅此一次） |
| POST | `/users/batch/enabled` | `user:update` | 返回 `{updated, skipped}` |
| POST | `/users/batch/reset-password` | `user:update` | 返回逐条临时密码 |
| GET | `/roles` | `role:view` | 角色 + 实时成员数 + 权限树 |
| PUT | `/roles/{key}` | `role:update` | 改名/描述/整体替换权限 |
| GET | `/params` | `param:view` | 参数及类型、可选值 |
| PUT | `/params` | `param:update` | 按 `ptype` 校验 |
| GET | `/audit` | `audit:view` | 筛选 + 分页 + 去重动作列表 |
| GET | `/audit/export.csv` | `audit:export` | 上限 20000 行 |
| GET | `/storage/capacity` | `storage:view` 或 `overview:view` | 容量四卡 + 趋势；`range` 支持「近 7 天」或纯天数；按角色锁定 `scope`——管理员可看全部（平台总容量 `capBasis=platform`），非管理员强制本租户（`capBasis=tenant`），返回 `capLabel`/`unlimited`/`totalGB`（当前口径总量）/`totalSourceGB`（平台配置值） |
| GET | `/storage/config` | `storage:view` | 存储配置 + 冷热策略选项 |
| PUT | `/storage/config` | `storage:update` | 局部更新；`totalGB` 为平台总容量（低于已分配配额时 400），`retainDays` 为日志生命周期唯一来源 |
| GET | `/storage/indexes` | `storage:view` | 索引真实体积/文档数/health/年龄 + 状态与判定依据 |
| POST | `/storage/indexes/{name}/clean` | `storage:clean` | `purge` 删过期文档（默认）/ `drop` 删冷索引（仅冷索引允许） |
| GET | `/storage/quotas` | `storage:view` | 各租户配额与真实占用，含总容量/已分配/剩余汇总；非管理员仅本租户 |
| PUT | `/storage/quotas/{tenant}` | `storage:update` | `0` = 不限制；非管理员只能改本租户 |

---

## 8. 配置与部署

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SYSTEM_PG_DSN` | 由 `POSTGRES_*` 拼出 | 需要单独指定时覆盖 |
| `JWT_SECRET` / `JWT_ALGORITHM` | `dev-only-not-secret` / `HS256` | **必须与 auth-service 一致**，否则解析不了它签发的 Token |
| `AUTH_SEED_ADMIN_PASSWORD` | `Admin@123` | 仅 `admin` 内置账号首次创建时使用 |
| `AUTH_SEED_DEMO_PASSWORD` | `Ops@12345` | 其余 5 个种子账号首次创建时使用 |

- 容器：`image: log_system-backend`，`depends_on: [postgres]`，`env_file: .env`；开发期由 `compose.override.yml` 暴露 `8007:8000` 并开 `--reload`。
- **首次部署本服务必须先于 `auth-service` 起来**（建表 + 种子）。
- 忘记 admin 密码：重启不会重置（种子只在账号不存在时插入）。用其他平台管理员重置，或改库里的 `password_hash`。
- 自检：`curl localhost:8080/api/system/health` → `200`；无 Token 访问 `/api/system/users` → `401 未登录，请先登录`。

---

## 9. 验证记录（2026-11-16 实测）

- 健康检查 `200`；库内实测 `tenants 3 / roles 4 / permissions 44 / params 5 / users 7`（7 = 6 个种子 + 1 个手工新建），审计 24 条。
- 权限拦截：只读用户访问 `/users`、`/audit` 均 403 且文案带出缺失权限点；审计员可访问 `/audit`、不可访问 `/users`。
- 用户管理：新建（临时密码弹窗仅一次）、编辑、单条停用/启用、行内重置密码、批量停用（自动跳过内置与当前账号并回显原因）、导出 CSV（Excel 直开不乱码）。
- 参数：`search_limit` 传非整数 → 400「「单次检索上限」必须是整数」；传非法枚举值 → 400。
- 角色：改运维值班权限后该角色用户无需重新登录即生效；裁剪 `platform_admin` → 400；移除自己角色的 `role:update` → 400。
- 保护项：停用自己 → 400；停用 `admin` → 400。
- 前端四 Tab 与顶栏用户展示正常，回归其余页面控制台零报错。

**2026-11-18~19 补测**：

- 容量口径按角色：以 admin / 省侧运维不同身份调 `/storage/capacity`，管理员看全部 = 40 GB 平台容量、切 acme 或普通账号 = 5 GB 配额口径，非管理员 `tenant=all` 被锁回本租户；越界改配额返回 400 且未落库，审计留痕完整（`audit_log` id 76–81）。
- 租户配额端到端生效：库内 `default 20 / acme 5 / gp 5`（Super 页面设置）与 `/quotas`、`/config` 接口返回逐项一致；容器重启后数据仍在 PG。
- 租户管理页 note 文案改为「有用户或有日志数据的租户不允许删除；配额与「日志空间」共用同一份数据。」（后端接口返回，前端 `{{ note }}` 渲染）。
- `npm run build` / `py_compile` 均通过，前端产物含新口径字段，nginx 首页 200。

---

## 10. 已知边界与待办

| 级别 | 项 | 说明 |
|---|---|---|
| ~~P0~~ | ~~只有本服务与 auth-service 校验 Token~~ 已解决（2026-11-18） | `search` / `dashboard` / `alarm` / `ingest` 均已引入 `authz.py` 按 JWT 强制解析租户；剩余 `agent` / `ai` 的主机/模型配置尚未租户化 |
| **P1** | 系统参数多数未驱动系统 | `search_p95_target` 已驱动运行总览 P95 达标判定；日志保留天数已下线（唯一来源为日志空间存储配置）；`search_limit` / `audit_enabled` 仍是登记层能力（见第 6 节） |
| **P1** | 日志空间无定时清理 | 保留期只在手动点「清理过期」时生效，未接 OpenSearch ISM 或定时任务（详见 `module-log-storage.md`） |
| **P1** | 无删除用户接口 | 与原型一致用停用代替；`user:delete` 权限点已定义但无操作 |
| **P1** | 角色权限替换非事务 | `DELETE` + 逐条 `INSERT` 在 autocommit 下中断会留下不完整权限集 |
| **P1** | 无角色增删接口 | 只有 4 个内置角色可改，`roles` 表可数据驱动但无管理入口 |
| **P2** | 审计写入同步且可能静默丢失 | 请求内同步 `INSERT`，失败只打日志；高并发应改异步或队列 |
| **P2** | `/audit` 不返回 `detail` | 结构化上下文落了库但界面不可见，排查需直接查表 |
| ~~P2~~ | ~~租户管理无接口~~ 已解决（2026-11-18） | 已实现租户 CRUD + 配额 + 启停 + 删除保护，新建自动建 `logs-<code>`、删除连带 drop |
| **P2** | 批量重置密码 N 次往返 | 逐条 `UPDATE`，应改 `executemany` 或 `UPDATE ... WHERE id = ANY()` |
| **P2** | 行级隔离只在 `users` 生效 | 其他业务表（告警、数据源、仪表盘）的 `tenant_id` 强约束未做，检索仍依赖前端透传租户 |

---

## 11. 相关文件索引

| 文件 | 作用 |
|---|---|
| `services/system-service/app/main.py` | 17 个路由、业务校验、保护性约束、CSV 导出 |
| `services/system-service/app/db.py` | 六表 DDL、种子、权限树、PG 访问层 |
| `services/system-service/app/security.py` | bcrypt、JWT 解析、`require()` / `require_any()` 权限闸门 |
| `services/system-service/app/storage.py` | `/storage` 全部接口：OpenSearch 取数、趋势、索引状态、清理 |
| `services/system-service/app/audit.py` | 审计写入（管理面与日志空间共用，失败不阻断业务） |
| `frontend/src/api/system.js` | 15 个接口封装（含 `saveCsv` blob 下载，`:88-98`） |
| `frontend/src/views/UserRoles.vue` | 两个 Tab：用户管理 / 角色权限（系统参数与审计日志已迁至系统设置页） |
| `frontend/src/views/LogStorage.vue` | 日志空间管理四个 Tab：容量概览 / 存储配置（含平台总容量）/ 索引管理 / 租户配额（含分配汇总） |
| `frontend/src/api/storage.js` | 7 个存储接口封装 |
| `frontend/src/App.vue` | 顶栏真实用户；菜单按 `resources` 数组过滤 |
| `frontend/src/router/index.js` | `meta.perm` / `meta.permAny` 双档守卫 |
| `docs/module-platform-admin.md` | 模块业务视角（原型对照、权限矩阵、端到端验证） |
