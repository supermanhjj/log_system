#!/usr/bin/env bash
# =============================================================================
# 应用 RuiLog 索引模板 + ISM 生命周期策略。
#
# 为什么必须是这套组合：
#   - 索引按天滚动（logs-<tenant>-YYYY.MM.DD），过期由 ISM 直接 drop 整个索引，
#     元数据操作、毫秒级、零 IO。旧的 delete_by_query 是逐条删，50GB/天撑不住。
#   - mapping 只在模板里定义一处（dynamic:false + attrs flat_object）。
#     管理服务里曾有第二套 dynamic:true 的显式 mapping，显式优先于模板，
#     现场字段一多就撞 total_fields.limit 导致整索引写入断流，已删除。
#
# 用法：
#   sh deploy/shared/scripts/opensearch/apply-logs-template.sh
#   可覆盖：
#     OPENSEARCH_URL=http://localhost:9200
#     OPENSEARCH_SHARDS=1  OPENSEARCH_REPLICAS=0    # 生产多节点时调大
#
# 已有 logs-<tenant> 单索引的迁移见同目录 migrate-to-daily-index.sh
# =============================================================================
set -euo pipefail

OS_URL="${OPENSEARCH_URL:-http://localhost:9200}"
# 容器内脚本位于 /init/，需显式指定项目根；本机直接执行时按脚本位置推导
ROOT="${RUILOG_HOME:-$(cd "$(dirname "$0")/../.." && pwd)}"
TPL="$ROOT/opensearch/index-templates/logs-template.json"
ISM="$ROOT/opensearch/ism/ruilog-detail-retention.json"

[ -f "$TPL" ] || { echo "[apply] 模板不存在：$TPL"; exit 1; }
[ -f "$ISM" ] || { echo "[apply] ISM 策略不存在：$ISM"; exit 1; }

python3 - "$OS_URL" "$TPL" "$ISM" <<'PY'
import json, os, sys, urllib.request, urllib.error

base, tpl_path, ism_path = sys.argv[1], sys.argv[2], sys.argv[3]
template = json.load(open(tpl_path, encoding="utf-8"))
policy = json.load(open(ism_path, encoding="utf-8"))

# 分片/副本可由环境变量覆盖：测试环境 1/0，生产按磁盘与节点数调整
settings = template["template"]["settings"]
settings["number_of_shards"] = int(os.getenv("OPENSEARCH_SHARDS",
                                              settings["number_of_shards"]))
settings["number_of_replicas"] = int(os.getenv("OPENSEARCH_REPLICAS",
                                               settings["number_of_replicas"]))

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

# 1) 组合式索引模板：只匹配带日期后缀的实际索引，不碰读别名
code, res = call("PUT", "/_index_template/logs-daily-template", template)
print(f"[apply] 模板 logs-daily-template -> HTTP {code} "
      f"{res.get('acknowledged', res)}")
if code >= 400:
    print(f"  详情：{json.dumps(res, ensure_ascii=False)[:500]}")
    sys.exit(1)

# 2) ISM 生命周期策略：索引创建时由模板的 index.plugins.index_state_management.policy_id 自动挂载。
#    策略是幂等播种：先用 GET 拿 if_seq_no/if_primary_term，存在则带版本号更新，不存在则新建。
#    直接 PUT 同名策略在已存在时会返回 409 version_conflict，导致 init 容器非 0 退出、
#    阻塞依赖它的 ingest 启动——所以必须走"读版本号再写"这条路。
pol_path = "/_plugins/_ism/policies/ruilog-detail-retention"
gcode, gres = call("GET", pol_path)
put_path = pol_path
if gcode == 200:
    seq = gres.get("_seq_no")
    prim = gres.get("_primary_term")
    if seq is not None and prim is not None:
        put_path = f"{pol_path}?if_seq_no={seq}&if_primary_term={prim}"
# policy 文件顶层已是 {"policy": {...}}，直接作为 body，不再包一层
code, res = call("PUT", put_path, policy)
print(f"[apply] ISM ruilog-detail-retention -> HTTP {code} "
      f"{res.get('_version', res.get('error', res))}")
if code >= 400:
    print(f"  详情：{json.dumps(res, ensure_ascii=False)[:500]}")
    sys.exit(1)

# 3) 核对（不自动改数据）
code, res = call("GET", "/_cat/indices/logs-*?h=index&format=json")
indices = [r["index"] for r in res] if code == 200 else []
daily = [i for i in indices if len(i) > 11 and i[-11] == "-" and i[-6] == "." and i[-3] == "."]
legacy = [i for i in indices if i not in daily]
print(f"[apply] 索引总数 {len(indices)}：按天 {len(daily)}，待迁移旧索引 {len(legacy)}")
if legacy:
    print(f"  旧索引：{legacy[:20]}")
    print("  请执行 sh deploy/shared/scripts/opensearch/migrate-to-daily-index.sh 完成迁移")

print(f"[apply] 完成。分片={settings['number_of_shards']} "
      f"副本={settings['number_of_replicas']}")
PY
