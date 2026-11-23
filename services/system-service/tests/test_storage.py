"""日志空间契约：索引名反解租户、容量单位、索引名白名单。"""
import re

import pytest

from app import storage


def test_index_tenant_handles_daily_index_write_alias_and_plain_index():
    assert storage.index_tenant("logs-i6000-2026.11.20") == "i6000"
    assert storage.index_tenant("logs-i6000") == "i6000"
    assert storage.index_tenant("logs-i6000-write") == "i6000"
    assert storage.index_tenant("logs-a-b-2026.11.20") == "a-b"
    assert storage.index_tenant("top_queries-2026.11.20") == ""


def test_fmt_bytes_switches_units():
    assert storage.fmt_bytes(0) == "0 B"
    assert storage.fmt_bytes(1023) == "1023 B"
    assert storage.fmt_bytes(1536) == "2 KB"  # B/KB 取整，MB 起保留两位
    assert storage.fmt_bytes(19 * 1024 ** 2) == "19.00 MB"
    assert storage.fmt_bytes(5 * 1024 ** 3) == "5.00 GB"


def test_trend_unit_follows_magnitude():
    assert storage.trend_unit(19 * 1024 ** 2) == ("MB", float(storage.MB))
    assert storage.trend_unit(2 * storage.GB) == ("GB", float(storage.GB))


def test_index_name_whitelist_blocks_path_and_injection():
    rx = re.compile(storage.INDEX_NAME_RE)
    assert rx.match("logs-i6000-2026.11.20")
    assert rx.match("logs-i6000")
    for bad in ("logs-../etc/passwd", "logs-i6000;drop", "*", "logs-i6000,logs-x"):
        assert rx.match(bad) is None


def test_quota_input_rejects_negative():
    from pydantic import ValidationError

    assert storage.QuotaIn(quotaGB=0).quotaGB == 0
    with pytest.raises(ValidationError):
        storage.QuotaIn(quotaGB="abc")
