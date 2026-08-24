# -*- coding: utf-8 -*-
# pylint: disable=no-name-in-module

import pytest

from app.modules.indexer.spider import haidan as haidan_module
from app.modules.indexer.spider.haidan import HaiDanSpider
from app.schemas import MediaType


def _build_indexer() -> dict:
    """构造海胆 API Spider 所需的最小站点配置。"""
    return {
        "id": "haidan",
        "name": "海胆之家",
        "domain": "https://www.haidan.cc/",
        "cookie": "haidan-cookie",
        "ua": "MoviePilot-Test",
        "proxy": False,
    }


@pytest.fixture()
def haidan_spider(monkeypatch):
    """构造不依赖真实数据库配置的 HaiDanSpider。"""
    monkeypatch.setattr(haidan_module, "get_configured_system_config", lambda: None)
    return HaiDanSpider(_build_indexer())


def test_music_search_uses_music_categories(haidan_spider):
    """音乐搜索应提交海胆 HQ Audio 和音乐视频分区。"""
    params = haidan_spider._HaiDanSpider__get_params("张学友", MediaType.MUSIC)

    assert "cat=406%2C408" in params or "cat=406,408" in params
    assert "401" not in params


def test_movie_search_keeps_movie_categories(haidan_spider):
    """电影搜索行为不受音乐分类新增影响。"""
    params = haidan_spider._HaiDanSpider__get_params("流浪地球", MediaType.MOVIE)

    assert "cat=401%2C404%2C405" in params or "cat=401,404,405" in params


def test_parse_result_reads_item_category(haidan_spider):
    """分类字段取自每条种子，音乐分区应标记为音乐媒体类型。"""
    result = {
        "code": 0,
        "data": {
            "1": {"name": "张学友 - 他在那里 FLAC", "category": 408, "size": "1024"},
            "2": {"name": "张学友演唱会", "category": 406, "size": "1024"},
            "3": {"name": "流浪地球", "category": 401, "size": "1024"},
            "4": {"name": "剧集 S01", "category": 402, "size": "1024"},
        },
    }

    torrents = haidan_spider._HaiDanSpider__parse_result(result)

    assert [torrent["category"] for torrent in torrents] == [
        MediaType.MUSIC.value,
        MediaType.MUSIC.value,
        MediaType.MOVIE.value,
        MediaType.TV.value,
    ]
