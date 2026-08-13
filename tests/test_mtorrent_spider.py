# -*- coding: utf-8 -*-
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
    monkeypatch.setattr(mtorrent_module, "SystemConfigOper", lambda: None)
    return MTorrentSpider(_build_indexer())


def test_music_search_uses_music_categories(mteam_spider):
    """音乐搜索应只提交馒头音乐分区分类，而不是电影分类。"""
    params = mteam_spider._MTorrentSpider__get_params("周杰伦 七里香", MediaType.MUSIC)

    assert params["categories"] == MTorrentSpider._music_category


def test_movie_search_keeps_movie_categories(mteam_spider):
    """电影搜索行为不受音乐分类新增影响。"""
    params = mteam_spider._MTorrentSpider__get_params("流浪地球", MediaType.MOVIE)

    assert params["categories"] == MTorrentSpider._movie_category


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
