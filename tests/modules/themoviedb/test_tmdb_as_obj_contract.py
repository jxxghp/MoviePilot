"""
验证 TMDB 历史属性包装对象的集合和字典兼容行为。
"""

import pytest

from app.modules.themoviedb.tmdbv3api.as_obj import AsObj


def test_nested_collection_and_public_views_keep_distinct_shapes() -> None:
    """
    嵌套集合迭代包装元素，公开字典视图仍保持浅层对象。

    :return: 无返回值
    """
    wrapped = AsObj({"results": [{"id": 1}, {"id": 2}]}, key="results")

    assert [item.id for item in wrapped] == [1, 2]
    assert len(wrapped) == 2
    assert wrapped[0].id == 1
    assert wrapped.to_dict() == {"results": wrapped.results}
    assert list(wrapped.keys()) == ["results"]


def test_empty_collection_integer_lookup_keeps_type_error() -> None:
    """
    空集合的整数下标仍按属性名称校验并抛出原有异常。

    :return: 无返回值
    """
    wrapped = AsObj([])

    with pytest.raises(TypeError, match="attribute name must be string"):
        wrapped[0]
    assert list(wrapped) == []
