"""日志索引生命周期写入侧：按天建索引 + 读写别名维护。

为什么必须有这一层
------------------
改造前所有日志写死进 `logs-<tenant>` 单索引。50GB/天跑一个月单索引就到 2.25TB，
超过 50GB 后 OpenSearch 查询与段合并明显劣化，且过期数据只能 delete_by_query
（逐条删、代价极高）。改成按天索引后，过期数据由 ISM 直接 drop 整个索引，是元数据
操作，毫秒级、零 IO。

索引与别名约定
--------------
  日期索引   logs-<tenant>-YYYY.MM.DD     实际数据存储，由 ISM 管理生命周期
  写别名     logs-<tenant>-write           始终只指向今天的索引（OpenSearch 要求
                                           写入别名只能指向一个索引）
  读别名     logs-<tenant>                 指向该租户所有日期索引，查询侧代码零改动

日期取 UTC，与 @timestamp 的 UTC 语义一致。北京时间早 8 点前会写进"昨天"日期后缀
的索引，不影响功能（ISM 按索引创建时间计算），且避免同一时刻出现两个"今天"的判断。

降级策略（关键）
----------------
别名维护失败时**不阻断写入**：返回当天日期索引名让 bulk 直接写死索引。
日志丢失比索引名不规范严重得多；后续请求会重试别名维护，自愈。
"""
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# tenant -> 已完成别名维护的 UTC 日期；命中后走极快路径，不每次请求都打 OS
_ready: Dict[str, str] = {}
_lock = threading.Lock()


def index_date() -> str:
    """当前 UTC 日期，格式 YYYY.MM.DD。"""
    return datetime.now(timezone.utc).strftime("%Y.%m.%d")


def normalize_tenant(tenant: Optional[str]) -> str:
    """写入目标必须有明确的租户编码。

    平台只保留真实业务系统租户，没有 `unknown` 这类兜底桶：无法归属的日志
    在入口（derive_tenant 之后）就被拒收并计数，不会被静默塞进某个模糊索引。
    因此这里是**硬约束**——传入空租户/聚合范围直接报错，让问题立刻暴露，
    而不是生成一个谁都说不清归属的索引。
    """
    if not tenant or tenant in ("all", "*"):
        raise ValueError(
            f"写入需要明确的租户编码，收到 {tenant!r}；无法归属的日志应在入口拒收，不允许兜底落库"
        )
    return tenant


def write_alias(tenant: Optional[str]) -> str:
    return f"logs-{normalize_tenant(tenant)}-write"


def read_alias(tenant: Optional[str]) -> str:
    return f"logs-{normalize_tenant(tenant)}"


def daily_index(tenant: Optional[str], date: Optional[str] = None) -> str:
    return f"logs-{normalize_tenant(tenant)}-{date or index_date()}"


def ensure_write_target(client: Any, tenant: Optional[str]) -> str:
    """确保当天索引存在且读写别名就绪，返回可安全写入的目标名。

    幂等：缓存命中当天日期直接返回；跨天或别名被外部改动时重建。
    任何异常都降级为日期索引名，保证日志不丢。
    """
    t = normalize_tenant(tenant)
    date = index_date()
    with _lock:
        wa = write_alias(t)
        # 缓存命中也要确认写别名仍是"别名"。若索引被外部删除/重建，
        # 缓存还记着"已就绪"就直接返回 wa，而此时 wa 不存在，OpenSearch bulk
        # 会自动创建同名真实索引 logs-<tenant>-write——数据写进去后查询侧查
        # 读别名 logs-<tenant> 找不到，表现为"清空重建后新日志查不到"。
        # exists_alias 是轻量元数据调用，本地量级可忽略；生产高频可改为 TTL 探测。
        if _ready.get(t) == date:
            try:
                if client.indices.exists_alias(name=wa):
                    return wa
            except Exception:  # noqa: BLE001
                pass  # 探测失败 → 缓存失效，走下方重建
            _ready.pop(t, None)

        idx = daily_index(t, date)
        ra = read_alias(t)
        try:
            # 0) 自愈：写别名名若已被误建成真实索引（缓存陈旧 + 别名被外部删除时，
            #    bulk 写 wa 会自动创建同名索引），必须先删掉才能建同名别名。
            #    只在"是索引且不是别名"时删，且 ignore_unavailable 避免竞态报错。
            try:
                if client.indices.exists(index=wa) and not client.indices.exists_alias(name=wa):
                    client.indices.delete(index=wa)
                    print(f"[index_manager] removed stale real index {wa}", flush=True)
            except Exception:  # noqa: BLE001
                pass  # 删不掉就让它走到别名维护，失败则降级日期索引

            # 1) 建当天索引。mapping / shards / replicas 全部来自 index template，
            #    这里不再传 TENANT_INDEX_MAPPING——那是两套冲突 mapping 的根源。
            if not client.indices.exists(index=idx):
                try:
                    client.indices.create(index=idx)
                except Exception as e:  # noqa: BLE001
                    # 并发下另一个进程可能刚建好，resource_already_exists 可忽略
                    if "resource_already_exists" not in str(e).lower():
                        raise

            # 2) 取写别名当前指向（跨天前指向昨天），原子地切成今天。
            # opensearch-py 在别名不存在时，即便 ignore=404 也可能返回 {'error': ...}
            # 而非空 dict。旧代码直接遍历其 key，会拿 "error" 当索引名去 remove，
            # 导致整个别名维护失败、降级直写日期索引（读别名永远建不起来）。
            # 只保留形如 logs-* 的真实索引名，过滤掉错误响应等非索引 key。
            alias_res = client.indices.get_alias(name=wa, ignore=404) or {}
            old = [k for k in alias_res if isinstance(k, str) and k.startswith("logs-")]
            actions = [
                {"add": {"index": idx, "alias": wa}},
                {"add": {"index": idx, "alias": ra}},
            ]
            for old_idx in old:
                if old_idx != idx:
                    actions.append({"remove": {"index": old_idx, "alias": wa}})
            client.indices.update_aliases(body={"actions": actions})

            _ready[t] = date
            return wa
        except Exception as e:  # noqa: BLE001
            # 降级：直接写日期索引。下次请求会重试别名维护，自愈。
            _ready.pop(t, None)
            print(f"[index_manager] alias setup failed for {t}: {e}; "
                  f"fallback to {idx}", flush=True)
            return idx


def forget(tenant: Optional[str] = None) -> None:
    """清除缓存。租户注销、索引被外部删除或测试时调用。"""
    with _lock:
        if tenant is None:
            _ready.clear()
        else:
            _ready.pop(normalize_tenant(tenant), None)
