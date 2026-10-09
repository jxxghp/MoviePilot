"""种子 GET 的有界重试、缓存与冷却策略离线回归。"""

from unittest.mock import Mock

import pytest
from requests import Response

import app.application.torrent.download as download
from app.chain.download.failure import DownloadFailureOwner

URL = "https://tracker.example/download.php?id=39"
CONTENT = b"d4:infod4:name10:Series.mkv6:lengthi1eee"


def _response(status=200, content=CONTENT):
    """使用真实 Response 保证错误状态的布尔值不会掩盖状态码。"""
    response = Response()
    response.status_code = status
    response._content = content
    response.encoding = "utf-8"
    return response


@pytest.fixture
def boundary(monkeypatch):
    """隔离 HTTP、缓存和等待，保留真实种子解析与用例编排。"""
    port = Mock()
    monkeypatch.setattr(download, "_require_torrent_port", Mock(return_value=port))
    cache = Mock()
    cache.get.return_value = None
    monkeypatch.setattr(download, "FileCache", Mock(return_value=cache))
    sleep = Mock()
    monkeypatch.setattr(download.time, "sleep", sleep)
    helper = download.TorrentHelper()
    helper.add_invalid = Mock()
    return helper, port, cache, sleep


@pytest.mark.parametrize("failure", [None, 403, 500, 502, 503, 525, b"", b"d4:info", b"<html>CF</html>"])
def test_transient_failure_recovers_without_invalid_cache(boundary, failure):
    """网络异常的 None、代理错误和截断响应均可恢复，保持访问参数。"""
    helper, port, cache, sleep = boundary
    failed = _response(failure) if isinstance(failure, int) else (
        _response(content=failure) if failure is not None else None
    )
    port.request.side_effect = [failed, _response()]

    result = helper.download_torrent(URL, cookie="session=test", ua="TestAgent", referer="https://tracker.example/")

    assert result[1:5] == (CONTENT, "", ["Series.mkv"], "")
    assert port.request.call_count == 2
    assert port.request.call_args_list[0] == port.request.call_args_list[1]
    sleep.assert_called_once_with(1)
    helper.add_invalid.assert_not_called()
    cache.set.assert_called_once()


@pytest.mark.parametrize("status", [None, 403, 525])
def test_exhausted_retry_is_bounded_and_does_not_poison_url(boundary, status):
    """失败耗尽后保留最终原因，总共三次请求且不写 24 小时缓存。"""
    helper, port, cache, sleep = boundary
    port.request.return_value = None if status is None else _response(status)

    result = helper.download_torrent(URL)

    assert result[1] is None
    assert result[4] == ("无法打开链接" if status is None else f"下载种子出错，状态码：{status}")
    assert port.request.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    helper.add_invalid.assert_not_called()
    cache.set.assert_not_called()


@pytest.mark.parametrize("status", [401, 404, 410, 429])
def test_permanent_errors_and_rate_limit_are_not_retried(boundary, status):
    """确定失效地址保留无效缓存，429 保留站点流控语义且不短退避。"""
    helper, port, _, sleep = boundary
    port.request.return_value = _response(status)

    assert helper.download_torrent(URL)[1] is None

    port.request.assert_called_once()
    sleep.assert_not_called()
    assert helper.add_invalid.call_count == (0 if status == 429 else 1)


@pytest.mark.parametrize("signed,cache_invalid", [(True, True), (False, False)])
def test_signed_and_indirect_links_keep_single_attempt(boundary, signed, cache_invalid):
    """签名刷新和换票继续由上层持有重试预算。"""
    helper, port, _, sleep = boundary
    port.request.return_value = _response(403)
    url = URL + "&t=1&sign=old" if signed else URL

    assert helper.download_torrent(url, cache_invalid=cache_invalid)[1] is None

    port.request.assert_called_once()
    sleep.assert_not_called()
    helper.add_invalid.assert_not_called()


def test_first_download_confirmation_post_is_not_retried(boundary):
    """识别首次下载确认页后只提交一次 POST，不重复有副作用的操作。"""
    helper, port, _, sleep = boundary
    page = '下载种子文件<form action="?"><input name="confirm" value="yes"></form>'.encode()
    port.request.side_effect = [_response(content=page), _response(503)]

    assert helper.download_torrent(URL)[1] is None

    assert [call.kwargs["method"] for call in port.request.call_args_list] == ["GET", "POST"]
    sleep.assert_not_called()


def test_redirected_get_retries_at_destination(boundary):
    """重定向目标上的瞬时错误也重试，并保留原始 URL 的成功缓存键。"""
    helper, port, cache, sleep = boundary
    redirect = _response(302)
    redirect.headers["Location"] = "https://tracker.example/file.torrent"
    port.request.side_effect = [redirect, _response(525), _response()]

    assert helper.download_torrent(URL)[1] == CONTENT

    assert [call.kwargs["url"] for call in port.request.call_args_list] == [URL, redirect.headers["Location"], redirect.headers["Location"]]
    sleep.assert_called_once_with(1)
    cache.set.assert_called_once()


def test_second_retry_can_recover(boundary):
    """第二次退避后成功时只缓存最终合法种子。"""
    helper, port, cache, sleep = boundary
    port.request.side_effect = [None, _response(content=b"d4:info"), _response()]
    assert helper.download_torrent(URL)[1] == CONTENT
    assert port.request.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    cache.set.assert_called_once()


@pytest.mark.parametrize("content", [b"", b"d4:info", b"<html>CF</html>"])
def test_invalid_content_exhaustion_never_caches_response(boundary, content):
    """空或无法解析的响应耗尽预算后仍不能进入种子及无效链接缓存。"""
    helper, port, cache, _ = boundary
    port.request.return_value = _response(content=content)
    assert helper.download_torrent(URL)[1] is None
    assert port.request.call_count == 3
    cache.set.assert_not_called()
    helper.add_invalid.assert_not_called()


@pytest.mark.parametrize("content", [CONTENT, b"magnet:?xt=urn:btih:abc"])
def test_valid_response_has_no_retry(boundary, content):
    """合法种子和磁力响应只发起一次请求。"""
    helper, port, _, sleep = boundary
    port.request.return_value = _response(content=content)
    assert helper.download_torrent(URL)[1]
    port.request.assert_called_once()
    sleep.assert_not_called()


def test_valid_cache_bypasses_network(boundary):
    """已有合法种子缓存时不发起请求或退避。"""
    helper, port, cache, sleep = boundary
    cache.get.return_value = CONTENT
    assert helper.download_torrent(URL)[1] == CONTENT
    port.request.assert_not_called()
    sleep.assert_not_called()


@pytest.mark.parametrize("error,ttl", [
    ("下载种子内容为空", 3600),
    ("无法打开链接", 3600),
    ("下载种子出错，状态码：525", 3600),
    ("无法读取种子文件", 86400),
    ("torrent not found", 86400),
])
def test_generic_empty_download_is_transient(error, ttl):
    """空内容不证明资源失效；明确文件损坏或删除继续按一天冷却。"""
    assert DownloadFailureOwner._download_failure_ttl(error) == ttl
