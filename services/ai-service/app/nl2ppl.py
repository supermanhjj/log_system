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
               "message", "raw", "traceId"}
# 租户维度的可用性**取决于检索范围**，不能一刀切：
#
# 单租户范围：租户是「日志从哪来」的部署元数据，由检索上下文（请求 tenant 参数 +
#   服务端按 JWT 强制解析）决定，物理上已按 logs-<tenant> 分索引。此时写进 PPL 等价于
#   把机器码当业务值匹配（'总部生产' 匹配不到 'default'），结果恒为 0 条且与真实隔离
#   边界重复，属于典型的「查不出还以为是没日志」→ 一律剥离。
#
# 全部租户范围（管理员跨租户巡检）：租户是唯一能回答「这些日志来自哪个租户」的维度，
#   此时必须开放，否则「按租户统计」只能退化成「按级别统计」，结论直接失真。
#   对外统一暴露为**虚拟字段 `tenant`**，由 search-service 翻译为物理字段 tenant_id
#   （接入端写入的 keyword）并在结果里换回租户中文名——业务侧不需要知道索引命名与机器码。
TENANT_FIELDS = {"tenant", "tenant_id", "tenant_code", "tenant_name"}
VIRTUAL_TENANT = "tenant"
CROSS_TENANT_SCOPES = {"all", "*"}

# 语句中出现租户标识（含物理字段写法）——用来判断模型是否已表达租户维度
_TENANT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.@])(?:tenant_id|tenant_code|tenant_name|tenant)(?![A-Za-z0-9_.@])", re.I)
_TENANT_PHYSICAL = re.compile(
    r"(?<![A-Za-z0-9_.@])(?:tenant_id|tenant_code|tenant_name)(?![A-Za-z0-9_.@])", re.I)
# 字符串字面量切分器：按奇偶下标区分「代码段 / 字面量段」，避免改写命中日志正文
_LITERAL_SPLIT = re.compile(r"('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")")
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


def _rule_touches_tenant(item: Dict[str, str]) -> bool:
    blob = f"{item.get('ruleKey', '')} {item.get('ruleValue', '')}".lower()
    return "租户" in blob or "tenant" in blob


# 全部租户范围的租户维度说明：规则库里与租户相关的 deny / example / field_alias
# 都是按「单租户」口径写的（禁止出现租户字段），跨租户时必须换成这一套，
# 否则模型会一边看到「租户可分组」、一边看到「禁止写租户」，行为不可预期。
CROSS_TENANT_SECTION = """【租户维度（当前为全部租户范围，租户可分组可过滤）】
- 字段：tenant（虚拟维度，即租户；执行时由平台翻译为物理字段，结果中显示租户中文名）
- 示例：按租户和级别统计最近 1 小时 → stats count() by tenant, level | head 100
- 示例：查询总部生产最近 10 分钟的错误日志 → where tenant = '总部生产' and level = 'ERROR' | head 100
- 禁止：直接写 tenant_id / tenant_code / tenant_name，或写任何索引名
"""


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


def allowed_fields(rules: List[Dict[str, Any]], cross_tenant: bool = False) -> set:
    """合法字段白名单。

    物理租户字段即使被规则库误登记也会被剔除；虚拟维度 tenant 只在「全部租户」
    范围下由代码注入白名单，不依赖规则库——否则单租户范围会跟着一起被放开。
    """
    fields = set(BASE_FIELDS)
    for r in rules:
        if r.get("category") == "field_alias" and r.get("enabled", True):
            v = str(r.get("rule_value") or "").strip()
            if re.fullmatch(r"[A-Za-z_@][A-Za-z0-9_.@]*", v) and v not in TENANT_FIELDS:
                fields.add(v)
    if cross_tenant:
        fields.add(VIRTUAL_TENANT)
    return fields


# 租户谓词的三种形态：在条件链开头、被 and/or 连接在中间/末尾。
_TENANT_OP = r"(?:=|!=|<>|>=|<=|>|<|\bin\b|\bnot\s+in\b|\blike\b|\bis\s+not\b|\bis\b)"
_TENANT_VAL = r"(?:'[^']*'|\"[^\"]*\"|\([^)]*\)|[A-Za-z0-9_.@\-]+)"
_TENANT_HEAD = re.compile(
    rf"\b(?:tenant_id|tenant_code|tenant_name|tenant)\b\s*{_TENANT_OP}\s*{_TENANT_VAL}"
    rf"\s*(?:and|or)\s+", re.I)
_TENANT_TAIL = re.compile(
    rf"\s*(?:and|or)\s+\b(?:tenant_id|tenant_code|tenant_name|tenant)\b\s*"
    rf"{_TENANT_OP}\s*{_TENANT_VAL}", re.I)
_TENANT_ANY = re.compile(
    rf"\b(?:tenant_id|tenant_code|tenant_name|tenant)\b\s*{_TENANT_OP}\s*{_TENANT_VAL}", re.I)

# stats 的分组维度（by 子句）里的租户项：单租户范围下该维度恒定，分组没有意义。
# 只按 list 项的位置处理，避免误伤 span(@timestamp, 5m) 这种带逗号的函数参数。
_BY_TENANT_ONLY = re.compile(
    r"^\s*(?:tenant_id|tenant_code|tenant_name|tenant)\s*$", re.I)
_BY_TENANT_HEAD = re.compile(
    r"^\s*(?:tenant_id|tenant_code|tenant_name|tenant)\s*,\s*", re.I)
_BY_TENANT_TAIL = re.compile(
    r"\s*,\s*(?:tenant_id|tenant_code|tenant_name|tenant)\s*$", re.I)


def _strip_stats_by_tenant(body: str) -> Tuple[str, bool]:
    """从 `stats ... by a, tenant` 里摘掉租户维度；只剩租户时整个 by 子句去掉。"""
    m = re.search(r"\bby\b", body, re.I)
    if not m:
        return body, False
    agg, by = body[:m.start()], body[m.end():]
    if _BY_TENANT_ONLY.match(by):
        return agg.rstrip(), True
    new = by
    for pat in (_BY_TENANT_HEAD, _BY_TENANT_TAIL):
        new = pat.sub("", new)
    if new == by:
        return body, False
    return f"{agg}by {new}", True


def strip_tenant_predicates(stmt: str) -> Tuple[str, bool]:
    """从语句中剥离租户过滤条件，返回 (新语句, 是否剥离过)。

    租户由检索上下文决定，写进 PPL 只会把机器码当业务值匹配，导致恒 0 条。
    因此这里做「纠错」而不是「告警后放行」：模型即便违规生成了，也必须在执行前清掉。
    """
    hit = False
    out: List[str] = []
    for seg in [s.strip() for s in stmt.split("|") if s.strip()]:
        m = re.match(r"^(where|fields|stats)\b(.*)$", seg, re.I | re.S)
        if not m:
            out.append(seg)
            continue
        verb, body = m.group(1).lower(), m.group(2)
        if verb == "fields":
            # 字段清单按逗号分隔，租户字段（含 .keyword 子字段）整项删除
            names = [n.strip() for n in body.split(",") if n.strip()]
            kept = [n for n in names
                    if n.strip("`").split(".")[0].lower() not in TENANT_FIELDS]
            if len(kept) != len(names):
                hit = True
            body = ", ".join(kept)
        elif verb == "stats":
            body, changed = _strip_stats_by_tenant(body)
            hit = hit or changed
        else:
            original = body
            for pat in (_TENANT_HEAD, _TENANT_TAIL, _TENANT_ANY):
                body = pat.sub("", body)
            if body != original:
                hit = True
        if body.strip():
            out.append(f"{verb} {body.strip()}")
        # 条件被清空则整段丢弃（例如语句只有 `where tenant_id = '总部生产'`）
    return " | ".join(out), hit


# ---------------------------------------------------------------------------
# 1.1 全部租户范围：租户维度收敛与补条件
# ---------------------------------------------------------------------------
def is_cross_tenant(tenant: Optional[str]) -> bool:
    return (tenant or "").strip().lower() in CROSS_TENANT_SCOPES


def map_outside_literals(text: str, fn) -> str:
    """只对「代码段」应用 fn，跳过字符串字面量（避免改写 message like '%tenant%'）。"""
    segs = _LITERAL_SPLIT.split(text or "")
    return "".join(fn(s) if i % 2 == 0 else s for i, s in enumerate(segs))


def canonicalize_tenant_field(stmt: str) -> Tuple[str, bool]:
    """跨租户范围：tenant_id / tenant_code / tenant_name 统一收敛为虚拟字段 tenant。

    对外的稳定契约是「一个租户维度 tenant」，物理字段名与索引命名不外泄；
    网关据此翻译为 tenant_id 并把值换回中文名。模型写哪种写法都能救回来。
    """
    new = map_outside_literals(stmt, lambda s: _TENANT_PHYSICAL.sub(VIRTUAL_TENANT, s))
    return new, new != stmt


def mentions_tenant(stmt: str) -> bool:
    cleaned = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", " ", stmt or "")
    return bool(_TENANT_TOKEN.search(cleaned))


def has_tenant_predicate(stmt: str) -> bool:
    """语句里是否已有租户**过滤条件**（只看 where 段）。

    只有 `stats ... by tenant` 的分组不算：那只是把各租户拆开，并没有把范围限定到某个租户。
    """
    for seg in (stmt or "").split("|"):
        seg = seg.strip()
        if re.match(r"^where\b", seg, re.I) and mentions_tenant(seg):
            return True
    return False


def inject_tenant_filter(stmt: str, code: str) -> str:
    """在语句开头补一段独立 where，把范围限定到指定租户。

    必须独立成段而不是与首段 where 用 and 合并：首段若含 or，拼接后会改变优先级
    （and 优先级高于 or），PPL 里连续 where 段是安全等价写法。
    """
    parts = [p.strip() for p in (stmt or "").split("|") if p.strip()]
    parts.insert(0, f"where {VIRTUAL_TENANT} = '{code}'")
    return " | ".join(parts)


def build_prompt(question: str, rules: List[Dict[str, Any]],
                 now: Optional[datetime] = None,
                 cross_tenant: bool = False) -> str:
    now = now or datetime.now(tz=CST)
    grouped = group_rules(rules)
    cmds = sorted(allowed_commands(rules))
    sections = []
    for cat, title in CATEGORY_TITLES.items():
        items = grouped.get(cat) or []
        if cross_tenant and cat in ("deny", "example", "field_alias"):
            # 这些类别里与租户相关的条目是单租户口径，跨租户范围下会与 tenant 维度冲突
            items = [i for i in items if not _rule_touches_tenant(i)]
        if not items:
            continue
        body = "\n".join(
            f"- {i['ruleKey']}" + (f" → {i['ruleValue']}" if i["ruleValue"] else "")
            for i in items
        )
        sections.append(f"【{title}】\n{body}")
    if cross_tenant:
        sections.append(CROSS_TENANT_SECTION.strip())

    schema = (
        '{"ppl":"<PPL 语句>", "explanation":"<一句话说明翻译依据>", '
        '"assumptions":["<假设，可为空数组>"], "timeRule":"<命中的时间词，无则空字符串>"}'
    )
    # 租户约束必须跟着检索范围走：单租户下租户不是可查询字段（写了只会恒 0 条），
    # 全部租户下租户恰恰是唯一能分租户统计的维度（不写就统计不出租户）。
    if cross_tenant:
        tenant_rules = (
            "- 当前检索范围是【全部租户】（跨租户巡检）。此时**租户是可查询维度**：字段名统一写 "
            f"`{VIRTUAL_TENANT}`（虚拟维度，平台自动落到物理字段并在结果里显示租户中文名），"
            "不要写 tenant_id / tenant_code / tenant_name。"
            "用户要求「按租户统计 / 分租户对比 / 每个租户多少」时，必须把 tenant 放进 stats 的 by 子句，"
            "例如「按租户和级别统计」→ `stats count() by tenant, level`；"
            "只查某个租户时写 `where tenant = '总部生产'`（中文名或编码都可以）。"
            "不要假设「租户无法分组」——当前范围下租户可以分组。"
        )
    else:
        tenant_rules = (
            "- **租户不是可查询的日志字段**：tenant / tenant_id / tenant_code / tenant_name "
            "一律不得出现在 where、fields 或任何条件里。用户提到「租户是 X」「X 租户的日志」时，"
            "不要把它翻译成过滤条件——当前只在一个租户内检索，范围已由检索上下文限定；"
            "只需照常生成日志内容条件，仅提到租户而没有其他条件时就生成 `head 100`。"
        )
    return "\n\n".join([
        "你是日志平台的 PPL 检索语句生成器。只输出一个 JSON 对象，不要输出任何解释文字、"
        "不要使用 Markdown 代码块。",
        f"输出格式：{schema}",
        "【硬约束】",
        "- 查询语言为 OpenSearch PPL，管道用 | 分隔。**不要输出 source= 开头的索引段**，"
        "语句直接从 where / stats / fields / sort 等命令开始；索引由检索网关按当前租户自动补齐，"
        "禁止在语句中写死任何租户名、租户编码或索引名。",
        tenant_rules,
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


def normalize_ppl(ppl: str, rules: List[Dict[str, Any]],
                  cross_tenant: bool = False) -> Tuple[str, List[str]]:
    """模型输出按不可信输入处理：剥离索引段、白名单、补上限；未知字段只告警不硬拒。"""
    warnings: List[str] = []
    stmt = (ppl or "").strip().strip("`").strip()
    stmt = re.sub(r"\s+", " ", stmt)
    if not stmt:
        raise Nl2PplError("validate", "模型未生成 PPL 语句", "ppl 字段为空")
    if len(stmt) > 2000:
        raise Nl2PplError("validate", "生成语句过长，已拒绝", f"{len(stmt)} 字符")

    cmds = allowed_commands(rules)
    fields = allowed_fields(rules, cross_tenant)
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

    # 租户维度按检索范围处理，两条路径互斥：
    # - 全部租户：租户可分组/过滤，把物理字段写法收敛为虚拟字段 tenant（网关负责翻译+回显中文名）；
    # - 单租户：租户条件必须剥掉——租户是检索上下文，不是日志字段，留着会让 '总部生产'
    #   这类中文名去匹配 tenant_id 的机器码，恒 0 条。
    if cross_tenant:
        canonical, changed = canonicalize_tenant_field(" | ".join(parts))
        if changed:
            warnings.append(
                f"已将语句中的 tenant_id / tenant_code / tenant_name 统一为虚拟字段 {VIRTUAL_TENANT}"
                "（执行时由检索网关翻译为物理字段，结果中显示租户中文名）")
            parts = [p.strip() for p in canonical.split("|") if p.strip()]
    else:
        stripped_stmt, tenant_stripped = strip_tenant_predicates(" | ".join(parts))
        if tenant_stripped:
            warnings.append(
                "已移除语句中的租户过滤条件：当前为单租户检索范围，租户由检索上下文（请求的 tenant 参数）"
                "决定，不是日志字段；写进 PPL 会用中文名去匹配机器码，导致查不到数据")
            parts = [p.strip() for p in stripped_stmt.split("|") if p.strip()] or [f"head {DEFAULT_HEAD}"]
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
# 5. 租户一致性提示
# ---------------------------------------------------------------------------
def detect_tenant(question: str, current: str,
                  tenants: Optional[List[Dict[str, str]]] = None) -> Optional[Dict[str, Any]]:
    """识别问句点名的租户，并与本次检索租户比对。

    单租户范围下租户条件已从 PPL 剥离，若用户点名 A 租户、而上下文是 B 租户，查询会静默
    落到 B。这类「看起来对、实际查错范围」比报错更危险，所以必须显式提示。
    全部租户范围下租户维度可用，点名**单个**租户会被补成 tenant 条件；点名多个（对比场景）
    则不能补，只交代「已按租户维度分别统计」，否则会把对比查询压成单租户。
    """
    if not tenants:
        return None
    q = question or ""
    hits: List[Dict[str, str]] = []
    for t in tenants:
        code = str(t.get("code") or "").strip()
        name = str(t.get("name") or "").strip()
        if not code:
            continue
        mentioned = bool(name and name in q) or bool(
            re.search(rf"(?<![A-Za-z0-9_]){re.escape(code)}(?![A-Za-z0-9_])", q, re.I))
        if mentioned:
            hits.append({"code": code, "label": name or code})
    if not hits:
        return None

    if len(hits) > 1:
        labels = "、".join(h["label"] for h in hits)
        return {
            "requested": labels, "code": "", "current": current,
            "applied": False, "mismatch": False,
            "message": (f"问句点名多个租户（{labels}），"
                        + ("已按 tenant 维度分别统计，未收窄到单个租户"
                           if current in CROSS_TENANT_SCOPES
                           else "但当前是单租户检索范围，只能命中当前租户")),
        }

    label, code = hits[0]["label"], hits[0]["code"]
    if current in CROSS_TENANT_SCOPES:
        return {
            "requested": label, "code": code, "current": current,
            "applied": True, "mismatch": False,
            "message": (f"问句点名租户「{label}」({code})，当前是「全部租户」范围；"
                        f"语句已按租户维度限定到该租户，结果不会混入其他租户"),
        }
    if code == current:
        return {"requested": label, "code": code, "current": current,
                "applied": True, "mismatch": False,
                "message": f"问句点名的租户「{label}」与当前检索租户一致"}
    return {
        "requested": label, "code": code, "current": current,
        "applied": False, "mismatch": True,
        "message": (f"问句点名的租户是「{label}」({code})，当前检索租户是「{current}」；"
                    f"语句已按当前租户生成，需把顶栏租户切到「{label}」后再执行才会命中"),
    }


# ---------------------------------------------------------------------------
# 6. 主流程
# ---------------------------------------------------------------------------
def generate(question: str, row: Dict[str, Any], rules: List[Dict[str, Any]],
             tenant: str = "default", model_name: str = "",
             include_prompt: bool = False,
             tenants: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    started = time.time()
    now = datetime.now(tz=CST)
    q = (question or "").strip()
    model_used = (model_name or row.get("model_name") or "").strip()
    cross = is_cross_tenant(tenant)
    tenant_hint = detect_tenant(q, tenant, tenants)
    base = {
        "ok": False, "question": q, "tenant": tenant,
        "tenantScope": "all" if cross else "single",
        "tenantDimension": cross,   # 本次生成是否开放租户维度（决定前端提示文案）
        "tenantHint": tenant_hint,
        "model": {"id": row.get("id"), "name": row.get("name"),
                  "kind": row.get("kind"), "modelName": model_used},
        "matchedRules": matched_rules(q, rules),
        "injectedRuleCount": len(rules),
    }
    try:
        prompt = build_prompt(q, rules, now, cross_tenant=cross)
        text, call_meta = invoke_model(row, prompt, model_name)
        parsed = parse_model_output(text)
        raw_ppl = str(parsed.get("ppl") or "")
        ppl, warnings = normalize_ppl(raw_ppl, rules, cross_tenant=cross)
        # 全部租户 + 问句点名某租户：确定性补一段租户条件。
        # 模型可能只生成了内容条件，那样会把全院租户的日志都算进去，结论直接失真；
        # 补条件而不是「提示用户去切顶栏」——跨租户范围下这个过滤本来就是可表达的。
        if cross and tenant_hint and tenant_hint.get("code") and not has_tenant_predicate(ppl):
            code = tenant_hint["code"]
            ppl = inject_tenant_filter(ppl, code)
            warnings.insert(0, f"问句点名租户「{tenant_hint['requested']}」，已在语句开头补 "
                               f"where {VIRTUAL_TENANT} = '{code}'，结果不会混入其他租户")
        # 时间范围不写进 PPL：由检索网关 /pql 在执行时作为独立 where 注入，避免模型算错边界
        time_range = resolve_time(q, now)
        if tenant_hint and tenant_hint.get("mismatch"):
            # 放在最前：租户范围不对会让整条查询的结论失真，比字段告警更该先看到
            warnings.insert(0, tenant_hint["message"])
        assumptions = [str(a) for a in (parsed.get("assumptions") or [])]
        if cross:
            # 跨租户范围下租户本就可分组，模型若还写「租户无法作为字段」会直接把用户带错，
            # 这类与当前范围矛盾的前提一律丢弃（语句已由代码保证可用 tenant）。
            assumptions = [a for a in assumptions
                           if not (re.search(r"租户", a) and re.search(r"无法|不能|不作为|不可", a))]
        explanation = str(parsed.get("explanation") or "")
        if cross and re.search(r"租户", explanation) and re.search(r"不作为|无法|不可|不能", explanation):
            explanation = ("当前为「全部租户」范围：租户维度可用（虚拟字段 tenant），"
                           "平台会翻译为物理字段并在结果中显示租户中文名。")
        result = {
            **base,
            "ok": True,
            "ppl": ppl,
            "rawPpl": raw_ppl,
            "explanation": explanation,
            "assumptions": assumptions[:8],
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
