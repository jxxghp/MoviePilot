# -*- coding: utf-8 -*-
"""
验证 HDWinG 促销标识补全爬虫的注入行为与专站解析器注册。
"""

import asyncio
import json
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from requests import Response

import app.application.site.throttle as throttle
import app.modules.indexer as indexer_module
import app.modules.indexer.spider as page_spider_module
import app.modules.indexer.spider.hdwing as hdwing
from app.adapters.system import rust as rust_accel
from app.modules.indexer import (
    _SPECIALIZED_SEARCH_ARGUMENTS,
    SPIDER_PARSER_CLASSES,
    HDWingPageSpider,
    HDWingSpider,
    IndexerModule,
)
from app.runtime.rate import RequestRateLimiter
from app.schemas.types import MediaType

# 模拟 HDWinG 列表页：占位符只保留种子 ID，促销标识由接口返回。
_PAGE_HTML = """
<html><head><title>HDWinG</title><meta name="x-csrf" content="a1b2c3d4e5"/></head><body>
<table class="torrent_list">
<tr class="torrent"><td><a title="Free.Torrent.2026" href="details.php?id=838806&amp;hit=1">Free.Torrent.2026</a></td>
<td class="discount"><span class="sp_state_placeholder" id="838806"></span></td></tr>
<tr class="torrent"><td><a title="Normal.Torrent.2026" href="details.php?id=838805&amp;hit=1">Normal.Torrent.2026</a></td>
<td class="discount"><span class="sp_state_placeholder" id="838805"></span></td></tr>
</table>
</body></html>
"""

# 无占位符的普通页面，不应触发促销接口请求。
_PLAIN_PAGE_HTML = """
<html><head><title>HDWinG</title></head><body>
<table class="torrent_list">
<tr class="torrent"><td><a title="Plain.Torrent.2026" href="details.php?id=838804&amp;hit=1">Plain.Torrent.2026</a></td></tr>
</table>
</body></html>
"""

# 仅通过退出链接声明 CSRF 令牌的页面。
_LOGOUT_CSRF_PAGE_HTML = """
<html><head><title>HDWinG</title></head><body>
<a href="logout.php?csrf=9988aabbcc">退出</a>
<table class="torrent_list">
<tr class="torrent"><td><a title="Free.Torrent.2026" href="details.php?id=838806&amp;hit=1">Free.Torrent.2026</a></td>
<td class="discount"><span class="sp_state_placeholder" id="838806"></span></td></tr>
</table>
</body></html>
"""

_PROMOTION_RESPONSE = {
    "status": 200,
    "message": {
        "838806": {
            "sp_state": ' <img class="pro_free" src="pic/trans.gif" alt="Free" title="免费" />',
            "timeout": "",
        },
        "838805": {"sp_state": "", "timeout": ""},
    },
}

_INDEXER = {
    "id": 42,
    "name": "HDWinG",
    "domain": "https://hdwing.org/",
    "parser": "HDWing",
    "search": {
        "paths": [{"path": "torrents.php"}],
        "params": {"search": "{keyword}"},
    },
    "torrents": {
        "list": {"selector": "tr.torrent"},
        "fields": {
            "title": {"selector": "a[title]"},
            "downloadvolumefactor": {
                "case": {
                    "img.pro_free": 0,
                    "img.pro_free2up": 0,
                    "img.pro_50pctdown": 0.5,
                    "img.pro_50pctdown2up": 0.5,
                    "img.pro_30pctdown": 0.3,
                }
            },
            "uploadvolumefactor": {
                "case": {
                    "img.pro_free2up": 2,
                    "img.pro_50pctdown2up": 2,
                    "img.pro_2up": 2,
                }
            },
        },
    },
}

_FAKE_TORRENTS = [{"title": "Fake.Torrent", "size": 1}]


@pytest.fixture(autouse=True)
def _isolate_site_throttle(monkeypatch):
    """
    默认隔离共享流控状态；流控专项用例单独安装真实限流器。
    """
    monkeypatch.setattr("app.modules.indexer.spider.hdwing.wait_for_site_request", MagicMock())


def _mock_response(json_data=None, status_code=200) -> MagicMock:
    """
    构造促销接口的模拟响应对象。

    :param json_data: 接口返回的 JSON 数据
    :param status_code: HTTP 状态码
    :return: 可注入 post_res 的模拟响应
    """
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data
    return response


def _http_response(body: str, status_code: int = 200) -> Response:
    """
    构造可由真实 HTTP 工具解码的离线响应。

    :param body: UTF-8 响应正文
    :param status_code: HTTP 状态码
    :return: 未连接网络的响应对象
    """
    response = Response()
    response.status_code = status_code
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response


def _patched_promotion(response: MagicMock):
    """
    替换爬虫模块内的 RequestUtils，捕获促销接口请求参数。

    :param response: post_res 返回的模拟响应
    :return: patch 上下文与 post_res 模拟对象
    """
    patcher = patch("app.modules.indexer.spider.hdwing.RequestUtils")
    mocked_utils = patcher.start()
    mocked_utils.return_value.post_res.return_value = response
    return patcher, mocked_utils.return_value.post_res


def test_parser_registry_declares_hdwing():
    """
    站点资源以 parser: HDWing 声明后应命中注册表并按标准参数集合调用。
    """
    assert SPIDER_PARSER_CLASSES["HDWing"] is HDWingSpider
    assert _SPECIALIZED_SEARCH_ARGUMENTS["HDWing"] == ("keyword", "mtype", "cat", "page")


def test_get_search_page_size_returns_fixed_capacity():
    """
    HDWinG 列表页分页容量固定为 100，与浏览和关键字搜索无关。
    """
    assert HDWingSpider.get_search_page_size() == 100
    assert HDWingSpider.get_search_page_size(keyword="tron") == 100


def test_search_applies_arguments_and_returns_contract(monkeypatch):
    """
    专站搜索入口应把参数转发给页面爬虫并返回 (错误标志, 种子列表) 契约。
    """
    spider = HDWingSpider(indexer=_INDEXER)
    monkeypatch.setattr(HDWingPageSpider, "get_torrents", lambda self: _FAKE_TORRENTS)

    error_flag, result = spider.search(keyword="Tron", mtype=None, cat="401", page=2)

    assert (error_flag, result) == (False, _FAKE_TORRENTS)
    assert spider.error_detail is None
    assert spider._spider.keyword == "Tron"
    assert spider._spider.cat == "401"
    assert spider._spider.page == 2


@pytest.mark.asyncio
async def test_async_search_applies_arguments_and_returns_contract(monkeypatch):
    """
    异步专站搜索入口应复用同一参数上下文并返回相同契约。
    """
    spider = HDWingSpider(indexer=_INDEXER)
    calls = []

    async def get_torrents(page_spider):
        calls.append(page_spider)
        page_spider.is_error = True
        return _FAKE_TORRENTS

    monkeypatch.setattr(HDWingPageSpider, "async_get_torrents", get_torrents)

    error_flag, result = await spider.async_search(keyword="Tron", page=1)

    assert (error_flag, result) == (True, _FAKE_TORRENTS)
    assert spider._spider.keyword == "Tron"
    assert spider._spider.page == 1
    assert calls == [spider._spider]


def test_parse_injects_promotion_and_recognizes_free():
    """
    解析前应把接口返回的促销标识注入占位符，使通用选择器识别免费种子。
    """
    patcher, post_res = _patched_promotion(_mock_response(_PROMOTION_RESPONSE))
    try:
        spider = HDWingPageSpider(indexer=_INDEXER)
        torrents = spider.parse(_PAGE_HTML)
    finally:
        patcher.stop()

    assert spider.is_error is False
    assert len(torrents) == 2
    free_torrent = next(t for t in torrents if t["title"] == "Free.Torrent.2026")
    normal_torrent = next(t for t in torrents if t["title"] == "Normal.Torrent.2026")
    assert free_torrent["downloadvolumefactor"] == 0
    assert normal_torrent["downloadvolumefactor"] == 1

    assert post_res.call_count == 1
    _, kwargs = post_res.call_args
    assert kwargs["url"] == "https://hdwing.org/ajax_promotion.php"
    assert kwargs["data"]["csrf"] == "a1b2c3d4e5"
    assert kwargs["data"]["ids[]"] == ["838806", "838805"]


def test_parse_uses_logout_link_csrf_fallback():
    """
    页面缺少 meta 声明时应回退到退出链接中的 CSRF 令牌。
    """
    patcher, post_res = _patched_promotion(_mock_response(_PROMOTION_RESPONSE))
    try:
        spider = HDWingPageSpider(indexer=_INDEXER)
        torrents = spider.parse(_LOGOUT_CSRF_PAGE_HTML)
    finally:
        patcher.stop()

    assert len(torrents) == 1
    _, kwargs = post_res.call_args
    assert kwargs["data"]["csrf"] == "9988aabbcc"


def test_parse_skips_promotion_request_without_placeholder():
    """
    页面没有促销占位符时不应发起额外请求。
    """
    patcher, post_res = _patched_promotion(_mock_response(_PROMOTION_RESPONSE))
    try:
        spider = HDWingPageSpider(indexer=_INDEXER)
        torrents = spider.parse(_PLAIN_PAGE_HTML)
    finally:
        patcher.stop()

    post_res.assert_not_called()
    hdwing.wait_for_site_request.assert_not_called()
    assert len(torrents) == 1
    assert torrents[0]["downloadvolumefactor"] == 1


def test_parse_keeps_page_usable_when_promotion_request_fails():
    """
    促销接口失败时应按无促销的原始页面完成解析，不影响种子列表获取。
    """
    for response in (
        None,
        _mock_response(status_code=500),
        _mock_response({"status": 500, "message": "missing parameter"}),
    ):
        patcher, post_res = _patched_promotion(response)
        try:
            spider = HDWingPageSpider(indexer=_INDEXER)
            torrents = spider.parse(_PAGE_HTML)
        finally:
            patcher.stop()

        assert spider.is_error is False
        assert len(torrents) == 2
        assert all(t["downloadvolumefactor"] == 1 for t in torrents)
        assert post_res.call_count == 1


@pytest.mark.parametrize(
    ("promotion", "download", "upload"),
    [
        ("pro_free", 0, 1),
        ("pro_free2up", 0, 2),
        ("pro_50pctdown", 0.5, 1),
        ("pro_50pctdown2up", 0.5, 2),
        ("pro_30pctdown", 0.3, 1),
        ("pro_2up", 1, 2),
    ],
)
@pytest.mark.parametrize("use_rust", [False, True])
def test_promotion_factors_match_in_python_and_rust(monkeypatch, promotion, download, upload, use_rust):
    """
    各种促销图标在 Python 和真实 Rust 解析路径中都使用站点配置的倍率。
    """
    monkeypatch.setattr(rust_accel, "is_enabled", lambda: use_rust)
    original_parse = rust_accel.parse_indexer_torrents
    rust_results = []

    def parse_with_observation(**kwargs):
        """
        记录 Rust 是否实际完成解析，避免静默回退掩盖回归。
        """
        result = original_parse(**kwargs)
        rust_results.append(result)
        return result

    monkeypatch.setattr(rust_accel, "parse_indexer_torrents", parse_with_observation)
    response = {"status": 200, "message": {
        "838806": {"sp_state": f'<img class="{promotion}" src="pic/trans.gif" />', "timeout": ""},
    }}
    patcher, _ = _patched_promotion(_mock_response(response))
    try:
        torrents = HDWingPageSpider(indexer=_INDEXER).parse(_PAGE_HTML)
    finally:
        patcher.stop()

    assert len(rust_results) == 1
    assert (rust_results[0] is not None) == use_rust
    free_torrent = next(t for t in torrents if t["title"] == "Free.Torrent.2026")
    assert free_torrent["downloadvolumefactor"] == download
    assert free_torrent["uploadvolumefactor"] == upload


def test_promotion_preserves_placeholder_with_reordered_attributes():
    """
    单引号、不同属性顺序和额外 CSS 类不应阻止获取令牌与补全促销标识。
    """
    html = _PAGE_HTML.replace(
        '<meta name="x-csrf" content="a1b2c3d4e5"/>',
        "<meta content='a1b2c3d4e5' name='x-csrf'/>",
    ).replace(
        '<span class="sp_state_placeholder" id="838806"></span>',
        "<span id='838806' class='other sp_state_placeholder'> </span>",
    )
    patcher, post_res = _patched_promotion(_mock_response(_PROMOTION_RESPONSE))
    try:
        spider = HDWingPageSpider(indexer=_INDEXER)
        injected = spider.inject_promotions(html)
        torrents = spider.parse(html)
    finally:
        patcher.stop()

    assert "<span id='838806' class='other sp_state_placeholder'><p>" in injected
    assert next(t for t in torrents if t["title"] == "Free.Torrent.2026")["downloadvolumefactor"] == 0
    assert post_res.call_args.kwargs["data"] == {"ids[]": ["838806", "838805"], "csrf": "a1b2c3d4e5"}


def test_promotion_deduplicates_ids_and_keeps_timeout():
    """
    同一 ID 的多个占位符共用一次查询，每个位置都保留接口返回的限时文本。
    """
    html = _PAGE_HTML.replace(
        "</table>",
        '<span class="sp_state_placeholder" id="838806"></span></table>',
    )
    response = deepcopy(_PROMOTION_RESPONSE)
    response["message"]["838806"]["timeout"] = "<span>限时免费</span>"
    patcher, post_res = _patched_promotion(_mock_response(response))
    try:
        injected = HDWingPageSpider(indexer=_INDEXER).inject_promotions(html)
    finally:
        patcher.stop()

    assert post_res.call_args.kwargs["data"]["ids[]"] == ["838806", "838805"]
    assert injected.count("限时免费") == 2
    assert injected.count('class="pro_free"') == 2


def test_missing_csrf_skips_promotion_request():
    """
    缺少 CSRF 时不申请额外请求额度，仍按原始列表解析。
    """
    html = _PAGE_HTML.replace('<meta name="x-csrf" content="a1b2c3d4e5"/>', "")
    patcher, post_res = _patched_promotion(_mock_response(_PROMOTION_RESPONSE))
    try:
        torrents = HDWingPageSpider(indexer=_INDEXER).parse(html)
    finally:
        patcher.stop()

    post_res.assert_not_called()
    hdwing.wait_for_site_request.assert_not_called()
    assert len(torrents) == 2
    assert all(t["downloadvolumefactor"] == 1 for t in torrents)


def test_similar_class_name_is_not_a_promotion_placeholder():
    """
    CSS 类名仅含占位符子串时不得误发促销请求。
    """
    html = _PAGE_HTML.replace('class="sp_state_placeholder"', 'class="disabled-sp_state_placeholder"')
    patcher, post_res = _patched_promotion(_mock_response(_PROMOTION_RESPONSE))
    try:
        torrents = HDWingPageSpider(indexer=_INDEXER).parse(html)
    finally:
        patcher.stop()

    post_res.assert_not_called()
    hdwing.wait_for_site_request.assert_not_called()
    assert len(torrents) == 2
    assert all(t["downloadvolumefactor"] == 1 for t in torrents)


def test_promotion_request_keeps_authenticated_http_context(monkeypatch):
    """
    在真实 RequestUtils 出口验证 AJAX 请求保留 UA、Cookie、Referer、代理和表单参数。
    """
    calls = []
    proxies = {"https": "http://proxy.example:8080"}
    monkeypatch.setattr(
        page_spider_module,
        "get_runtime_setting",
        lambda name: proxies if name == "PROXY" else None,
    )

    def request(method, url, **kwargs):
        """
        记录真实请求参数并回放促销接口响应。
        """
        calls.append((method, url, kwargs))
        return _http_response(json.dumps(_PROMOTION_RESPONSE))

    monkeypatch.setattr("app.adapters.network.http.requests.request", request)
    site = {**_INDEXER, "ua": "HDWing-Test-UA", "cookie": "uid=123; pass=abc", "proxy": True, "timeout": 9}
    torrents = HDWingPageSpider(indexer=site).parse(_PAGE_HTML)

    assert next(t for t in torrents if t["title"] == "Free.Torrent.2026")["downloadvolumefactor"] == 0
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert (method.lower(), url) == ("post", "https://hdwing.org/ajax_promotion.php")
    assert kwargs["headers"]["User-Agent"] == "HDWing-Test-UA"
    assert kwargs["headers"]["referer"] == "https://hdwing.org/torrents.php"
    assert kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert kwargs["headers"]["Content-Type"].startswith("application/x-www-form-urlencoded")
    assert kwargs["cookies"] == {"uid": "123", "pass": "abc"}
    assert kwargs["proxies"] == proxies
    assert kwargs["timeout"] == 9
    assert kwargs["data"] == {"ids[]": ["838806", "838805"], "csrf": "a1b2c3d4e5"}


def test_promotion_request_shares_search_rate_limit(monkeypatch):
    """
    列表请求已占用站点额度时，促销 POST 应等待同一站点的最小访问间隔。
    """
    clock = SimpleNamespace(now=100.0, waits=[])
    monkeypatch.setattr(throttle, "_site_requests", RequestRateLimiter())
    monkeypatch.setattr(hdwing, "wait_for_site_request", throttle.wait_for_site_request)
    monkeypatch.setattr(throttle.time, "monotonic", lambda: clock.now)

    def advance(delay):
        """
        推进虚拟时钟，不进行真实等待。
        """
        clock.waits.append(delay)
        clock.now += delay

    monkeypatch.setattr(throttle.time, "sleep", advance)
    site = {**_INDEXER, "limit_seconds": 10, "limit_interval": 60, "limit_count": 2}
    throttle.wait_for_site_request(site)
    patcher, post_res = _patched_promotion(_mock_response(_PROMOTION_RESPONSE))
    starts = []
    post_res.side_effect = lambda **_kwargs: starts.append(clock.now) or _mock_response(_PROMOTION_RESPONSE)
    try:
        torrents = HDWingPageSpider(indexer=site).parse(_PAGE_HTML)
    finally:
        patcher.stop()

    assert starts == [110.0]
    assert clock.waits == [10.0]
    assert next(t for t in torrents if t["title"] == "Free.Torrent.2026")["downloadvolumefactor"] == 0


def test_promotion_cooldown_does_not_send_request(monkeypatch):
    """
    站点处于远端冷却期时不再发出促销请求，原列表仍可解析。
    """
    monkeypatch.setattr(throttle, "_site_requests", RequestRateLimiter())
    monkeypatch.setattr(hdwing, "wait_for_site_request", throttle.wait_for_site_request)
    throttle.defer_site_request(_INDEXER, "Request frequency limit, please try in 600 seconds.")
    patcher, post_res = _patched_promotion(_mock_response(_PROMOTION_RESPONSE))
    try:
        torrents = HDWingPageSpider(indexer=_INDEXER).parse(_PAGE_HTML)
    finally:
        patcher.stop()

    post_res.assert_not_called()
    assert len(torrents) == 2
    assert all(t["downloadvolumefactor"] == 1 for t in torrents)


def test_promotion_429_defers_following_site_requests(monkeypatch):
    """
    促销接口返回 HTTP 429 后，同一站点的后续请求必须进入共享冷却。
    """
    monkeypatch.setattr(throttle, "_site_requests", RequestRateLimiter())
    monkeypatch.setattr(hdwing, "wait_for_site_request", throttle.wait_for_site_request)
    patcher, post_res = _patched_promotion(_mock_response(status_code=429))
    try:
        spider = HDWingPageSpider(indexer=_INDEXER)
        first = spider.parse(_PAGE_HTML)
        second = spider.parse(_PAGE_HTML)
    finally:
        patcher.stop()

    assert len(first) == len(second) == 2
    assert all(t["downloadvolumefactor"] == 1 for t in first + second)
    post_res.assert_called_once()
    assert 3500 < throttle.site_request_cooldown(_INDEXER) <= 3600


@pytest.mark.parametrize("payload", [[], {"status": 200, "message": []},
                                      {"status": 200, "message": ["invalid"]},
                                      {"status": 200, "message": {"838806": "invalid"}}])
def test_invalid_promotion_response_keeps_original_page(payload):
    """
    非法的促销响应不能导致页面搜索失败或错误标记免费。
    """
    patcher, _ = _patched_promotion(_mock_response(payload))
    try:
        spider = HDWingPageSpider(indexer=_INDEXER)
        torrents = spider.parse(_PAGE_HTML)
    finally:
        patcher.stop()

    assert not spider.is_error
    assert len(torrents) == 2
    assert all(t["downloadvolumefactor"] == 1 for t in torrents)


def test_invalid_promotion_json_keeps_original_page():
    """
    响应正文不是 JSON 时保留原始列表，而不是把种子搜索判为失败。
    """
    response = _mock_response()
    response.json.side_effect = ValueError("invalid JSON")
    patcher, _ = _patched_promotion(response)
    try:
        spider = HDWingPageSpider(indexer=_INDEXER)
        torrents = spider.parse(_PAGE_HTML)
    finally:
        patcher.stop()

    assert not spider.is_error
    assert len(torrents) == 2
    assert all(t["downloadvolumefactor"] == 1 for t in torrents)


def test_search_initializes_fields_for_keyword_and_browse(monkeypatch):
    """
    页面爬虫必须在构造时收到关键字，才能正确选择搜索或浏览专用字段。
    """
    site = deepcopy(_INDEXER)
    site["browse"] = {
        "path": "browse.php",
        "list": {"selector": "tr.browse"},
        "fields": {"title": {"selector": "td.browse-title"}},
    }
    observed = []

    def get_torrents(spider):
        """
        记录页面爬虫选用的字段而不发起请求。
        """
        observed.append((spider.keyword, spider.list, spider.fields))
        return []

    monkeypatch.setattr(HDWingPageSpider, "get_torrents", get_torrents)
    spider = HDWingSpider(site)
    spider.search(keyword="Tron", mtype=MediaType.MOVIE, cat="401", page=2)
    spider.search()

    assert observed == [
        ("Tron", site["torrents"]["list"], site["torrents"]["fields"]),
        (None, site["browse"]["list"], site["browse"]["fields"]),
    ]


@pytest.mark.asyncio
async def test_failed_page_response_preserves_sync_async_failure_details(monkeypatch):
    """
    同步和异步列表页无响应时不得在促销补全阶段抛错或丢失失败原因。
    """
    monkeypatch.setattr(page_spider_module.RequestUtils, "get_res", lambda *_args, **_kwargs: None)

    async def get_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(page_spider_module.AsyncRequestUtils, "get_res", get_async)
    sync_spider = HDWingSpider(_INDEXER)
    async_spider = HDWingSpider(_INDEXER)

    assert sync_spider.search(keyword="Tron") == (True, [])
    assert await async_spider.async_search(keyword="Tron") == (True, [])
    assert sync_spider.error_detail == async_spider.error_detail == "站点无响应（网络错误或超时）"
    hdwing.wait_for_site_request.assert_not_called()


@pytest.mark.asyncio
async def test_async_promotion_wait_does_not_block_event_loop(monkeypatch):
    """
    异步搜索的促销流控等待必须位于页面解析线程，不占用事件循环。
    """
    entered = threading.Event()
    release = threading.Event()
    event_loop_thread = threading.get_ident()

    def wait_for_permit(_site):
        """
        阻塞解析线程，直到测试确认事件循环仍能运行。
        """
        assert threading.get_ident() != event_loop_thread
        entered.set()
        if not release.wait(5):
            raise TimeoutError("测试未释放促销请求")

    async def get_async(_client, _url, **_kwargs):
        return _http_response(_PAGE_HTML)

    monkeypatch.setattr(hdwing, "wait_for_site_request", wait_for_permit)
    monkeypatch.setattr(page_spider_module.AsyncRequestUtils, "get_res", get_async)
    patcher, post_res = _patched_promotion(_mock_response(_PROMOTION_RESPONSE))
    task = asyncio.create_task(HDWingSpider(_INDEXER).async_search(keyword="Tron"))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), timeout=4)
        assert not task.done()
    finally:
        release.set()
        try:
            result = await asyncio.wait_for(task, timeout=5)
        finally:
            patcher.stop()

    assert not result[0]
    assert result[1][0]["downloadvolumefactor"] == 0
    post_res.assert_called_once()


@pytest.mark.asyncio
async def test_indexer_dispatches_sync_and_async_search_with_promotions(monkeypatch):
    """
    从 IndexerModule 按资源声明的 parser 路由，离线验证两种入口的免费种子结果。
    """
    requests = []

    def request(method, url, **kwargs):
        """
        按 HTTP 方法回放列表页或促销 JSON。
        """
        requests.append((method.lower(), url, kwargs))
        body = _PAGE_HTML if method.lower() == "get" else json.dumps(_PROMOTION_RESPONSE)
        return _http_response(body)

    async def get_async(_client, url, **_kwargs):
        """
        回放异步列表页，促销请求仍经过真实同步 HTTP 工具。
        """
        requests.append(("async-get", url, {}))
        return _http_response(_PAGE_HTML)

    async def async_statistic(**_kwargs):
        """
        隔离与解析结果无关的站点健康持久化。
        """

    monkeypatch.setattr("app.adapters.network.http.requests.request", request)
    monkeypatch.setattr(page_spider_module.AsyncRequestUtils, "get_res", get_async)
    monkeypatch.setattr(IndexerModule, "_IndexerModule__search_check", staticmethod(lambda *_args: True))
    monkeypatch.setattr(IndexerModule, "_IndexerModule__indexer_statistic", staticmethod(lambda **_kwargs: None))
    monkeypatch.setattr(IndexerModule, "_IndexerModule__async_indexer_statistic", staticmethod(async_statistic))
    module = object.__new__(indexer_module.IndexerModule)

    sync_results = module.search_torrents(site=_INDEXER, keyword="Tron", page=1)
    async_results = await module.async_search_torrents(site=_INDEXER, keyword="Tron", page=1)

    assert [(t.title, t.downloadvolumefactor) for t in sync_results] == [
        ("Free.Torrent.2026", 0), ("Normal.Torrent.2026", 1),
    ]
    assert [(t.title, t.downloadvolumefactor) for t in async_results] == [
        ("Free.Torrent.2026", 0), ("Normal.Torrent.2026", 1),
    ]
    assert [method for method, _url, _kwargs in requests] == ["get", "post", "async-get", "post"]
