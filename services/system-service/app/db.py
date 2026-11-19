"""system-service 数据层：RBAC 六表 + 日志空间三表、种子数据、权限矩阵定义。

表的归属约定（重要）：本模块是 `users` / `roles` / `permissions` / `tenants` /
`audit_log` / `system_params` 六张 RBAC 表，以及 `storage_config` /
`tenant_quotas` / `storage_snapshots` 三张日志空间表的**唯一建表方**；
auth-service 只读不建，避免两个服务各写一份 DDL 互相冲突（详见
docs/module-platform-admin.md）。

租户编码与 OpenSearch 真实索引对齐：default / acme / gp，
登录时选定的租户即前端 `app.tenant`，检索链路无需再做映射。
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

PG_DSN = os.getenv(
    "SYSTEM_PG_DSN",
    f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
    f"user={os.getenv('POSTGRES_USER', 'loguser')} "
    f"password={os.getenv('POSTGRES_PASSWORD', 'logpass')} "
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')}",
)
CST = timezone(timedelta(hours=8))

# 权限资源与动作：前端菜单过滤、角色权限矩阵共用同一份定义
PERMISSION_TREE: List[Dict[str, Any]] = [
    {"resource": "overview", "label": "运行总览", "actions": ["view"]},
    {"resource": "search", "label": "日志检索", "actions": ["view", "export"]},
    {"resource": "dashboards", "label": "仪表盘", "actions": ["view", "update"]},
    {"resource": "alarms", "label": "告警中心", "actions": ["view", "update"]},
    {"resource": "data", "label": "数据接入", "actions": ["view", "update"]},
    {"resource": "agents", "label": "Agent 管理", "actions": ["view", "update"]},
    {"resource": "ai", "label": "智能管理", "actions": ["view", "update"]},
    {"resource": "settings", "label": "平台管理", "actions": ["view"]},
    {"resource": "user", "label": "用户管理", "actions": ["view", "create", "update", "delete", "export"]},
    {"resource": "role", "label": "角色权限", "actions": ["view", "update"]},
    # 租户是独立一级资源：租户编码决定日志索引隔离边界，变更属高危操作，
    # 故与「用户/角色」分开授权，并单独记录审计。
    {"resource": "tenant", "label": "租户管理", "actions": ["view", "create", "update", "delete"]},
    {"resource": "param", "label": "系统参数", "actions": ["view", "update"]},
    {"resource": "audit", "label": "审计日志", "actions": ["view", "export"]},
    {"resource": "storage", "label": "日志空间", "actions": ["view", "update", "clean"]},
]
ACTION_LABELS = {
    "view": "查看",
    "create": "新建",
    "update": "修改",
    "delete": "删除",
    "export": "导出",
    "clean": "清理",
}

ALL_PERMS = [f"{i['resource']}:{a}" for i in PERMISSION_TREE for a in i["actions"]]

# 角色：前三类来自 docs 已确认的三级模型，审计员为原型 P-SYS-0601 角色列表中的第四类
ROLE_SEEDS = [
    ("platform_admin", "平台管理员", "全部菜单 · 全部数据源 · 用户与角色管理", ALL_PERMS, 1),
    (
        "ops_duty",
        "运维值班",
        "检索 · 告警处置 · 仪表盘查看",
        ["overview:view", "search:view", "search:export", "dashboards:view",
         "alarms:view", "alarms:update", "data:view", "agents:view", "ai:view",
         "storage:view"],
        2,
    ),
    (
        "viewer",
        "只读用户",
        "检索查看 · 仪表盘查看（只读）",
        ["overview:view", "search:view", "dashboards:view", "alarms:view",
         "data:view", "agents:view"],
        3,
    ),
    (
        "auditor",
        "审计员",
        "审计日志 · 操作记录导出 · 检索查看",
        ["overview:view", "search:view", "audit:view", "audit:export"],
        4,
    ),
]

TENANT_SEEDS = [
    ("default", "总部生产"),
    ("acme", "省侧运维"),
    ("gp", "测试租户"),
    # 未归属：采集端无法判定来源租户时统一写入 logs-unknown，供管理员治理，
    # 禁止静默归入 default（否则造成跨租户数据污染，比查不到更危险）。
    ("unknown", "未归属日志"),
]

# 保留租户编码：不能被新建，也不能删除（unknown 是采集兜底落点）
RESERVED_TENANT_CODES = ("all", "*", "unknown")

# 种子用户：(账号, 姓名, 角色, 租户, 启用, 手机号, 是否内置)
USER_SEEDS = [
    ("admin", "系统管理员", "platform_admin", "default", True, "", True),
    ("ops_chenli", "陈立", "platform_admin", "default", True, "13800000001", False),
    ("ops_zhouhang", "周航", "ops_duty", "default", True, "13800000002", False),
    ("ops_gaoyuan", "高远", "viewer", "acme", True, "13800000003", False),
    ("audit_linjing", "林静", "auditor", "default", True, "13800000004", False),
    ("ops_zhaolei", "赵磊", "ops_duty", "default", False, "13800000005", False),
]

# 系统参数：(key, 值, 类型, 名称, 说明, 可选项)
PARAM_SEEDS = [
    ("search_limit", "5000", "int", "单次检索上限", "超过上限请缩小时间范围或使用聚合语句", []),
    ("search_p95_target", "800", "int", "检索耗时目标",
     "单位 ms，运行总览的检索耗时 P95 卡片按此值判定达标 / 超标", []),
    ("timezone", "Asia/Shanghai", "enum", "默认时区", "检索时间与审计时间的展示时区", ["Asia/Shanghai", "UTC"]),
    ("session_timeout_min", "30", "int", "会话超时", "分钟，超时后需重新登录", []),
    ("audit_enabled", "true", "bool", "操作审计", "记录检索、导出、规则变更等操作，保留 180 天", []),
]

# 已下线的参数：日志保留天数只由「日志空间管理 · 存储配置」承载（storage_config.retain_days），
# 系统设置里不再重复一份，避免两处各存一个值、页面显示不一致。启动时幂等清掉残留行。
RETIRED_PARAMS = ("log_retain_days",)

_pg_conn = None


def pg_conn():
    """惰性连接 + autocommit（与 ai-service 等其余微服务保持同一惯例）。"""
    global _pg_conn
    if _pg_conn is None or getattr(_pg_conn, "closed", 1):
        _pg_conn = psycopg2.connect(PG_DSN, connect_timeout=5)
        _pg_conn.autocommit = True
    return _pg_conn


def pg_exec(sql: str, params: tuple = ()) -> None:
    with pg_conn().cursor() as cur:
        cur.execute(sql, params)


def pg_query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with pg_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def pg_scalar(sql: str, params: tuple = ()):
    rows = pg_query(sql, params)
    if not rows:
        return None
    return list(rows[0].values())[0]


def to_cst_str(v) -> Optional[str]:
    """DB 时间统一按东八区格式化，前端与导出直接用，避免各处再转。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    """建表 + 种子数据；失败不阻塞服务启动（与其他微服务一致）。"""
    try:
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id SERIAL PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS roles (
                key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                scope_desc TEXT NOT NULL DEFAULT '',
                is_builtin BOOLEAN NOT NULL DEFAULT TRUE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS permissions (
                id SERIAL PRIMARY KEY,
                role_key TEXT NOT NULL REFERENCES roles(key) ON DELETE CASCADE,
                resource TEXT NOT NULL,
                action TEXT NOT NULL,
                UNIQUE (role_key, resource, action)
            )
            """
        )
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                account TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role_key TEXT NOT NULL REFERENCES roles(key),
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                last_login_at TIMESTAMPTZ,
                last_login_ip TEXT NOT NULL DEFAULT '',
                is_builtin BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        pg_exec("CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id)")
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY,
                tenant_id INTEGER,
                user_id INTEGER,
                user_name TEXT NOT NULL DEFAULT '',
                account TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                detail JSONB NOT NULL DEFAULT '{}'::jsonb,
                ip TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT 'success',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        pg_exec("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC)")
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS system_params (
                pkey TEXT PRIMARY KEY,
                pvalue TEXT NOT NULL DEFAULT '',
                ptype TEXT NOT NULL DEFAULT 'string',
                label TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                options JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # ---- 日志空间管理（2026-11-16）：存储配置、租户配额、每日容量快照 ----
        # storage_config 是单行表：id 恒为 1 且 CHECK 约束挡住第二行，避免出现多份配置。
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS storage_config (
                id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                retain_days INTEGER NOT NULL DEFAULT 30,
                roll_threshold INTEGER NOT NULL DEFAULT 80,
                hot_cold_strategy TEXT NOT NULL DEFAULT 'hot7d',
                quota_gb INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # 幂等迁移（2026-11-19）：双引擎保留期分开。
        # retain_days 管 OpenSearch 明细（占空间大），aggregate_retain_days 管
        # ClickHouse 聚合（列存压缩 8~10 倍，可留更久）。合用一个值会牺牲
        # 「明细 7 天 + 趋势 90 天」这类最有价值的组合。
        pg_exec(
            "ALTER TABLE storage_config "
            "ADD COLUMN IF NOT EXISTS aggregate_retain_days INTEGER NOT NULL DEFAULT 90"
        )
        # quota_gb = 0 表示「不限制（跟随物理卷容量）」，不给假配额
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS tenant_quotas (
                tenant_code TEXT PRIMARY KEY REFERENCES tenants(code) ON DELETE CASCADE,
                quota_gb INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # 容量趋势的真实来源：每次查询容量时按 scope 记录当天快照，运行天数够了就自动从
        # 「推算」切换为「实测」。scope 为租户编码或 all（全租户）。
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS storage_snapshots (
                snap_date DATE NOT NULL,
                scope TEXT NOT NULL DEFAULT 'all',
                used_bytes BIGINT NOT NULL DEFAULT 0,
                doc_count BIGINT NOT NULL DEFAULT 0,
                index_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (snap_date, scope)
            )
            """
        )

        for code, name in TENANT_SEEDS:
            pg_exec(
                "INSERT INTO tenants (code, name) VALUES (%s, %s) ON CONFLICT (code) DO NOTHING",
                (code, name),
            )
        for key, name, desc, perms, order in ROLE_SEEDS:
            pg_exec(
                """INSERT INTO roles (key, name, scope_desc, sort_order)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (key) DO NOTHING""",
                (key, name, desc, order),
            )
            for perm in perms:
                resource, action = perm.split(":", 1)
                pg_exec(
                    """INSERT INTO permissions (role_key, resource, action)
                       VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                    (key, resource, action),
                )

        # 种子用户：仅在账号不存在时创建，绝不覆盖已改过的密码 / 启停状态
        admin_pwd = os.getenv("AUTH_SEED_ADMIN_PASSWORD", "Admin@123")
        demo_pwd = os.getenv("AUTH_SEED_DEMO_PASSWORD", "Ops@12345")
        from .security import hash_password  # 延迟导入，避免循环依赖

        for account, name, role_key, tenant_code, enabled, phone, builtin in USER_SEEDS:
            exists = pg_scalar("SELECT 1 FROM users WHERE account=%s", (account,))
            if exists:
                continue
            tenant_id = pg_scalar("SELECT id FROM tenants WHERE code=%s", (tenant_code,))
            if tenant_id is None:
                continue
            pg_exec(
                """INSERT INTO users (tenant_id, account, name, password_hash, role_key,
                                      phone, enabled, is_builtin)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (tenant_id, account, name,
                 hash_password(admin_pwd if builtin else demo_pwd),
                 role_key, phone, enabled, builtin),
            )

        pg_exec("DELETE FROM system_params WHERE pkey = ANY(%s)", (list(RETIRED_PARAMS),))
        for key, value, ptype, label, note, options in PARAM_SEEDS:
            pg_exec(
                """INSERT INTO system_params (pkey, pvalue, ptype, label, note, options)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (pkey) DO NOTHING""",
                (key, value, ptype, label, note, pg_json(options)),
            )
        # 存储配置单行 + 各租户配额行；配额一律 0（不限制），不预置看似合理的假数值
        pg_exec("INSERT INTO storage_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        for t in pg_query("SELECT code FROM tenants ORDER BY id"):
            pg_exec(
                """INSERT INTO tenant_quotas (tenant_code) VALUES (%s)
                   ON CONFLICT (tenant_code) DO NOTHING""",
                (t["code"],),
            )
        print("[system-service] init_db done", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[system-service] init_db skipped: {e}", flush=True)


def pg_json(v: Any) -> str:
    import json

    return json.dumps(v, ensure_ascii=False)
