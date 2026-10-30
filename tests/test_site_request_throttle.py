"""
站点搜索、换票、取种子的共享额度及远端冷却离线回归。
"""

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import Response

import app.application.site.throttle as throttle
import app.application.torrent.download as download
import app.chain.download.submission as submission
from app.adapters.cache.backends import FileBackend
from app.application.site.observation import capture_site_search_observation
from app.chain.download import DownloadChain
from app.chain.search import provider
from app.domain.context import TorrentInfo
from app.runtime.rate import RequestRateLimiter
from app.schemas.exception import RateLimitExceededException
from app.schemas.site import Site

URL = "https://api.tracker.example/download.php?id=39"
CONTENT = b"d4:infod4:name10:Series.mkv6:lengthi1eee"
MESSAGE = "Request frequency limit, please try in 600 seconds."


def _response(content=CONTENT, status=200):
    """
    构造保留非成功状态布尔语义的真实 HTTP 响应。
    """
    response = Response()
    response.status_code = status
    response._content = content
    response.encoding = "utf-8"
    response.close = Mock()
    return response


def _ticket(method="post") -> str:
    """
    生成带显式结果字段的双阶段换票地址。
    """
    payload = json.dumps({"method": method, "result": "data"}).encode()
    return f"[{base64.b64encode(payload).decode()}]https://ticket.tracker.example/token"


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    """
    隔离站点配置、缓存和网络，按真实等待推进虚拟单调时钟。
    """
    monkeypatch.setattr(throttle, "_site_requests", RequestRateLimiter())
    clock = SimpleNamespace(now=100.0, waits=[])

    def advance(delay):
        """
        推进虚拟时间，真实 HTTP 边界据此记录请求起点。
        """
        clock.waits.append(delay)
        clock.now += delay

    monkeypatch.setattr(throttle.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(throttle.time, "sleep", advance)
    query = Mock()
    query.get_sync.side_effect = lambda site_id: Site(
        id=site_id, domain="tracker.example", limit_seconds=10, limit_interval=60, limit_count=2,
    )
    query.get_by_domain_sync.return_value = None
    monkeypatch.setattr(throttle, "get_configured_site_query_service", lambda: query)
    cache = FileBackend(tmp_path)
    monkeypatch.setattr(download, "FileCache", lambda **_kwargs: cache)
    monkeypatch.setattr(submission, "FileCache", lambda **_kwargs: cache)
    port = Mock()
    port.request.return_value = _response()
    monkeypatch.setattr(download, "_require_torrent_port", lambda: port)
    ticket_port = Mock()
    ticket_port.post.return_value = _response(json.dumps({"data": URL}).encode())
    monkeypatch.setattr(submission, "_download_ports_snapshot", lambda: (ticket_port, Mock()))
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    return SimpleNamespace(clock=clock, port=port, ticket=ticket_port, chain=chain, query=query)


def test_search_ticket_and_content_share_interval_and_window(boundary):
    """
    同站不同网页与 API 主机按十秒间隔、六十秒两次共用额度，缓存命中不消耗额度。
    """
    starts = []
    owner = Mock()
    owner.search_site_torrents.side_effect = lambda **_kwargs: starts.append(boundary.clock.now) or []
    boundary.ticket.post.side_effect = lambda *_args, **_kwargs: (
        starts.append(boundary.clock.now) or _response(json.dumps({"data": URL}).encode())
    )
    boundary.port.request.side_effect = lambda **_kwargs: starts.append(boundary.clock.now) or _response()
    site = {"id": 42, "domain": "web.tracker.example", "limit_seconds": 10,
            "limit_interval": 60, "limit_count": 2}
    provider._search_site_page(owner, site=site, keyword="Series", media_type=None, page=0)
    torrent = TorrentInfo(site=42, enclosure=_ticket(), page_url="https://web.tracker.example/detail/39")

    assert boundary.chain.download_torrent(torrent)[0] == CONTENT
    assert boundary.chain.download_torrent(torrent)[0] == CONTENT

    assert starts == [100, 110, 160]
    assert boundary.clock.waits == [10, 50]
    boundary.ticket.post.assert_called_once()
    boundary.port.request.assert_called_once()


@pytest.mark.parametrize("first", ["redirect", "retry", "confirm"])
def test_each_actual_http_request_consumes_one_slot(boundary, first):
    """
    跳转、普通重试及首次确认 POST 均需重新申请同一站点请求额度。
    """
    first_response = _response(status=302 if first == "redirect" else 503)
    if first == "redirect":
        first_response.headers["Location"] = "https://cdn.example/file.torrent"
    if first == "confirm":
        first_response = _response('下载种子文件<form method="post"><input name="confirm" value="yes"></form>'.encode())
    responses = iter([first_response, _response()])
    starts = []

    def request(**_kwargs):
        """
        在 HTTP 出口记录实际请求时刻并回放响应。
        """
        starts.append(boundary.clock.now)
        return next(responses)

    boundary.port.request.side_effect = request

    assert download.TorrentHelper().download_torrent(URL, site_id=42)[1] == CONTENT
    assert starts == [100, 110]
    methods = [call.kwargs["method"] for call in boundary.port.request.call_args_list]
    assert methods == ["GET", "POST" if first == "confirm" else "GET"]


def test_remote_cooldown_blocks_other_resources_and_search_until_expiry(boundary):
    """
    频控响应冷却整个站点，其他种子和搜索不再出站，其他站点仍可请求，到期恢复。
    """
    boundary.port.request.return_value = _response(f"<html>{MESSAGE}</html>".encode(), 403)
    helper = download.TorrentHelper()
    error = f"站点限流：{MESSAGE}"

    assert helper.download_torrent(URL, site_id=42)[4] == error
    assert helper.download_torrent(URL + "0", site_id=42)[4] == error
    owner = Mock()
    with capture_site_search_observation() as observation:
        assert provider._search_site_page(owner, site={"id": 42}, keyword="Series", media_type=None, page=0) == []
    assert not observation.attempted
    assert observation.outcome == "deferred"
    assert observation.error == error
    owner.search_site_torrents.assert_not_called()
    assert provider._site_rate_limit_backoff({"id": 42}, error) == 601
    boundary.port.request.assert_called_once()
    boundary.port.request.return_value = _response()
    assert helper.download_torrent(URL + "1", site_id=43)[1] == CONTENT
    boundary.clock.now += 599
    assert helper.download_torrent(URL + "2", site_id=42)[4] == error
    assert boundary.port.request.call_count == 2
    boundary.clock.now += 1
    assert helper.download_torrent(URL + "2", site_id=42)[1] == CONTENT
    assert boundary.port.request.call_count == 3
    assert boundary.clock.waits == []


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("status", [200, 403, 429])
def test_ticket_limit_keeps_reason_and_closes_response(boundary, method, status):
    """
    换票阶段的 JSON 或 HTML 限流保留原文，失败响应不被布尔值丢弃且始终释放。
    """
    content = (json.dumps({"message": MESSAGE}).encode() if status == 200 else
               f"<html><h1>403 Forbidden</h1>{MESSAGE}</html>".encode())
    response = _response(content, status)
    getattr(boundary.ticket, method).return_value = response
    errors = []
    torrent = TorrentInfo(site=42, enclosure=_ticket(method), title="Series")

    assert boundary.chain.download_torrent(torrent, on_failure=errors.append) == (None, "", [])

    assert errors == [f"站点限流：{MESSAGE}"]
    assert errors[0] in boundary.chain.post_message.call_args.args[0].text
    response.close.assert_called_once()
    boundary.port.request.assert_not_called()
    assert boundary.chain.download_torrent(torrent, on_failure=errors.append) == (None, "", [])
    getattr(boundary.ticket, method).assert_called_once()


def test_confirmation_limit_stops_before_next_resource(boundary):
    """
    确认 POST 返回频控时也建立站点冷却，后续资源不再发送 GET。
    """
    page = '下载种子文件<form method="post"><input name="confirm" value="yes"></form>'.encode()
    boundary.port.request.side_effect = [_response(page), _response(f"<html>{MESSAGE}</html>".encode(), 403)]
    helper = download.TorrentHelper()
    assert helper.download_torrent(URL, site_id=42)[4] == f"站点限流：{MESSAGE}"
    assert helper.download_torrent(URL + "1", site_id=42)[4] == f"站点限流：{MESSAGE}"
    assert boundary.port.request.call_count == 2


def test_direct_helper_matches_domain_config_and_cache_skips_wait(boundary):
    """
    未提供站点 ID 的独立下载按地址匹配配置，成功缓存命中不重复申请额度。
    """
    boundary.query.get_by_domain_sync.return_value = Site(id=42, limit_seconds=10)
    helper = download.TorrentHelper()
    assert helper.download_torrent(URL)[1] == CONTENT
    assert helper.download_torrent(URL)[1] == CONTENT
    boundary.port.request.assert_called_once()
    boundary.query.get_by_domain_sync.assert_called_once_with("tracker.example")
    assert boundary.clock.waits == []


@pytest.mark.parametrize("invalid", [None, 0, -1, "invalid", "nan", "inf", {}, float("nan")])
def test_invalid_limits_do_not_wait(boundary, invalid):
    """
    空值、负数和不可解析配置不制造等待或无穷循环。
    """
    site = {"id": 42, "limit_seconds": invalid, "limit_interval": invalid, "limit_count": invalid}
    throttle.wait_for_site_request(site)
    throttle.wait_for_site_request(site)
    assert boundary.clock.waits == []


def test_concurrent_requests_cannot_claim_same_slot(boundary):
    """
    并发请求同一站点只发放一个许可；其他来源的许可独立。
    """
    limiter = RequestRateLimiter()
    with ThreadPoolExecutor(max_workers=12) as executor:
        delays = list(executor.map(lambda _index: limiter.acquire("42", interval=10, window=60, count=2), range(12)))
    assert delays.count((0, "")) == 1
    assert delays.count((10, "")) == 11
    assert limiter.acquire("43", interval=10, window=60, count=2) == (0, "")
    assert boundary.clock.waits == []


def test_late_wakeup_rechecks_actual_request_time(boundary, monkeypatch):
    """
    等待者迟醒后按实际开始时间计费，不能沿用过期预约连续发出请求。
    """
    def late_sleep(delay):
        """
        模拟线程调度比原定唤醒时间晚五秒。
        """
        boundary.clock.now += delay + 5

    monkeypatch.setattr(throttle.time, "sleep", late_sleep)
    site = {"id": 42, "limit_seconds": 10}
    starts = []
    for _ in range(3):
        throttle.wait_for_site_request(site)
        starts.append(boundary.clock.now)
    assert starts == [100, 115, 130]


@pytest.mark.asyncio
async def test_cancelled_async_wait_does_not_consume_request(boundary, monkeypatch):
    """
    取消等待不会提前占用计数窗口；异步搜索也遵守远端站点冷却。
    """
    sleeping = asyncio.Event()

    async def blocked_sleep(_delay):
        """
        暂停等待者，允许测试在获准请求前取消。
        """
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(throttle.asyncio, "sleep", blocked_sleep)
    site = {"id": 42, "limit_seconds": 10, "limit_interval": 60, "limit_count": 2}
    await throttle.async_wait_for_site_request(site)
    task = asyncio.create_task(throttle.async_wait_for_site_request(site))
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    boundary.clock.now += 10
    await throttle.async_wait_for_site_request(site)
    throttle.defer_site_request(site, f"站点限流：{MESSAGE}")
    with capture_site_search_observation() as observation:
        assert await provider._async_wait_for_site_request(site) == f"站点限流：{MESSAGE}"
    assert observation.outcome == "deferred"


def test_remote_cooldown_cannot_be_shortened(boundary):
    """
    后续较短的频控响应不缩短已有冷却，普通错误也不能改变其到期时间。
    """
    site = {"id": 42}
    assert throttle.site_request_cooldown(site) == 0
    throttle.defer_site_request(site, f"站点限流：{MESSAGE}")
    boundary.clock.now += 10
    throttle.defer_site_request(site, "站点限流：Request frequency limit, please try in 30 seconds.")
    throttle.defer_site_request(site, "torrent not found")
    assert throttle.site_request_cooldown(site) == 590
    with pytest.raises(RateLimitExceededException, match="600 seconds"):
        throttle.wait_for_site_request(site)
