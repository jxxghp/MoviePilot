from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from app.modules.emby.emby import Emby
from app.schemas.types import MediaSource


class _FakeResponse:
    """提供 Emby 接口响应的最小 JSON 封装。"""

    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _build_client() -> Emby:
    client = Emby.__new__(Emby)
    client._host = "http://emby.local/"
    client._apikey = "api-key"
    client.user = "user-id"
    return client


def test_get_movies_prefers_provider_id_when_titles_are_aliases():
    """Provider ID 相同时应识别标题不同的 Emby 电影条目。"""
    client = _build_client()
    provider_item = {
        "Id": "movie-id",
        "Name": "电影别名",
        "ProductionYear": 2026,
        "Type": "Movie",
        "ProviderIds": {"Tmdb": "12345"},
    }

    with patch("app.modules.emby.emby.RequestUtils") as request_utils_cls:
        request_utils = request_utils_cls.return_value
        request_utils.get_res.return_value = _FakeResponse({"Items": [provider_item]})

        movies = client.get_movies(
            title="电影主标题",
            year="2026",
            media_source=MediaSource.TMDB,
            media_id="12345",
        )

    assert [movie.item_id for movie in movies] == ["movie-id"]
    params = request_utils.get_res.call_args.args[1]
    assert params["AnyProviderIdEquals"] == "Tmdb.12345"
    assert "SearchTerm" not in params


def test_get_tv_episodes_prefers_provider_id_when_titles_are_aliases():
    """Provider ID 相同时应通过别名剧集返回已入库季集。"""
    client = _build_client()
    client.get_iteminfo = Mock(
        return_value=SimpleNamespace(
            media_source=MediaSource.TMDB,
            media_id="37854",
        )
    )
    provider_item = {
        "Id": "series-id",
        "Name": "海贼王",
        "ProductionYear": 1999,
        "Type": "Series",
        "ProviderIds": {"Tmdb": "37854"},
    }

    with patch("app.modules.emby.emby.RequestUtils") as request_utils_cls:
        request_utils = request_utils_cls.return_value
        request_utils.get_res.side_effect = [
            _FakeResponse({"Items": [provider_item]}),
            _FakeResponse({
                "Items": [
                    {"ParentIndexNumber": 1, "IndexNumber": 1},
                    {"ParentIndexNumber": 2, "IndexNumber": 3},
                ]
            }),
        ]

        item_id, episodes = client.get_tv_episodes(
            title="航海王",
            year="1999",
            media_source=MediaSource.TMDB,
            media_id="37854",
        )

    assert item_id == "series-id"
    assert episodes == {1: [1], 2: [3]}
    first_params = request_utils.get_res.call_args_list[0].args[1]
    assert first_params["AnyProviderIdEquals"] == "Tmdb.37854"
    assert "SearchTerm" not in first_params


def test_get_tv_episodes_falls_back_to_exact_title_when_provider_id_misses():
    """Provider ID 未命中时应保留原有标题和年份查找。"""
    client = _build_client()
    client.get_iteminfo = Mock(
        return_value=SimpleNamespace(
            media_source=MediaSource.TMDB,
            media_id="37854",
        )
    )

    with patch("app.modules.emby.emby.RequestUtils") as request_utils_cls:
        request_utils = request_utils_cls.return_value
        request_utils.get_res.side_effect = [
            _FakeResponse({
                "Items": [{
                    "Id": "wrong-series-id",
                    "Name": "同名错误条目",
                    "ProductionYear": 1999,
                    "ProviderIds": {"Tmdb": "99999"},
                }]
            }),
            _FakeResponse({
                "Items": [{
                    "Id": "series-id",
                    "Name": "航海王",
                    "ProductionYear": 1999,
                }]
            }),
            _FakeResponse({
                "Items": [{"ParentIndexNumber": 1, "IndexNumber": 1}]
            }),
        ]

        item_id, episodes = client.get_tv_episodes(
            title="航海王",
            year="1999",
            media_source=MediaSource.TMDB,
            media_id="37854",
        )

    assert item_id == "series-id"
    assert episodes == {1: [1]}
    title_params = request_utils.get_res.call_args_list[1].args[1]
    assert title_params["SearchTerm"] == "航海王"
    assert "AnyProviderIdEquals" not in title_params
