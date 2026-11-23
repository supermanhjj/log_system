#!/usr/bin/env python3
"""compose → K8s 运维配置迁移：storage_config / tenant_quotas / dashboards

为什么需要这个脚本
------------------
两套部署形态各有一个独立的 PostgreSQL：
  - compose：数据在 docker 卷 log_system_pgdata
  - K8s    ：数据在 PVC data-postgres-0
切到 K8s 之后，界面上配过的「总容量 / 租户配额 / 自建看板」还留在旧卷里，
新库只有 db.py 的种子值，用户看到的现象就是"配置丢了、看板没了"。
这不是重启造成的，也不是容器挂掉造成的，纯粹是两套库没打通。

迁移策略
--------
  storage_config  覆盖目标 id=1 那一行（retain_days / quota_gb / 冷热策略等）
  tenant_quotas   只搬目标库里仍然存在的租户，按 tenant_code UPSERT
  dashboards      按 name 匹配：有则更新 widgets 等内容（保留目标里的 tenant / id），
                  无则插入（不搬 id，交给自增序列，避免撞现有行）

用法
----
  python3 tools/migrate-config-from-compose.py            # 预览，只打印 SQL，不写库
  python3 tools/migrate-config-from-compose.py --apply    # 真正写进 K8s 库

可选参数
--------
  --source-volume  compose 侧 PG 卷名（默认 log_system_pgdata）
  --namespace      K8s 命名空间（默认 ruilog-system）
  --target-pod     目标 PG Pod（默认 postgres-0）
  --target-user    目标库用户（默认 loguser）
  --target-db      目标库名（默认 logdb）

注意
----
- 脚本只用 `docker run --rm --network none` 挂载旧卷做只读式查询，不启动 compose 栈；
- 写库在一个事务里，失败即整体回滚；
- 重复执行是幂等的（UPDATE / ON CONFLICT / NOT EXISTS）。
"""

import argparse
import json
import subprocess
import sys
import time

PROBE = "ruilog-compose-db-probe"
DQ = "$mig$"


def run(cmd, stdin_text=None):
    p = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def lit(value, is_jsonb=False):
    """生成 SQL 字面量。文本用 dollar-quote，避免中文和引号转义出错。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if is_jsonb:
        return quote(json.dumps(value, ensure_ascii=False)) + "::jsonb"
    return quote(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))


def quote(text):
    if DQ in text:
        return "'" + text.replace("'", "''") + "'"
    return DQ + text + DQ


def start_probe(volume):
    run(["docker", "rm", "-f", PROBE])
    code, out, err = run([
        "docker", "run", "-d", "--rm", "--network", "none", "--name", PROBE,
        "-v", f"{volume}:/var/lib/postgresql/data",
        "-e", "POSTGRES_PASSWORD=probe", "-e", "POSTGRES_USER=loguser",
        "-e", "POSTGRES_DB=logdb", "postgres:16-alpine",
    ])
    if code != 0:
        sys.exit(f"[migrate] 无法启动探针容器：{err.strip()}")
    for _ in range(30):
        c, o, _e = run(["docker", "exec", PROBE, "pg_isready", "-U", "loguser", "-d", "logdb"])
        if c == 0:
            return
        time.sleep(1)
    sys.exit("[migrate] 旧库没有在 30s 内就绪（卷名或 POSTGRES_USER 可能不对）")


def stop_probe():
    run(["docker", "rm", "-f", PROBE])


def src_rows(sql):
    code, out, err = run(["docker", "exec", PROBE, "psql", "-U", "loguser", "-d", "logdb",
                          "-t", "-A", "-c", sql])
    if code != 0:
        sys.exit(f"[migrate] 查询旧库失败：{err.strip()}")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def tgt_query(ns, pod, user, db, sql):
    code, out, err = run(["kubectl", "exec", "-n", ns, pod, "--",
                          "psql", "-U", user, "-d", db, "-t", "-A", "-c", sql])
    if code != 0:
        sys.exit(f"[migrate] 查询目标库失败：{err.strip()}")
    return [line for line in out.splitlines() if line.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库；不加只预览")
    ap.add_argument("--source-volume", default="log_system_pgdata")
    ap.add_argument("--namespace", default="ruilog-system")
    ap.add_argument("--target-pod", default="postgres-0")
    ap.add_argument("--target-user", default="loguser")
    ap.add_argument("--target-db", default="logdb")
    args = ap.parse_args()

    print(f"[migrate] 源：docker 卷 {args.source_volume}")
    print(f"[migrate] 目标：{args.namespace}/{args.target_pod} ({args.target_db})")

    start_probe(args.source_volume)
    try:
        storage = src_rows("SELECT row_to_json(t) FROM storage_config t WHERE id = 1;")
        quotas = src_rows("SELECT row_to_json(t) FROM tenant_quotas t;")
        dashboards = src_rows("SELECT row_to_json(t) FROM dashboards t;")
    finally:
        stop_probe()

    if not storage:
        sys.exit("[migrate] 旧库里没有 storage_config 记录，可能卷不对或从没配过")

    tgt_tenants = set(tgt_query(args.namespace, args.target_pod, args.target_user, args.target_db,
                                "SELECT code FROM tenants;"))
    tgt_dash = {}
    for line in tgt_query(args.namespace, args.target_pod, args.target_user, args.target_db,
                          "SELECT id||'|'||coalesce(name,'') FROM dashboards;"):
        did, dname = line.split("|", 1)
        tgt_dash[dname] = did

    stmts = []
    notes = []

    # ---- storage_config ------------------------------------------------------
    cols = [c for c in ("retain_days", "roll_threshold", "hot_cold_strategy",
                        "quota_gb", "aggregate_retain_days") if c in storage[0]]
    sets = ", ".join(f"{c} = {lit(storage[0][c])}" for c in cols)
    stmts.append(f"-- storage_config："
                 f"quota_gb={storage[0].get('quota_gb')}, "
                 f"retain_days={storage[0].get('retain_days')}, "
                 f"hot_cold={storage[0].get('hot_cold_strategy')}")
    stmts.append(f"UPDATE storage_config SET {sets} WHERE id = 1;")

    # ---- tenant_quotas ------------------------------------------------------
    for row in quotas:
        tenant = row.get("tenant_code")
        if tenant not in tgt_tenants:
            notes.append(f"跳过租户 {tenant}（目标库里不存在）")
            continue
        stmts.append(
            "INSERT INTO tenant_quotas (tenant_code, quota_gb) VALUES "
            f"({lit(tenant)}, {lit(row.get('quota_gb'))}) "
            "ON CONFLICT (tenant_code) DO UPDATE SET "
            "quota_gb = EXCLUDED.quota_gb, updated_at = now();"
        )
        notes.append(f"租户 {tenant} → {row.get('quota_gb')} GB")

    # ---- dashboards ---------------------------------------------------------
    for row in dashboards:
        name = row.get("name") or ""
        tenant = row.get("tenant") or "default"
        vals = [
            ("owner", lit(row.get("owner"))),
            ("description", lit(row.get("description"))),
            ("status", lit(row.get("status"))),
            ("default_range", lit(row.get("default_range"))),
            ("widgets", lit(row.get("widgets") or [], is_jsonb=True)),
        ]
        # 看板列表是按 tenant 等值过滤的：目标库种子行的 tenant='all' 只有管理员
        # 用 tenant=all 巡检时才看得到，普通租户看不到。所以源租户要一起搬过去。
        sets = ", ".join(f"{c} = {v}" for c, v in vals)
        if tenant not in ("", "default"):
            sets = f"tenant = {lit(tenant)}, " + sets
        stmts.append(f"-- dashboard：{name}（widgets {len(row.get('widgets') or [])} 个）")
        stmts.append(f"UPDATE dashboards SET {sets}, updated_at = now() WHERE name = {lit(name)};")
        ins_cols = "tenant, name, " + ", ".join(c for c, _ in vals)
        ins_vals = f"{lit(tenant)}, {lit(name)}, " + ", ".join(v for _, v in vals)
        stmts.append(
            f"INSERT INTO dashboards ({ins_cols}) SELECT {ins_vals} "
            f"WHERE NOT EXISTS (SELECT 1 FROM dashboards WHERE name = {lit(name)});"
        )
        if name in tgt_dash:
            notes.append(f"看板 {name}：更新目标库 id={tgt_dash[name]}")
        else:
            notes.append(f"看板 {name}：目标库没有，将插入")

    print("\n[migrate] 计划：")
    for n in notes:
        print("  -", n)
    print()
    print("\n".join(stmts))

    if not args.apply:
        print("\n[migrate] 预览模式，未写库。确认无误后加 --apply 执行。")
        return

    sql = "BEGIN;\n" + "\n".join(stmts) + "\nCOMMIT;\n"
    code, out, err = run(["kubectl", "exec", "-i", "-n", args.namespace, args.target_pod, "--",
                          "psql", "-U", args.target_user, "-d", args.target_db,
                          "-v", "ON_ERROR_STOP=1"], stdin_text=sql)
    if code != 0:
        sys.exit(f"[migrate] 写库失败，已回滚：{(err or out).strip()}")
    print("\n[migrate] 已写入目标库（单事务提交）：")
    print(out.strip() or "(psql 无输出，视为成功)")


if __name__ == "__main__":
    main()
