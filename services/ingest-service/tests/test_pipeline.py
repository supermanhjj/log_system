"""解析管线契约：优先级、映射转换、active/shadow 两种落地方式、租户写法。"""
from app import pipeline


def _parser(kind_pattern="(?P<level>ERROR|INFO)", pid: int = 1):
    return {"id": pid, "definition": {"type": "regex", "pattern": kind_pattern}}


def _rule(rule_id, priority, parser_id, mapping=None, tenant_code="", match=None):
    return {"id": rule_id, "name": f"rule{rule_id}", "priority": priority,
            "parser_id": parser_id, "mapping": mapping or {}, "tenant_code": tenant_code,
            "match": match or {}}


# ---------------- 优先级 ----------------
def test_priority_zero_runs_before_priority_100():
    # 100 与 0 同命中时，priority 数值更小的必须先执行（默认值 100 不能被当成 0 的下界）
    rules = [_rule(2, 100, 2, {"level": "level"}), _rule(1, 0, 1, {"level": "level"})]
    pmap = {1: _parser(pid=1), 2: _parser(pid=2)}
    result = pipeline.execute_one({"raw": "ERROR"}, rules, pmap)
    assert result["pipelineName"] == "rule1"


def test_lower_priority_wins_in_normal_order():
    rules = [_rule(1, 10, 1, {"level": "level"}), _rule(2, 20, 2, {"level": "level"})]
    pmap = {1: _parser(pid=1), 2: _parser(pid=2)}
    assert pipeline.execute_one({"raw": "ERROR"}, rules, pmap)["pipelineId"] == 1


# ---------------- 匹配 ----------------
def test_match_combines_conditions_and_list_values():
    rule = _rule(1, 100, 1, match={"log_class": "business", "level": ["ERROR", "WARN"]})
    assert pipeline.matches(rule, {"log_class": "business", "level": "WARN"}) is True
    assert pipeline.matches(rule, {"log_class": "business", "level": "INFO"}) is False
    assert pipeline.matches(rule, {"log_class": "system", "level": "ERROR"}) is False


def test_match_path_glob_on_src_file():
    rule = _rule(1, 100, 1, match={"path_glob": "/var/log/nginx/*.log"})
    assert pipeline.matches(rule, {"src_file": "/var/log/nginx/error.log"}) is True
    assert pipeline.matches(rule, {"src_file": "/var/log/redis/redis.log"}) is False


# ---------------- 映射 ----------------
def test_mapping_casts_types_and_skips_bad_values():
    fields = {"code": "404", "cost": "12.5", "bad": "x"}
    mapped = pipeline._apply_mapping(fields, {"status": "code:int", "attrs.cost": "cost:float",
                                              "num": "bad:int"})
    assert mapped == {"status": 404, "attrs.cost": 12.5}


def test_mapping_falls_back_to_leaf_name():
    mapped = pipeline._apply_mapping({"level": "ERROR"}, {"level": "attrs.level"})
    assert mapped == {"level": "ERROR"}


def test_mapping_normalizes_level_value():
    result = pipeline.execute_one({"raw": "err"}, [_rule(1, 100, 1, {"level": "level"})],
                                  {1: _parser("(?P<level>err)")})
    assert result["mapped"]["level"] == "ERROR"


# ---------------- 落地模式 ----------------
def test_active_mode_overrides_main_fields_and_sets_parsed(monkeypatch):
    rules = [_rule(1, 100, 1, {"level": "level", "attrs.thread": "thread"},
                   tenant_code="tenant-a", match={"log_class": "business"})]
    monkeypatch.setattr(pipeline, "_load", lambda: (rules, {1: _parser() }))
    doc = {"raw": "ERROR", "log_class": "business", "level": "INFO"}
    out = pipeline.process([doc], mode="active")[0]
    assert out["level"] == "ERROR"
    assert out["parse_status"] == "parsed"
    assert out["attrs"]["__tenant_hint"] == "tenant-a"
    # 解析规则永远不能直接写 tenant_id，只能给提示
    assert "tenant_id" not in out


def test_active_mode_unmatched_keeps_record_and_marks_unmatched(monkeypatch):
    rules = [_rule(1, 100, 1, {"level": "level"}, match={"log_class": "system"})]
    monkeypatch.setattr(pipeline, "_load", lambda: (rules, {1: _parser() }))
    out = pipeline.process([{"raw": "INFO", "log_class": "business"}], mode="active")[0]
    assert out["parse_status"] == "unmatched"
    assert out.get("level") is None


def test_shadow_mode_does_not_touch_main_fields(monkeypatch):
    rules = [_rule(1, 100, 1, {"level": "level"})]
    monkeypatch.setattr(pipeline, "_load", lambda: (rules, {1: _parser() }))
    doc = {"raw": "ERROR", "level": "INFO"}
    out = pipeline.process([doc], mode="shadow")[0]
    assert out["level"] == "INFO"
    assert out["parse_status"] == "parsed"
    assert out["attrs"]["__shadow"]["mapped"]["level"] == "ERROR"


def test_off_mode_skips_everything(monkeypatch):
    def boom():
        raise AssertionError("off 模式不应读取规则库")

    monkeypatch.setattr(pipeline, "_load", boom)
    doc = {"raw": "ERROR", "level": "INFO"}
    assert pipeline.process([doc], mode="off")[0]["level"] == "INFO"


def test_rule_parse_error_does_not_block_other_rules(monkeypatch):
    bad = _rule(1, 10, 1, {"level": "level"})
    good = _rule(2, 20, 2, {"level": "level"})
    pmap = {1: {"id": 1, "definition": {"type": "regex", "pattern": "(?P<level>ERROR"}},
            2: _parser(pid=2)}
    monkeypatch.setattr(pipeline, "_load", lambda: ([bad, good], pmap))
    out = pipeline.process([{"raw": "ERROR"}], mode="active")[0]
    assert out["parse_status"] == "parsed"
    assert out["level"] == "ERROR"


# ---------------- 时间归一 ----------------
def test_iso_utc_numeric_and_offset_inputs():
    assert pipeline._iso_utc(1763680800) == "2025-11-20T23:20:00.000Z"
    assert pipeline._iso_utc(1763680800000) == "2025-11-20T23:20:00.000Z"
    assert pipeline._iso_utc("2026-11-21T10:00:00+08:00") == "2026-11-21T02:00:00.000Z"
    assert pipeline._iso_utc("2026-11-21 10:00:00") == "2026-11-21T10:00:00.000Z"


def test_iso_utc_unrecognized_returns_none():
    assert pipeline._iso_utc("不是时间") is None
    assert pipeline._iso_utc("") is None
