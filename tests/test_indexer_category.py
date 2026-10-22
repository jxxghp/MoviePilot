"""
验证 API 站点搜索的多选分类优先级及同步、异步请求体。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.indexer.spider import hddolby, mtorrent, yema
from app.schemas.types import MediaType


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("selection", [None, "", "single", "multiple"])
@pytest.mark.parametrize(
    ("module", "spider_class", "selected_ids", "default_ids"),
    [
        (mtorrent, mtorrent.MTorrentSpider, ["402", "403"], mtorrent.MTorrentSpider._movie_category),
        (hddolby, hddolby.HddolbySpider, [402, 403], hddolby.HddolbySpider._movie_category),
        (yema, yema.YemaSpider, [5, 6], yema.YemaSpider._movie_category),
    ],
)
def test_search_requests_use_explicit_categories_before_media_type(
    monkeypatch, asynchronous, selection, module, spider_class, selected_ids, default_ids,
):
    """
    单选和多选都优先于电影预置分类，空分类保持旧行为及各站分页单位。
    """
    if module is not yema:
        monkeypatch.setattr(module, "get_configured_system_config", lambda: None)
    spider = spider_class({
        "id": 1, "name": "测试站点", "domain": "https://site.example/",
        "apikey": "offline-key", "ua": "MoviePilot-Test", "proxy": False,
    })
    ids = selected_ids[:1] if selection == "single" else selected_ids
    cat = ",".join(str(category) for category in ids) if selection else selection
    expected_ids = ids if selection else default_ids
    payload = {"success": True, "data": []} if module is not mtorrent else {"code": "0", "data": {"data": []}}
    response = SimpleNamespace(status_code=200, json=lambda: payload)
    request = AsyncMock(return_value=response) if asynchronous else Mock(return_value=response)
    transport = module.AsyncRequestUtils if asynchronous else module.RequestUtils
    monkeypatch.setattr(transport, "post_res", request)

    arguments = {"keyword": "关键词", "mtype": MediaType.MOVIE, "page": 2, "cat": cat}
    result = asyncio.run(spider.async_search(**arguments)) if asynchronous else spider.search(**arguments)

    assert result == (False, [])
    requests = [call.kwargs["json"] for call in request.call_args_list]
    if module is yema:
        assert [body["categoryId"] for body in requests] == expected_ids
        assert all(body["pageParam"] == {"current": 3, "pageSize": 40} for body in requests)
        assert all(body["keyword"] == "关键词" for body in requests)
    else:
        assert len(requests) == 1
        assert requests[0]["categories"] == expected_ids
        page_key = "pageNumber" if module is mtorrent else "page_number"
        assert requests[0][page_key] == (3 if module is mtorrent else 2)
        assert requests[0]["keyword"] == "关键词"
