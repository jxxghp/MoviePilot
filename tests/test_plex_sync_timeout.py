from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from app.modules.plex import plex as plex_module
from app.modules.plex.plex import Plex


class _ReadTimeout(Exception):
    """模拟 PlexAPI 底层 HTTP 客户端的 ReadTimeout。"""


def _make_client(section: Mock) -> Plex:
    """构造不建立真实网络连接的 Plex 客户端。"""
    client = Plex.__new__(Plex)
    client._plex = SimpleNamespace(
        library=SimpleNamespace(sectionByID=Mock(return_value=section))
    )
    client._Plex__build_media_server_item = lambda item: item
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
    section.all.side_effect = [_ReadTimeout("read timeout"), items[:2], items[2:]]
    client = _make_client(section)

    result = list(client.get_items("1"))

    assert result == items
    assert section.all.call_args_list == [
        call(container_start=0, container_size=2, maxresults=2),
        call(container_start=0, container_size=2, maxresults=2),
        call(container_start=2, container_size=2, maxresults=2),
    ]


def test_plex_sync_honors_explicit_page_start_and_limit():
    """显式分页调用应只读取请求的起点和条目数。"""
    items = [SimpleNamespace(key=f"item-{index}") for index in range(2)]
    section = Mock()
    section.all.return_value = items
    client = _make_client(section)

    result = list(client.get_items("1", start_index=8, limit=2))

    assert result == items
    section.all.assert_called_once_with(container_start=8, container_size=2, maxresults=2)


def test_plex_sync_retries_item_conversion_timeout(monkeypatch):
    """媒体项目转换超时时应重试当前项目，成功后继续产出结果。"""
    monkeypatch.setattr(plex_module, "PLEX_LIBRARY_SYNC_PAGE_SIZE", 2)
    monkeypatch.setattr(plex_module.time, "sleep", lambda _seconds: None)
    item = SimpleNamespace(key="item-1")
    section = Mock()
    section.all.return_value = [item]
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
    section.all.side_effect = _ReadTimeout("read timeout")
    client = _make_client(section)

    with pytest.raises(_ReadTimeout, match="read timeout"):
        list(client.get_items("1"))

    assert section.all.call_count == plex_module.PLEX_SYNC_TIMEOUT_MAX_ATTEMPTS
