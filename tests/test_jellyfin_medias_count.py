# -*- coding: utf-8 -*-
from unittest.mock import patch

from app.modules.jellyfin.jellyfin import Jellyfin


class _FakeResponse:
    """模拟媒体服务器 HTTP 响应。"""

    def __init__(self, payload: dict, status_code: int = 200):
        """保存响应数据和状态码。"""
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        """返回模拟的 JSON 数据。"""
        return self._payload


def _make_client(user: str = "user-id") -> Jellyfin:
    """构造跳过初始化的 Jellyfin 客户端。"""
    client = Jellyfin.__new__(Jellyfin)
    client._host = "http://media.local/"
    client._apikey = "token"
    client.user = user
    client._sync_libraries = []
    return client


def _routed_get_res(views: dict, counts: dict, global_counts: dict = None):
    """按 URL 分发的 get_res 模拟：视图列表、单库统计与全局统计。"""

    def _get_res(url, params=None, **_kwargs):
        if url.endswith("/Views"):
            return _FakeResponse(views)
        if url.endswith("/Items") and params:
            key = (params.get("ParentId"), params.get("IncludeItemTypes"))
            return _FakeResponse({"TotalRecordCount": counts.get(key, 0)})
        if url.endswith("Items/Counts"):
            return _FakeResponse(global_counts or {})
        raise AssertionError(f"意外的请求地址：{url}")

    return _get_res


def test_medias_count_deduplicates_multi_folder_library():
    """多文件夹电影库应按用户视图统计，避免版本重复计数（#5915）。"""
    client = _make_client()
    views = {"Items": [{"Id": "lib-movie", "CollectionType": "movies"}]}
    # 用户级查询会折叠同一影片的多个版本，返回 67 而非数据库原始行数 201
    counts = {("lib-movie", "Movie"): 67}

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.side_effect = _routed_get_res(
            views, counts, global_counts={"MovieCount": 201}
        )
        stat = client.get_medias_count()

    assert stat.movie_count == 67
    assert stat.tv_count == 0
    assert stat.episode_count == 0


def test_medias_count_buckets_by_collection_type():
    """电影与剧集库应按视图类型分桶累计，未知类型库不参与统计。"""
    client = _make_client()
    views = {
        "Items": [
            {"Id": "lib-movie", "CollectionType": "movies"},
            {"Id": "lib-tv", "CollectionType": "tvshows"},
            {"Id": "lib-music", "CollectionType": "music"},
            {"Id": None, "CollectionType": "movies"},
        ]
    }
    counts = {
        ("lib-movie", "Movie"): 12,
        ("lib-tv", "Series"): 3,
        ("lib-tv", "Episode"): 45,
    }

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.side_effect = _routed_get_res(
            views, counts
        )
        stat = client.get_medias_count()

    assert stat.movie_count == 12
    assert stat.tv_count == 3
    assert stat.episode_count == 45


def test_medias_count_falls_back_without_user():
    """无可用用户时应回退到全局 Items/Counts 统计。"""
    client = _make_client(user=None)

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.return_value = _FakeResponse(
            {"MovieCount": 5, "SeriesCount": 2, "EpisodeCount": 30}
        )
        stat = client.get_medias_count()

    assert stat.movie_count == 5
    assert stat.tv_count == 2
    assert stat.episode_count == 30
    args = request_utils_cls.return_value.get_res.call_args.args
    assert args[0] == "http://media.local/Items/Counts"


def test_medias_count_falls_back_when_views_unavailable():
    """媒体库视图查询失败时应回退到全局 Items/Counts 统计。"""
    client = _make_client()

    def _get_res(url, params=None, **_kwargs):
        if url.endswith("/Views"):
            return None
        if url.endswith("Items/Counts"):
            return _FakeResponse({"MovieCount": 7, "SeriesCount": 1, "EpisodeCount": 9})
        raise AssertionError(f"意外的请求地址：{url}")

    with patch("app.modules.jellyfin.jellyfin.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.side_effect = _get_res
        stat = client.get_medias_count()

    assert stat.movie_count == 7
    assert stat.tv_count == 1
    assert stat.episode_count == 9
