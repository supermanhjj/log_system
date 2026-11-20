"""解析规则与采集器凭证的持久化层（PostgreSQL）。

独立于 main.py 的 pg 工具，避免循环导入；沿用同一套 PG_DSN / 环境变量约定。
CRUD 频率很低（改规则才写），每次短连接即可；执行侧的 30 秒缓存才是热路径。

表建成后由 api_pipelines.py 暴露管理接口，服务端解析规则不再写死在
Fluent Bit 的 parsers.conf 里——改规则不用碰现场任何一台机器。
"""
import json
import os
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

PG_DSN = os.getenv(
    "INGEST_PG_DSN",
    f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
    f"user={os.getenv('POSTGRES_USER', 'loguser')} "
    f"password={os.getenv('POSTGRES_PASSWORD', 'logpass')} "
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')}",
)


def pg_query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def pg_exec(sql: str, params: tuple = ()) -> None:
    conn = psycopg2.connect(PG_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def init_tables() -> None:
    """幂等建表。失败不阻塞服务启动。"""
    pg_exec("""
        CREATE TABLE IF NOT EXISTS log_parsers (
            id          SERIAL PRIMARY KEY,
            code        TEXT NOT NULL UNIQUE,
            name        TEXT NOT NULL,
            ptype       TEXT NOT NULL,
            definition  JSONB NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            enabled     BOOLEAN NOT NULL DEFAULT true,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    pg_exec("""
        CREATE TABLE IF NOT EXISTS log_pipelines (
            id          SERIAL PRIMARY KEY,
            code        TEXT NOT NULL UNIQUE,
            name        TEXT NOT NULL,
            priority    INTEGER NOT NULL DEFAULT 100,
            match       JSONB NOT NULL DEFAULT '{}'::jsonb,
            parser_id   INTEGER REFERENCES log_parsers(id) ON DELETE SET NULL,
            mapping     JSONB NOT NULL DEFAULT '{}'::jsonb,
            tenant_code TEXT NOT NULL DEFAULT '',
            enabled     BOOLEAN NOT NULL DEFAULT true,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    pg_exec("""
        CREATE TABLE IF NOT EXISTS log_collectors (
            id           SERIAL PRIMARY KEY,
            code         TEXT NOT NULL UNIQUE,
            name         TEXT NOT NULL DEFAULT '',
            tenant_allow JSONB NOT NULL DEFAULT '[]'::jsonb,
            key_hash     TEXT NOT NULL DEFAULT '',
            last_seen    TIMESTAMPTZ,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


# ---- 解析器 ----
def list_parsers(enabled_only: bool = False) -> List[Dict[str, Any]]:
    where = "WHERE enabled" if enabled_only else ""
    return pg_query(f"SELECT * FROM log_parsers {where} ORDER BY id")


def upsert_parser(code: str, name: str, ptype: str, definition: Dict[str, Any],
                  description: str = "", enabled: bool = True) -> Dict[str, Any]:
    rows = pg_query("""
        INSERT INTO log_parsers (code, name, ptype, definition, description, enabled, updated_at)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s, now())
        ON CONFLICT (code) DO UPDATE SET
            name=EXCLUDED.name, ptype=EXCLUDED.ptype, definition=EXCLUDED.definition,
            description=EXCLUDED.description, enabled=EXCLUDED.enabled, updated_at=now()
        RETURNING id, code, name, ptype, definition, enabled
    """, (code, name, ptype, _json(definition), description, enabled))
    return rows[0]


def delete_parser(code: str) -> bool:
    pg_exec("DELETE FROM log_parsers WHERE code=%s", (code,))
    return True


# ---- 管线规则 ----
def list_pipelines(enabled_only: bool = False) -> List[Dict[str, Any]]:
    where = "WHERE p.enabled" if enabled_only else ""
    return pg_query(f"""
        SELECT p.*, r.code AS parser_code, r.ptype AS parser_type
        FROM log_pipelines p LEFT JOIN log_parsers r ON r.id = p.parser_id
        {where}
        ORDER BY p.priority, p.id
    """)


def upsert_pipeline(code: str, name: str, priority: int, match: Dict[str, Any],
                    parser_code: str, mapping: Dict[str, Any],
                    tenant_code: str = "", enabled: bool = True) -> Dict[str, Any]:
    rows = pg_query("""
        WITH parser AS (SELECT id FROM log_parsers WHERE code=%s)
        INSERT INTO log_pipelines
            (code, name, priority, match, parser_id, mapping, tenant_code, enabled, updated_at)
        SELECT %s, %s, %s, %s::jsonb, parser.id, %s::jsonb, %s, %s, now() FROM parser
        ON CONFLICT (code) DO UPDATE SET
            name=EXCLUDED.name, priority=EXCLUDED.priority, match=EXCLUDED.match,
            parser_id=EXCLUDED.parser_id, mapping=EXCLUDED.mapping,
            tenant_code=EXCLUDED.tenant_code, enabled=EXCLUDED.enabled, updated_at=now()
        RETURNING id, code, name, priority, match, parser_id, mapping, tenant_code, enabled
    """, (parser_code, code, name, priority, _json(match), _json(mapping),
          tenant_code, enabled))
    if not rows:
        raise ValueError(f"解析器不存在：{parser_code}")
    return rows[0]


def delete_pipeline(code: str) -> bool:
    pg_exec("DELETE FROM log_pipelines WHERE code=%s", (code,))
    return True


# ---- 采集器凭证（E 批次安全加固使用） ----
def list_collectors() -> List[Dict[str, Any]]:
    return pg_query("SELECT * FROM log_collectors ORDER BY id")


def upsert_collector(code: str, name: str, tenant_allow: List[str],
                     key_hash: str = "") -> Dict[str, Any]:
    rows = pg_query("""
        INSERT INTO log_collectors (code, name, tenant_allow, key_hash)
        VALUES (%s, %s, %s::jsonb, %s)
        ON CONFLICT (code) DO UPDATE SET
            name=EXCLUDED.name, tenant_allow=EXCLUDED.tenant_allow,
            key_hash=CASE WHEN EXCLUDED.key_hash='' THEN log_collectors.key_hash
                          ELSE EXCLUDED.key_hash END
        RETURNING id, code, name, tenant_allow, key_hash
    """, (code, name, json.dumps(tenant_allow or [], ensure_ascii=False), key_hash))
    return rows[0]


def seed_defaults() -> None:
    """播种 K8s 两类容器日志的内置规则：Java logback（业务）+ RFC3164（系统）。

    两条规则对应「采集器只搬原文、服务端解析」的边界：K8s 采集端只做 CRI 拆包
    与多行分帧，正文落在 log 字段；这里把正文抽成 level/source/host/message。

    时间字段的取舍（踩过坑，勿改）：
        @timestamp 一律不映射。K8s 容器日志的 CRI 时间带时区，是绝对时刻的
        唯一可信来源；正文里的时间戳没有时区（甚至 RFC3164 连年份都没有），
        映射进去会把正确时间改成错的（naive 时间被当 UTC，整体偏 8 小时）。
        正文时间只作为 attrs.body_ts 保留，供排障时对照。

    只插不改（ON CONFLICT DO NOTHING），避免一重启就冲掉现场调好的规则；
    唯一的例外是下面那条针对早期种子 mapping 的定点迁移。
    """
    pg_exec("""
        INSERT INTO log_parsers (code, name, ptype, definition, description)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (code) DO NOTHING
    """, (
        "java-logback",
        "Java logback 标准格式",
        "grok",
        json.dumps({
            "type": "grok",
            "pattern": (r"%{TIMESTAMP_ISO8601:ts}\s+\[%{JAVATHREAD:thread}\]\s+"
                        r"%{LOGLEVEL:level}\s+%{JAVACLASS:logger}\s+-\s+"
                        r"%{GREEDYDATA:message}"),
        }, ensure_ascii=False),
        "SG-I6000 等 SpringBoot 应用的默认 logback 输出；异常栈需采集端 java 多行分帧",
    ))
    pg_exec("""
        INSERT INTO log_pipelines
            (code, name, priority, match, parser_id, mapping, tenant_code)
        SELECT %s, %s, %s, %s::jsonb, p.id, %s::jsonb, %s
        FROM log_parsers p WHERE p.code=%s
        ON CONFLICT (code) DO NOTHING
    """, (
        "java-logback-business",
        "业务应用 logback（示例）",
        100,
        json.dumps({"log_class": "business", "source_type": "app"}, ensure_ascii=False),
        json.dumps({
            "level": "level",
            "source": "logger",
            "attrs.thread": "thread",
            "attrs.body_ts": "ts",
            "message": "message",
        }, ensure_ascii=False),
        "",
        "java-logback",
    ))

    # ---- 系统日志：RFC3164（K8s 容器内的 syslog 与主机 /var/log/syslog 同构）----
    #   Nov 20 02:41:37 i6000-node-01 authpriv.warn sshd: Failed password for ...
    pg_exec("""
        INSERT INTO log_parsers (code, name, ptype, definition, description)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (code) DO NOTHING
    """, (
        "syslog-rfc3164",
        "RFC3164 系统日志",
        "grok",
        json.dumps({
            "type": "grok",
            "pattern": (r"%{SYSLOGTIMESTAMP:body_ts}\s+%{HOSTNAME:host}\s+"
                        r"%{WORD:facility}\.%{WORD:severity_name}\s+"
                        r"(?P<source>[^\s:]+):?\s+%{GREEDYDATA:message}"),
        }, ensure_ascii=False),
        "syslogd/rsyslog 标准输出；level 由 facility.severity 归一（authpriv.warn → WARN）",
    ))
    pg_exec("""
        INSERT INTO log_pipelines
            (code, name, priority, match, parser_id, mapping, tenant_code)
        SELECT %s, %s, %s, %s::jsonb, p.id, %s::jsonb, %s
        FROM log_parsers p WHERE p.code=%s
        ON CONFLICT (code) DO NOTHING
    """, (
        "syslog-system",
        "系统日志 RFC3164",
        100,
        json.dumps({"log_class": "system", "source_type": "syslog"}, ensure_ascii=False),
        json.dumps({
            "level": "severity_name",
            "source": "source",
            "host": "host",
            "attrs.facility": "facility",
            "attrs.severity_name": "severity_name",
            "attrs.body_ts": "body_ts",
            "message": "message",
        }, ensure_ascii=False),
        "",
        "syslog-rfc3164",
    ))

    # ---- 定点迁移：早期 java-logback 种子把 @timestamp 映射到正文时间 ----
    # 只改「mapping 仍等于旧种子」的行：现场自己调过的规则（mapping 已变）不动。
    pg_exec("""
        UPDATE log_pipelines
           SET mapping = %s::jsonb
         WHERE code = %s
           AND mapping->>'@timestamp' = 'ts'
    """, (
        json.dumps({
            "level": "level",
            "source": "logger",
            "attrs.thread": "thread",
            "attrs.body_ts": "ts",
            "message": "message",
        }, ensure_ascii=False),
        "java-logback-business",
    ))
