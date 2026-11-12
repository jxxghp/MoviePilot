"""
验证高清视界下载换票、最终种子 GET、失败通知及凭据隔离。
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from requests import Response

import app.application.site.throttle as throttle
import app.application.torrent.download as torrent_download
import app.chain.download.submission as submission
import app.chain.search.provider as provider
import app.chain.site as site_chain
import app.modules.indexer.spider.hdarea as hdarea_spider
from app.adapters.cache.backends import FileBackend
from app.application.site.contract import SiteSnapshot
from app.chain.download import DownloadChain
from app.chain.site import SiteChain
from app.domain.context import TorrentInfo
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser.hdarea import HDAreaSiteUserInfo
from app.runtime.rate import RequestRateLimiter
from app.schemas.site import Site

# 合成短效地址和单文件种子，无真实 Tracker、密钥或下载器请求。
DOWNLOAD_URL = "https://hdarea.club/download.php?id=42&dl_token=synthetic-ticket"
CONTENT = b"d4:infod4:name11:Example.mkv6:lengthi1eee"


def _response(data, status=200, success=True, code=0):
    """
    模拟 HTTP 响应，原始种子内容与换票信封采用独立类型。
    """
    response = Response()
    response.status_code = status
    response._content = (
        json.dumps({"success": success, "code": code, "data": data}).encode() if isinstance(data, dict) else data
    )
    response.encoding = "utf-8"
    response.close = Mock()
    return response


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
    query.get_sync.return_value = Site(id=7370, domain="hdarea.club")
    query.get_by_domain_sync.return_value = query.get_sync.return_value
    monkeypatch.setattr(throttle, "get_configured_site_query_service", lambda: query)
    ticket_response = _response({"download_url": DOWNLOAD_URL})
    ticket = Mock(post=Mock(return_value=ticket_response))
    monkeypatch.setattr(submission, "_download_ports_snapshot", lambda: (ticket, Mock()))
    content_response = _response(CONTENT)
    port = Mock(request=Mock(return_value=content_response))
    monkeypatch.setattr(torrent_download, "_require_torrent_port", lambda: port)
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    module = object.__new__(IndexerModule)
    chain.run_module = lambda method, **kwargs: getattr(module, method)(
        **{key: value for key, value in kwargs.items() if key != "raise_exception"})
    torrent = TorrentInfo(
        site=7370, site_name="高清视界", title="Example", site_cookie="unused-cookie", site_ua="HDArea-Test",
        page_url="https://hdarea.club/details.php?id=42",
        enclosure=hdarea_spider.download_enclosure("https://hdarea.club/api/v1", "https://hdarea.club/", 42,
                                                      "synthetic-key", ua="HDArea-Test"),
    )
    return SimpleNamespace(chain=chain, torrent=torrent, ticket=ticket, ticket_response=ticket_response,
                           port=port, content_response=content_response)


def test_download_exchanges_key_for_ticket_and_final_get_has_no_credentials(boundary):
    """
    下载时才换票，最终 GET 只使用短效链接，成功种子缓存后不重复消耗票据。
    """
    assert boundary.chain.download_torrent(boundary.torrent)[0] == CONTENT
    request = boundary.ticket.post.call_args.kwargs
    assert boundary.ticket.post.call_args.args == ("https://hdarea.club/api/v1/torrents/genDlToken",)
    assert request["json_body"] == {"id": 42} and request["allow_redirects"] is False
    assert request["headers"]["X-API-Key"] == "synthetic-key" and request["cookies"] is None
    final = boundary.port.request.call_args.kwargs
    assert final["method"] == "GET" and final["url"] == DOWNLOAD_URL
    assert final["cookie"] is None and "headers" not in final
    boundary.ticket_response.close.assert_called_once()
    assert boundary.chain.download_torrent(boundary.torrent)[0] == CONTENT
    boundary.ticket.post.assert_called_once()
    boundary.port.request.assert_called_once()


@pytest.mark.parametrize("data", [{"dl_token": "synthetic-ticket"},
                                  {"download_url": "/download.php?id=42&dl_token=synthetic-ticket"}])
def test_token_only_and_relative_download_url_use_configured_site(boundary, data):
    """
    独立票据与相对下载链接均按配置的网页来源拼接，而不是 API 路径。
    """
    boundary.ticket.post.return_value = _response(data)
    assert boundary.chain.download_torrent(boundary.torrent)[0] == CONTENT
    assert boundary.port.request.call_args.kwargs["url"] == DOWNLOAD_URL


@pytest.mark.parametrize("status,success,code", [(401, False, 401), (403, False, 403), (500, True, 0),
                                                (200, False, 0), (200, True, 3)])
def test_failed_ticket_never_requests_torrent(boundary, status, success, code):
    """
    换票 HTTP、success 或业务码失败时不请求种子，即使响应附带有效链接。
    """
    response = _response({"download_url": DOWNLOAD_URL}, status, success, code)
    boundary.ticket.post.return_value = response
    failures = []
    assert boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append) == (None, "", [])
    assert failures and "synthetic-key" not in failures[0]
    boundary.port.request.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("url", [None, "javascript:alert(1)", "https://other.test/download.php?id=42",
                                "https://hdarea.club:invalid/download.php?id=42",
                                "https://user:password@hdarea.club/download.php?id=42",
                                "https://hdarea.club/download.php?id=43"])
def test_invalid_or_wrong_resource_download_url_is_rejected(boundary, url):
    """
    下载来源和站内 ID 必须匹配当前资源，不接受脚本、坏端口、内嵌认证或其他种子。
    """
    boundary.ticket.post.return_value = _response({"download_url": url})
    failures = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    assert failures and "地址" in failures[0]
    boundary.port.request.assert_not_called()


@pytest.mark.parametrize("status", [200, 500])
def test_non_json_ticket_is_explicit_failure(boundary, status):
    """
    非 JSON 或空 500 响应不会逃出下载入口，也不会回退 Cookie 下载。
    """
    response = _response(b"", status)
    boundary.ticket.post.return_value = response
    failures = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    assert failures
    boundary.port.request.assert_not_called()
    response.close.assert_called_once()


def test_missing_ticket_response_preserves_failure_reason(boundary):
    """
    网络无响应时明确提示 API 连接失败，不静默返回无法获取链接。
    """
    boundary.ticket.post.return_value = None
    failures = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    assert failures == ["无法连接 HDArea API"]
    boundary.port.request.assert_not_called()


def test_rate_limit_cools_down_entire_site_without_immediate_retry(boundary):
    """
    换票限流进入站点共享冷却，后续请求在 HTTP 之前被拒绝。
    """
    boundary.ticket.post.return_value = _response({}, 429, False, 429)
    failures = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    boundary.chain.download_torrent(boundary.torrent, on_failure=failures.append)
    assert len(failures) == 2 and failures[0] == failures[1] and "限流" in failures[0]
    boundary.ticket.post.assert_called_once()
    boundary.port.request.assert_not_called()
    assert throttle.site_request_cooldown({"id": 7370}) > 0


@pytest.mark.parametrize("value", [None, 0, True, -1, "invalid"])
def test_recipe_rejects_invalid_torrent_id(value):
    """
    配方不接受非法站内 ID，缺少密钥时也不生成需要 Cookie 的下载链接。
    """
    with pytest.raises(ValueError, match="整数字段"):
        hdarea_spider.download_enclosure(
            "https://hdarea.club/api/v1", "https://hdarea.club/", value, "synthetic-key",
        )
    assert hdarea_spider.download_enclosure("https://hdarea.club/api/v1", "https://hdarea.club/", value, "") == ""


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_every_actual_hdarea_request_charges_one_shared_slot(boundary, monkeypatch, asynchronous):
    """
    搜索编排、资料、连通性、换票和最终 GET 共用额度，每次真实请求只计费一次。
    """
    clock = SimpleNamespace(now=100.0, waits=[])
    starts = []

    def advance(delay):
        clock.waits.append(delay)
        clock.now += delay

    monkeypatch.setattr(throttle.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(throttle.time, "sleep", advance)

    async def async_advance(delay):
        advance(delay)

    monkeypatch.setattr(throttle.asyncio, "sleep", async_advance)
    configured = Site(id=7370, domain="hdarea.club", limit_seconds=3)
    query = Mock(get_sync=Mock(return_value=configured), get_by_domain_sync=Mock(return_value=configured))
    monkeypatch.setattr(throttle, "get_configured_site_query_service", lambda: query)

    def search_request(**_kwargs):
        starts.append(clock.now)
        response = _response({"items": [], "pagination": {"page": 0, "has_more": False}})
        response.aclose = AsyncMock()
        return response

    request = Mock(get_res=AsyncMock(side_effect=search_request) if asynchronous else Mock(side_effect=search_request))
    monkeypatch.setattr(hdarea_spider, "AsyncRequestUtils" if asynchronous else "RequestUtils", Mock(return_value=request))
    indexer = {"id": 7370, "domain": "https://hdarea.club/", "parser": "HDArea", "apikey": "synthetic-key",
               "ua": "HDArea-Test", "limit_seconds": 3}
    spider = hdarea_spider.HDAreaSpider(indexer)
    if asynchronous:
        assert await spider.async_search(None) == (False, [])
    else:
        owner = Mock(search_site_torrents=Mock(side_effect=lambda **kwargs: spider.search(kwargs["keyword"])[1]))
        assert provider._search_site_page(owner, site=indexer, keyword="Example", media_type=None, page=0) == []

    profile = {"id": 42, "username": "synthetic-user", "class_name": "User", "uploaded": 0,
               "downloaded": 0, "seeding_count": 0, "seeding_size": 0}

    def profile_request(**_kwargs):
        starts.append(clock.now)
        return _response(profile)

    parser = HDAreaSiteUserInfo("高清视界", "https://hdarea.club/", "unused-cookie", "synthetic-key", None,
                               request_utils=Mock(get_res=Mock(side_effect=profile_request)), ua="HDArea-Test")
    parser.parse()
    assert parser.userid == 42

    def probe(**_kwargs):
        """
        在连接适配器的 HTTP 出口记录请求开始时间。
        """
        starts.append(clock.now)
        return _response({"id": 42})

    monkeypatch.setattr(hdarea_spider, "RequestUtils", Mock(return_value=Mock(get_res=Mock(side_effect=probe))))
    monkeypatch.setattr(site_chain, "SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={}))))
    chain = SiteChain()
    module = object.__new__(IndexerModule)
    monkeypatch.setattr(chain, "run_module", lambda method, **kwargs: getattr(module, method)(
        **{key: value for key, value in kwargs.items() if key != "raise_exception"},
    ))
    site = SiteSnapshot(id=7370, name="高清视界", domain="hdarea.club", url="https://hdarea.club/", apikey="synthetic-key", limit_seconds=3)
    chain.site_repository = Mock(get_by_domain=Mock(return_value=site))
    assert chain.test(site.url) == (True, "连接成功")
    boundary.ticket.post.side_effect = lambda *_args, **_kwargs: starts.append(clock.now) or boundary.ticket_response
    boundary.port.request.side_effect = lambda **_kwargs: starts.append(clock.now) or boundary.content_response
    assert boundary.chain.download_torrent(boundary.torrent)[0] == CONTENT
    assert starts == [100, 103, 106, 109, 112]
    assert clock.waits == [3] * 4
