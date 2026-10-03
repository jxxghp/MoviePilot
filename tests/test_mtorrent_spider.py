# -*- coding: utf-8 -*-
# pylint: disable=no-name-in-module

import asyncio
from types import SimpleNamespace

import pytest

from app.modules.indexer.spider import mtorrent as mtorrent_module
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.schemas import MediaType


def _build_indexer() -> dict:
    """构造 M-Team API Spider 所需的最小站点配置。"""
    return {
        "id": "mteam",
        "name": "馒头",
        "domain": "https://xp.m-team.io/",
        "apikey": "mteam-secret",
        "ua": "MoviePilot-Test",
        "proxy": False,
    }


@pytest.fixture()
def mteam_spider(monkeypatch):
    """构造不依赖真实数据库配置的 MTorrentSpider。"""
    monkeypatch.setattr(mtorrent_module, "get_configured_system_config", lambda: None)
    return MTorrentSpider(_build_indexer())


def test_music_search_uses_music_categories(mteam_spider):
    """音乐搜索应只提交馒头音乐分区分类，而不是电影分类。"""
    params = mteam_spider._MTorrentSpider__get_params("周杰伦 七里香", MediaType.MUSIC)

    assert params["categories"] == MTorrentSpider._music_category


def test_movie_search_keeps_movie_categories(mteam_spider):
    """电影搜索行为不受音乐分类新增影响。"""
    params = mteam_spider._MTorrentSpider__get_params("流浪地球", MediaType.MOVIE)

    assert params["categories"] == MTorrentSpider._movie_category


def test_api_domain_can_come_from_site_resource(mteam_spider):
    """站点资源提供 API 主机时应优先使用该配置。"""
    indexer = {
        **_build_indexer(),
        "api_domain": "https://subtitle-api.example/api",
    }

    assert mteam_spider._MTorrentSpider__resolve_api_domain(indexer) == "subtitle-api.example"


def test_parse_result_marks_music_torrents(mteam_spider):
    """音乐分区种子应标记为音乐媒体类型，供音乐搜索链路筛选。"""
    results = mteam_spider._MTorrentSpider__parse_result([
        {"id": "1", "name": "周杰伦 - 七里香 [FLAC]", "category": "434", "size": "1024", "status": {}},
        {"id": "2", "name": "周杰伦演唱会", "category": "406", "size": "1024", "status": {}},
        {"id": "3", "name": "流浪地球 2160p", "category": "419", "size": "1024", "status": {}},
        {"id": "4", "name": "其他资源", "category": "999", "size": "1024", "status": {}},
    ])

    assert [torrent["category"] for torrent in results] == [
        MediaType.MUSIC.value,
        MediaType.MUSIC.value,
        MediaType.MOVIE.value,
        MediaType.UNKNOWN.value,
    ]


def test_subtitle_search_uses_api_and_generates_download_links(monkeypatch, mteam_spider):
    """M-Team 字幕搜索应使用 JSON API，并把字幕 ID 转换为下载链接。"""
    calls = []

    class FakeRequest:
        """记录请求适配器收到的配置。"""

        def __init__(self, **kwargs):
            """保存请求头和超时配置。"""
            self.kwargs = kwargs

        def post_res(self, url, json=None, data=None):
            """记录请求地址与 JSON 请求体。"""
            calls.append((self.kwargs, url, json, data))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "code": 0,
                    "data": {
                        "data": [{
                            "id": "subtitle-1",
                            "name": "Kill Bill 简中",
                            "filename": "Kill.Bill.zh.srt",
                            "torrent": "torrent-1",
                            "size": "12 KB",
                            "lang": "简中",
                            "hits": 3,
                            "author": "uploader",
                            "createdDate": "2026-01-01 00:00:00",
                        }],
                    },
                },
            )

    monkeypatch.setattr(mtorrent_module, "RequestUtils", FakeRequest)
    monkeypatch.setattr(
        mteam_spider,
        "_MTorrentSpider__subtitle_genlink",
        lambda subtitle_id: f"https://api.m-team.io/api/subtitle/dlV2?credential={subtitle_id}",
    )

    error, subtitles = mteam_spider.search_subtitles("杀死比尔", page=2)

    assert error is False
    assert subtitles[0]["subtitle_id"] == "subtitle-1"
    assert subtitles[0]["enclosure"].endswith("credential=subtitle-1")
    assert subtitles[0]["size"] == 12 * 1024
    request_kwargs, url, payload, data = calls[0]
    assert data is None
    assert url == "https://api.m-team.io/api/subtitle/search"
    assert payload == {"keyword": "杀死比尔", "pageNumber": 3, "pageSize": 20}
    assert request_kwargs["headers"]["x-api-key"] == "mteam-secret"


def test_async_subtitle_search_uses_same_api_contract(monkeypatch, mteam_spider):
    """M-Team 字幕搜索的异步入口应保持同步入口的地址和请求体。"""
    calls = []

    class FakeAsyncRequest:
        """记录异步请求适配器收到的配置。"""

        def __init__(self, **kwargs):
            """保存请求头和超时配置。"""
            self.kwargs = kwargs

        async def post_res(self, url, json=None, data=None):
            """记录异步请求地址与 JSON 请求体。"""
            calls.append((self.kwargs, url, json, data))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"code": 0, "data": {"data": []}},
            )

    monkeypatch.setattr(mtorrent_module, "AsyncRequestUtils", FakeAsyncRequest)

    error, subtitles = asyncio.run(mteam_spider.async_search_subtitles("杀死比尔", page=1))

    assert error is False
    assert subtitles == []
    request_kwargs, url, payload, data = calls[0]
    assert data is None
    assert url == "https://api.m-team.io/api/subtitle/search"
    assert payload == {"keyword": "杀死比尔", "pageNumber": 2, "pageSize": 20}
    assert request_kwargs["headers"]["x-api-key"] == "mteam-secret"


@pytest.mark.parametrize("status_code", [401, 403, 404, 405])
def test_subtitle_search_reports_http_errors(mteam_spider, status_code):
    """字幕接口鉴权或路径错误应保留明确的 HTTP 状态原因。"""
    error, subtitles = mteam_spider._MTorrentSpider__process_subtitle_response(
        SimpleNamespace(status_code=status_code, json=lambda: {})
    )

    assert error is True
    assert subtitles == []
    assert mteam_spider.error_detail == f"HTTP {status_code}"
