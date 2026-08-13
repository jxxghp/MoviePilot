import asyncio

from app.modules.indexer.spider.torrentleech import TorrentLeech
from app.schemas import MediaType


class _FakeResponse:
    """构造 TorrentLeech 搜索测试使用的最小 JSON 响应。"""

    status_code = 200

    def json(self) -> dict:
        """返回包含音乐分类种子的固定响应。"""
        return {
            "torrentList": [{
                "fid": 100,
                "filename": "album.torrent",
                "name": "Artist Album FLAC",
                "categoryID": 31,
                "addedTimestamp": 1767225600,
                "size": 1024,
            }]
        }


def _build_indexer() -> dict:
    """构造 TorrentLeech 音乐搜索所需的最小站点配置。"""
    return {
        "id": "torrentleech",
        "name": "TorrentLeech",
        "domain": "https://www.torrentleech.org/",
        "ua": "MoviePilot-Test",
        "category": {
            "movie": [
                {"id": 8, "cat": "Movies"},
                {"id": 9, "cat": "Movies"},
            ],
            "tv": [
                {"id": 26, "cat": "TV"},
                {"id": 32, "cat": "TV"},
            ],
            "music": [
                {"id": 31, "cat": "Music"},
                {"id": 16, "cat": "Music"},
            ]
        },
    }


def test_torrentleech_music_search_filters_categories_and_maps_result(monkeypatch):
    """TorrentLeech 同步音乐搜索应提交 Audio/MV 分类并标记结果类型。"""
    captured = {}

    def fake_get_res(_request, url: str, **_kwargs):
        """记录同步搜索地址并回放 JSON 响应。"""
        captured["url"] = url
        return _FakeResponse()

    monkeypatch.setattr(
        "app.modules.indexer.spider.torrentleech.RequestUtils.get_res",
        fake_get_res,
    )

    error, torrents = TorrentLeech(_build_indexer()).search(
        keyword="Artist Album",
        mtype=MediaType.MUSIC,
    )

    assert not error
    assert captured["url"] == (
        "https://www.torrentleech.org/torrents/browse/list/categories/31,16/"
        "exact/1/query/Artist%20Album"
    )
    assert torrents[0]["category"] == MediaType.MUSIC.value


def test_torrentleech_video_search_uses_requested_category_contract(monkeypatch):
    """TorrentLeech 影视搜索应使用资源配置中的分类并标记请求类型。"""
    captured = {}

    def fake_get_res(_request, url: str, **_kwargs):
        """记录影视搜索地址并返回与电影分类匹配的固定响应。"""
        captured["url"] = url
        response = _FakeResponse()
        response.json = lambda: {
            "torrentList": [{
                "fid": 101,
                "filename": "movie.torrent",
                "name": "Movie 2026",
                "categoryID": 8,
                "addedTimestamp": 1767225600,
                "size": 2048,
            }]
        }
        return response

    monkeypatch.setattr(
        "app.modules.indexer.spider.torrentleech.RequestUtils.get_res",
        fake_get_res,
    )

    error, torrents = TorrentLeech(_build_indexer()).search(
        keyword="Movie 2026",
        mtype=MediaType.MOVIE,
    )

    assert not error
    assert "/categories/8,9/" in captured["url"]
    assert torrents[0]["category"] == MediaType.MOVIE.value


def test_torrentleech_async_music_search_uses_same_contract(monkeypatch):
    """TorrentLeech 异步音乐搜索应复用同步搜索的分类契约。"""
    captured = {}

    async def fake_get_res(_request, url: str, **_kwargs):
        """记录异步搜索地址并回放 JSON 响应。"""
        captured["url"] = url
        return _FakeResponse()

    monkeypatch.setattr(
        "app.modules.indexer.spider.torrentleech.AsyncRequestUtils.get_res",
        fake_get_res,
    )

    error, torrents = asyncio.run(
        TorrentLeech(_build_indexer()).async_search(
            keyword="Artist Album",
            mtype=MediaType.MUSIC,
        )
    )

    assert not error
    assert "/categories/31,16/" in captured["url"]
    assert torrents[0]["category"] == MediaType.MUSIC.value
