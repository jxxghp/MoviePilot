# -*- coding: utf-8 -*-
"""
验证 HDDolby 站点搜索请求与种子结果解析。
"""

# MediaType 使用历史惰性导出入口，Pylint 无法静态解析。
# pylint: disable=no-name-in-module

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.indexer.spider import hddolby as hddolby_module
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.schemas import MediaType


def _build_indexer() -> dict:
    """
    构造杜比 API Spider 所需的最小站点配置。
    """
    return {
        "id": "hddolby",
        "name": "高清杜比",
        "domain": "https://www.hddolby.com/",
        "apikey": "dolby-secret",
        "ua": "MoviePilot-Test",
        "proxy": False,
    }


@pytest.fixture()
def hddolby_spider(monkeypatch):
    """
    构造不依赖真实数据库配置的 HddolbySpider。
    """
    monkeypatch.setattr(hddolby_module, "get_configured_system_config", lambda: None)
    return HddolbySpider(_build_indexer())


def test_music_search_uses_music_categories(hddolby_spider):
    """
    音乐搜索应提交杜比高品质音频和音乐视频分区。
    """
    params = hddolby_spider._HddolbySpider__get_params("张学友", MediaType.MUSIC, 0)

    assert params["categories"] == HddolbySpider._music_category


def test_movie_search_keeps_movie_categories(hddolby_spider):
    """
    电影搜索行为不受音乐分类新增影响。
    """
    params = hddolby_spider._HddolbySpider__get_params("流浪地球", MediaType.MOVIE, 0)

    assert params["categories"] == HddolbySpider._movie_category


def test_parse_result_marks_music_torrents(hddolby_spider):
    """
    高品质音频和音乐视频分区都应标记为音乐媒体类型。
    """
    results = hddolby_spider._HddolbySpider__parse_result([
        {"id": 1, "name": "VA - Chillout 2022 FLAC", "category": 408, "size": 1024},
        {"id": 2, "name": "Epica Live 1080p Blu-ray", "category": 406, "size": 1024},
        {"id": 3, "name": "流浪地球 2160p", "category": 401, "size": 1024},
        {"id": 4, "name": "剧集 S01", "category": 402, "size": 1024},
    ])

    assert [torrent["category"] for torrent in results] == [
        MediaType.MUSIC.value,
        MediaType.MUSIC.value,
        MediaType.MOVIE.value,
        MediaType.TV.value,
    ]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("domain", "api_url", "expected_url"),
    [
        ("https://www.hddolby.com/", None, "https://api.hddolby.com/api/v1/torrent/search"),
        ("https://www.hddolby.com/", "", "https://api.hddolby.com/api/v1/torrent/search"),
        ("https://www.uhddolby.com/", "https://www.uhddolby.com/api/v1",
         "https://www.uhddolby.com/api/v1/torrent/search"),
        ("https://www.uhddolby.com/", "https://www.uhddolby.com/api/v1/",
         "https://www.uhddolby.com/api/v1/torrent/search"),
        ("https://www.uhddolby.com/", "https://api.hddolby.com/api/v1",
         "https://api.hddolby.com/api/v1/torrent/search"),
        ("https://www.uhddolby.com/", "https://api.hddolby.com/api/v1/",
         "https://api.hddolby.com/api/v1/torrent/search"),
        ("https://www.uhddolby.com/", "https://custom-api.test:8443/dolby/v1/",
         "https://custom-api.test:8443/dolby/v1/torrent/search"),
    ],
)
def test_search_honors_resource_api_url(
    monkeypatch, asynchronous, domain, api_url, expected_url,
):
    """
    同步和异步搜索使用资源 API 地址及 API Key，详情和下载链接仍属于站点前端。
    """
    monkeypatch.setattr(hddolby_module, "get_configured_system_config", lambda: None)
    indexer = {**_build_indexer(), "domain": domain, "api_url": api_url}
    spider = HddolbySpider(indexer)
    response = Mock(status_code=200)
    response.json.return_value = {
        "status": 0,
        "data": [{"id": 42, "name": "Example.Movie", "category": 401, "downhash": "test-hash"}],
    }
    request = Mock()
    transport = Mock(return_value=response)
    if asynchronous:
        transport = AsyncMock(return_value=response)
    request.return_value.post_res = transport
    monkeypatch.setattr(hddolby_module, "AsyncRequestUtils" if asynchronous else "RequestUtils", request)

    if asynchronous:
        error, torrents = asyncio.run(spider.async_search("Movie", MediaType.MOVIE, page=2))
    else:
        error, torrents = spider.search("Movie", MediaType.MOVIE, page=2)

    assert error is False
    transport.assert_called_once_with(url=expected_url, json={
        "keyword": "Movie", "page_number": 2, "page_size": 100,
        "categories": HddolbySpider._movie_category, "visible": 1,
    })
    assert request.call_args.kwargs["headers"]["x-api-key"] == "dolby-secret"
    assert torrents[0]["page_url"] == f"{domain}details.php?id=42&hit=1"
    assert torrents[0]["enclosure"] == f"{domain}download.php?id=42&downhash=test-hash"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_search_connection_failure_logs_api_host_without_credentials(monkeypatch, asynchronous):
    """
    同步和异步搜索的连接告警指出 API 主机，避免误报网页故障并隐藏地址中的凭据。
    """
    monkeypatch.setattr(hddolby_module, "get_configured_system_config", lambda: None)
    spider = HddolbySpider({
        **_build_indexer(),
        "domain": "https://www.uhddolby.com/",
        "api_url": "https://user:secret@api.hddolby.com/api/v1?token=secret",
    })
    request = Mock()
    request.return_value.post_res = AsyncMock(return_value=None) if asynchronous else Mock(return_value=None)
    monkeypatch.setattr(hddolby_module, "AsyncRequestUtils" if asynchronous else "RequestUtils", request)
    warning = Mock()
    monkeypatch.setattr(hddolby_module.logger, "warn", warning)

    result = asyncio.run(spider.async_search("Movie")) if asynchronous else spider.search("Movie")

    assert result == (True, [])
    warning.assert_called_once_with(
        "高清杜比 搜索失败，HDDolby API 主机 api.hddolby.com 无法连接，请检查 API 地址及网络代理",
    )
