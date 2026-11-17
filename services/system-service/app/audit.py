"""审计写入：管理面与日志空间管理共用。

抽成独立模块是为了避免 storage.py 反向 import main.py 造成循环依赖。
行为与原 main.write_audit 完全一致：失败不阻断业务，只留痕到服务日志。
"""
import json
from typing import Any, Dict, Optional

from .db import pg_exec


def write_audit(user: Dict[str, Any], action: str, target: str = "",
                detail: Optional[Dict[str, Any]] = None, ip: str = "",
                result: str = "success") -> None:
    try:
        pg_exec(
            """INSERT INTO audit_log (tenant_id, user_id, user_name, account,
                                      action, target, detail, ip, result)
               VALUES ((SELECT id FROM tenants WHERE code=%s), %s, %s, %s, %s, %s, %s, %s, %s)""",
            (user.get("tenant_code"), user.get("id"), user.get("name", ""),
             user.get("account", ""), action, target,
             json.dumps(detail or {}, ensure_ascii=False), ip, result),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[system-service] audit write failed: {e}", flush=True)
