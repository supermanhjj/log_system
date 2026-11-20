# 平台管理 · 登录与用户角色（SYS-0601 / AUTH-0100）

> 本文记录 2026-11-16 落地的「登录 + 平台管理-用户与角色 + 顶栏真实登录用户」实现现状。
> 对应原型：`prototype/settings.html`（P-SYS-0601）、登录规格 `prototype/specs/P-AUTH-0100.json`（原型无 login 页面文件，样式按 `design-tokens.css` 令牌自建）。
> 规划依据：`docs/tech-stack-selection.md` 3.2(1)、3.2(9)、6.2。
> 服务级实现细节另见：[auth-service 服务说明](service-auth.md) · [system-service 服务说明](service-system.md) · [日志空间管理](module-log-storage.md)。
> **2026-11-16 页面拆分**：原「用户与角色」四 Tab 拆成三个菜单——用户与角色（用户管理 / 角色权限）、系统设置（系统参数 / 审计日志）、日志空间管理（容量概览 / 存储配置 / 索引管理 / 租户配额）。
> **2026-11-18~19 菜单调整**：平台管理分组最终顺序为「租户管理 → 用户角色 → 日志空间 → 系统设置」；菜单名「用户与角色」→「用户角色」、「日志空间管理」→「日志空间」，页面内大标题仍为「用户与角色管理」「日志空间管理」。新增独立「租户管理」页（租户 CRUD + 配额 + 启停 + 删除保护）。产品品牌定为 **RuiLog 日志分析平台**，展示在顶栏与登录页 logo 后。

## 1. 范围与定位

| 能力 | 原型编号 | 实现状态 |
|---|---|---|
| 登录（账号 / 密码 / 租户 / 记住账号 + 四态） | AUTH-0100 | 已落地，真实校验 PostgreSQL 账号与 bcrypt 密码 |
| 用户管理（筛选 / CRUD / 启停 / 重置密码 / 批量 / 导出 / 分页） | SYS-0601 | 已落地 |
| 角色权限（成员数 / 权限矩阵编辑 / 实时生效） | SYS-0601 | 已落地 |
| 系统参数（检索上限 / **检索耗时目标** / 时区 / 会话超时 / 审计开关） | SYS-0601 | 已落地；日志保留参数已下线（唯一来源为「日志空间 · 存储配置」），检索耗时目标真实驱动运行总览 P95 达标判定 |
| 审计日志（筛选 / 分页 / 导出） | SYS-0601 | 已落地，登录、登出、改密、用户与角色变更、参数变更、导出均留痕 |
| 系统设置（系统参数 + 审计日志，独立菜单 `/settings/system`） | SYS-0601 | 已落地，从「用户与角色」页迁出 |
| 日志空间（容量 / 配置 / 索引 / 配额，`/settings/storage`） | 无原型（新增页） | 已落地，容量与索引数据来自 OpenSearch 实时取数；容量口径按角色切换 |
| 租户管理（CRUD / 配额 / 启停 / 删除保护，`/settings/tenants`） | 无原型（新增页） | 已落地，与「日志空间」共用同一份配额数据 |
| 顶栏实际登录用户与角色 | 全站 | 已落地（姓名 + 角色标签 + 租户 + 下拉：修改密码 / 退出登录）；产品品牌 RuiLog 日志分析平台 |

## 2. 双服务职责边界（关键）

规划里 SYS-0601 落在 **`system-service`**，`auth-service` 只承担登录与判定。两者**共用同一套 PG 表与同一个 `JWT_SECRET`**，但写入权严格分离，避免各写一份 DDL 互相冲突：

| | `system-service`（:8007） | `auth-service`（:8001） |
|---|---|---|
| 定位 | 管理面 CRUD | 认证面：登录、JWT 签发、`/me`、改密 |
| 建表 | **六张表的唯一建表方**（`app/db.py` 的 `init_db()`） | **不建表** |
| 写权限 | 全部业务表 | 仅 `users.last_login_at/last_login_ip` 与 `password_hash`、`audit_log` 追加 |
| 权限判定 | 自行校验 Token + 现查 `permissions` | 只返回权限点，不做路由级判定 |
| 目录 | `services/system-service/app/{main,db,security}.py` | `services/auth-service/app/{main,db,security}.py` |

`security.py` 在两边各有一份（容器独立打包、未抽公共包），**改动必须同步**：密码策略、JWT 算法、`current_user` 查询字段要一致，否则一边签发的 Token 另一边解析不了。

> 前序文档中 `6.2` 服务清单把 `auth-service` 的「对应功能模块」写成 `AUTH/SYS`、`auth-service/app/main.py` 旧 docstring 写「对应 AUTH-0100 / SYS-0601」，容易误读为 SYS-0601 要在 auth-service 建。本次已在代码注释与 `tech-stack-selection.md` 中修正为「AUTH-0100（SYS-0601 仅做登录态与权限判定）」。

## 3. 数据模型（六表，全部在 `system-service/app/db.py`）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `tenants` | `code` / `name` / `enabled`（+ `tenant_quotas.quota_gb`） | **唯一真源**：编码即 OpenSearch 索引名 `logs-<code>`，全链路（查询/看板/告警/写入/前端下拉）都从这里取，代码里不再硬编码。种子 4 个：`default` 总部生产、`acme` 省侧运维、`gp` 测试租户、`unknown` 未归属日志。**保留字** `all` / `*` / `unknown` 不可创建；编码创建后不可改（是隔离边界） |
| `roles` | `key` / `name` / `scope_desc` / `is_builtin` / `sort_order` | 4 个内置角色，`key` 即 Token 里的 `role` |
| `permissions` | `role_key` / `resource` / `action`，唯一键三者组合 | 权限点按「资源:操作」粒度 |
| `users` | `tenant_id` / `account` 唯一 / `name` / `password_hash` / `role_key` / `phone` / `email` / `enabled` / `last_login_at` / `last_login_ip` / `is_builtin` | 账号规则：小写字母开头，可含小写字母/数字/下划线，3-32 位；密码 bcrypt(cost=12)，**任何接口都不返回哈希** |
| `audit_log` | `user_id` / `user_name` / `account` / `action` / `target` / `detail`(jsonb) / `ip` / `result` | 追加写；用户名与账号冗余存储，用户改名后历史记录仍可读 |
| `system_params` | `pkey` / `pvalue` / `ptype`(int/enum/bool) / `label` / `note` / `options`(jsonb) | 保存时按 `ptype` 校验，非法值 400 |

**种子数据**（仅当记录不存在时插入，绝不覆盖已改密码或启停状态）：

| 账号 | 姓名 | 角色 | 租户 | 初始密码 |
|---|---|---|---|---|
| `admin` | 系统管理员 | 平台管理员 | 总部生产 | `Admin@123`（`AUTH_SEED_ADMIN_PASSWORD` 可覆盖） |
| `ops_chenli` | 陈立 | 平台管理员 | 总部生产 | `Ops@12345`（`AUTH_SEED_DEMO_PASSWORD`） |
| `ops_zhouhang` | 周航 | 运维值班 | 总部生产 | `Ops@12345` |
| `ops_gaoyuan` | 高远 | 只读用户 | 省侧运维 | `Ops@12345` |
| `huangjinjin_test` | 黄金金（测试） | 运维值班 | 省侧运维 | `Ops@12345` |
| `audit_linjing` | 林静 | 审计员 | 总部生产 | `Ops@12345` |
| `ops_zhaolei` | 赵磊 | 运维值班 | 总部生产（**停用**，用于验证停用后登录被拒） | `Ops@12345` |

> 实际账号清单以 PG `users` 表为准（`select account, role_key, tenant_id from users`），上表为种子基线。

## 4. 权限模型

12 个资源 × 动作（`docs/tech-stack-selection.md` 已确认的三级角色 + 原型第四类审计员）：

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
| **tenant 租户管理** | view / create / update / delete | ✓ | | | |

- 权限点合计：平台管理员 32、运维值班 10、只读用户 6、审计员 4。
- **租户管理为平台管理员专属**：普通账号即便直接访问 `/settings/tenants` 也会被后端 403（前端隐藏菜单只是体验层）。
- **权限不写进 Token**，每次请求按 `role_key` 现查 `permissions` 表 → 改角色权限后**实时生效，无需重新登录**。
- 保护性约束：`platform_admin` 权限不可裁剪；不能停用/删除自己；内置账号不可停用、角色与租户不可改；不能移除自己所在角色的 `role:update`（防把自己锁在门外）。
- **平台管理入口按「任一 Tab 有权限」放行**：路由 `meta.permAny`，`/settings/users` 在 `settings/user/role/param/audit` 任一 `view` 下可进。否则审计员有 `audit:view` 却进不了页面（本次实测发现并修正）。

## 5. 接口清单

`auth-service`（nginx `/api/auth/`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tenants` | 登录页租户下拉（只返回 `enabled`） |
| POST | `/login` | 体 `{account, password, tenant, remember}` → `{token, expiresIn, expiresAt, user, permissions}` |
| GET | `/me` | 当前用户 + 权限点 + 密码策略说明 |
| POST | `/logout` | 写登出审计（JWT 无状态，服务端不维护黑名单） |
| PUT | `/password` | 改密：校验原密码 + 策略 + 新旧不同 |

登录失败语义（前端据此区分展示，不糊成一句「登录失败」）：`400` 账号或密码为空 / `401` 账号不存在或密码错误（两者统一文案，避免账号枚举）/ `403` 账号已停用、租户不匹配（带出账号所属租户）。

`system-service`（nginx `/api/system/`）：全部接口要求 `Authorization: Bearer`，并按资源动作校验。

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/meta` | 登录即可（角色、租户、权限矩阵、当前用户） |
| GET | `/users` | `user:view`（非平台管理员强制限定本租户） |
| POST | `/users` | `user:create` |
| PUT | `/users/{id}`、`/users/{id}/enabled`、`/users/{id}/reset-password` | `user:update` |
| POST | `/users/batch/enabled`、`/users/batch/reset-password` | `user:update` |
| GET | `/users/export.csv` | `user:export` |
| GET | `/roles`、PUT `/roles/{key}` | `role:view` / `role:update` |
| GET | `/params`、PUT `/params` | `param:view` / `param:update` |
| GET | `/audit`、GET `/audit/export.csv` | `audit:view` / `audit:export` |
| GET | `/tenants` | `tenant:view`（平台管理员看全量；普通账号仅本租户，用于前端下拉） |
| POST | `/tenants` | `tenant:create`（**自动建 `logs-<code>` 索引**，显式 mapping） |
| PUT | `/tenants/{code}`、`PUT /tenants/{code}/enabled` | `tenant:update` |
| DELETE | `/tenants/{code}` | `tenant:delete`（**连带 drop 索引**，有用户/有日志/当前登录租户均拒绝） |

CSV 均带 UTF-8 BOM（Excel 直开不乱码）；临时密码只在创建/重置的响应里返回一次，并落审计（不含密码本身）。

## 6. 前端落点

| 文件 | 内容 |
|---|---|
| `frontend/src/views/LoginView.vue` | 独立登录页（不套平台布局）：品牌区 + 账号/密码/租户 + 记住账号 + 错误/无权限两种提示态 + 登录 loading |
| `frontend/src/views/UserRoles.vue` | 用户与角色管理两 Tab：用户管理、角色权限矩阵（面包屑「平台管理 / 用户角色」、大标题「用户与角色管理」） |
| `frontend/src/views/SystemSettings.vue` | 系统设置两 Tab：系统参数、审计日志（含导出） |
| `frontend/src/views/LogStorage.vue` | 日志空间四 Tab：容量概览（含趋势图与范围切换，**口径按角色**）、存储配置、索引管理（含清理确认）、租户配额（面包屑「平台管理 / 日志空间」、大标题「日志空间管理」） |
| `frontend/src/views/TenantManagement.vue` | **租户管理页**：筛选（编码/名称、启用状态）、表格（名称+保留标记、编码+索引名、用户数、日志量、已用空间、配额、启停开关、编辑/删除）、新建/编辑弹窗（编码创建后只读）、配额输入 |
| `frontend/src/stores/app.js` | 租户清单从服务端 `listTenants()` 拉取（`loadTenants({includeAll})`，`all` 仅管理员注入），当前租户持久化到 localStorage `ruilog.currentTenant`；租户被停用/删除时自动回落首项 |
| `frontend/src/api/http.js` | 统一请求拦截 `attachAuth`：全局注入 `Authorization`；`params.tenant` **仅按作用域注入**（`TENANT_SCOPED_PREFIXES` = `/api/search`、`/api/dashboards`、`/api/alarms`、`/api/data`），平台域只带 Token。修复了此前无差别注入导致 `/system/users?tenant=all` 恒 0 条的问题（2026-11-18） |
| `frontend/src/App.vue` | 顶栏品牌区（logo + **RuiLog 日志分析平台**，194px 宽与侧栏对齐）+ 真实用户（首字头像 + 姓名 + 角色标签 + 租户）；用户下拉：账号/租户、修改密码、退出登录；改密弹窗标签现在换行（label-width 104px + nowrap）；登录页不渲染布局；菜单与顶部页签按权限过滤 |
| `frontend/src/router/index.js` | `/login` 公开路由 + 全局守卫（无 Token → 登录页并带 `redirect`；有 Token 无用户 → `/me` 回填；`perm`/`permAny` 不足 → 提示并回 `/overview`） |
| `frontend/src/stores/auth.js` | Token（localStorage `ls_token`）、`user`、`permissions`、`has(resource, action)`、登录/登出/改密 |
| `frontend/src/api/{auth,system}.js` | axios 实例 + 请求拦截注入 Bearer |

登录选定租户会写入全局 `app.tenant`；刷新页面时由守卫 `/me` 回填，保证检索、告警、数据接入取数租户一致。顶栏租户切换仅对平台管理员开放（其他角色锁定本租户，避免跨租户越权）。

## 7. 部署与首次启动

1. `system-service` 启动时 `init_db()` 建表 + 种子（PG 不可用则降级启动并打印 `init_db skipped`，不会挂进程）。
2. 两服务必须共用 `.env` 中的 `JWT_SECRET` / `JWT_ALGORITHM`；`ACCESS_TOKEN_EXPIRE_MINUTES` 默认 1440（24h）。
3. `.env.example` 已补 `AUTH_SEED_ADMIN_PASSWORD` / `AUTH_SEED_DEMO_PASSWORD`（可选，覆盖种子密码；**生产必须先改**）。
4. 首次部署建议先起 `system-service` 再起 `auth-service`（表还不存在时登录会返回明确错误，而不是假装成功）。
5. 忘记管理员密码：`docker compose restart system-service` 不会重置（种子只在账号不存在时插入），需在 PG 内改哈希或用其他管理员重置。

## 8. 验证记录（2026-11-16 实测）
- 接口：登录 200/401/403 三态、`/me` 权限点、只读用户访问 `/users` 与 `/audit` 均 403（文案带缺失权限点）；重复账号 409、非法账号 400、停用自己 400、改平台管理员权限 400、参数传非整数 400。
- 界面（headless Chrome，1440×900）：未登录访问 `/settings/users` → 跳登录并带 `redirect`；登录后顶栏显示「系统管理员 / 平台管理员」；四 Tab 渲染；用户表 6 行、分页中文「共 6 个用户 · 20条/页」；新建用户 → 弹出「用户已创建 · 初始密码」；行内重置密码 → 临时密码弹窗；开关停用 → 「已停用该用户」；导出 → 「已导出用户信息（CSV）」；修改密码 → 自动登出并跳登录页，旧密码 401、新密码 200；审计留痕 14 条含登录成功/失败、登出、改密、新建用户、重置密码、停用、导出。
- 角色视图：只读用户（高远）无「平台管理」入口、无租户切换器、直访被拦并提示；审计员（林静）有「平台管理」入口，页内只有「审计日志」一个 Tab。
- 回归：`/search`、`/overview`、`/alarms/events`、`/data-sources`、`/agents`、`/ai/management` 均正常渲染，浏览器控制台零报错。

### 8.1 租户隔离验收（2026-11-18 实测，36 项全通过）

| 类别 | 验证点 | 结果 |
|---|---|---|
| 查询隔离 | 普通账号查他人租户 → 403「无权访问其他租户的日志」；请求 `all` → 403；查本租户仅返回 `tenant_id=acme` | ✅ |
| PPL 防注入 | `source=logs-default` 被服务端整段丢弃并重写为登录租户索引 | ✅ |
| 看板 | 列表/详情/改/删全部按租户收口；跨租户读取与删除统一按 404（不泄露资产是否存在） | ✅ |
| 告警 | 规则按租户存；`tenant='all'` 为平台级通用规则（对所有租户生效、普通账号只读）；事件只按本租户规则判定 | ✅ |
| 接入端点 | `/meta` 按登录身份返回 `logs-<本人租户>`；`tenantOptions` 普通账号仅本租户；匿名 401；`PUT /settings` 非管理员 403 | ✅ |
| 写入 | `/ingest` 无凭证 401、API Key 绑定租户写入正确；`/ingest/collector` 无 Key 401（鉴权前置于 body 解析）、未知租户落 `logs-unknown` | ✅ |
| 生命周期 | 新建租户自动建 `logs-<code>` 索引（可立即查询，total=0）；重复/保留字被拒；删除当前登录租户 400 | ✅ |
| 网关 | 8 条路由返回结构均与所属服务匹配（无串服务）；后端重启后不重启 web 仍正常路由 | ✅ |

### 8.2 本轮修复与调整（2026-11-18~19）

| 项 | 变化 |
|---|---|
| 用户列表空白（数据未丢） | `http.js` 的 `attachAuth` 曾给所有请求注入 `params.tenant`，平台管理接口把它当筛选条件，`/users?tenant=all` 恒 0 条；改为按作用域前缀注入，平台域只带 Token（esbuild 单测 5/5 通过） |
| 跨服务鉴权补齐 | `search`/`dashboard`/`alarm`/`ingest` 各引入 `authz.py`，五个业务服务全部按 JWT 强制解析租户（原第 9 节 P0 已解决） |
| ChatPPL 租户维度 | 单租户范围剥除租户字段；「全部租户」范围开放虚拟字段 `tenant` 并翻译为物理字段、结果回填中文名（详见 `module-log-search.md` 第 9 章） |
| 权限点 / 参数 | 权限点 25→28（加 `storage` 资源）、落库 44→48；`search_p95_target` 驱动总览 P95 达标判定；`log_retain_days` 下线 |
| 容量口径按角色 | 平台管理员看平台总容量、租户看本租户配额，四项闭合；非管理员传 `all` 被锁回本租户（详见 `module-log-storage.md` 验证记录） |
| UI 修复 | 改密弹窗「确认新密码」标签不再折行；运行总览两处 ECharts tooltip 加 `appendToBody`，修复 hover 时区块右移 |
| 品牌 / 菜单 | 产品名定为 RuiLog 日志分析平台；菜单名与顺序调整（见本文开头） |

## 9. 已知边界与待办

1. **跨服务鉴权已完成（2026-11-18）**：`search` / `dashboard` / `alarm` / `ingest` 各自引入 `authz.py`，统一 `current_identity` + `resolve_tenant` + `resolve_ingest_tenant`，全部业务端点要求 Bearer 并按登录身份强制解析租户；`agent` / `ai` 两服务的配置数据尚未租户化（见 `service-inventory.md` 多租户边界）。
2. **系统参数部分已驱动**：`search_p95_target` 已驱动运行总览 P95 卡片的达标 / 超标判定（改完约 10 秒内生效）；日志保留天数已从系统参数下线，只由「日志空间管理 · 存储配置」维护，避免两处各存一份；`search_limit` / `audit_enabled` 仍是登记 + 校验，未反向驱动检索网关上限与审计开关。
3. 无删除用户接口（与原型一致，用停用代替），`user:delete` 权限点已定义但暂无对应操作。
4. 审计写入是请求内同步 INSERT（失败不阻断业务，仅打日志）；高并发下应改异步或批量。
5. 密码策略仅「长度 + 字母数字」，无失败锁定、验证码、历史密码校验、强制首次改密。
6. 临时密码仅一次性展示，未接短信/邮件下发（原型文案提「短信下发」，本实现如实改为管理员转交）。
7. **租户行级隔离已完成（2026-11-20）**：日志查询/PPL/直方图/上下文、运行总览、看板配置、告警规则与事件、数据源接入与配置导出、agent 主机配置（`agents` / `agent_commands` / 主机字段映射，唯一键改为 `(tenant_id, hostname)`）、AI 模型与语法配置（`providers` / `grammar_rules` / `ai_capabilities`，唯一键带租户）、`saved-views`（按「租户 + 用户」落库），均按登录态强制解析租户；前端 `TENANT_SCOPED_PREFIXES` 已收录 `/api/agents`、`/api/ai`。
8. `roles` 的四个角色与 `tech-stack-selection.md` 已确认的「三类」存在一处差异：原型列表含第四类「审计员」，本次按原型实现（数据驱动，可在角色表内调整）。
