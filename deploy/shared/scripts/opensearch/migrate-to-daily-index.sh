#!/usr/bin/env bash
# =============================================================================
# 把已有的 logs-<tenant> 单索引迁移为按天索引 + 读写别名。
#
# 必须执行的原因：
#   读别名就叫 logs-<tenant>，与旧索引同名。同名索引存在时无法创建别名，
#   所以新版 ingest 的 ensure_write_target 会一直降级到直写日期索引，
#   查询侧查 logs-<tenant> 仍只看到旧索引里的历史数据——表现为"新日志查不到"。
#
# 执行顺序（重要）：
#   1) 先跑本脚本（此时旧版 ingest 仍在写 logs-<tenant>，reindex 后删旧索引建别名，
#      旧 ingest 随后写同名别名，因别名只指向一个索引，写入自动落到新索引）
#   2) 再部署新版 ingest / system / Flink
#   3) 最后执行 apply-logs-template.sh 挂 ISM 策略
#
# 用法：
#   sh deploy/shared/scripts/opensearch/migrate-to-daily-index.sh           # 预演，只读
#   sh deploy/shared/scripts/opensearch/migrate-to-daily-index.sh --execute # 真正执行
#   OPENSEARCH_URL=http://host:9200 sh ... --execute
# =============================================================================
set -euo pipefail

OS_URL="${OPENSEARCH_URL:-http://localhost:9200}"
DO_EXECUTE=0
for a in "$@"; do [ "$a" = "--execute" ] && DO_EXECUTE=1; done

python3 - "$OS_URL" "$DO_EXECUTE" <<'PY'
import json, sys, urllib.request, urllib.error
from datetime import datetime, timezone

base, do_execute = sys.argv[1], sys.argv[2] == "1"
date = datetime.now(timezone.utc).strftime("%Y.%m.%d")
mode = "执行" if do_execute else "预演（只读）"
print(f"[migrate] 模式：{mode}  目标日期后缀：{date}")

def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def is_daily(idx: str) -> bool:
    # logs-<tenant>-YYYY.MM.DD：末尾固定 11 字符 "-YYYY.MM.DD"
    return len(idx) > 11 and idx[-11] == "-" and idx[-6] == "." and idx[-3] == "."

code, res = call("GET", "/_cat/indices/logs-*?h=index&format=json")
if code != 200:
    print(f"[migrate] 读取索引列表失败 HTTP {code}: {res}")
    sys.exit(1)
indices = [r["index"] for r in res]
legacy = [i for i in indices if i.startswith("logs-") and not is_daily(i)]
if not legacy:
    print("[migrate] 没有旧格式索引，无需迁移")
    sys.exit(0)

print(f"[migrate] 待迁移 {len(legacy)} 个：{legacy}")
for old in legacy:
    tenant = old[len("logs-"):]
    new = f"logs-{tenant}-{date}"
    code, cnt = call("GET", f"/{old}/_count")
    docs = cnt.get("count", 0) if code == 200 else "?"
    print(f"\n  {old} ({docs} 条) -> {new}")
    if not do_execute:
        print("    预演：将创建新索引、reindex 历史、删除旧索引、建立读写别名")
        continue

    # 1) 建新索引。显式空 body，mapping 由 index-daily-template 提供
    if call("HEAD", f"/{new}")[0] != 200:
        c, r = call("PUT", f"/{new}", {})
        if c >= 400:
            print(f"    建索引失败 HTTP {c}: {r}")
            sys.exit(1)

    # 2) reindex。op_type=create 跳过已由新版写入的文档，不覆盖、不重复
    body = {
        "source": {"index": old},
        "dest": {"index": new, "op_type": "create"},
        "conflicts": "proceed",
    }
    c, r = call("POST", "/_reindex?wait_for_completion=true&requests_per_second=2000",
                body, timeout=1800)
    if c >= 400:
        print(f"    reindex 失败 HTTP {c}: {json.dumps(r, ensure_ascii=False)[:300]}")
        sys.exit(1)
    print(f"    reindex 完成：创建 {r.get('created', 0)}，冲突跳过 "
          f"{r.get('version_conflicts', 0)}")

    # 3) 删旧索引，腾出 logs-<tenant> 这个名字给读别名
    c, r = call("DELETE", f"/{old}")
    if c >= 400:
        print(f"    删除旧索引失败 HTTP {c}: {r}")
        sys.exit(1)

    # 4) 建读写别名。读别名保留历史日期索引，写别名只指当天
    actions = [
        {"add": {"index": new, "alias": f"logs-{tenant}"}},
        {"add": {"index": new, "alias": f"logs-{tenant}-write"}},
    ]
    c, r = call("POST", "/_aliases", {"actions": actions})
    if c >= 400:
        print(f"    建别名失败 HTTP {c}: {r}")
        sys.exit(1)
    print("    别名就绪")

print(f"\n[migrate] {mode}结束。执行后核对：")
print(f"  curl -s '{base}/_cat/aliases/logs-*?v'")
PY
