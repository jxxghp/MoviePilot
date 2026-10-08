# -*- coding: utf-8 -*-
# pylint: disable=no-name-in-module

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.indexer.spider import milkie as milkie_module
from app.modules.indexer.spider.milkie import MilkieSpider
from app.schemas.types import MediaType


def _build_indexer() -> dict:
    """构造 Milkie API Spider 所需的最小站点配置。"""
    return {
        "id": "milkie",
        "name": "Milkie",
        "domain": "https://milkie.cc/",
        "apikey": "milkie+secret",
        "ua": "MoviePilot-Test",
        "proxy": False,
    }


def _sample_torrent() -> dict:
    """构造 Milkie 搜索接口的一条真实形态结果。"""
    return {
        "id": "wrJoUrlySnVm",
        "releaseName": "Dune.Part.Two.2024.2160p.UHD.BluRay.H265-GAZPROM",
        "category": 1,
        "createdAt": "2026-10-08T08:46:25+02:00",
        "size": 1076476044,
        "downloaded": 12,
        "seeders": 11,
        "leechers": 0,
        "externals": {"imdb": "tt15239678", "tmdb": None, "tvmaze": None},
    }


@pytest.fixture()
def milkie_spider():
    """构造不依赖真实站点配置的 MilkieSpider。"""
    return MilkieSpider(_build_indexer())


def test_search_page_size_matches_api_limit(milkie_spider):
    """单页容量应与接口上限一致，供分页器正确切片。"""
    assert MilkieSpider.get_search_page_size("dune") == 100


def test_keyword_search_maps_media_categories(milkie_spider):
    """带关键字的搜索应按媒体类型收敛分类。"""
    params = milkie_spider._MilkieSpider__get_params(
        "Dune", MediaType.MOVIE, 0
    )

    assert params["query"] == "Dune"
    assert params["categories"] == "1"
    assert params["pi"] == 0
    assert params["ps"] == 100


def test_tv_and_music_category_mapping(milkie_spider):
    """剧集与音乐分别映射到站点 2、3 分类。"""
    tv_params = milkie_spider._MilkieSpider__get_params("Dune", MediaType.TV, 1)
    music_params = milkie_spider._MilkieSpider__get_params("Dune", MediaType.MUSIC, 0)

    assert tv_params["categories"] == "2"
    assert tv_params["pi"] == 1
    assert music_params["categories"] == "3"


def test_keyword_search_without_media_type_omits_categories(milkie_spider):
    """未指定媒体类型时不提交分类，避免站点接口过滤失效。"""
    params = milkie_spider._MilkieSpider__get_params("Dune", None, 0)

    assert "categories" not in params


def test_browse_mode_supports_pagination(milkie_spider):
    """无关键字的浏览模式同样支持服务端翻页。"""
    first_page = milkie_spider._MilkieSpider__get_params(None, None, 0)
    second_page = milkie_spider._MilkieSpider__get_params(None, None, 1)

    assert "query" not in first_page
    assert first_page["pi"] == 0
    assert second_page["pi"] == 1
    assert first_page["oby"] == "created_at"


def test_parse_result_projects_torrent_fields(milkie_spider):
    """结果字段应完整映射到站点搜索契约。"""
    torrents = milkie_spider._MilkieSpider__parse_result([_sample_torrent()])

    assert len(torrents) == 1
    torrent = torrents[0]
    assert torrent["title"] == "Dune.Part.Two.2024.2160p.UHD.BluRay.H265-GAZPROM"
    assert torrent["enclosure"] == (
        "https://milkie.cc/api/v1/torrents/wrJoUrlySnVm"
        "/torrent?key=milkie%2Bsecret"
    )
    assert torrent["page_url"] == "https://milkie.cc/browse/wrJoUrlySnVm"
    assert torrent["size"] == 1076476044
    assert torrent["seeders"] == 11
    assert torrent["peers"] == 0
    assert torrent["grabs"] == 12
    assert torrent["pubdate"] == "2026-10-08 08:46:25"
    assert torrent["imdbid"] == "tt15239678"
    assert torrent["category"] == MediaType.MOVIE.value


def test_parse_result_marks_site_as_freeleech(milkie_spider):
    """站点无分享率考核，所有种子应视为免费。"""
    torrents = milkie_spider._MilkieSpider__parse_result([_sample_torrent()])

    assert torrents[0]["downloadvolumefactor"] == 0
    assert torrents[0]["uploadvolumefactor"] == 1


def test_parse_result_category_fallback(milkie_spider):
    """游戏、软件等无对应媒体类型的分类不应输出媒体类型。"""
    raw = _sample_torrent()
    raw["category"] = 4

    torrents = milkie_spider._MilkieSpider__parse_result([raw])

    assert torrents[0]["category"] is None


def test_search_requires_api_key(milkie_spider, monkeypatch):
    """缺少 API Key 时应直接失败且不发起请求。"""
    request_utils = MagicMock()
    monkeypatch.setattr(milkie_module, "RequestUtils", request_utils)

    milkie_spider._apikey = None
    error, results = milkie_spider.search("Dune")

    assert error is True
    assert results == []
    request_utils.assert_not_called()


def _install_json_response(monkeypatch, status_code: int, payload: dict) -> MagicMock:
    """在模块边界替换同步 HTTP 客户端并返回调用记录。"""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    request_utils = MagicMock()
    request_utils.return_value.get_res.return_value = response
    monkeypatch.setattr(milkie_module, "RequestUtils", request_utils)
    return request_utils


def test_search_sends_auth_header_and_parses_response(milkie_spider, monkeypatch):
    """搜索应携带 x-milkie-auth 请求头并解析 torrents 数组。"""
    request_utils = _install_json_response(
        monkeypatch, 200, {"hits": 1, "torrents": [_sample_torrent()]}
    )

    error, results = milkie_spider.search("Dune", MediaType.MOVIE)

    assert error is False
    assert len(results) == 1
    headers = request_utils.call_args.kwargs["headers"]
    assert headers["x-milkie-auth"] == "milkie+secret"


def test_search_reports_invalid_api_key(milkie_spider, monkeypatch):
    """401 响应应判定为 API Key 失效。"""
    _install_json_response(monkeypatch, 401, {"message": "Unauthorized"})

    error, results = milkie_spider.search("Dune")

    assert error is True
    assert results == []


def test_search_reports_unreachable_site(milkie_spider, monkeypatch):
    """网络不可达时应返回错误标志。"""
    request_utils = MagicMock()
    request_utils.return_value.get_res.return_value = None
    monkeypatch.setattr(milkie_module, "RequestUtils", request_utils)

    error, results = milkie_spider.search("Dune")

    assert error is True
    assert results == []


@pytest.mark.asyncio
async def test_async_search_matches_sync_behavior(milkie_spider, monkeypatch):
    """异步搜索应与同步搜索使用同样的认证与解析路径。"""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"hits": 1, "torrents": [_sample_torrent()]}
    async_request_utils = MagicMock()
    async_request_utils.return_value.get_res = AsyncMock(return_value=response)
    monkeypatch.setattr(milkie_module, "AsyncRequestUtils", async_request_utils)

    error, results = await milkie_spider.async_search("Dune", MediaType.MOVIE)

    assert error is False
    assert results[0]["title"] == "Dune.Part.Two.2024.2160p.UHD.BluRay.H265-GAZPROM"
    headers = async_request_utils.call_args.kwargs["headers"]
    assert headers["x-milkie-auth"] == "milkie+secret"
