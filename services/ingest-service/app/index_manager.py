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
    """未归属租户统一落 logs-unknown，绝不静默写 default。"""
    if not tenant or tenant in ("all", "*"):
        return "unknown"
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
        if _ready.get(t) == date:
            return write_alias(t)

        idx = daily_index(t, date)
        wa = write_alias(t)
        ra = read_alias(t)
        try:
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
            old = client.indices.get_alias(name=wa, ignore=404) or {}
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
