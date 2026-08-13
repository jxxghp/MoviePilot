# -*- coding: utf-8 -*-
import pytest

from app.modules.indexer.spider import hddolby as hddolby_module
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.schemas import MediaType


def _build_indexer() -> dict:
    """构造杜比 API Spider 所需的最小站点配置。"""
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
    """构造不依赖真实数据库配置的 HddolbySpider。"""
    monkeypatch.setattr(hddolby_module, "SystemConfigOper", lambda: None)
    return HddolbySpider(_build_indexer())


def test_music_search_uses_music_categories(hddolby_spider):
    """音乐搜索应提交杜比高品质音频和音乐视频分区。"""
    params = hddolby_spider._HddolbySpider__get_params("张学友", MediaType.MUSIC, 0)

    assert params["categories"] == HddolbySpider._music_category


def test_movie_search_keeps_movie_categories(hddolby_spider):
    """电影搜索行为不受音乐分类新增影响。"""
    params = hddolby_spider._HddolbySpider__get_params("流浪地球", MediaType.MOVIE, 0)

    assert params["categories"] == HddolbySpider._movie_category


def test_parse_result_marks_music_torrents(hddolby_spider):
    """高品质音频和音乐视频分区都应标记为音乐媒体类型。"""
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
