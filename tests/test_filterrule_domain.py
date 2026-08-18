"""过滤规则领域：内置规则集查询、规则表达式解析器及其加速协议的行为验证。

覆盖加速器未注入时的纯 Python 解析路径、加速器注入后的解析路径，
并断言两条路径在同一表达式上解析结果一致。
"""

import pytest

from app.domain.filterrule import (
    BUILTIN_RULE_SET,
    RuleParser,
    configure_filter_rule_runtime,
    get_builtin_rule_set,
    get_filter_rule_accelerator,
    parse_rule_group,
)


@pytest.fixture(autouse=True)
def _restore_filter_rule_accelerator():
    """用例结束后恢复用例开始前的加速器注册，避免影响其它用例。"""
    original = get_filter_rule_accelerator()
    yield
    configure_filter_rule_runtime(accelerator=original)


class _FakeAccelerator:
    """规则表达式解析加速器测试替身，记录调用并返回预置结果。"""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def parse_filter_rule(self, expression: str):
        """记录传入表达式并返回预置的解析结果。"""
        self.calls.append(expression)
        return self._result


def test_get_builtin_rule_set_returns_shared_definition():
    """内置规则集查询函数应返回模块内维护的同一份定义。"""
    assert get_builtin_rule_set() is BUILTIN_RULE_SET
    assert "4K" in get_builtin_rule_set()


def test_rule_parser_falls_back_to_python_when_accelerator_not_injected():
    """未注入加速器时应回落到纯 Python 解析路径，保持既有布尔表达式语义。"""
    configure_filter_rule_runtime(accelerator=None)

    result = RuleParser().parse("HDR & !BLU").as_list()

    assert result == [["HDR", "and", ["not", "BLU"]]]


def test_rule_parser_falls_back_to_python_when_accelerator_returns_none():
    """加速器返回空值（不可用/未启用）时应回落到纯 Python 解析路径。"""
    configure_filter_rule_runtime(accelerator=_FakeAccelerator(None))

    result = RuleParser().parse("HDR & !BLU").as_list()

    assert result == [["HDR", "and", ["not", "BLU"]]]


def test_rule_parser_uses_injected_accelerator_and_matches_python_result():
    """注入加速器后应优先使用其解析结果，且与纯 Python 路径的解析结果一致。"""
    configure_filter_rule_runtime(accelerator=None)
    python_result = RuleParser().parse("HDR & !BLU").as_list()

    fake_accelerator = _FakeAccelerator(python_result)
    configure_filter_rule_runtime(accelerator=fake_accelerator)

    accelerated_result = RuleParser().parse("HDR & !BLU").as_list()

    assert fake_accelerator.calls == ["HDR & !BLU"]
    assert accelerated_result == python_result


def test_parse_rule_group_matches_rule_parser_output():
    """单层规则表达式解析函数应与 RuleParser 直接解析的结果一致。"""
    expected = RuleParser().parse("4K").as_list()[0]

    assert parse_rule_group("4K") == expected
