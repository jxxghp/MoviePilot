"""
验证 agent API 参数合同对空字符串联合参数的类型归一行为。
"""

from app.agent.api.arguments import _normalize_value


def test_empty_string_with_null_union_normalizes_to_none():
    """
    空字符串在 anyOf[integer, null] 下表示模型未提供该值，归一为 None。
    """
    schema = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert _normalize_value("", schema, {}) is None
    assert _normalize_value("   ", schema, {}) is None


def test_empty_string_with_string_union_is_preserved():
    """
    anyOf[string, null] 的空字符串本身是合法值，不应被改成 None。
    """
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert _normalize_value("", schema, {}) == ""


def test_integer_union_still_accepts_integer():
    """
    正常整数输入不受空字符串容错影响。
    """
    schema = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert _normalize_value(3, schema, {}) == 3
