#!/usr/bin/env bash
# =============================================================================
# 应用 RuiLog 日志索引模板（显式 mapping），并给已存在的 logs-* 索引补齐新字段。
#
# 为什么要做：
#   OpenSearch 的动态 mapping 按"第一个写入值"推断类型。系统日志的 pid / severity
#   一旦被推断成 text，后续数值写入就会失败或退化成字符串，只能重建索引才能改正。
#   所以必须在灌数据之前把字段类型钉死。
#
# 用法：
#   sh scripts/opensearch/apply-logs-template.sh
#   可覆盖地址：OPENSEARCH_URL=http://localhost:9200 sh ...
# =============================================================================
set -euo pipefail

OS_URL="${OPENSEARCH_URL:-http://localhost:9200}"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
TPL="$HERE/opensearch/index-templates/logs-template.json"

[ -f "$TPL" ] || { echo "[apply] 模板不存在：$TPL"; exit 1; }

python3 - "$OS_URL" "$TPL" <<'PY'
import json, sys, urllib.request, urllib.error

base, tpl_path = sys.argv[1], sys.argv[2]
template = json.load(open(tpl_path, encoding="utf-8"))

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

# 1) 组合式索引模板：影响之后新建的 logs-<tenant>
code, res = call("PUT", "/_index_template/logs-template", template)
print(f"[apply] 模板 logs-template -> HTTP {code} {res.get('acknowledged', res)}")

# 2) 已存在索引补字段（新增字段允许在线添加，无需重建）
props = template["template"]["mappings"]["properties"]
code, res = call("GET", "/_cat/indices/logs-*?h=index&format=json")
if code != 200:
    print(f"[apply] 读取索引列表失败 HTTP {code}")
    sys.exit(1)
indices = [row["index"] for row in res]
print(f"[apply] 待补 mapping 的索引：{indices or '（无）'}")
for idx in indices:
    c, r = call("PUT", f"/{idx}/_mapping",
                {"dynamic": False, "properties": props})
    ok = r.get("acknowledged")
    print(f"  - {idx}: HTTP {c} acknowledged={ok}")
    if c >= 400:
        print(f"    详情：{json.dumps(r, ensure_ascii=False)[:300]}")
PY

echo "[apply] 完成。用以下命令核对字段类型："
echo "  curl -s '$OS_URL/logs-default/_mapping' | python3 -m json.tool | head -40"
