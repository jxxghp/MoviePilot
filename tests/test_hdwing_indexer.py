# -*- coding: utf-8 -*-
"""
验证 HDWinG 促销标识补全爬虫的注入行为与专站解析器注册。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.indexer import (
    _SPECIALIZED_SEARCH_ARGUMENTS,
    SPIDER_PARSER_CLASSES,
)
from app.modules.indexer.spider.hdwing import HDWingPageSpider, HDWingSpider

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
    "id": "hdwing",
    "name": "HDWinG",
    "domain": "https://hdwing.org/",
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
                    "img.pro_50pctdown": 0.5,
                }
            },
            "uploadvolumefactor": {
                "case": {
                    "img.pro_2up": 2,
                }
            },
        },
    },
}

_FAKE_TORRENTS = [{"title": "Fake.Torrent", "size": 1}]


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


def test_search_applies_arguments_and_returns_contract():
    """
    专站搜索入口应把参数转发给页面爬虫并返回 (错误标志, 种子列表) 契约。
    """
    spider = HDWingSpider(indexer=_INDEXER)
    spider._spider.get_torrents = MagicMock(return_value=_FAKE_TORRENTS)
    spider._spider.is_error = False

    error_flag, result = spider.search(keyword="Tron", mtype=None, cat="401", page=2)

    assert (error_flag, result) == (False, _FAKE_TORRENTS)
    assert spider.error_detail is None
    assert spider._spider.keyword == "Tron"
    assert spider._spider.cat == "401"
    assert spider._spider.page == 2


@pytest.mark.asyncio
async def test_async_search_applies_arguments_and_returns_contract():
    """
    异步专站搜索入口应复用同一参数上下文并返回相同契约。
    """
    spider = HDWingSpider(indexer=_INDEXER)
    spider._spider.async_get_torrents = AsyncMock(return_value=_FAKE_TORRENTS)
    spider._spider.is_error = True

    error_flag, result = await spider.async_search(keyword="Tron", page=1)

    assert (error_flag, result) == (True, _FAKE_TORRENTS)
    assert spider._spider.keyword == "Tron"
    assert spider._spider.page == 1
    spider._spider.async_get_torrents.assert_awaited_once()


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
