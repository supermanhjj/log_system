"""NL→PPL 闸门契约：索引段剥离、写操作拒绝、租户维度收发、head 上限、时间词换算。"""
from datetime import datetime, timedelta, timezone

import pytest

from app import nl2ppl

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 21, 10, 0, 0, tzinfo=CST)


# ---------------- 索引段 / 命令白名单 ----------------
def test_source_segment_is_stripped():
    ppl, warnings = nl2ppl.normalize_ppl("source=logs-* | where level = 'ERROR'", [])
    assert ppl == "where level = 'ERROR' | head 100"
    assert any("source" in w for w in warnings)


def test_only_source_segment_is_rejected():
    with pytest.raises(nl2ppl.Nl2PplError) as err:
        nl2ppl.normalize_ppl("source=logs-*", [])
    assert err.value.stage == "validate"


@pytest.mark.parametrize("ppl", [
    "where level='ERROR' | delete",
    "where level='ERROR' | DROP",
    "insert into logs",
])
def test_write_operations_are_rejected(ppl):
    with pytest.raises(nl2ppl.Nl2PplError):
        nl2ppl.normalize_ppl(ppl, [])


def test_write_word_inside_literal_is_not_treated_as_write():
    # 检索正文里的「delete failed」是数据，不是命令
    ppl, _ = nl2ppl.normalize_ppl("where message = 'delete failed'", [])
    assert ppl == "where message = 'delete failed' | head 100"


def test_first_segment_must_be_query_command():
    with pytest.raises(nl2ppl.Nl2PplError):
        nl2ppl.normalize_ppl("level = 'ERROR' | head 10", [])


def test_unknown_field_warns_but_keeps_statement():
    ppl, warnings = nl2ppl.normalize_ppl("where foo = 1", [])
    assert ppl == "where foo = 1 | head 100"
    assert any("foo" in w for w in warnings)


# ---------------- head 上限 ----------------
def test_missing_head_is_appended():
    ppl, warnings = nl2ppl.normalize_ppl("stats count() by level", [])
    assert ppl.endswith("| head 100")
    assert any("上限" in w for w in warnings)


def test_head_above_max_is_capped():
    ppl, warnings = nl2ppl.normalize_ppl("where level='ERROR' | head 900", [])
    assert ppl.endswith("head 500")
    assert warnings


def test_head_within_max_is_kept():
    ppl, _ = nl2ppl.normalize_ppl("where level='ERROR' | head 20", [])
    assert ppl == "where level='ERROR' | head 20"


# ---------------- 单租户：剥离租户条件 ----------------
def test_single_tenant_strips_tenant_predicate_but_keeps_other_conditions():
    ppl, warnings = nl2ppl.normalize_ppl(
        "where tenant_id = '总部生产' | stats count() by level", [])
    assert ppl == "stats count() by level | head 100"
    assert any("租户" in w for w in warnings)


def test_single_tenant_drops_stats_by_tenant_dimension():
    ppl, _ = nl2ppl.normalize_ppl("stats count() by tenant, level", [])
    assert ppl == "stats count() by level | head 100"


def test_single_tenant_only_tenant_condition_leaves_head():
    ppl, warnings = nl2ppl.normalize_ppl("where tenant = 'x'", [])
    assert ppl == "head 100"
    assert any("租户" in w for w in warnings)


# ---------------- 全部租户：收敛为虚拟字段 ----------------
def test_cross_tenant_canonicalizes_physical_field_names():
    ppl, warnings = nl2ppl.normalize_ppl("where tenant_name = '总部生产'", [], cross_tenant=True)
    assert ppl == "where tenant = '总部生产' | head 100"
    assert any("tenant" in w for w in warnings)


def test_cross_tenant_keeps_literals_untouched():
    ppl, _ = nl2ppl.normalize_ppl("where message like '%tenant_id%'", [], cross_tenant=True)
    assert "'%tenant_id%'" in ppl


def test_inject_tenant_filter_prepends_independent_where():
    assert nl2ppl.inject_tenant_filter("where level='ERROR'", "i6000") == (
        "where tenant = 'i6000' | where level='ERROR'")


def test_allowed_fields_opens_tenant_only_for_cross_tenant():
    rules = []
    assert "tenant" not in nl2ppl.allowed_fields(rules, cross_tenant=False)
    assert "tenant" in nl2ppl.allowed_fields(rules, cross_tenant=True)
    assert "tenant_id" not in nl2ppl.allowed_fields(
        [{"category": "field_alias", "rule_value": "tenant_id"}], cross_tenant=True)


# ---------------- 模型输出解析 ----------------
def test_parse_model_output_handles_json_and_code_fence():
    assert nl2ppl.parse_model_output('```json\n{"ppl": "where level = 1"}\n```')["ppl"] == (
        "where level = 1")


def test_parse_model_output_accepts_bare_ppl_line():
    parsed = nl2ppl.parse_model_output("where level = 'ERROR' | head 10")
    assert parsed["ppl"] == "where level = 'ERROR' | head 10"


def test_parse_model_output_rejects_garbage():
    with pytest.raises(nl2ppl.Nl2PplError):
        nl2ppl.parse_model_output("抱歉，我无法回答这个问题")


# ---------------- 时间词 ----------------
def test_resolve_time_relative_window():
    r = nl2ppl.resolve_time("最近 30 分钟的错误日志", NOW)
    assert r["from"] == "2026-11-21T09:30:00+08:00"
    assert r["to"] == "2026-11-21T10:00:00+08:00"


def test_resolve_time_natural_day():
    assert nl2ppl.resolve_time("今天的告警", NOW)["from"] == "2026-11-21T00:00:00+08:00"
    assert nl2ppl.resolve_time("昨天的告警", NOW)["to"] == "2026-11-21T00:00:00+08:00"


def test_resolve_time_none_when_no_time_word():
    assert nl2ppl.resolve_time("查一下错误日志", NOW) is None


# ---------------- 租户一致性提示 ----------------
TENANTS = [{"code": "i6000", "name": "总部生产"}, {"code": "pms", "name": "项目管理"}]


def test_detect_tenant_same_scope_is_not_mismatch():
    hint = nl2ppl.detect_tenant("总部生产最近报错", "i6000", TENANTS)
    assert hint["applied"] is True and hint["mismatch"] is False


def test_detect_tenant_other_scope_is_mismatch():
    hint = nl2ppl.detect_tenant("总部生产最近报错", "pms", TENANTS)
    assert hint["mismatch"] is True and hint["code"] == "i6000"


def test_detect_tenant_multiple_mentions_do_not_narrow():
    hint = nl2ppl.detect_tenant("对比总部生产与项目管理", "all", TENANTS)
    assert hint["applied"] is False and hint["code"] == ""
    assert "多个租户" in hint["message"]
