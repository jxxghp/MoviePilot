"""
猪猪换票、最终种子 GET、公告错误和最小共享访问间隔的离线回归。
"""

import asyncio
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
import app.modules.indexer.spider.piggo as piggo_spider
import app.startup.composition.network as network
from app.adapters.cache.backends import FileBackend
from app.application.site.contract import SiteSnapshot
from app.chain.download import DownloadChain
from app.chain.site import SiteChain
from app.domain.context import TorrentInfo
from app.modules.indexer import IndexerModule
from app.modules.indexer.parser.piggo import PiggoSiteUserInfo
from app.runtime.cache import MemoryBackend
from app.runtime.rate import RequestRateLimiter
from app.schemas.site import Site

# 合成临时下载 URL 与合法单文件种子，不对应真实站点下载或配额。
DOWNLOAD_URL = "https://piggo.me/v1/web/torrents/56354/dl/synthetic-ticket"
CONTENT = b"d4:infod4:name10:Series.mkv6:lengthi1eee"


def _response(content, status=200):
    response = Response()
    response.status_code = status
    response._content = json.dumps(content).encode() if isinstance(content, dict) else content
    response.encoding = "utf-8"
    response.close = Mock()
    return response


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    """
    私有缓存和虚拟时钟保留真实下载链、种子解析器与站点流控。
    """
    clock = SimpleNamespace(now=100.0, waits=[])

    def advance(delay):
        clock.waits.append(delay)
        clock.now += delay

    monkeypatch.setattr(throttle.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(throttle.time, "sleep", advance)
    monkeypatch.setattr(throttle, "_site_requests", RequestRateLimiter())
    query = Mock()
    query.get_sync.return_value = Site(id=7274, domain="piggo.me", limit_seconds=3)
    query.get_by_domain_sync.return_value = query.get_sync.return_value
    monkeypatch.setattr(throttle, "get_configured_site_query_service", lambda: query)
    cache = FileBackend(tmp_path)
    monkeypatch.setattr(torrent_download, "FileCache", lambda **_kwargs: cache)
    monkeypatch.setattr(submission, "FileCache", lambda **_kwargs: cache)
    ticket_response = _response({"code": 0, "data": {"url": DOWNLOAD_URL}, "msg": "ok"})
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
    torrent = TorrentInfo(site=7274, site_name="猪猪", title="Series", site_cookie="unused-cookie", site_ua="Piggo-Test",
                          page_url="https://piggo.me/detail/56354",
                          enclosure=piggo_spider.download_enclosure(piggo_spider.API_URL, 56354, "synthetic-key"))
    return SimpleNamespace(chain=chain, torrent=torrent, ticket=ticket, ticket_response=ticket_response,
                           port=port, content_response=content_response, clock=clock, cache=cache)


def test_recipe_posts_json_and_final_get_has_no_credentials(boundary):
    """
    换票只发送 Bearer 和空 JSON，最终 GET 使用返回的网页主机且不携带 Cookie 或令牌。
    """
    assert boundary.chain.download_torrent(boundary.torrent)[0] == CONTENT
    request = boundary.ticket.post.call_args.kwargs
    assert request["json_body"] == {} and request["allow_redirects"] is False
    assert request["headers"]["Authorization"] == "Bearer synthetic-key"
    assert request["cookies"] is None and request["params"] is None
    final = boundary.port.request.call_args.kwargs
    assert final["method"] == "GET" and final["url"] == DOWNLOAD_URL
    assert final["cookie"] is None and "headers" not in final
    assert boundary.clock.waits == [3]
    boundary.ticket_response.close.assert_called_once()
    assert boundary.chain.download_torrent(boundary.torrent)[0] == CONTENT
    boundary.ticket.post.assert_called_once()
    boundary.port.request.assert_called_once()


@pytest.mark.parametrize("status", [200, 403])
def test_notice_gate_preserves_reason_and_never_fetches_torrent(boundary, status):
    """
    外层 code 为零不能掩盖必读公告，不自动确认或请求种子文件。
    """
    response = _response({"code": 0, "data": {"code": "notice_unread", "notice_gate": {"title": "公告标题"}}, "msg": "请先阅读公告"}, status)
    boundary.ticket.post.return_value = response
    errors = []
    assert boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append) == (None, "", [])
    assert len(errors) == 1 and "网页端阅读" in errors[0] and "公告标题" in errors[0]
    assert errors[0] in boundary.chain.post_message.call_args.args[0].text
    boundary.port.request.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("status,code", [(401, 401), (200, 401), (500, 0), (302, 0)])
def test_failed_ticket_never_uses_url_from_error_response(boundary, status, code):
    """
    错误状态即使附带看似有效的 URL，也不能向下载主机发出请求。
    """
    response = _response({"code": code, "data": {"url": DOWNLOAD_URL}}, status)
    boundary.ticket.post.return_value = response
    errors = []
    assert boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append) == (None, "", [])
    assert errors
    boundary.port.request.assert_not_called()
    response.close.assert_called_once()


def test_rate_limit_cools_down_search_and_download(boundary):
    """
    猪猪换票返回 429 时保存原始原因，整个站点进入既有冷却而不是立即重试。
    """
    boundary.ticket.post.return_value = _response({"code": 429, "data": {"message": "请在 60 秒后重试"}}, 429)
    errors = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append)
    boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append)
    assert len(errors) == 2 and errors[0] == errors[1]
    assert "限流" in errors[0] and "60 秒" in errors[0]
    boundary.ticket.post.assert_called_once()
    boundary.port.request.assert_not_called()
    assert throttle.site_request_cooldown({"id": 7274}) == 3600


@pytest.mark.parametrize("url", ["/relative", "magnet:?xt=invalid", "javascript:alert(1)", "https://user:password@piggo.me/file", "https://piggo.me:bad/file"])
def test_invalid_download_url_is_rejected(boundary, url):
    """
    下载链接必须是合法绝对 HTTP 地址，不接受脚本、磁力、内嵌认证或坏端口。
    """
    boundary.ticket.post.return_value = _response({"code": 0, "data": {"url": url}})
    errors = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append)
    assert errors and "地址" in errors[0]
    boundary.port.request.assert_not_called()


def test_post_adapter_preserves_empty_json_and_disables_redirects(monkeypatch):
    """
    真实下载传输适配器必须透传空 JSON，而不是把它当作缺失请求体。
    """
    adapter = network._DownloadHttpAdapter()
    request = Mock()
    constructor = Mock(return_value=request)
    monkeypatch.setattr(adapter, "_request", constructor)
    response = adapter.post("https://api.piggo.me/v1/api/me", headers={"Authorization": "Bearer synthetic-key"},
                            json_body={}, allow_redirects=False)
    assert response is request.post_res.return_value
    request.post_res.assert_called_once_with("https://api.piggo.me/v1/api/me", json={}, allow_redirects=False)
    assert constructor.call_args.kwargs["cookies"] is None


@pytest.mark.parametrize("status", [200, 401, 429])
def test_non_json_ticket_keeps_http_failure_semantics(boundary, status):
    """
    WAF 的非 JSON 响应不能被误解析为下载地址，401 与 429 仍保留鉴权和限流含义。
    """
    boundary.ticket.post.return_value = _response(b"<html>WAF</html>", status)
    errors = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append)
    assert errors
    assert {200: "结构无效", 401: "令牌", 429: "限流"}[status] in errors[0]
    boundary.port.request.assert_not_called()


def test_missing_ticket_response_reports_failure(boundary):
    """
    无响应也必须交回下载失败原因，不静默返回空结果。
    """
    boundary.ticket.post.return_value = None
    errors = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append)
    assert errors == ["无法连接猪猪实验室 API"]
    boundary.port.request.assert_not_called()


def _isolate_catalog(monkeypatch):
    """
    只替换猪猪缓存实例的区域字典，不清除其他用例或运行时的缓存。
    """
    cache = MemoryBackend(maxsize=128, ttl=3600)
    monkeypatch.setattr(cache, "_region_caches", {})
    monkeypatch.setattr(piggo_spider.PiggoSpider, "_catalog_cache", cache)


def test_every_actual_piggo_request_shares_one_slot(boundary, monkeypatch):
    """
    搜索、资料、连接检测、换票和最终 GET 每次只计费一次，编排层不重复占额。
    """
    _isolate_catalog(monkeypatch)
    starts = []
    responses = iter([
        _response({"code": 0, "data": {"sections": [], "categories": []}}),
        _response({"code": 0, "data": {"items": []}}),
    ])

    def search_request(**_kwargs):
        starts.append(boundary.clock.now)
        return next(responses)

    request = Mock(post_res=Mock(side_effect=search_request))
    monkeypatch.setattr(piggo_spider, "RequestUtils", Mock(return_value=request))
    indexer = {"id": 7274, "domain": "https://piggo.me/", "parser": "Piggo", "apikey": "synthetic-key", "ua": "Piggo-Test", "limit_seconds": 3}
    spider = piggo_spider.PiggoSpider(indexer)
    owner = Mock(search_site_torrents=Mock(side_effect=lambda **kwargs: spider.search(kwargs["keyword"])[1]))
    assert provider._search_site_page(owner, site=indexer, keyword="Series", media_type=None, page=0) == []

    pages = iter([
        _response({"code": 0, "data": {"user": {"id": 42, "username": "synthetic-user", "class": {"name": "主管"},
                                                "stats": {"uploaded": 0, "downloaded": 0}}}}),
        _response({"code": 0, "data": {"kind": "seeding", "items": [], "page": 1, "pages": 1, "per_page": 100, "total": 0}}),
        _response({"code": 0, "data": {"kind": "leeching", "items": [], "page": 1, "pages": 1, "per_page": 100, "total": 0}}),
    ])

    def profile_request(**_kwargs):
        starts.append(boundary.clock.now)
        return next(pages)

    parser = PiggoSiteUserInfo("猪猪", "https://piggo.me/", "unused-cookie", "synthetic-key", None,
                              request_utils=Mock(post_res=Mock(side_effect=profile_request)), ua="Piggo-Test")
    parser.parse()
    assert parser.userid == 42

    def probe(**_kwargs):
        """
        在适配器的真实 HTTP 出口记录连接检测起点。
        """
        starts.append(boundary.clock.now)
        return _response({"code": 0, "data": {"user": {"id": 42}}})

    monkeypatch.setattr(piggo_spider, "RequestUtils", Mock(return_value=Mock(post_res=Mock(side_effect=probe))))
    monkeypatch.setattr(site_chain, "SitesHelper", Mock(return_value=Mock(get_indexer=Mock(return_value={"parser": "Piggo"}))))
    chain = SiteChain()
    site = SiteSnapshot(id=7274, name="猪猪", domain="piggo.me", url="https://piggo.me/", apikey="synthetic-key", limit_seconds=3)
    chain.site_repository = Mock(get_by_domain=Mock(return_value=site))
    module = object.__new__(IndexerModule)
    monkeypatch.setattr(chain, "run_module", lambda method, **kwargs: getattr(module, method)(
        **{key: value for key, value in kwargs.items() if key != "raise_exception"},
    ))
    assert chain.test(site.url) == (True, "连接成功")
    boundary.ticket.post.side_effect = lambda *_args, **_kwargs: starts.append(boundary.clock.now) or boundary.ticket_response
    boundary.port.request.side_effect = lambda **_kwargs: starts.append(boundary.clock.now) or boundary.content_response
    assert boundary.chain.download_torrent(boundary.torrent)[0] == CONTENT
    assert starts == [100, 103, 106, 109, 112, 115, 118, 121]
    assert boundary.clock.waits == [3] * 7


@pytest.mark.asyncio
async def test_async_search_cancellation_does_not_send_next_request(boundary, monkeypatch):
    """
    异步搜索在列表请求前取消时只完成已发出的目录请求，不留下后台 HTTP 请求。
    """
    _isolate_catalog(monkeypatch)
    response = _response({"code": 0, "data": {"sections": [], "categories": []}})
    response.aclose = AsyncMock()
    request = Mock(post_res=AsyncMock(return_value=response))
    monkeypatch.setattr(piggo_spider, "AsyncRequestUtils", Mock(return_value=request))
    sleeping = asyncio.Event()

    async def blocked_sleep(_delay):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(throttle.asyncio, "sleep", blocked_sleep)
    indexer = {"id": 7274, "domain": "https://piggo.me/", "parser": "Piggo", "apikey": "synthetic-key", "ua": "Piggo-Test", "limit_seconds": 3}
    task = asyncio.create_task(piggo_spider.PiggoSpider(indexer).async_search(None))
    try:
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        request.post_res.assert_awaited_once()
        assert request.post_res.call_args.kwargs["url"].endswith("/catalog")
        response.aclose.assert_awaited_once()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("header_ua", [None, "custom-UA"])
def test_post_adapter_preserves_ua_with_custom_headers(monkeypatch, header_ua):
    """
    认证请求的自定义头部不能丢弃 UA；显式 UA 头优先且不修改调用方字典。
    """
    request = Mock()
    constructor = Mock(return_value=request)
    monkeypatch.setattr(network, "RequestUtils", constructor)
    headers = {"Authorization": "Bearer synthetic-key"}
    if header_ua:
        headers["user-agent"] = header_ua
    before = headers.copy()
    network._DownloadHttpAdapter().post("https://api.piggo.me/v1/api/me", ua="browser-UA", headers=headers,
                                       json_body={}, allow_redirects=False)
    actual = constructor.call_args.kwargs["headers"]
    assert actual == (before if header_ua else dict(before, **{"User-Agent": "browser-UA"}))
    assert headers == before
    request.post_res.assert_called_once_with("https://api.piggo.me/v1/api/me", json={}, allow_redirects=False)


def test_feed_balance_error_is_preserved_without_downloading(boundary):
    """
    收费种签发被站点余额规则拒绝时保留原因，不绕过规则，也不拉取文件。
    """
    reason = "饲料不足，需要 100"
    boundary.ticket.post.return_value = _response({"code": 422, "data": {"message": reason}, "msg": reason}, 422)
    errors = []
    boundary.chain.download_torrent(boundary.torrent, on_failure=errors.append)
    assert len(errors) == 1 and reason in errors[0]
    boundary.port.request.assert_not_called()
