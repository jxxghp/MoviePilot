"""
Plex SDK 返回值与类型收窄的异常边界回归。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.plex.plex import Plex


def test_root_refresh_preserves_sdk_return_identity():
    """
    整库刷新原样返回 SDK 对象，不能转换为布尔值。
    """
    client = Plex.__new__(Plex)
    result = object()
    client._plex = SimpleNamespace(library=SimpleNamespace(update=Mock(return_value=result)))
    assert client.refresh_root_library() is result
    client._plex = None
    assert client.refresh_root_library() is False


def test_missing_refresh_path_keeps_root_refresh_success_semantics():
    """
    目标路径缺失时继续回退根库，SDK 空返回值仍算请求成功。
    """
    client = Plex.__new__(Plex)
    update = Mock(return_value=None)
    client._plex = SimpleNamespace(library=SimpleNamespace(update=update))
    client._libraries = []
    assert client.refresh_library_by_items([SimpleNamespace(target_path=None)]) is True
    update.assert_called_once_with()


def test_optional_image_depth_preserves_short_circuit_and_error():
    """
    未连接仍提前返回，连接后空递归深度仍在 SDK 查询前报错。
    """
    client = Plex.__new__(Plex)
    client._plex = None
    assert client.get_remote_image_by_id("1", "Poster", depth=None) is None
    client._plex = Mock()
    with pytest.raises(TypeError):
        client.get_remote_image_by_id("1", "Poster", depth=None)
    client._plex.fetchItem.assert_not_called()


def test_latest_none_count_keeps_library_query_before_error():
    """
    空数量仍先读取库范围，只有库可用时才到比较数量的失败点。
    """
    client = Plex.__new__(Plex)
    client._plex = Mock()
    client.get_librarys = Mock(return_value=None)
    assert client.get_latest(num=None) is None
    client.get_librarys.return_value = []
    with pytest.raises(TypeError):
        client.get_latest(num=None)
    client._plex.fetchItems.assert_not_called()


def test_guid_parsing_retains_null_and_identifier_types():
    """
    非法 TMDB 标识仍忽略，其他标识保持文本，合法 TMDB 转为整数。
    """
    assert Plex._Plex__get_ids([{"id": "tmdb://bad"}, {"id": "imdb://tt1"}]) == {
        "tmdb_id": None, "imdb_id": "tt1", "tvdb_id": None,
    }
    assert Plex._Plex__get_ids([{"id": "tmdb://123"}])["tmdb_id"] == 123
