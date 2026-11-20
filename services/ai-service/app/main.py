"""ai-service —— AI 能力配置中心。

对应前端「智能管理」页（/ai/management）。三类能力：
1. 模型来源接入：商用 API（DeepSeek 等）/ 本地自定义（Ollama、oMLX）。
   支持多实例、默认实例、模型选择、真实连通性探测。
2. PPL 语法规则库：命令、字段别名、业务词映射、时间规则、示例对、禁止项；
   可从 ingest-service /meta 按真实 mapping 同步字段，供 NL→PPL 生成注入。
3. AI 能力注册表：为智能检索 / 智能解读 / 辅助决策 / 深度分析预留配置扩展位。

边界（务必如实理解）：
- 2026-11-14 起提供 `POST /nl2ppl` 运行时生成端点：规则库注入 prompt → 已配置模型 → 语句规范化闸门；
  仅生成 / 校验语句，**不执行检索、不访问日志原文**。
- 不编造指标：连通性测试真实发 HTTP，失败如实返回原因。
- 种子语法规则是「可编辑的默认模板」，不是已验证的全量 PPL 语法；字段别名可用 /grammar/sync-fields 从真实 mapping 覆盖。
- 2026-11-17 起移除「WorkBuddy / CodeBuddy CLI」接入方式（容器侧挂载与宿主机桥接一并下线），
  模型接入仅保留商用 API 与本地自定义两类。
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from . import nl2ppl

PG_DSN = os.getenv(
    "AI_PG_DSN",
    f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
    f"user={os.getenv('POSTGRES_USER', 'loguser')} "
    f"password={os.getenv('POSTGRES_PASSWORD', 'logpass')} "
    f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')}",
)
INGEST_META_URL = os.getenv("AI_INGEST_META_URL", "http://ingest-service:8000/meta")
CST = timezone(timedelta(hours=8))

app = FastAPI(title="ai-service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

KINDS = [
    {
        "key": "commercial",
        "label": "商用 API",
        "desc": "DeepSeek 等云端大模型，走OpenAI兼容协议",
        "hint": "需填 Base URL、模型名、API Key。",
    },
    {
        "key": "local",
        "label": "本地自定义",
        "desc": "Ollama、oMLX 等本机 / 内网自建模型服务",
    },
]
KIND_KEYS = {k["key"] for k in KINDS}

PROVIDER_PRESETS = {
    "deepseek": {"label": "DeepSeek", "kind": "commercial", "base_url": "https://api.deepseek.com",
                 "model_name": "deepseek-chat", "probe_path": "/models"},
    "openai-compatible": {"label": "OpenAI 兼容接口", "kind": "commercial", "base_url": "",
                          "model_name": "", "probe_path": "/v1/models"},
    "ollama": {"label": "Ollama", "kind": "local", "base_url": "http://host.docker.internal:11434",
               "model_name": "qwen2.5:7b", "probe_path": "/api/tags"},
    "omlx": {"label": "oMLX (MLX)", "kind": "local", "base_url": "http://host.docker.internal:8080",
             "model_name": "", "probe_path": "/v1/models"},
}
DEFAULT_PROBE_PATH = {
    "deepseek": "/models",
    "openai-compatible": "/v1/models",
    "ollama": "/api/tags",
    "omlx": "/v1/models",
    "custom": "/v1/models",
}

# CLI 可选模型清单已随 WorkBuddy CLI 接入方式一并移除（2026-11-17）。

GRAMMAR_CATEGORIES = [
    {"key": "command", "label": "命令白名单"},
    {"key": "field_alias", "label": "字段别名"},
    {"key": "term_mapping", "label": "业务词映射"},
    {"key": "time_rule", "label": "时间规则"},
    {"key": "example", "label": "NL→PPL 示例"},
    {"key": "deny", "label": "禁止项"},
]
CATEGORY_KEYS = {c["key"] for c in GRAMMAR_CATEGORIES}

# 默认模板：与当前平台真实字段（fluentbit/parsers.conf）对齐，全部可编辑/可删除。
DEFAULT_GRAMMAR = [
    ("command", "source", "source=<index>", "默认模板；生成的语句默认不带 source= 段，索引由检索网关按租户补齐"),
    ("command", "where", "where <条件>", "默认模板；条件过滤，仅只读"),
    ("command", "stats", "stats count() by <字段>", "默认模板；聚合统计"),
    ("command", "fields", "fields <字段列表>", "默认模板；输出列裁剪"),
    ("command", "sort", "sort <字段>", "默认模板；排序"),
    ("command", "head", "head 100", "默认模板；结果条数上限"),
    ("field_alias", "级别", "level", "默认模板；可用「同步索引字段」按真实 mapping 覆盖"),
    ("field_alias", "服务", "source", "默认模板"),
    ("field_alias", "主机", "host", "默认模板"),
    ("field_alias", "状态码", "status", "默认模板"),
    ("field_alias", "链路ID", "traceId", "默认模板"),
    ("field_alias", "日志内容", "message", "默认模板"),
    ("field_alias", "原始日志", "raw", "默认模板"),
    # v2.1 多类型采集：分类维度别名。值取枚举（system/middleware/database/business）
    ("field_alias", "日志类型", "log_class", "默认模板；取值 system / middleware / database / business"),
    ("field_alias", "来源类型", "source_type", "默认模板；如 syslog、nginx-access、mysql-slow、redis、app"),
    ("field_alias", "应用", "app", "默认模板；中间件/数据库实例所属应用名"),
    ("term_mapping", "系统日志", "log_class = 'system'", "默认模板"),
    ("term_mapping", "中间件日志", "log_class = 'middleware'", "默认模板"),
    ("term_mapping", "数据库日志", "log_class = 'database'", "默认模板"),
    ("term_mapping", "业务日志", "log_class = 'business'", "默认模板"),
    ("term_mapping", "慢查询", "source_type = 'mysql-slow'", "默认模板；对应 MySQL slow query log"),
    ("term_mapping", "登录失败", "source_type = 'auth' and level = 'WARN'", "默认模板；系统认证日志"),
    ("term_mapping", "错误日志", "level = 'ERROR'", "默认模板；按实际问法持续补充"),
    ("term_mapping", "5xx", "status >= 500", "默认模板"),
    ("term_mapping", "超时", "message 关键字 timeout", "默认模板；PPL 文本匹配写法需实测后固化"),
    ("time_rule", "最近10分钟", "由后端换算为绝对时间范围，不由模型计算", "确定性规则"),
    ("time_rule", "今天", "东八区自然日 00:00:00 起", "确定性规则"),
    ("example", "查询错误日志", "where level = 'ERROR' | head 100", "默认模板；不含索引前缀，索引由检索网关按租户补齐"),
    ("example", "按级别统计", "stats count() by level | head 100", "默认模板；不含索引前缀，索引由检索网关按租户补齐"),
    ("deny", "写操作关键字", "drop / delete / update / insert / create / alter / grant", "安全闸门"),
    ("deny", "跨索引 source", "仅允许 logs-<tenant> / logs-*", "安全闸门"),
    ("deny", "无 head 全表扫描", "生成语句必须带结果上限", "安全闸门"),
]

CAPABILITY_SEEDS = [
    ("nl2ppl", "智能检索", "自然语言转 PPL 查询语句，生成后经校验闸门回填检索框", 1),
    ("log_interpret", "智能解读", "多源日志查询与异常日志语义解读", 2),
    ("assist_decision", "辅助决策", "故障模式匹配、预制规则分析、处置方案生成", 3),
    ("deep_analysis", "深度分析", "自主规划排查思路、动态智能分析、处置方案生成", 4),
]


# ---------------------------------------------------------------------------
# PostgreSQL（沿用其余微服务的惰性连接 + startup 建表惯例）
# ---------------------------------------------------------------------------
_pg_conn = None


def pg_conn():
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


def init_db() -> None:
    """建表 + 写入可编辑的默认模板；失败不阻塞服务启动。"""
    try:
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS ai_model_providers (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'custom',
                base_url TEXT NOT NULL DEFAULT '',
                model_name TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                cli_path TEXT NOT NULL DEFAULT '',
                cli_args TEXT NOT NULL DEFAULT '',
                work_dir TEXT NOT NULL DEFAULT '',
                timeout INTEGER NOT NULL DEFAULT 30,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                note TEXT NOT NULL DEFAULT '',
                extra JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS ai_grammar_rules (
                id SERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                rule_key TEXT NOT NULL,
                rule_value TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 0,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                note TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (category, rule_key)
            )
            """
        )
        pg_exec(
            """
            CREATE TABLE IF NOT EXISTS ai_capabilities (
                cap_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned',
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                config JSONB NOT NULL DEFAULT '{}'::jsonb,
                note TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        for key, name, desc, order in CAPABILITY_SEEDS:
            pg_exec(
                """INSERT INTO ai_capabilities (cap_key, name, description, status, enabled, sort_order)
                   VALUES (%s, %s, %s, 'planned', FALSE, %s)
                   ON CONFLICT (cap_key) DO NOTHING""",
                (key, name, desc, order),
            )
        # NL→PPL 运行时已落地：只更新状态，不改动用户已保存的 enabled 开关
        pg_exec("UPDATE ai_capabilities SET status='available' WHERE cap_key='nl2ppl' AND status<>'available'")
        for category, key, value, note in DEFAULT_GRAMMAR:
            pg_exec(
                """INSERT INTO ai_grammar_rules (category, rule_key, rule_value, note)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (category, rule_key) DO NOTHING""",
                (category, key, value, note),
            )
        print("[ai-service] init_db done", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[ai-service] init_db skipped: {e}", flush=True)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def now_str() -> str:
    return datetime.now(tz=CST).strftime("%Y-%m-%d %H:%M:%S")


def snippet(text: str, limit: int = 240) -> str:
    return (text or "").strip().replace("\n", " ")[:limit]


def mask_key(key: str) -> str:
    """API Key 只在服务端保存，列表/详情一律脱敏。"""
    k = key or ""
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return f"{k[:3]}****{k[-4:]}"


def public_provider(row: Dict[str, Any]) -> Dict[str, Any]:
    r = dict(row)
    # CLI 接入已下线：cli_path / cli_args / work_dir 仅保留在 DB 列中兼容旧数据，API 不再暴露
    for legacy in ("cli_args", "cli_path", "work_dir"):
        r.pop(legacy, None)
    r["api_key"] = mask_key(r.get("api_key") or "")
    r["api_key_set"] = bool(row.get("api_key"))
    for k in ("created_at", "updated_at"):
        if r.get(k) is not None:
            r[k] = str(r[k])
    if isinstance(r.get("extra"), str):
        try:
            r["extra"] = json.loads(r["extra"])
        except ValueError:
            r["extra"] = {}
    return r


def as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            d = json.loads(value)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {}


def require_kind(kind: str) -> None:
    if kind not in KIND_KEYS:
        raise HTTPException(status_code=400, detail=f"kind 必须是 {sorted(KIND_KEYS)} 之一")


def require_category(category: str) -> None:
    if category not in CATEGORY_KEYS:
        raise HTTPException(status_code=400, detail=f"category 必须是 {sorted(CATEGORY_KEYS)} 之一")


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ProviderIn(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    name: str
    kind: str
    provider: str = "custom"
    base_url: str = ""
    model_name: str = ""
    api_key: str = ""
    timeout: int = 30
    enabled: bool = True
    note: str = ""
    extra: Dict[str, Any] = {}


class GrammarIn(BaseModel):
    category: str
    rule_key: str
    rule_value: str = ""
    priority: int = 0
    enabled: bool = True
    note: str = ""


class ImportIn(BaseModel):
    rules: List[GrammarIn]
    mode: str = "merge"


class PreviewIn(BaseModel):
    question: str


class Nl2PplIn(BaseModel):
    question: str
    provider_id: Optional[int] = None
    tenant: Optional[str] = None
    model_name: str = ""          # 预览时临时覆盖模型 ID，不写回配置库
    include_prompt: bool = False


class CapabilityIn(BaseModel):
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# 基础信息
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "ai-service", "version": "0.1.0"}


@app.get("/")
def root():
    return {
        "service": "ai-service",
        "version": "0.1.0",
        "scope": "模型接入配置 / PPL 语法规则库 / AI 能力注册表 / NL→PPL 生成（仅生成与校验，不执行检索）",
        "endpoints": ["/meta", "/providers", "/grammar", "/capabilities", "/nl2ppl"],
    }


@app.get("/meta")
def meta():
    counts: Dict[str, Any] = {}
    try:
        counts = {
            "providers": pg_query("SELECT count(*) AS n FROM ai_model_providers")[0]["n"],
            "grammar_rules": pg_query("SELECT count(*) AS n FROM ai_grammar_rules")[0]["n"],
            "capabilities": pg_query("SELECT count(*) AS n FROM ai_capabilities")[0]["n"],
        }
    except Exception as e:  # noqa: BLE001
        counts = {"error": f"配置库暂不可用：{e}"}
    return {
        "kinds": KINDS,
        "presets": PROVIDER_PRESETS,
        "grammarCategories": GRAMMAR_CATEGORIES,
        "capabilityStatus": {
            "planned": "仅登记配置，运行时能力待接入",
            "available": "运行时能力已接入",
        },
        "counts": counts,
        "note": "连通性测试为真实探测；语法规则为可编辑默认模板，可用 /grammar/sync-fields 同步真实字段。",
    }


# ---------------------------------------------------------------------------
# 模型来源接入
# ---------------------------------------------------------------------------
@app.get("/providers")
def list_providers(kind: str = Query("")):
    if kind:
        require_kind(kind)
        rows = pg_query("SELECT * FROM ai_model_providers WHERE kind=%s ORDER BY id", (kind,))
    else:
        rows = pg_query("SELECT * FROM ai_model_providers ORDER BY id")
    items = [public_provider(r) for r in rows]
    return {
        "items": items,
        "total": len(items),
        "byKind": {k: sum(1 for i in items if i["kind"] == k) for k in sorted(KIND_KEYS)},
        "defaultId": next((i["id"] for i in items if i["is_default"]), None),
    }


def _provider_values(req: ProviderIn, key_value: str) -> tuple:
    return (
        req.name.strip(), req.kind, req.provider.strip() or "custom",
        req.base_url.strip(), req.model_name.strip(), key_value,
        max(1, min(int(req.timeout or 30), 300)), req.enabled, req.note.strip(),
        psycopg2.extras.Json(req.extra or {}),
    )


@app.post("/providers")
def create_provider(req: ProviderIn):
    require_kind(req.kind)
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="名称不能为空")
    if req.kind in ("commercial", "local") and not req.base_url.strip().lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="商用 API / 本地模型需填 http(s) 开头的 Base URL")
    try:
        rows = pg_query(
            """INSERT INTO ai_model_providers
               (name, kind, provider, base_url, model_name, api_key,
                timeout, enabled, note, extra)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            _provider_values(req, req.api_key.strip()),
        )
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="同名接入已存在")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"保存失败：{e}")
    return public_provider(rows[0])


@app.put("/providers/{pid:int}")
def update_provider(pid: int, req: ProviderIn):
    require_kind(req.kind)
    old = pg_query("SELECT * FROM ai_model_providers WHERE id=%s", (pid,))
    if not old:
        raise HTTPException(status_code=404, detail="接入配置不存在")
    # 空或脱敏占位均表示「保持原密钥不变」
    incoming = req.api_key.strip()
    key_value = old[0]["api_key"] if (not incoming or incoming == mask_key(old[0]["api_key"])) else incoming
    rows = pg_query(
        """UPDATE ai_model_providers SET
           name=%s, kind=%s, provider=%s, base_url=%s, model_name=%s, api_key=%s,
           timeout=%s, enabled=%s, note=%s,
           extra=%s, updated_at=now()
           WHERE id=%s RETURNING *""",
        _provider_values(req, key_value) + (pid,),
    )
    return public_provider(rows[0])


@app.delete("/providers/{pid:int}")
def delete_provider(pid: int):
    rows = pg_query("DELETE FROM ai_model_providers WHERE id=%s RETURNING id", (pid,))
    if not rows:
        raise HTTPException(status_code=404, detail="接入配置不存在")
    return {"status": "deleted", "id": pid}


@app.post("/providers/{pid:int}/default")
def set_default_provider(pid: int):
    if not pg_query("SELECT id FROM ai_model_providers WHERE id=%s", (pid,)):
        raise HTTPException(status_code=404, detail="接入配置不存在")
    pg_exec("UPDATE ai_model_providers SET is_default=FALSE")
    pg_exec("UPDATE ai_model_providers SET is_default=TRUE, updated_at=now() WHERE id=%s", (pid,))
    return {"status": "ok", "defaultId": pid}


def probe(row: Dict[str, Any]):
    """真实探测：对商用 / 本地接入的 HTTP 端点发一次真实请求，失败如实返回原因。"""
    kind = row["kind"]
    ex = as_dict(row.get("extra"))
    if kind in ("commercial", "local"):
        base = (row.get("base_url") or "").rstrip("/")
        if not base.lower().startswith(("http://", "https://")):
            return False, "Base URL 非法（需 http:// 或 https://）", {}
        path = ex.get("probe_path") or DEFAULT_PROBE_PATH.get(row.get("provider"), "/v1/models")
        url = base + str(path)
        headers = dict(ex.get("headers") or {})
        if row.get("api_key"):
            headers["Authorization"] = f"Bearer {row['api_key']}"
        try:
            resp = httpx.get(url, headers=headers, timeout=min(int(row.get("timeout") or 30), 15))
        except Exception as e:  # noqa: BLE001
            return False, f"请求失败：{type(e).__name__}: {e}", {"url": url}
        model_count = None
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                for k in ("data", "models"):
                    if isinstance(payload.get(k), list):
                        model_count = len(payload[k])
                        break
        except ValueError:
            pass
        detail = f"HTTP {resp.status_code} · {url}"
        if model_count is not None:
            detail += f" · 返回 {model_count} 个模型"
        detail += f" · 响应：{snippet(resp.text)}"
        return resp.is_success, detail, {"url": url, "statusCode": resp.status_code, "modelCount": model_count}

    return False, f"不支持的接入类型：{kind}", {}


@app.post("/providers/{pid:int}/test")
def test_provider(pid: int):
    rows = pg_query("SELECT * FROM ai_model_providers WHERE id=%s", (pid,))
    if not rows:
        raise HTTPException(status_code=404, detail="接入配置不存在")
    started = time.time()
    ok, detail, extra = probe(rows[0])
    elapsed = int((time.time() - started) * 1000)
    result = {
        "ok": ok, "kind": rows[0]["kind"], "provider": rows[0]["provider"],
        "detail": detail, "elapsedMs": elapsed, "checkedAt": now_str(), **extra,
    }
    recent = {"ok": ok, "detail": snippet(detail, 200), "elapsedMs": elapsed, "checkedAt": result["checkedAt"]}
    try:
        pg_exec(
            "UPDATE ai_model_providers SET extra = COALESCE(extra,'{}'::jsonb) || %s::jsonb, updated_at=now() WHERE id=%s",
            (psycopg2.extras.Json({"lastProbe": recent}), pid),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[ai-service] save probe failed: {e}", flush=True)
    return result


# ---------------------------------------------------------------------------
# PPL 语法规则库（静态路径先注册，避免与 /grammar/{id} 冲突）
# ---------------------------------------------------------------------------
@app.get("/grammar")
def list_grammar(category: str = Query(""), keyword: str = Query(""), enabled: str = Query("")):
    sql = "SELECT * FROM ai_grammar_rules WHERE 1=1"
    params: List[Any] = []
    if category:
        require_category(category)
        sql += " AND category=%s"
        params.append(category)
    if keyword:
        sql += " AND (rule_key ILIKE %s OR rule_value ILIKE %s)"
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if enabled in ("true", "false"):
        sql += " AND enabled=%s"
        params.append(enabled == "true")
    sql += " ORDER BY category, priority DESC, id"
    rows = pg_query(sql, tuple(params))
    items = [dict(r) for r in rows]
    for i in items:
        if i.get("updated_at") is not None:
            i["updated_at"] = str(i["updated_at"])
    counts = {}
    for c in GRAMMAR_CATEGORIES:
        counts[c["key"]] = sum(1 for i in items if i["category"] == c["key"])
    return {"items": items, "total": len(items), "byCategory": counts}


@app.post("/grammar")
def create_grammar(req: GrammarIn):
    require_category(req.category)
    if not req.rule_key.strip():
        raise HTTPException(status_code=400, detail="规则键不能为空")
    try:
        rows = pg_query(
            """INSERT INTO ai_grammar_rules (category, rule_key, rule_value, priority, enabled, note)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            (req.category, req.rule_key.strip(), req.rule_value, req.priority, req.enabled, req.note),
        )
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="同分类下规则键已存在")
    return dict(rows[0])


@app.get("/grammar/export")
def export_grammar():
    rows = pg_query("SELECT category, rule_key, rule_value, priority, enabled, note FROM ai_grammar_rules ORDER BY category, id")
    payload = {"version": 1, "exportedAt": now_str(), "rules": [dict(r) for r in rows]}
    content = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="ai-grammar-rules.json"'},
    )


@app.post("/grammar/import")
def import_grammar(req: ImportIn):
    if req.mode not in ("merge", "replace"):
        raise HTTPException(status_code=400, detail="mode 只能是 merge 或 replace")
    if req.mode == "replace":
        pg_exec("DELETE FROM ai_grammar_rules")
    created, updated = 0, 0
    for r in req.rules:
        require_category(r.category)
        if not r.rule_key.strip():
            continue
        exists = pg_query(
            "SELECT id FROM ai_grammar_rules WHERE category=%s AND rule_key=%s",
            (r.category, r.rule_key.strip()),
        )
        if exists:
            pg_exec(
                """UPDATE ai_grammar_rules SET rule_value=%s, priority=%s, enabled=%s, note=%s, updated_at=now()
                   WHERE category=%s AND rule_key=%s""",
                (r.rule_value, r.priority, r.enabled, r.note, r.category, r.rule_key.strip()),
            )
            updated += 1
        else:
            pg_exec(
                """INSERT INTO ai_grammar_rules (category, rule_key, rule_value, priority, enabled, note)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (r.category, r.rule_key.strip(), r.rule_value, r.priority, r.enabled, r.note),
            )
            created += 1
    return {"created": created, "updated": updated, "mode": req.mode}


@app.post("/grammar/sync-fields")
def sync_fields(tenant: str = Query("i6000")):
    """从 ingest-service /meta 读真实 mapping，写入字段别名（不编造字段）。"""
    try:
        resp = httpx.get(INGEST_META_URL, params={"tenant": tenant}, timeout=8)
        resp.raise_for_status()
        meta_data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取 ingest-service /meta 失败：{e}")
    fields = meta_data.get("fields") or {}
    normalized: Dict[str, str] = {}
    if isinstance(fields, dict):
        for name, info in fields.items():
            normalized[str(name)] = str(info.get("type")) if isinstance(info, dict) else str(info)
    elif isinstance(fields, list):
        for f in fields:
            normalized[str(f.get("name", ""))] = str(f.get("type", "")) if isinstance(f, dict) else ""
    # text 字段的 .keyword 子字段不是独立业务字段，不作为别名入库；
    # 租户字段同样排除：租户是检索上下文不是查询条件，导进来只会诱导模型生成无效过滤
    normalized = {
        k: v for k, v in normalized.items()
        if k and not k.endswith(".keyword") and k not in nl2ppl.TENANT_FIELDS
    }
    if not normalized:
        raise HTTPException(status_code=502, detail="/meta 未返回字段清单，未写入任何规则")
    for name, ftype in normalized.items():
        pg_exec(
            """INSERT INTO ai_grammar_rules (category, rule_key, rule_value, priority, enabled, note)
               VALUES ('field_alias', %s, %s, 10, TRUE, %s)
               ON CONFLICT (category, rule_key) DO UPDATE
               SET note=EXCLUDED.note, updated_at=now()""",
            (name, name, f"来自 {tenant} 索引真实 mapping（{ftype}）；同步不会覆盖人工编辑的映射值"),
        )
    return {"synced": len(normalized), "source": "ingest-service /meta",
            "tenant": tenant, "index": meta_data.get("index"), "fields": sorted(normalized.keys())}


@app.post("/grammar/preview")
def preview_grammar(req: PreviewIn):
    """规则键字面命中预览：展示将注入模型的语法片段，不调用模型。"""
    q = (req.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="question 不能为空")
    rows = pg_query(
        """SELECT * FROM ai_grammar_rules
           WHERE enabled=TRUE AND category IN ('term_mapping','field_alias','example','time_rule')
           ORDER BY priority DESC, id"""
    )
    hits = [r for r in rows if r["rule_key"] and r["rule_key"] in q]
    lines = [f"[{r['category']}] {r['rule_key']} → {r['rule_value']}" for r in hits]
    return {
        "question": q,
        "matched": len(hits),
        "hits": [{"category": r["category"], "ruleKey": r["rule_key"], "ruleValue": r["rule_value"]} for r in hits],
        "promptContext": "\n".join(lines),
        "note": "仅做规则键字面命中预览，不调用模型；真实转换请点「转换为 PPL」。",
    }


@app.post("/nl2ppl")
def nl_to_ppl(req: Nl2PplIn):
    """自然语言 → PPL：规则库注入 + 已配置模型 + 后端规范化闸门；失败按 stage 如实返回。"""
    q = (req.question or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="question 不能为空")
    if req.provider_id:
        rows = pg_query("SELECT * FROM ai_model_providers WHERE id=%s", (req.provider_id,))
    else:
        rows = pg_query(
            "SELECT * FROM ai_model_providers WHERE enabled=TRUE ORDER BY is_default DESC, id LIMIT 1"
        )
    if not rows:
        return {
            "ok": False, "stage": "config", "question": q,
            "error": "指定的模型接入不存在" if req.provider_id else "没有已启用的模型接入配置",
            "detail": "", "hint": "到「模型接入」新增或启用一个接入", "elapsedMs": 0,
        }
    try:
        rules = pg_query(
            """SELECT category, rule_key, rule_value, enabled FROM ai_grammar_rules
               WHERE enabled=TRUE ORDER BY priority DESC, id"""
        )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False, "stage": "grammar", "question": q,
            "error": f"读取 PPL 语法规则库失败：{e}", "detail": "", "hint": "", "elapsedMs": 0,
        }
    # 租户清单用于「问句点名租户 vs 当前检索租户」一致性提示；查不到不影响主流程
    try:
        tenants = pg_query("SELECT code, name FROM tenants WHERE enabled ORDER BY id")
    except Exception:  # noqa: BLE001
        tenants = []
    return nl2ppl.generate(
        q, rows[0], rules, tenant=(req.tenant or "i6000").strip(),
        model_name=req.model_name, include_prompt=req.include_prompt,
        tenants=tenants,
    )


@app.put("/grammar/{rid:int}")
def update_grammar(rid: int, req: GrammarIn):
    require_category(req.category)
    rows = pg_query(
        """UPDATE ai_grammar_rules SET category=%s, rule_key=%s, rule_value=%s,
           priority=%s, enabled=%s, note=%s, updated_at=now()
           WHERE id=%s RETURNING *""",
        (req.category, req.rule_key.strip(), req.rule_value, req.priority, req.enabled, req.note, rid),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="规则不存在")
    return dict(rows[0])


@app.delete("/grammar/{rid:int}")
def delete_grammar(rid: int):
    rows = pg_query("DELETE FROM ai_grammar_rules WHERE id=%s RETURNING id", (rid,))
    if not rows:
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"status": "deleted", "id": rid}


# ---------------------------------------------------------------------------
# AI 能力注册表（后续扩展位）
# ---------------------------------------------------------------------------
@app.get("/capabilities")
def list_capabilities():
    rows = pg_query("SELECT * FROM ai_capabilities ORDER BY sort_order, cap_key")
    items = [dict(r) for r in rows]
    for i in items:
        if i.get("updated_at") is not None:
            i["updated_at"] = str(i["updated_at"])
    return {"items": items, "total": len(items),
            "note": "status=available 表示运行时已接入；nl2ppl（自然语言转 PPL）已可用，其余能力待接入。"}


@app.put("/capabilities/{key}")
def update_capability(key: str, req: CapabilityIn):
    if not pg_query("SELECT cap_key FROM ai_capabilities WHERE cap_key=%s", (key,)):
        raise HTTPException(status_code=404, detail="能力不存在")
    sets, params = [], []
    if req.enabled is not None:
        sets.append("enabled=%s")
        params.append(req.enabled)
    if req.config is not None:
        sets.append("config=%s")
        params.append(psycopg2.extras.Json(req.config))
    if req.note is not None:
        sets.append("note=%s")
        params.append(req.note)
    if not sets:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    sets.append("updated_at=now()")
    params.append(key)
    rows = pg_query(f"UPDATE ai_capabilities SET {', '.join(sets)} WHERE cap_key=%s RETURNING *", tuple(params))
    return dict(rows[0])
