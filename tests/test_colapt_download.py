"""
验证可乐下载地址签发、最终种子 GET、失败通知及凭据隔离。
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import Response

import app.application.site.throttle as throttle
import app.application.torrent.download as torrent_download
import app.chain.download.submission as submission
import app.modules.indexer.spider.colapt as colapt_spider
from app.adapters.cache.backends import FileBackend
from app.chain.download import DownloadChain
from app.domain.context import TorrentInfo
from app.modules.indexer import IndexerModule
from app.runtime.rate import RequestRateLimiter
from app.schemas.site import Site

# 合成签名地址和单文件种子，无真实 Tracker、密钥或下载器请求。
DOWNLOAD_URL = "https://www.colapt.com/downloads/synthetic-credential/42?exp=1795000000&sig=synthetic-sig"
CONTENT = b"d4:infod4:name11:Example.mkv6:lengthi1eee"


def _response(data, status=201):
    """
    模拟 HTTP 响应，原始种子内容与签发结果采用独立类型。
    """
    response = Response()
    response.status_code = status
    response._content = json.dumps(data).encode() if isinstance(data, dict) else data
    response.encoding = "utf-8"
    response.close = Mock()
    return response


def _error(code, status, **extra):
    """
    构造开放 API 的统一错误信封。
    """
    return _response({"error": {"code": code, "message": "synthetic-key", "request_id": "r", "fields": {},
                                **extra}}, status)


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    """
    保留真实下载链、缓存、种子解析和流控，所有 HTTP 通过本次替身执行。
    """
    cache = FileBackend(tmp_path)
    monkeypatch.setattr(torrent_download, "FileCache", lambda **_kwargs: cache)
    monkeypatch.setattr(submission, "FileCache", lambda **_kwargs: cache)
    monkeypatch.setattr(throttle, "_site_requests", RequestRateLimiter())
    query = Mock()
    query.get_sync.return_value = Site(id=9101, domain="colapt.com")
    query.get_by_domain_sync.return_value = query.get_sync.return_value
    monkeypatch.setattr(throttle, "get_configured_site_query_service", lambda: query)
    ticket_response = _response({"download_url": DOWNLOAD_URL})
    ticket = Mock(post=Mock(return_value=ticket_response))
    monkeypatch.setattr(submission, "_download_ports_snapshot", lambda: (ticket, Mock()))
    content_response = _response(CONTENT, 200)
    port = Mock(request=Mock(return_value=content_response))
    monkeypatch.setattr(torrent_download, "_require_torrent_port", lambda: port)
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    module = object.__new__(IndexerModule)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(
        **{key: value for key, value in kwargs.items() if key != "raise_exception"})
    torrent = TorrentInfo(
        site=9101, site_name="可乐", title="Example", site_cookie="unused-cookie", site_ua="ColaPT-Test",
        page_url="https://www.colapt.com/torrents/42",
        enclosure=colapt_spider.download_enclosure("https://www.colapt.com/api/v1", "https://www.colapt.com/", 42,
                                                   "synthetic-key", ua="ColaPT-Test"),
    )
    return SimpleNamespace(chain=chain, torrent=torrent, ticket=ticket, ticket_response=ticket_response,
                           port=port, content_response=content_response)


def test_download_signs_link_and_final_get_has_no_credentials(boundary):
    """
    下载时才签发地址，最终 GET 只使用签名地址，不带 API Key 或 Cookie。
    """
    assert boundary.chain.download_torrent(boundary.torrent)[0] == CONTENT
    request = boundary.ticket.post.call_args.kwargs
    assert boundary.ticket.post.call_args.args == ("https://www.colapt.com/api/v1/torrents/42/download-links",)
    assert "json_body" not in request and request["allow_redirects"] is False
    assert request["headers"]["X-API-Key"] == "synthetic-key" and request["cookies"] is None
    final = boundary.port.request.call_args.kwargs
    assert final["method"] == "GET" and final["url"] == DOWNLOAD_URL
    assert final["cookie"] is None and "headers" not in final
    boundary.ticket_response.close.assert_called_once()


@pytest.mark.parametrize("code,status,expected", [
    ("invalid_credentials", 401, "已失效"), ("api_key_scope_denied", 403, "下载"),
    ("permission_denied", 403, "无权"), ("not_found", 404, "不存在"), ("service_unavailable", 503, "HTTP 503"),
])
def test_failed_signing_never_requests_torrent(boundary, code, status, expected):
    """
    签发失败时不请求种子，错误按错误码提示且不回显密钥。
    """
    response = _error(code, status)
    boundary.ticket.post.return_value = response
    failures = []
    assert boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append) == (None, "", [])
    assert failures and expected in failures[0] and "synthetic-key" not in failures[0]
    boundary.port.request.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("url", [
    None, "javascript:alert(1)", "http://www.colapt.com/downloads/c/42?exp=1&sig=s",
    "https://other.test/downloads/c/42?exp=1&sig=s", "https://user:pw@www.colapt.com/downloads/c/42?exp=1&sig=s",
    "https://www.colapt.com/downloads/c/42", "https://www.colapt.com/torrents/42",
])
def test_invalid_download_url_is_rejected(boundary, url):
    """
    只接受本站 HTTPS 签名下载地址，不接受脚本、明文、内嵌认证、无签名或其他站点。
    """
    boundary.ticket.post.return_value = _response({"download_url": url})
    failures = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    assert failures and "地址" in failures[0]
    boundary.port.request.assert_not_called()


def test_download_url_for_another_torrent_is_rejected(boundary):
    """
    签名地址必须对应所选种子，不能下载到其他资源。
    """
    boundary.ticket.post.return_value = _response(
        {"download_url": "https://www.colapt.com/downloads/c/43?exp=1&sig=s"})
    failures = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    assert failures and "不符" in failures[0]
    boundary.port.request.assert_not_called()


@pytest.mark.parametrize("status", [201, 502])
def test_non_json_signing_is_explicit_failure(boundary, status):
    """
    非 JSON 或网关错误不会逃出下载入口，也不会回退 Cookie 下载。
    """
    response = _response(b"<html></html>", status)
    boundary.ticket.post.return_value = response
    failures = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    assert failures
    boundary.port.request.assert_not_called()
    response.close.assert_called_once()


def test_missing_signing_response_preserves_failure_reason(boundary):
    """
    网络无响应时明确提示 API 连接失败。
    """
    boundary.ticket.post.return_value = None
    failures = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    assert len(failures) == 1 and failures[0].startswith("换票失败：") and "无法连接 ColaPT API" in failures[0]
    boundary.port.request.assert_not_called()


def test_rate_limit_cools_down_entire_site_without_immediate_retry(boundary):
    """
    签发限流进入站点共享冷却，后续请求在 HTTP 之前被拒绝。
    """
    boundary.ticket.post.return_value = _error("rate_limited", 429, retry_after=60)
    failures = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    assert len(failures) == 2 and failures[0] == failures[1] and "限流" in failures[0]
    boundary.ticket.post.assert_called_once()
    boundary.port.request.assert_not_called()
    assert throttle.site_request_cooldown({"id": 9101}) > 0


@pytest.mark.parametrize("value", [None, 0, True, -1, "invalid"])
def test_recipe_rejects_invalid_torrent_id(value):
    """
    配方不接受非法站内 ID，缺少密钥时也不生成需要 Cookie 的下载链接。
    """
    with pytest.raises(ValueError, match="整数字段"):
        colapt_spider.download_enclosure(
            "https://www.colapt.com/api/v1", "https://www.colapt.com/", value, "synthetic-key",
        )
    assert colapt_spider.download_enclosure(
        "https://www.colapt.com/api/v1", "https://www.colapt.com/", value, "") == ""
