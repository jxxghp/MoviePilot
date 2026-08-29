"""TmdbApi 同步与异步入口共享业务决策的回归测试。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Optional, Union
from unittest.mock import AsyncMock, Mock, call

import pytest

from app.modules.themoviedb.tmdbapi import TmdbApi
from app.modules.themoviedb.tmdbv3api.exceptions import (
    TMDbConnectionError,
    TMDbException,
)
from app.schemas.types import MediaType


def _api() -> TmdbApi:
    """构造不初始化真实 TMDB 客户端的最小 API 实例。"""
    api = object.__new__(TmdbApi)
    api.tmdb = SimpleNamespace(language="en")
    api.search = SimpleNamespace(
        total_results=0,
        movies=Mock(),
        async_movies=AsyncMock(),
        tv_shows=Mock(),
        async_tv_shows=AsyncMock(),
        multi=Mock(),
        async_multi=AsyncMock(),
        collections=Mock(),
        async_collections=AsyncMock(),
    )
    api.movie = SimpleNamespace(details=Mock(), async_details=AsyncMock())
    api.tv = SimpleNamespace(details=Mock(), async_details=AsyncMock())
    return api


def _movie_detail(tmdbid: int, title: str = "展示标题") -> dict:
    """构造包含别名、分级和多语言标题的电影详情。"""
    return {
        "id": tmdbid,
        "title": title,
        "original_title": "Original Title",
        "original_language": "en",
        "release_date": "2024-06-01",
        "genres": [{"id": 18}, {"id": 35}],
        "alternative_titles": {"titles": [{"iso_3166_1": "US", "title": "目标别名"}]},
        "translations": {
            "translations": [
                {
                    "iso_3166_1": "HK",
                    "data": {"title": "香港标题"},
                }
            ]
        },
        "release_dates": {
            "results": [
                {
                    "iso_3166_1": "US",
                    "release_dates": [{"certification": "PG-13"}],
                }
            ]
        },
    }


def _tv_detail(tmdbid: int, name: str = "目标剧") -> dict:
    """构造包含季年份和译名的电视剧详情。"""
    return {
        "id": tmdbid,
        "name": name,
        "original_name": "Original Series",
        "original_language": "en",
        "first_air_date": "2018-01-01",
        "genres": [{"id": 18}],
        "seasons": [{"season_number": 3, "air_date": "2024-03-02"}],
        "alternative_titles": {"results": [{"iso_3166_1": "US", "title": "剧集别名"}]},
        "translations": {"translations": []},
        "content_ratings": {"results": []},
    }


@pytest.mark.parametrize(
    ("mtype", "sync_name", "async_name", "title_field", "year_kwargs"),
    [
        (
            MediaType.MOVIE,
            "search_movies",
            "async_search_movies",
            "title",
            {"term": "目标", "year": "2024"},
        ),
        (
            MediaType.TV,
            "search_tvs",
            "async_search_tvs",
            "name",
            {"term": "目标", "release_year": "2024"},
        ),
    ],
)
def test_search_lists_share_query_and_projection(
    mtype: MediaType,
    sync_name: str,
    async_name: str,
    title_field: str,
    year_kwargs: dict[str, str],
) -> None:
    """电影和剧集列表双入口应共享参数、标题过滤和类型投影。"""
    api = _api()
    sync_items = [
        {"id": 1, title_field: "目标作品"},
        {"id": 2, title_field: "其他作品"},
    ]
    async_items = deepcopy(sync_items)
    sync_client = api.search.movies if mtype == MediaType.MOVIE else api.search.tv_shows
    async_client = api.search.async_movies if mtype == MediaType.MOVIE else api.search.async_tv_shows
    sync_client.return_value = sync_items
    async_client.return_value = async_items

    sync_result = getattr(api, sync_name)("目标", "2024")
    async_result = asyncio.run(getattr(api, async_name)("目标", "2024"))

    assert sync_result == async_result == [{"id": 1, title_field: "目标作品", "media_type": mtype}]
    sync_client.assert_called_once_with(**year_kwargs)
    async_client.assert_awaited_once_with(**year_kwargs)


def test_match_movie_sync_async_share_year_fallback_and_original_title() -> None:
    """电影匹配双入口应按当前、下一年顺序回退并采信原标题。"""
    api = _api()
    sync_hit = {
        "id": 10,
        "title": "本地标题",
        "original_title": "Target Movie",
        "release_date": "2025-02-01",
    }
    async_hit = deepcopy(sync_hit)
    api.search.movies.side_effect = [[], [sync_hit]]
    api.search.async_movies.side_effect = [[], [async_hit]]

    sync_result = api.match("Target Movie", MediaType.MOVIE, year="2024")
    async_result = asyncio.run(api.async_match("Target Movie", MediaType.MOVIE, year="2024"))

    assert sync_result == async_result
    assert sync_result["media_type"] == MediaType.MOVIE
    expected_calls = [
        call(term="Target Movie", year="2024"),
        call(term="Target Movie", year="2025"),
    ]
    assert api.search.movies.call_args_list == expected_calls
    assert api.search.async_movies.call_args_list == expected_calls


def test_match_movie_sync_async_share_alias_detail_fallback() -> None:
    """标题未命中时双入口应同序查询详情并使用别名。"""
    api = _api()
    sync_candidate = {
        "id": 11,
        "title": "展示标题",
        "original_title": "Original Title",
        "release_date": "2024-06-01",
    }
    async_candidate = deepcopy(sync_candidate)
    sync_detail = _movie_detail(11)
    async_detail = deepcopy(sync_detail)
    api.search.movies.return_value = [sync_candidate]
    api.search.async_movies.return_value = [async_candidate]
    api.movie.details.return_value = sync_detail
    api.movie.async_details.return_value = async_detail

    sync_result = api.match("目标别名", MediaType.MOVIE, year="2024")
    async_result = asyncio.run(api.async_match("目标别名", MediaType.MOVIE, year="2024"))

    assert sync_result == async_result
    assert sync_result["id"] == 11
    assert "目标别名" in sync_result["names"]
    api.movie.details.assert_called_once()
    api.movie.async_details.assert_awaited_once()


@pytest.mark.parametrize(
    "group_seasons",
    [
        None,
        [{"order": 3, "episodes": [{"air_date": "2024-04-01"}]}],
    ],
)
def test_match_tv_season_sync_async_share_detail_year_decision(
    group_seasons: Optional[list[dict]],
) -> None:
    """首播年份不同的剧集应由同一季年份规则在双入口命中。"""
    api = _api()
    sync_candidate = {
        "id": 20,
        "name": "目标剧",
        "original_name": "Original Series",
        "first_air_date": "2018-01-01",
    }
    async_candidate = deepcopy(sync_candidate)
    sync_detail = _tv_detail(20)
    async_detail = deepcopy(sync_detail)
    api.search.tv_shows.return_value = [sync_candidate]
    api.search.async_tv_shows.return_value = [async_candidate]
    api.tv.details.return_value = sync_detail
    api.tv.async_details.return_value = async_detail

    sync_result = api.match(
        "目标剧",
        MediaType.TV,
        year="2018",
        season_year="2024",
        season_number=3,
        group_seasons=group_seasons,
    )
    async_result = asyncio.run(
        api.async_match(
            "目标剧",
            MediaType.TV,
            year="2018",
            season_year="2024",
            season_number=3,
            group_seasons=group_seasons,
        )
    )

    assert sync_result == async_result
    assert sync_result["id"] == 20
    assert api.search.tv_shows.call_args_list == [call(term="目标剧")]
    assert api.search.async_tv_shows.call_args_list == [call(term="目标剧")]
    api.tv.details.assert_called_once()
    api.tv.async_details.assert_awaited_once()


def test_match_multi_sync_async_share_sorting_and_alias_type_lookup() -> None:
    """多类型别名匹配双入口应跳过无关类型并按候选类型查详情。"""
    api = _api()
    sync_multis = [
        {"id": 1, "media_type": "person", "name": "目标别名"},
        {
            "id": 21,
            "media_type": "tv",
            "name": "展示剧名",
            "original_name": "Original Series",
            "first_air_date": "2024-01-01",
        },
    ]
    async_multis = deepcopy(sync_multis)
    sync_detail = _tv_detail(21, name="展示剧名")
    sync_detail["alternative_titles"]["results"][0]["title"] = "目标别名"
    async_detail = deepcopy(sync_detail)
    api.search.multi.return_value = sync_multis
    api.search.async_multi.return_value = async_multis
    api.tv.details.return_value = sync_detail
    api.tv.async_details.return_value = async_detail

    sync_result = api.match_multi("目标别名")
    async_result = asyncio.run(api.async_match_multi("目标别名"))

    assert sync_result == async_result
    assert sync_result["id"] == 21
    assert sync_result["media_type"] == MediaType.TV
    api.movie.details.assert_not_called()
    api.movie.async_details.assert_not_awaited()
    api.tv.details.assert_called_once()
    api.tv.async_details.assert_awaited_once()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [([], {}), (TMDbException("查询失败"), None)],
)
def test_match_multi_sync_async_share_empty_and_exception_results(
    outcome: Union[list, Exception], expected: Optional[dict]
) -> None:
    """多类型搜索的空结果和异常在双入口应保留相同三态语义。"""
    api = _api()
    if isinstance(outcome, Exception):
        api.search.multi.side_effect = outcome
        api.search.async_multi.side_effect = outcome
    else:
        api.search.multi.return_value = outcome
        api.search.async_multi.return_value = deepcopy(outcome)

    assert api.match_multi("目标") == expected
    assert asyncio.run(api.async_match_multi("目标")) == expected


def test_get_info_sync_async_share_projection() -> None:
    """详情双入口应共享类型、别名、分级和多语言字段投影。"""
    api = _api()
    sync_detail = _movie_detail(30)
    async_detail = deepcopy(sync_detail)
    api.movie.details.return_value = sync_detail
    api.movie.async_details.return_value = async_detail

    sync_result = api.get_info(MediaType.MOVIE, 30)
    async_result = asyncio.run(api.async_get_info(MediaType.MOVIE, 30))

    assert sync_result == async_result
    assert sync_result["media_type"] == MediaType.MOVIE
    assert sync_result["genre_ids"] == [18, 35]
    assert sync_result["content_rating"] == "PG-13"
    assert sync_result["en_title"] == "Original Title"
    assert "目标别名" in sync_result["names"]


@pytest.mark.parametrize(
    ("tv_exists", "movie_exists", "expected_type"),
    [
        (True, False, MediaType.TV),
        (False, True, MediaType.MOVIE),
        (True, True, None),
        (False, False, None),
    ],
)
def test_get_info_unknown_type_sync_async_share_lookup_order(
    tv_exists: bool,
    movie_exists: bool,
    expected_type: Optional[MediaType],
) -> None:
    """未知类型详情双入口应先查剧集再查电影并统一判断歧义。"""
    api = _api()
    sync_calls = []
    async_calls = []

    def sync_tv(*args, **kwargs):
        sync_calls.append("tv")
        return _tv_detail(40) if tv_exists else {}

    def sync_movie(*args, **kwargs):
        sync_calls.append("movie")
        return _movie_detail(40) if movie_exists else {}

    async def async_tv(*args, **kwargs):
        async_calls.append("tv")
        return _tv_detail(40) if tv_exists else {}

    async def async_movie(*args, **kwargs):
        async_calls.append("movie")
        return _movie_detail(40) if movie_exists else {}

    api.tv.details.side_effect = sync_tv
    api.movie.details.side_effect = sync_movie
    api.tv.async_details.side_effect = async_tv
    api.movie.async_details.side_effect = async_movie

    sync_result = api.get_info(None, 40)
    async_result = asyncio.run(api.async_get_info(None, 40))

    assert sync_calls == async_calls == ["tv", "movie"]
    assert (sync_result or {}).get("media_type") == expected_type
    assert (async_result or {}).get("media_type") == expected_type
    assert bool(sync_result) == bool(async_result)


def test_get_info_sync_async_propagate_connection_error_at_same_boundary() -> None:
    """显式连接错误传播开关在同步与异步详情入口应保持一致。"""
    api = _api()
    error = TMDbConnectionError("无法连接 TMDB")
    api.movie.details.side_effect = error
    api.movie.async_details.side_effect = error

    with pytest.raises(TMDbConnectionError):
        api.get_info(MediaType.MOVIE, 50, raise_on_connection_error=True)
    with pytest.raises(TMDbConnectionError):
        asyncio.run(api.async_get_info(MediaType.MOVIE, 50, raise_on_connection_error=True))
