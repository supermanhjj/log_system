# 平台管理 · 登录与用户角色（SYS-0601 / AUTH-0100）

> 本文记录 2026-11-16 落地的「登录 + 平台管理-用户与角色 + 顶栏真实登录用户」实现现状。
> 对应原型：`prototype/settings.html`（P-SYS-0601）、登录规格 `prototype/specs/P-AUTH-0100.json`（原型无 login 页面文件，样式按 `design-tokens.css` 令牌自建）。
> 规划依据：`docs/tech-stack-selection.md` 3.2(1)、3.2(9)、6.2。

## 1. 范围与定位

| 能力 | 原型编号 | 实现状态 |
|---|---|---|
| 登录（账号 / 密码 / 租户 / 记住账号 + 四态） | AUTH-0100 | 已落地，真实校验 PostgreSQL 账号与 bcrypt 密码 |
| 用户管理（筛选 / CRUD / 启停 / 重置密码 / 批量 / 导出 / 分页） | SYS-0601 | 已落地 |
| 角色权限（成员数 / 权限矩阵编辑 / 实时生效） | SYS-0601 | 已落地 |
| 系统参数（日志保留 / 检索上限 / 时区 / 会话超时 / 审计开关） | SYS-0601 | 已落地（登记 + 校验层） |
| 审计日志（筛选 / 分页 / 导出） | SYS-0601 | 已落地，登录、登出、改密、用户与角色变更、参数变更、导出均留痕 |
| 顶栏实际登录用户与角色 | 全站 | 已落地（姓名 + 角色标签 + 租户 + 下拉：修改密码 / 退出登录） |

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
| `tenants` | `code` / `name` / `enabled` | 编码与 OpenSearch 真实索引对齐：`default` 总部生产、`acme` 省侧运维、`gp` 测试租户。**登录选定租户 = 前端 `app.tenant`**，检索链路无需再做映射 |
| `roles` | `key` / `name` / `scope_desc` / `is_builtin` / `sort_order` | 4 个内置角色，`key` 即 Token 里的 `role` |
| `permissions` | `role_key` / `resource` / `action`，唯一键三者组合 | 权限点按「资源:操作」粒度 |
| `users` | `tenant_id` / `account` 唯一 / `name` / `password_hash` / `role_key` / `phone` / `email` / `enabled` / `last_login_at` / `last_login_ip` / `is_builtin` | 账号规则：小写字母开头，可含小写字母/数字/下划线，3-32 位；密码 bcrypt(cost=12)，**任何接口都不返回哈希** |
| `audit_log` | `user_id` / `user_name` / `account` / `action` / `target` / `detail`(jsonb) / `ip` / `result` | 追加写；用户名与账号冗余存储，用户改名后历史记录仍可读 |
| `system_params` | `pkey` / `pvalue` / `ptype`(int/enum/bool) / `label` / `note` / `options`(jsonb) | 保存时按 `ptype` 校验，非法值 400 |

**种子数据**（仅当记录不存在时插入，绝不覆盖已改密码或启停状态）：

| 账号 | 姓名 | 角色 | 租户 | 初始密码 |
|---|---|---|---|---|
| `admin` | 系统管理员 | 平台管理员 | 总部生产 | `Admin@123`（`AUTH_SEED_ADMIN_PASSWORD` 可覆盖） |
| `ops_chenli` | 陈立 | 平台管理员 | 总部生产 | `Ops@12345` |
| `ops_zhouhang` | 周航 | 运维值班 | 总部生产 | `Ops@12345` |
| `ops_gaoyuan` | 高远 | 只读用户 | 省侧运维 | `Ops@12345` |
| `audit_linjing` | 林静 | 审计员 | 总部生产 | `Ops@12345` |
| `ops_zhaolei` | 赵磊 | 运维值班 | 总部生产（**停用**，用于验证停用后登录被拒） | `Ops@12345` |

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

- 权限点合计：平台管理员 25、运维值班 9、只读用户 6、审计员 4。
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

CSV 均带 UTF-8 BOM（Excel 直开不乱码）；临时密码只在创建/重置的响应里返回一次，并落审计（不含密码本身）。

## 6. 前端落点

| 文件 | 内容 |
|---|---|
| `frontend/src/views/LoginView.vue` | 独立登录页（不套平台布局）：品牌区 + 账号/密码/租户 + 记住账号 + 错误/无权限两种提示态 + 登录 loading |
| `frontend/src/views/UserRoles.vue` | 用户与角色四 Tab；筛选、批量栏、表格、分页、新建/编辑弹窗、临时密码弹窗、角色权限矩阵（两列 12 组）、参数表单、审计表 |
| `frontend/src/App.vue` | 顶栏真实用户（首字头像 + 姓名 + 角色标签 + 租户）；用户下拉：账号/租户、修改密码、退出登录；登录页不渲染布局；菜单与顶部页签按权限过滤 |
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

## 9. 已知边界与待办

1. **跨服务鉴权未强绑**：目前只有 `auth-service` / `system-service` 校验 Token；`search` / `alarm` / `ingest` / `agent` / `dashboard` / `ai` 仍不校验（前端也不带 Token）。要真隔离需在 nginx 或各服务加统一中间件——这是下一步最该做的安全项。
2. **系统参数未驱动系统**：`log_retain_days` / `search_limit` / `audit_enabled` 目前是登记 + 校验，未反向驱动 OpenSearch ISM 策略、检索网关上限、审计开关。
3. 无删除用户接口（与原型一致，用停用代替），`user:delete` 权限点已定义但暂无对应操作。
4. 审计写入是请求内同步 INSERT（失败不阻断业务，仅打日志）；高并发下应改异步或批量。
5. 密码策略仅「长度 + 字母数字」，无失败锁定、验证码、历史密码校验、强制首次改密。
6. 临时密码仅一次性展示，未接短信/邮件下发（原型文案提「短信下发」，本实现如实改为管理员转交）。
7. 租户行级隔离只在 `users` 表与管理接口生效；其他业务表（告警、数据源、仪表盘）的 `tenant_id` 强约束未做，检索仍依赖前端透传租户。
8. `roles` 的四个角色与 `tech-stack-selection.md` 已确认的「三类」存在一处差异：原型列表含第四类「审计员」，本次按原型实现（数据驱动，可在角色表内调整）。
