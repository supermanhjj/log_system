"""写入入口归属判定契约：服务端推导优先、越白名单拒收、时间归一、租户强制覆盖。"""
import time

import pytest

from app import main


@pytest.fixture
def cst_tz(monkeypatch):
    """部署约定：容器 TZ=Asia/Shanghai，naive 时间按东八区解释。"""
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


@pytest.fixture
def empty_ledgers(monkeypatch):
    """台账默认空：namespace / host 都查不到，用于隔离外部依赖。"""
    monkeypatch.setattr(main, "namespace_tenant_map", lambda: {})
    monkeypatch.setattr(main, "host_tenant_map", lambda: {})
    audits = []
    monkeypatch.setattr(main, "audit_tenant_decision", lambda *a, **k: audits.append(a))
    return audits


# ---------------- 来源解析 ----------------
def test_k8s_namespace_from_src():
    assert main.k8s_namespace_from_src(
        "/var/log/containers/order-7d9f_i6000_order-1a2b.log") == "i6000"
    assert main.k8s_namespace_from_src("/var/log/syslog") == ""
    assert main.k8s_namespace_from_src("/var/log/containers/single.log") == ""


# ---------------- 归属判定 ----------------
def test_namespace_mapping_beats_injected_tenant(empty_ledgers, monkeypatch):
    monkeypatch.setattr(main, "namespace_tenant_map", lambda: {"i6000": "tenant-a"})
    rec = {"tenant_id": "tenant-b",
           "src_file": "/var/log/containers/pod_i6000_app.log"}
    assert main.derive_tenant(rec, None) == ("tenant-a", "mismatch")
    assert empty_ledgers and empty_ledgers[-1][0] == "mismatch"


def test_host_ledger_used_when_no_namespace(empty_ledgers, monkeypatch):
    monkeypatch.setattr(main, "host_tenant_map", lambda: {"web-01": "tenant-a"})
    assert main.derive_tenant({"host": "web-01", "tenant_id": "tenant-x"}, None) == (
        "tenant-a", "mismatch")


def test_out_of_scope_rejected_and_audited(empty_ledgers, monkeypatch):
    monkeypatch.setattr(main, "namespace_tenant_map", lambda: {"prod": "tenant-b"})
    rec = {"src_file": "/var/log/containers/pod_prod_app.log"}
    collector = {"code": "c1", "tenant_allow": ["tenant-a"]}
    assert main.derive_tenant(rec, collector) == ("", "out_of_scope")
    assert empty_ledgers[-1][0] == "out_of_scope"


def test_injected_tenant_alone_is_not_trusted(empty_ledgers):
    # 判不出归属就必须拒收：只凭采集端注入的 tenant_id 认账等于把租户隔离交给调用方
    rec = {"tenant_id": "tenant-b"}
    collector = {"code": "env-legacy", "tenant_allow": []}
    assert main.derive_tenant(rec, collector)[0] == ""


# ---------------- 时间归一 ----------------
def test_to_iso_utc_epoch_and_iso_forms(monkeypatch):
    assert main.to_iso_utc(1763680800) == "2025-11-20T23:20:00.000Z"
    assert main.to_iso_utc(1763680800123) == "2025-11-20T23:20:00.123Z"
    assert main.to_iso_utc("1763680800") == "2025-11-20T23:20:00.000Z"
    assert main.to_iso_utc("2026-11-21T10:00:00+08:00") == "2026-11-21T02:00:00.000Z"
    assert main.to_iso_utc("2026-11-21T10:00:00Z") == "2026-11-21T10:00:00.000Z"


def test_to_iso_utc_naive_string_uses_deploy_timezone(cst_tz):
    assert main.to_iso_utc("2026-11-21 10:00:00") == "2026-11-21T02:00:00.000Z"


def test_to_iso_utc_rejects_non_time_values():
    assert main.to_iso_utc("") is None
    assert main.to_iso_utc(True) is None
    assert main.to_iso_utc(123) is None
    assert main.to_iso_utc("不是时间") is None


# ---------------- 记录归一 ----------------
def test_normalize_record_forces_tenant_and_drops_time_aliases(cst_tz):
    rec = {"@timestamp": "2026-11-21 10:00:00", "date": "2026-11-21",
           "tenant_id": "tenant-b", "level": "err", "message": "password=x"}
    out = main.normalize_record(rec, "tenant-a")
    assert out["tenant_id"] == "tenant-a"
    assert out["@timestamp"] == "2026-11-21T02:00:00.000Z"
    assert "date" not in out
    assert out["level"] == "ERROR"
    assert out["message"] == "password=***"


def test_normalize_record_falls_back_to_ingest_time():
    out = main.normalize_record({"message": "m"}, "tenant-a")
    assert out["@timestamp"].endswith("Z")
    assert out["tenant_id"] == "tenant-a"
