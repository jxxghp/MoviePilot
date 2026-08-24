# -*- coding: utf-8 -*-
# pylint: disable=no-name-in-module

import pytest

from app.modules.indexer.spider import rousi as rousi_module
from app.modules.indexer.spider.rousi import RousiSpider
from app.schemas import MediaType


def _build_indexer() -> dict:
    """构造 Rousi Pro API Spider 所需的最小站点配置。"""
    return {
        "id": "rousipro",
        "name": "Rousi Pro",
        "domain": "https://rousi.pro/",
        "apikey": "rousi-secret",
        "ua": "MoviePilot-Test",
        "proxy": False,
    }


@pytest.fixture()
def rousi_spider(monkeypatch):
    """构造不依赖真实数据库配置的 RousiSpider。"""
    monkeypatch.setattr(rousi_module, "get_configured_system_config", lambda: None)
    return RousiSpider(_build_indexer())


def test_music_search_uses_music_category(rousi_spider):
    """音乐搜索应提交 Rousi Pro 的 music 分类参数。"""
    params = rousi_spider._RousiSpider__get_params("张学友", MediaType.MUSIC, None, 0)

    assert params["category"] == "music"


def test_user_selected_music_category_maps_to_api_name(rousi_spider):
    """用户选择音乐分类 ID 时应映射为 API 的 music 分类名。"""
    params = rousi_spider._RousiSpider__get_params("张学友", None, "5", 0)

    assert params["category"] == "music"


def test_parse_result_marks_music_torrents(rousi_spider):
    """music 分类种子应标记为音乐媒体类型。"""
    torrents = rousi_spider._RousiSpider__parse_result([
        {"id": 1, "uuid": "u1", "title": "张学友 - 他在那里 FLAC", "category": "music"},
        {"id": 2, "uuid": "u2", "title": "流浪地球", "category": "movie"},
        {"id": 3, "uuid": "u3", "title": "剧集 S01", "category": {"slug": "tv"}},
    ])

    assert [torrent["category"] for torrent in torrents] == [
        MediaType.MUSIC.value,
        MediaType.MOVIE.value,
        MediaType.TV.value,
    ]
