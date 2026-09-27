from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from xml.etree.ElementTree import fromstring

import pytest
from plexapi.library import MovieSection

# 注册真实 Plex 电影类型，供列表 XML 解析测试使用。
from plexapi.video import Movie  # noqa: F401  # pylint: disable=unused-import

from app.modules.plex import plex as plex_module
from app.modules.plex.plex import Plex


class _ReadTimeout(Exception):
    """模拟 PlexAPI 底层 HTTP 客户端的 ReadTimeout。"""


def _make_client(section: Mock | MovieSection) -> Plex:
    """构造不建立真实网络连接的 Plex 客户端。"""
    client = Plex.__new__(Plex)
    client._plex = SimpleNamespace(
        library=SimpleNamespace(sectionByID=Mock(return_value=section))
    )
    client._Plex__build_media_server_item = lambda item: item
    if isinstance(section, Mock):
        section.TYPE = "movie"
        section._buildSearchKey.return_value = "/library/sections/1/all?includeGuids=1&type=1"
    return client


def test_plex_uses_configured_timeout_for_connect_and_reconnect():
    """Plex 初次连接和重连都应使用媒体服务器配置的超时秒数。"""
    server = SimpleNamespace(library=SimpleNamespace(sections=Mock(return_value=[])))

    with (
        patch.object(plex_module, "PlexServer", return_value=server) as server_factory,
        patch.object(plex_module, "RequestUtils"),
    ):
        client = Plex("http://plex.local:32400", "token", timeout=90)
        client.reconnect()

    assert server_factory.call_args_list == [
        call(client._host, "token", timeout=90),
        call(client._host, "token", timeout=90),
    ]


@pytest.mark.parametrize("timeout", [None, 0, -1, "invalid"])
def test_plex_invalid_timeout_uses_default(timeout):
    """无效的超时配置应回退到兼容默认值 30 秒。"""
    server = SimpleNamespace(library=SimpleNamespace(sections=Mock(return_value=[])))

    with (
        patch.object(plex_module, "PlexServer", return_value=server) as server_factory,
        patch.object(plex_module, "RequestUtils"),
    ):
        client = Plex("http://plex.local:32400", "token", timeout=timeout)

    assert client._timeout == 30
    server_factory.assert_called_once_with(client._host, "token", timeout=30)


def test_plex_authentication_uses_configured_timeout():
    """Plex 用户认证取得令牌后也应使用服务器配置的超时。"""
    client = Plex.__new__(Plex)
    client._host = "http://plex.local:32400/"
    client._timeout = 120
    account = SimpleNamespace(authToken="auth-token", username="tester")

    with (
        patch.object(plex_module, "MyPlexAccount", return_value=account),
        patch.object(plex_module, "PlexServer") as server_factory,
    ):
        result = client.authenticate("tester", "password")

    assert result == ("auth-token", "tester")
    server_factory.assert_called_once_with(client._host, "auth-token", timeout=120)


def test_plex_sync_reads_full_library_in_pages_and_retries_timeouts(monkeypatch):
    """整库同步应分批读取，并在读取分页超时时重试后继续。"""
    monkeypatch.setattr(plex_module, "PLEX_LIBRARY_SYNC_PAGE_SIZE", 2)
    monkeypatch.setattr(plex_module.time, "sleep", lambda _seconds: None)
    items = [SimpleNamespace(key=f"item-{index}") for index in range(3)]
    section = Mock()
    section.fetchItems.side_effect = [_ReadTimeout("read timeout"), items[:2], items[2:]]
    client = _make_client(section)

    result = list(client.get_items("1"))

    assert result == items
    options = dict(params={
        "excludeElements": plex_module.PLEX_SYNC_EXCLUDE_ELEMENTS,
        "skipRefresh": 1,
    })
    assert section.fetchItems.call_args_list == [
        call(section._buildSearchKey.return_value, container_start=0, container_size=2,
             maxresults=2, **options),
        call(section._buildSearchKey.return_value, container_start=0, container_size=2,
             maxresults=2, **options),
        call(section._buildSearchKey.return_value, container_start=2, container_size=2,
             maxresults=2, **options),
    ]
    section._buildSearchKey.assert_called_once_with(libtype="movie")


def test_plex_sync_honors_explicit_page_start_and_limit():
    """显式分页调用应只读取请求的起点和条目数。"""
    items = [SimpleNamespace(key=f"item-{index}") for index in range(2)]
    section = Mock()
    section.fetchItems.return_value = items
    client = _make_client(section)

    result = list(client.get_items("1", start_index=8, limit=2))

    assert result == items
    section.fetchItems.assert_called_once_with(
        section._buildSearchKey.return_value,
        container_start=8, container_size=2, maxresults=2,
        params={"excludeElements": plex_module.PLEX_SYNC_EXCLUDE_ELEMENTS, "skipRefresh": 1},
    )


def test_plex_sync_retries_item_conversion_timeout(monkeypatch):
    """媒体项目转换超时时应重试当前项目，成功后继续产出结果。"""
    monkeypatch.setattr(plex_module, "PLEX_LIBRARY_SYNC_PAGE_SIZE", 2)
    monkeypatch.setattr(plex_module.time, "sleep", lambda _seconds: None)
    item = SimpleNamespace(key="item-1")
    section = Mock()
    section.fetchItems.return_value = [item]
    client = _make_client(section)
    convert = Mock(side_effect=[_ReadTimeout("read timeout"), "converted-item"])
    monkeypatch.setattr(client, "_Plex__build_media_server_item", convert)

    result = list(client.get_items("1"))

    assert result == ["converted-item"]
    assert convert.call_count == 2


def test_plex_sync_raises_after_all_page_timeout_retries(monkeypatch):
    """分页超时重试耗尽时应中止整库同步，不能静默跳过页面。"""
    monkeypatch.setattr(plex_module.time, "sleep", lambda _seconds: None)
    section = Mock()
    section.fetchItems.side_effect = _ReadTimeout("read timeout")
    client = _make_client(section)

    with pytest.raises(_ReadTimeout, match="read timeout"):
        list(client.get_items("1"))

    assert section.fetchItems.call_count == plex_module.PLEX_SYNC_TIMEOUT_MAX_ATTEMPTS


def test_plex_sync_raises_on_connection_failure_after_partial_page(monkeypatch):
    """连接拒绝后不得把已读部分视作完整媒体库。"""
    monkeypatch.setattr(plex_module, "PLEX_LIBRARY_SYNC_PAGE_SIZE", 1)
    section = Mock()
    section.fetchItems.side_effect = [[SimpleNamespace(key="first")], ConnectionError("refused")]
    client = _make_client(section)
    with pytest.raises(ConnectionError, match="refused"):
        list(client.get_items("1"))


def test_plex_sync_raises_on_item_conversion_failure():
    """条目转换失败时不能跳过记录后继续执行陈旧清理。"""
    section = Mock()
    section.fetchItems.return_value = [SimpleNamespace(key="first")]
    client = _make_client(section)
    client._Plex__build_media_server_item = Mock(side_effect=ConnectionError("refused"))
    with pytest.raises(ConnectionError, match="refused"):
        list(client.get_items("1"))


def test_plex_sync_raises_when_server_or_library_is_unavailable():
    """服务器失联或媒体库消失时不得返回空库并触发旧数据清理。"""
    section = Mock()
    client = _make_client(section)
    client._plex = None
    with pytest.raises(ConnectionError, match="未连接"):
        list(client.get_items("1"))

    client = _make_client(section)
    client._plex.library.sectionByID.return_value = None
    with pytest.raises(ValueError, match="不存在"):
        list(client.get_items("1"))


def test_plex_sync_uses_list_metadata_without_reloading_each_movie():
    """列表中的文件路径和 Guid 足够生成缓存条目，不应发起逐条详情请求。"""
    server = Mock()
    server.query.return_value = fromstring(
        '<MediaContainer size="1" totalSize="1" librarySectionID="1">'
        '<Video type="movie" key="/library/metadata/1" ratingKey="1" '
        'title="Demo" year="2026">'
        '<Media><Part file="/cloud/Demo.mkv"/></Media>'
        '<Guid id="tmdb://123"/></Video></MediaContainer>'
    )
    section = MovieSection(
        server, fromstring('<Directory key="1" type="movie" title="Movies"/>'),
        "/library/sections",
    )
    client = _make_client(section)
    del client._Plex__build_media_server_item

    result = list(client.get_items("1"))

    assert len(result) == 1
    assert result[0].path == "/cloud/Demo.mkv"
    assert str(result[0].media_id) == "123"
    server.query.assert_called_once_with(
        "/library/sections/1/all?includeGuids=1&type=1",
        headers={"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "50"},
        params={"excludeElements": plex_module.PLEX_SYNC_EXCLUDE_ELEMENTS, "skipRefresh": 1},
    )
