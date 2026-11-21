"""
验证旧规则 ID 的完整定位、规则配置诊断与过滤器的延迟解析合同。
"""

from collections import Counter
from types import SimpleNamespace

import pytest

from app.application import rules
from app.chain.search.manual import _ManualPage
from app.domain.context import MediaInfo, TorrentInfo
from app.modules.filter import FilterModule
from app.schemas.rule import CustomRule, FilterRuleGroup
from app.schemas.types import MediaType


@pytest.mark.parametrize("accelerated", [False, True])
def test_runtime_reports_full_legacy_id_and_rule_group(monkeypatch, accelerated) -> None:
    """
    Python 解析及 Rust 失败后的回退都应定位完整旧 ID，而不是只报告 Candidate。
    """
    group = FilterRuleGroup(name="国产剧", rule_string="1080P &  Pre Candidate")
    module = FilterModule()
    module.rulehelper = SimpleNamespace(get_rule_group_by_media=lambda **_kwargs: [group])
    module.rule_set = {**rules.BUILTIN_RULE_SET, " Pre Candidate": {"include": "QHstudIo"}}
    monkeypatch.setattr("app.modules.filter.rust_accel.is_enabled", lambda: accelerated)
    with pytest.raises(Exception) as caught:
        module.filter_torrents(
            rule_groups=["国产剧"],
            torrent_list=[TorrentInfo(title="Show S01E01 1080p QHstudIo", description="")],
        )
    message = str(caught.value)
    assert "国产剧" in message
    assert "' Pre Candidate'" in message
    assert "无效的规则 ID" in message
    assert "found 'Candidate'" not in message


@pytest.mark.parametrize(
    ("expression", "ids", "code", "invalid_ids"),
    [
        ("1080P &  Pre Candidate", [" Pre Candidate"], "rule_id_invalid", [" Pre Candidate"]),
        ("!M-Team & 1080P", ["M-Team"], "rule_id_invalid", ["M-Team"]),
        ("!Missing & 1080P", [], "rule_id_missing", ["Missing"]),
        ("1080P >", [], "rule_syntax_invalid", []),
        ("(1080P", [], "rule_syntax_invalid", []),
        ("1080P & 123", ["123"], "rule_id_invalid", ["123"]),
    ],
)
def test_rule_expression_diagnosis_keeps_complete_operands(expression, ids, code, invalid_ids) -> None:
    """
    配置诊断保留完整 ID，区分无效 ID、悬空引用和真正的表达式语法错误。
    """
    issue = rules.describe_rule_string_error(expression, [*rules.BUILTIN_RULE_SET, *ids])
    assert issue is not None
    assert issue.code == code
    assert list(issue.rule_ids) == invalid_ids


def test_valid_rule_expression_has_no_configuration_warning() -> None:
    """
    括号、否定、多层优先级和数字开头的合法 ID 都不产生配置告警。
    """
    assert rules.describe_rule_string_error("(1080P | 4K) & !BLU > 60FPS", rules.BUILTIN_RULE_SET) is None


def test_diagnostics_only_checks_selected_applicable_rule_groups(monkeypatch) -> None:
    """
    无效电影规则不污染电视剧搜索，未选择的组也不进入当前页诊断。
    """
    helper = rules.RuleHelper()
    groups = [
        FilterRuleGroup(name="国产剧", media_type="电视剧", rule_string="1080P &  Pre Candidate"),
        FilterRuleGroup(name="电影", media_type="电影", rule_string="!Missing"),
        FilterRuleGroup(name="未选择", rule_string="!OtherMissing"),
    ]
    monkeypatch.setattr(helper, "get_rule_groups", lambda: groups)
    monkeypatch.setattr(helper, "get_custom_rules", lambda: [CustomRule(id=" Pre Candidate", name="候补组")])
    warnings = helper.diagnose_rule_groups(
        media=MediaInfo(type=MediaType.TV), group_names=["国产剧", "电影", "已删除", "国产剧"],
    )
    assert [warning.model_dump() for warning in warnings] == [{
        "code": "rule_id_invalid", "rule_group": "国产剧", "rule_ids": [" Pre Candidate"],
    }]
    assert helper.diagnose_rule_groups(media=None, group_names=[]) == []


def test_terminal_page_diagnostics_contains_rule_configuration_warning(monkeypatch) -> None:
    """
    页完成事件携带可供前端显示的完整规则定位，不复制 pyparsing 的原始报错。
    """
    monkeypatch.setattr(rules.RuleHelper, "get_rule_groups", staticmethod(lambda: [
        FilterRuleGroup(name="国产剧", media_type="电视剧", rule_string="1080P &  Pre Candidate"),
    ]))
    monkeypatch.setattr(rules.RuleHelper, "get_custom_rules", staticmethod(lambda: [
        CustomRule(id=" Pre Candidate", name="候补组"),
    ]))
    page = object.__new__(_ManualPage)
    page.plan = SimpleNamespace(rule_groups=["国产剧"])
    page.raw_title = False
    page.media = MediaInfo(type=MediaType.TV)
    page.search_id = "legacy-rule-search"
    page.torrents = [TorrentInfo(title="Show S01E01 1080p")]
    page.counts = Counter()
    page.observations = []
    diagnostics = page.diagnostics([]).model_dump(mode="json")
    assert diagnostics["warnings"] == [{
        "code": "rule_id_invalid", "rule_group": "国产剧", "rule_ids": [" Pre Candidate"],
    }]
    assert diagnostics["stage_counts"] == {"candidates": 1, "retained": 0}
    assert diagnostics["rejection_reasons"] == []
    assert "Expected end of text" not in str(diagnostics)
