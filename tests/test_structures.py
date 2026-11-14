"""
验证集合展开工具的标量保留与输入边界。
"""

from app.foundation.collections import ListUtils


def test_flatten_keeps_scalar_items_in_mixed_list() -> None:
    """
    展开混合列表时保留原有标量及嵌套项顺序。
    """
    assert ListUtils.flatten([1, [2, 3], 4]) == [1, 2, 3, 4]


def test_flatten_returns_plain_list_unchanged() -> None:
    """
    普通列表展开后内容保持不变。
    """
    source = [1, 2, 3]
    assert ListUtils.flatten(source) == source


def test_flatten_rejects_non_list_input() -> None:
    """
    非列表输入返回空列表。
    """
    assert ListUtils.flatten("1,2,3") == []
