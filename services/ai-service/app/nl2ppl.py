"""ai-service / NL→PPL 运行时。

职责（与配置中心 main.py 解耦，规则由调用方从 PG 传入，便于独立验证）：
1. build_prompt：把 PPL 语法规则库按类别注入 prompt，连同当前时间一起交给模型；
2. invoke_model：商用 / 本地接入统一走 OpenAI 兼容 /chat/completions，失败如实抛出 stage/原因；
3. normalize：模型输出按「不可信输入」处理——剥离模型自带的 source= 索引段
   （索引由检索网关按租户补齐）、命令白名单、拒绝写操作、补 head 上限、未知字段告警；
4. resolve_time：时间词由后端确定性换算为绝对区间，不让模型算数。

边界：本模块只生成并校验语句，不执行检索、不访问日志原文。
WorkBuddy / CodeBuddy CLI 接入方式已于 2026-11-17 移除，此处只保留 HTTP 模型接入。
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

CST = timezone(timedelta(hours=8))
MAX_HEAD = 500
DEFAULT_HEAD = 100
WRITE_WORDS = {"drop", "delete", "update", "insert", "create", "alter", "grant",
               "truncate", "replace", "remove", "put", "post", "patch"}
# 基础字段：动态 mapping 下允许时间字段与常见系统字段，其余以规则库 field_alias 为准
BASE_FIELDS = {"@timestamp", "_time", "level", "status", "source", "host",
               "message", "raw", "traceId", "tenant"}
# 从条件里抽字段时要排除的 PPL 关键字 / 函数名 / 运算符
NON_FIELD_TOKENS = {
    "and", "or", "not", "like", "in", "is", "null", "true", "false", "as", "by",
    "asc", "desc", "count", "sum", "avg", "min", "max", "distinct_count", "dc",
    "span", "eval", "where", "fields", "stats", "sort", "head", "source", "case",
    "when", "then", "else", "end", "if", "abs", "ceil", "floor", "round", "concat",
    "lower", "upper", "substr", "length", "now", "timestamp", "date_format",
}


class Nl2PplError(Exception):
    """带阶段与修复建议的失败，便于前端如实展示。"""

    def __init__(self, stage: str, message: str, detail: str = "", hint: str = ""):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.detail = detail
        self.hint = hint


def snippet(text: str, limit: int = 400) -> str:
    return (text or "").strip().replace("\n", " ")[:limit]


# ---------------------------------------------------------------------------
# 1. 规则库 → prompt
# ---------------------------------------------------------------------------
CATEGORY_TITLES = {
    "command": "可用命令（只能使用以下命令，管道 | 分隔）",
    "field_alias": "可用字段 / 中文别名（只能使用右列字段名，不要杜撰）",
    "term_mapping": "业务词映射（左列说法应翻译为右列条件）",
    "time_rule": "时间规则（不要自己算时间，只需在 timeRule 回报命中的时间词）",
    "example": "已验证的 NL→PPL 示例",
    "deny": "禁止项",
}


def group_rules(rules: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, str]]]:
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for r in rules:
        cat = str(r.get("category") or "")
        grouped.setdefault(cat, []).append({
            "ruleKey": str(r.get("rule_key") or ""),
            "ruleValue": str(r.get("rule_value") or ""),
        })
    return grouped


def matched_rules(question: str, rules: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """规则键字面命中，用于展示「本次注入了哪些规则」。"""
    return [
        {"category": str(r["category"]), "ruleKey": str(r["rule_key"]),
         "ruleValue": str(r.get("rule_value") or "")}
        for r in rules
        if r.get("rule_key") and str(r["rule_key"]) in question
    ]


def allowed_commands(rules: List[Dict[str, Any]]) -> set:
    cmds = {str(r["rule_key"]).strip().lower() for r in rules
            if r.get("category") == "command" and r.get("enabled", True)}
    return cmds or {"source", "where", "fields", "stats", "sort", "head"}


def allowed_fields(rules: List[Dict[str, Any]]) -> set:
    fields = set(BASE_FIELDS)
    for r in rules:
        if r.get("category") == "field_alias" and r.get("enabled", True):
            v = str(r.get("rule_value") or "").strip()
            if re.fullmatch(r"[A-Za-z_@][A-Za-z0-9_.@]*", v):
                fields.add(v)
    return fields


def build_prompt(question: str, rules: List[Dict[str, Any]],
                 now: Optional[datetime] = None) -> str:
    now = now or datetime.now(tz=CST)
    grouped = group_rules(rules)
    cmds = sorted(allowed_commands(rules))
    sections = []
    for cat, title in CATEGORY_TITLES.items():
        items = grouped.get(cat) or []
        if not items:
            continue
        body = "\n".join(
            f"- {i['ruleKey']}" + (f" → {i['ruleValue']}" if i["ruleValue"] else "")
            for i in items
        )
        sections.append(f"【{title}】\n{body}")

    schema = (
        '{"ppl":"<PPL 语句>", "explanation":"<一句话说明翻译依据>", '
        '"assumptions":["<假设，可为空数组>"], "timeRule":"<命中的时间词，无则空字符串>"}'
    )
    return "\n\n".join([
        "你是日志平台的 PPL 检索语句生成器。只输出一个 JSON 对象，不要输出任何解释文字、"
        "不要使用 Markdown 代码块。",
        f"输出格式：{schema}",
        "【硬约束】",
        "- 查询语言为 OpenSearch PPL，管道用 | 分隔。**不要输出 source= 开头的索引段**，"
        "语句直接从 where / stats / fields / sort 等命令开始；索引由检索网关按当前租户自动补齐，"
        "禁止在语句中写死任何租户名或索引名。",
        f"- 只能使用这些命令：{', '.join(cmds)}。只生成只读查询，禁止任何写操作。",
        "- 语句末尾必须带 head 加数字（结果上限），没有就补 head 100。",
        "- 时间范围不要写进 PPL：后端会按当前时间确定性换算；只需在 timeRule 字段回报"
        "命中的时间词（如「最近10分钟」「今天」），没有时间词就填空字符串。",
        "- 只有当用户问句里明确提到「来源 / 服务 / 索引」时，才按业务词映射把它翻译成 where 里的"
        "字段过滤条件；问句没提到来源时，语句中禁止出现 source 字段，也不要输出任何索引名。",
        "- 只使用下面列出的字段名，字段名区分大小写；遇到无法确定的业务名词，"
        "把它放进 assumptions，不要杜撰字段。",
        *sections,
        f"【当前时间】{now.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)",
        f"【用户问句】{question}",
    ])


# ---------------------------------------------------------------------------
# 2. 模型网关
# ---------------------------------------------------------------------------
def _chat_completions(row: Dict[str, Any], prompt: str, model: str) -> Tuple[str, Dict[str, Any]]:
    base = (row.get("base_url") or "").rstrip("/")
    if not base.lower().startswith(("http://", "https://")):
        raise Nl2PplError("model", "Base URL 非法", base, "请到「模型接入」填写 http(s) 开头的地址")
    path = "/chat/completions" if base.endswith("/v1") else "/v1/chat/completions"
    url = base + path
    headers = {"Content-Type": "application/json"}
    key = (row.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    extra = row.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    if isinstance(extra, dict):
        headers.update(extra.get("headers") or {})
    timeout = min(int(row.get("timeout") or 30), 90)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你只输出严格 JSON，不输出其它内容。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 800,
        "stream": False,
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise Nl2PplError("model", f"模型请求失败：{type(e).__name__}", str(e)[:300],
                          "检查网络 / Base URL / 容器能否访问该地址") from e
    if resp.status_code != 200:
        detail = snippet(resp.text)
        hint = ""
        if resp.status_code in (400, 404) and "model" in detail.lower():
            hint = "模型名可能不受该服务支持，请到「模型接入」核对模型 ID（可用 /models 查询清单）"
        raise Nl2PplError("model", f"模型返回 HTTP {resp.status_code}", detail, hint)
    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise Nl2PplError("model", "模型响应格式无法解析", snippet(resp.text),
                          "确认该接口为 OpenAI 兼容 /chat/completions") from e
    return text, {"via": "chat-completions", "url": url, "statusCode": resp.status_code}


def invoke_model(row: Dict[str, Any], prompt: str,
                 model_name_override: str = "") -> Tuple[str, Dict[str, Any]]:
    kind = row.get("kind")
    model = (model_name_override or row.get("model_name") or "").strip()
    if not model:
        raise Nl2PplError("config", "该接入未配置模型名", "",
                          "到「模型接入」编辑该配置，填写或选择模型")
    if kind in ("commercial", "local"):
        return _chat_completions(row, prompt, model)
    raise Nl2PplError("config", f"不支持的接入类型：{kind}")


# ---------------------------------------------------------------------------
# 3. 输出解析与语句规范化闸门
# ---------------------------------------------------------------------------
def parse_model_output(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    candidate = t[start:end + 1] if start >= 0 and end > start else t
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    # 兼容小模型不听话、直接吐一行 PPL 的情况（索引段可选，语句可直接以 where 开头）
    if re.search(r"\bsource\s*=", t, re.I) or re.match(
            r"^\s*(where|stats|fields|sort|head)\b", t, re.I):
        return {"ppl": t, "explanation": "模型直接返回语句（未按 JSON 格式），已按语句解析",
                "assumptions": [], "timeRule": ""}
    raise Nl2PplError("parse", "模型输出既不是 JSON 也不是 PPL 语句", snippet(t, 500),
                      "可换用指令遵循更好的模型，或降低 temperature")


def _extract_field_tokens(segment: str) -> set:
    cleaned = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", " ", segment)
    tokens = set(re.findall(r"[@A-Za-z_][A-Za-z0-9_.@]*", cleaned))
    return {t for t in tokens
            if t.lower() not in NON_FIELD_TOKENS
            and not t.replace(".", "", 1).isdigit()}


def normalize_ppl(ppl: str, rules: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """模型输出按不可信输入处理：剥离索引段、白名单、补上限；未知字段只告警不硬拒。"""
    warnings: List[str] = []
    stmt = (ppl or "").strip().strip("`").strip()
    stmt = re.sub(r"\s+", " ", stmt)
    if not stmt:
        raise Nl2PplError("validate", "模型未生成 PPL 语句", "ppl 字段为空")
    if len(stmt) > 2000:
        raise Nl2PplError("validate", "生成语句过长，已拒绝", f"{len(stmt)} 字符")

    cmds = allowed_commands(rules)
    fields = allowed_fields(rules)
    parts = [p.strip() for p in stmt.split("|") if p.strip()]
    if not parts:
        raise Nl2PplError("validate", "生成语句为空")

    # 第一段：生成器不输出索引，索引由 search-service /pql 按租户补齐。
    # 模型若照抄示例自行加了 source=，一律剥离，避免把当前租户 / 索引写死进语句。
    if re.match(r"^source\s*=", parts[0], re.I):
        parts.pop(0)
        warnings.append("已移除模型自行添加的 source= 索引段；索引由检索网关按当前租户补齐")
        if not parts:
            raise Nl2PplError("validate", "模型只生成了索引段，没有查询条件")
    first = parts[0]
    if not re.match(r"^\s*(where|stats|fields|sort|head)\b", first, re.I):
        raise Nl2PplError("validate", f"语句首段不是允许的查询命令：{first[:60]}", "",
                          "检查模型输出或规则库命令白名单")

    for seg in parts:
        m = re.match(r"^([A-Za-z_@]+)", seg)
        verb = m.group(1).lower() if m else ""
        if verb in WRITE_WORDS:
            raise Nl2PplError("validate", f"检测到写操作命令「{verb}」，已拒绝", seg[:200])
        if verb not in cmds:
            raise Nl2PplError("validate", f"命令「{verb}」不在 PPL 语法规则库白名单内", seg[:200],
                              "到「PPL 语法规则库 → 命令白名单」登记后再试")
        unknown = sorted(_extract_field_tokens(seg) - fields)
        if unknown:
            warnings.append(f"段「{verb}」出现规则库未登记的字段：{', '.join(unknown[:5])}（动态 mapping 下保留但请核对）")

    # 写操作关键字整句兜底（避开字符串字面量）
    literals_removed = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", " ", stmt)
    hit = next((w for w in WRITE_WORDS
                if re.search(rf"\b{w}\b", literals_removed, re.I)), None)
    if hit:
        raise Nl2PplError("validate", f"语句含写操作关键字「{hit}」，已拒绝", stmt[:200])

    # head 上限：缺失补齐，过大收敛
    head_idx = next((i for i, s in enumerate(parts)
                     if re.match(r"^head\b", s, re.I)), None)
    if head_idx is None:
        parts.append(f"head {DEFAULT_HEAD}")
        warnings.append(f"模型语句缺少结果上限，后端已补 head {DEFAULT_HEAD}")
    else:
        m = re.search(r"\bhead\s+(\d+)", parts[head_idx], re.I)
        if m and int(m.group(1)) > MAX_HEAD:
            parts[head_idx] = re.sub(r"\bhead\s+\d+", f"head {MAX_HEAD}", parts[head_idx], flags=re.I)
            warnings.append(f"结果上限超过 {MAX_HEAD}，已收敛")

    return " | ".join(parts), warnings


# ---------------------------------------------------------------------------
# 4. 时间词 → 绝对区间（确定性代码，不交给模型）
# ---------------------------------------------------------------------------
def resolve_time(question: str, now: Optional[datetime] = None) -> Optional[Dict[str, str]]:
    now = now or datetime.now(tz=CST)
    m = re.search(r"最近\s*(\d+)\s*(分钟|小时|天)", question)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"分钟": timedelta(minutes=n), "小时": timedelta(hours=n),
                 "天": timedelta(days=n)}[unit]
        return {"from": (now - delta).isoformat(timespec="seconds"),
                "to": now.isoformat(timespec="seconds"), "label": m.group(0)}
    if "今天" in question:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return {"from": start.isoformat(timespec="seconds"),
                "to": now.isoformat(timespec="seconds"), "label": "今天"}
    if "昨天" in question:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return {"from": (today - timedelta(days=1)).isoformat(timespec="seconds"),
                "to": today.isoformat(timespec="seconds"), "label": "昨天"}
    return None


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def generate(question: str, row: Dict[str, Any], rules: List[Dict[str, Any]],
             tenant: str = "default", model_name: str = "",
             include_prompt: bool = False) -> Dict[str, Any]:
    started = time.time()
    now = datetime.now(tz=CST)
    q = (question or "").strip()
    model_used = (model_name or row.get("model_name") or "").strip()
    base = {
        "ok": False, "question": q, "tenant": tenant,
        "model": {"id": row.get("id"), "name": row.get("name"),
                  "kind": row.get("kind"), "modelName": model_used},
        "matchedRules": matched_rules(q, rules),
        "injectedRuleCount": len(rules),
    }
    try:
        prompt = build_prompt(q, rules, now)
        text, call_meta = invoke_model(row, prompt, model_name)
        parsed = parse_model_output(text)
        raw_ppl = str(parsed.get("ppl") or "")
        ppl, warnings = normalize_ppl(raw_ppl, rules)
        # 时间范围不写进 PPL：由检索网关 /pql 在执行时作为独立 where 注入，避免模型算错边界
        time_range = resolve_time(q, now)
        result = {
            **base,
            "ok": True,
            "ppl": ppl,
            "rawPpl": raw_ppl,
            "explanation": str(parsed.get("explanation") or ""),
            "assumptions": [str(a) for a in (parsed.get("assumptions") or [])][:8],
            "timeRule": str(parsed.get("timeRule") or ""),
            "timeRange": time_range,
            "warnings": warnings,
            "call": call_meta,
            "elapsedMs": int((time.time() - started) * 1000),
        }
        if include_prompt:
            result["prompt"] = prompt
        return result
    except Nl2PplError as e:
        return {**base, "ok": False, "stage": e.stage, "error": e.message,
                "detail": e.detail, "hint": e.hint,
                "elapsedMs": int((time.time() - started) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {**base, "ok": False, "stage": "unknown",
                "error": f"{type(e).__name__}: {e}", "detail": "", "hint": "",
                "elapsedMs": int((time.time() - started) * 1000)}
