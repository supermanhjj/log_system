# 登录与鉴权模块（auth-service）开发记录

> 快照日期：2026-11-19（GMT+8）｜基于当前工作区真实代码整理，行号随代码变动可能偏移
> 模块范围：**登录**（原型 AUTH-0100）+ **鉴权**（JWT 签发、校验、权限点下发）
> 关联文档：[平台管理模块](module-platform-admin.md) · [用户与角色模块（system-service）](service-system.md) · [服务清单](service-inventory.md) · [技术栈选型](tech-stack-selection.md)
> 服务 / 端口：`log_system-auth-service-1`，宿主机 `8001` → 容器 `8000`；网关前缀 `/api/auth/`（`nginx/nginx.conf:21`）

---

## 0. 本期进展快照

| 能力 | 状态 | 落点 |
|---|---|---|
| 登录（账号 + 密码 + 租户 + 记住账号 + 四态提示） | ✅ 已落地，真实校验 PG 账号与 bcrypt 密码 | `app/main.py:112-168` |
| JWT 签发（HS256，默认 24h） | ✅ | `app/security.py:43-59` |
| Token 校验与当前用户解析（每请求现查库） | ✅ | `app/security.py:68-90` |
| 权限点下发（不写进 Token，每请求现查） | ✅ | `app/security.py:93-98` |
| 租户下拉（只列启用项） | ✅ | `app/main.py:105-109` |
| 登出 / 改密（含审计） | ✅ | `app/main.py:180-206` |
| 失败锁定 / 限流 / MFA / refresh token | ❌ 未做 | 见第 9 节 |

代码规模：`main.py` 207 行、`security.py` 105 行、`db.py` 60 行。**共 7 个路由**（5 个业务 + `/health`、`/`）。

---

## 1. 模块定位与开发边界

登录与鉴权为什么要单独一个服务？因为它是**唯一不需要先登录就能访问的写入口**，也是全站唯一的 Token 签发方。把它和平台管理的 CRUD 混在一起，会让「认证面」和「管理面」的爆炸半径绑在一起。

| | 内容 |
|---|---|
| 做 | 登录校验（账号 + 密码 + 租户匹配）、JWT 签发、`/me` 返回当前用户与权限、登出与改密的审计、租户下拉 |
| 不做 | 用户 / 角色 / 权限 / 参数 / 审计的**管理面 CRUD**（在 `system-service`）、建表、路由级权限判定 |
| 依赖的表 | `users` / `tenants` / `roles` / `permissions` / `audit_log`，**全部由 `system-service` 创建，本服务一行 DDL 都没有** |
| 对外契约 | 与 `system-service` 共用 `JWT_SECRET` / `JWT_ALGORITHM`；两边各存一份 `security.py`，改动必须同步 |

表归属的硬约定写在 `app/db.py:1-12`：两个服务各写一份 DDL 必然互相覆盖，所以只让 `system-service` 建表。**代价**是首次部署必须先起 `system-service`——表还不存在时登录会返回明确错误，而不是假装成功。

---

## 2. 登录模块：开发内容

### 2.1 功能清单

| 功能点 | 实现细节 |
|---|---|
| 账号输入 | 提交前 `strip().lower()` 小写化，避免大小写绕过唯一约束（`main.py:114`） |
| 密码输入 | 前端可切明文；后端只做 bcrypt 校验，日志与响应**永不回显明文** |
| 租户选择 | 登录页下拉来自 `GET /tenants`，只返回 `enabled` 的租户，且不暴露内部 id |
| 租户匹配校验 | 选了租户就必须与账号所属租户一致，否则 403 并**带出账号真实所属租户名**，避免用户不知道找谁 |
| 记住账号 | 前端 `localStorage('ls_account')`；后端 `remember` 字段**解析但不使用**（不延长有效期） |
| 四态展示 | 空值 400 / 密码错 401 / 停用或租户不符 403 / 网络异常兜底——四类提示文案不同 |
| 登录成功 | 写 Token → 拉 `/me` → 按 `redirect` 参数回跳原目标页（默认 `/overview`） |
| 审计 | 成功、失败（含账号不存在）都留痕，失败记录尝试的账号名但 `user_id` 为空 |

### 2.2 登录链路（实现方式）

```
LoginView.vue
  → POST /api/auth/login {account, password, tenant, remember}
      → UPDATE-free 只读查询：users JOIN tenants JOIN roles（按小写账号）
      → ① 无记录 或 bcrypt.checkpw 失败  → 401「账号或密码错误，请重新输入」+ 审计 failed
      → ② enabled = false               → 403「该账号未开通平台权限…」+ 审计 failed
      → ③ 租户不匹配                     → 403「该账号不属于所选租户…（账号所属：X）」+ 审计 failed
      → ④ UPDATE users SET last_login_at=now(), last_login_ip=$ip
      → ⑤ 回查 last_login_at（见 5.1，回填本次时间）
      → ⑥ 写「登录」审计 → create_token() → 组装响应
  → 前端存 localStorage('ls_token')，axios 拦截器统一注入 Authorization: Bearer
```

响应结构（`main.py:158-168`）：
`{ok, token, tokenType:"bearer", expiresIn, expiresAt, user, permissions, message, note}`

- `permissions` 是 `["resource:action", ...]` 数组，登录时一次性给全量，前端据此渲染菜单与按钮。
- `user` 结构（`main.py:77-87`）：`{id, account, name, roleKey, roleName, tenant, tenantName, lastLogin}`，时间为 CST 字符串。
- **任何接口都不返回 `password_hash`**。

### 2.3 失败语义（前端依赖，不能糊成一句「登录失败」）

| 状态码 | 触发条件 | 文案 | 留痕 |
|---|---|---|---|
| 400 | 账号或密码为空 | 请输入账号与密码 | 否（先于查询返回） |
| 401 | 账号不存在 **或** 密码错误 | 账号或密码错误，请重新输入（**统一文案，防账号枚举**） | 是，`result=failed` |
| 401 | Token 无效 / 过期 / 账号已删 / 已被停用 | 登录态无效或已过期… / 账号已不存在… / 账号已被停用… | 否 |
| 403 | 账号 `enabled=false` | 该账号未开通平台权限，请联系管理员 | 是 |
| 403 | 所选租户与账号不符 | 该账号不属于所选租户，请联系管理员（账号所属：X） | 是 |
| 400 | 改密：原密码错 / 新密码不合策略 / 新旧相同 | 原密码不正确 · 密码长度 8-32 位… · 新密码不能与原密码相同 | 否 |

### 2.4 密码存储与策略

- 哈希：`bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12))`（`security.py:24-25`）。代码直接 `import bcrypt`，依赖由 `passlib[bcrypt]` 传递安装。
- 策略：8-32 位，**必须同时含字母与数字**（`security.py:35-40`）。上限取 32 是为了避开 bcrypt 对 >72 字节输入拒算的坑。
- 改密：校验原密码 → 校验策略 → 校验新旧不同 → `UPDATE password_hash` → 写「修改密码」审计（`main.py:188-206`）。

---

## 3. 鉴权模块：开发内容与实现方式

### 3.1 JWT 载荷（`security.py:46-54`）

| 字段 | 用途 |
|---|---|
| `sub` | `users.id` 的字符串形式 —— **解析时按它现查库**，所以停用/删除账号后旧 Token 立刻失效 |
| `account` / `name` / `role` / `tenant` | 便于排查与审计，**不作权限依据** |
| `iat` / `exp` | 签发与过期时间戳；TTL = `ACCESS_TOKEN_EXPIRE_MINUTES`（默认 1440 分钟 = 24h） |

**权限刻意不写进 Token。** 每次请求由 `perms_for_role()` 现查 `permissions` 表（`security.py:93-98`），换来的是「改角色权限后实时生效、无需重新登录」——这是整套设计里最关键的一条取舍：牺牲一次 SQL，省掉「权限变更要等 24h 过期」的运维痛点。

### 3.2 三个鉴权原语的职责

| 原语 | 位置 | 做什么 |
|---|---|---|
| `bearer_token()` | `security.py:62-65` | 校验 `Authorization: Bearer` 头格式，缺失或格式不对直接 401 |
| `current_user()` | `security.py:68-90` | 解 Token → 按 `sub` 现查 users JOIN tenants JOIN roles → 账号不存在 401 / 被停用 401 |
| `perms_for_role()` | `security.py:93-98` | 按 `role_key` 查权限点，返回 `["resource:action"]` |

管理面的权限闸门 `require(resource, action)` 在 `system-service` 侧（见其文档第 5 节）；本服务**只下发权限点，不做路由级判定**——登录服务不该替管理服务决定谁能做什么。

### 3.3 前端如何消费（完整链路）

| 环节 | 文件 | 要点 |
|---|---|---|
| 接口封装 | `frontend/src/api/auth.js` | `baseURL:'/api/auth'`、`timeout:20000`、`TOKEN_KEY='ls_token'`（`:5-8`） |
| 登录态 | `frontend/src/stores/auth.js` | Pinia 存 `token/user/permissions`；`has(resource, action)`（`:23`）、`hasAny`（`:26`） |
| 路由守卫 | `frontend/src/router/index.js` | 无 Token → `/login?redirect=`；有 Token 无 user → `/me` 回填；`meta.perm` 不足 → 提示并回 `/overview`（`:42-75`） |
| 页面 | `frontend/src/views/LoginView.vue` | 独立页（不套后台布局）：品牌区（**RuiLog 日志分析平台**，logo 后）+ 表单 + 记住账号 + 两态提示 + loading |
| 顶栏 | `frontend/src/App.vue` | 品牌区（RuiLog 日志分析平台，logo 后）+ 首字头像 + 真实姓名 + 角色标签 + 当前租户；下拉：修改密码 / 退出登录 |

登录选定租户会写入全局 `app.tenant`，刷新时由守卫 `/me` 回填，保证检索、告警、数据接入取数租户一致。

品牌命名（2026-11-18）：产品显示名定为 **RuiLog 日志分析平台**，RuiLog 为主品牌词加粗，"日志分析平台"同色作后缀；顶栏两行堆叠（RuiLog 18px / 后缀 11px 大字距）与 210px 侧栏对齐，登录页标题与浏览器标签同步。

改密弹窗样式（2026-11-18）：`App.vue` 修改密码表单 `label-width` 88→104px 并强制 `white-space:nowrap`，修复「确认新密码」标签折行错乱。

---

## 4. 关键设计决策与取舍

| 决策 | 理由 | 代价 |
|---|---|---|
| 权限不进 Token，每请求现查 | 改权限实时生效，不用全员重登 | 每个请求多一次 PG 查询（当前量级可忽略） |
| `sub` 现查库而非信任 Token 里的 role | 停用/删号即时失效，避免「Token 没过期就能继续用」 | 同上，一次查询 |
| 账号不存在与密码错误同状态码同文案 | 防账号枚举 | 用户不知道自己是不是账号输错了（可接受） |
| bcrypt cost=12 | 单次哈希约百毫秒级，足够挡离线爆破 | 登录接口较慢，批量重置密码时更明显 |
| 认证与管理分两个服务 | 爆炸半径隔离；认证服务薄到不需要分层 | 两份 `security.py` 要手工同步 |
| auth 不建表 | 避免双写 DDL 冲突 | 首次部署有启动顺序依赖 |

---

## 5. 开发中遇到的问题与解法

### 5.1 `/me` 返回的 `lastLogin` 是上一次的时间

- **现象**：登录成功后顶栏与用户列表显示的「最近登录」都对不上刚才这次。
- **根因**：两个原因叠加——① `current_user()` 的 SELECT 漏掉了 `last_login_at / last_login_ip` 两列，返回 `None`；② 即使补上，`UPDATE` 之后 `rows[0]` 里仍是被查询时的旧值。
- **解法**：SQL 补齐两列；登录成功后**回查一次** `last_login_at` 覆盖到响应对象上（`main.py:149-155`），保证「本次登录时间」在顶栏与列表页口径一致。

### 5.2 分页控件显示英文 `20/page`、`Go to`

- **根因**：Element Plus 未挂语言包，与登录逻辑无关但影响整套平台观感。
- **解法**：`main.js` 引入 `element-plus/es/locale/lang/zh-cn`，`app.use(ElementPlus, { locale: zhCn })`。

### 5.3 改密后旧 Token 仍在有效期（未解决，已记录）

前端改密成功后会主动清 Token 并跳登录页，但服务端是纯无状态 JWT、不比对 `password_hash`、也没有 token 版本号，**旧 Token 在 24h 内仍可调用接口**。属已知边界，见第 9 节 P1。

---

## 6. 接口清单（7 个路由）

除 `/health`、`/`、`/tenants`、`/login` 外均需 `Authorization: Bearer`。

| 方法 | 路径 | 入参 | 出参要点 |
|---|---|---|---|
| GET | `/health` | — | `{status:"ok", service:"auth-service"}` |
| GET | `/` | — | 服务自述与端点列表（排障用） |
| GET | `/tenants` | — | `{items:[{code,name}]}`，只含启用租户 |
| POST | `/login` | `{account, password, tenant?, remember?}` | 见 2.2 |
| GET | `/me` | — | `{user, permissions, passwordRule}` |
| POST | `/logout` | — | 写审计后返回；**JWT 无状态，服务端不维护黑名单** |
| PUT | `/password` | `{oldPassword, newPassword}` | 校验三重后更新哈希并留痕 |

---

## 7. 配置与部署

| 变量 | 默认值 | 说明 |
|---|---|---|
| `JWT_SECRET` | `dev-only-not-secret` | **必须与 system-service 一致**；生产必改 |
| `JWT_ALGORITHM` | `HS256` | 两边必须一致 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | 登录态有效期（分钟） |
| `AUTH_PG_DSN` | 由 `POSTGRES_*` 拼出 | 需要单独指定时覆盖 |

- 容器：`image: log_system-backend`，`depends_on: [postgres, redis]`，`env_file: .env`；开发期由 `compose.override.yml` 暴露 `8001:8000` 并开 `--reload`。
- **启动顺序：先 `system-service`（建表 + 种子）→ 再本服务。**
- 自检：`curl localhost:8080/api/auth/health` → `200`；`curl localhost:8080/api/auth/tenants` → 三个租户。

---

## 8. 验证记录（2026-11-16 实测）

- `GET /api/auth/health` → `200`；`GET /api/auth/tenants` → `default 总部生产 / acme 省侧运维 / gp 测试租户`。
- 登录三态：正常 200（平台管理员返回 25 个权限点）；错误密码 401；停用账号（`ops_zhaolei`）403；租户不匹配 403 且带出所属租户名。
- 无效 Token 访问 `/me` → `401`；无 Token 访问 `/api/system/users` → `401 未登录，请先登录`。
- 改密：旧密码登录 401、新密码 200；前端自动登出并跳登录页。
- 刷新页面不丢登录态（`/me` 回填），直接访问受限路由被守卫拦回。
- 回归：`/search`、`/overview`、`/alarms/events`、`/data-sources`、`/agents`、`/ai/management` 登录后均正常，控制台零报错。

---

## 9. 已知边界与待办

| 级别 | 项 | 说明 |
|---|---|---|
| **P0** | `JWT_SECRET` 有可用默认值 | 未配置时静默用 `dev-only-not-secret`，生产漏配等于没有签名保护。建议启动时未设置就拒绝起服务 |
| **P0** | 无失败锁定 / 限流 / 验证码 | 文案防了枚举，但可无限次试密码。应在网关或本服务加「同账号 / 同 IP N 次失败锁定」 |
| **P1** | 改密后旧 Token 不失效 | 见 5.3；需引入 token 版本号或 `password_changed_at` 比对 |
| **P1** | 停用租户仍可登录 | 登录只校验用户 `enabled` 与租户**匹配**，未校验 `tenants.enabled`——`/tenants` 过滤了但登录放行了 |
| **P1** | `remember` 参数未使用 | 后端解析但不延长有效期，「记住账号」是纯前端行为 |
| **P2** | 无 refresh token | 24h 到期直接跳登录，无静默续期 |
| **P2** | 无 MFA / SSO | 按规划属后续阶段 |
| **P2** | `client_ip()` 可被伪造 | nginx 用 `$proxy_add_x_forwarded_for` 追加而非覆写，客户端自带的首段会被采信为「来源 IP」 |
| **P2** | `security.py` 两份拷贝 | 密码策略、JWT 参数、`current_user` 字段需人工同步；建议后续抽 `shared/` 公共包 |

---

## 10. 相关文件索引

| 文件 | 作用 |
|---|---|
| `services/auth-service/app/main.py` | 登录 / `/me` / 登出 / 改密 / 租户下拉 + 失败语义 + 审计 |
| `services/auth-service/app/security.py` | bcrypt、JWT 签发与解析、`current_user`、`perms_for_role` |
| `services/auth-service/app/db.py` | PG 最小访问层（只读为主，不建表） |
| `frontend/src/api/auth.js` | 5 个接口封装 |
| `frontend/src/stores/auth.js` | Pinia 登录态与权限判定 |
| `frontend/src/views/LoginView.vue` | 独立登录页 |
| `frontend/src/router/index.js` | 全局守卫与 `redirect` 回跳 |
| `frontend/src/App.vue` | 顶栏真实用户与退出入口 |
| `docs/module-platform-admin.md` | 模块业务视角（原型对照、权限矩阵、验证） |
