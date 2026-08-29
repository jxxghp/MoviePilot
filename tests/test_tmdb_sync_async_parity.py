"""TMDB 模块同步与异步公开能力的业务决策一致性测试。"""

import asyncio
from unittest.mock import AsyncMock, Mock, call

from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.tmdbv3api.exceptions import TMDbConnectionError
from app.schemas.types import MediaSource, MediaType


def _module_with_clients() -> TheMovieDbModule:
    """构造不触发模块生命周期、只注入测试客户端的 TMDB 模块。"""
    module = object.__new__(TheMovieDbModule)
    module.tmdb = Mock()
    module.cache = Mock()
    module.category = Mock()
    return module


def _movie_info(tmdbid: int = 10) -> dict:
    """构造可投影为 MediaInfo 的最小电影详情。"""
    return {
        "id": tmdbid,
        "media_type": MediaType.MOVIE,
        "title": "测试电影",
        "release_date": "2024-02-03",
        "genres": [{"id": 18, "name": "剧情"}],
    }


def test_recognize_sync_async_share_identity_cache_and_result_decisions() -> None:
    """显式 TMDB 身份在双入口应产生相同查询、缓存写入和媒体结果。"""
    module = _module_with_clients()
    info = _movie_info()
    module.tmdb.get_info.return_value = info
    module.tmdb.async_get_info = AsyncMock(return_value=info)
    module.category.get_movie_category.return_value = "剧情片"
    sync_meta = MetaBase("测试电影 2024")
    async_meta = MetaBase("测试电影 2024")

    sync_result = module.recognize_media(
        meta=sync_meta,
        mtype=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="10",
        cache=False,
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=async_meta,
            mtype=MediaType.MOVIE,
            media_source=MediaSource.TMDB,
            media_id="10",
            cache=False,
        )
    )

    assert sync_result is not None
    assert async_result is not None
    assert (
        sync_result.media_source,
        sync_result.media_id,
        sync_result.title,
        sync_result.year,
        sync_result.category,
        sync_result.recognize_cache_hit,
    ) == (
        async_result.media_source,
        async_result.media_id,
        async_result.title,
        async_result.year,
        async_result.category,
        async_result.recognize_cache_hit,
    )
    module.tmdb.get_info.assert_called_once_with(
        mtype=MediaType.MOVIE, tmdbid=10, raise_on_connection_error=True
    )
    module.tmdb.async_get_info.assert_awaited_once_with(
        mtype=MediaType.MOVIE, tmdbid=10, raise_on_connection_error=True
    )
    assert sync_meta.media_source == async_meta.media_source == MediaSource.TMDB
    assert sync_meta.media_id == async_meta.media_id == "10"
    assert module.cache.update.call_args_list == [
        call(sync_meta, info),
        call(async_meta, info),
    ]


def test_recognize_sync_async_share_negative_cache_short_circuit() -> None:
    """负缓存命中时双入口都不得继续访问 TMDB 客户端。"""
    module = _module_with_clients()
    module.tmdb.async_get_info = AsyncMock()
    module.tmdb.async_match = AsyncMock()
    module.cache.get.side_effect = [
        {"title": None},
        {"title": None},
    ]
    sync_meta = MetaBase("负缓存电影 2026")
    async_meta = MetaBase("负缓存电影 2026")
    sync_meta.name = "负缓存电影"
    async_meta.name = "负缓存电影"

    sync_result = module.recognize_media(
        meta=sync_meta, media_source=MediaSource.TMDB, cache=True
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=async_meta, media_source=MediaSource.TMDB, cache=True
        )
    )

    assert sync_result is None
    assert async_result is None
    assert module.cache.get.call_args_list == [call(sync_meta), call(async_meta)]
    module.tmdb.get_info.assert_not_called()
    module.tmdb.async_get_info.assert_not_awaited()
    module.tmdb.match.assert_not_called()
    module.tmdb.async_match.assert_not_awaited()
    module.cache.update.assert_not_called()


def test_recognize_sync_async_share_identity_connection_failure() -> None:
    """显式身份查询连接失败时双入口都应失败且不得写入负缓存。"""
    module = _module_with_clients()
    sync_lookup = Mock(side_effect=TMDbConnectionError("offline"))
    async_lookup = AsyncMock(side_effect=TMDbConnectionError("offline"))
    module._get_info_by_tmdbid = sync_lookup
    module._async_get_info_by_tmdbid = async_lookup
    sync_meta = MetaBase("连接失败电影 2026")
    async_meta = MetaBase("连接失败电影 2026")

    sync_result = module.recognize_media(
        meta=sync_meta,
        mtype=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="10",
        cache=False,
    )
    async_result = asyncio.run(
        module.async_recognize_media(
            meta=async_meta,
            mtype=MediaType.MOVIE,
            media_source=MediaSource.TMDB,
            media_id="10",
            cache=False,
        )
    )

    assert sync_result is None
    assert async_result is None
    sync_lookup.assert_called_once_with(
        tmdbid=10, mtype=MediaType.MOVIE, meta=sync_meta
    )
    async_lookup.assert_awaited_once_with(
        tmdbid=10, mtype=MediaType.MOVIE, meta=async_meta
    )
    module.cache.update.assert_not_called()


def test_name_match_sync_async_share_ordered_fallback_plan() -> None:
    """电视剧名称识别的带季查询与无年份回退在双入口应完全同序。"""
    module = _module_with_clients()
    meta = MetaBase("测试剧 S02 2024")
    meta.name = "测试剧"
    meta.type = MediaType.TV
    meta.year = "2024"
    meta.begin_season = 2
    result = {"id": 20}
    module.tmdb.match.side_effect = [None, result]
    module.tmdb.async_match = AsyncMock(side_effect=[None, result])
    group_seasons = [{"order": 2, "episodes": []}]

    sync_result = module._search_by_name(meta.name, meta, group_seasons)
    async_result = asyncio.run(
        module._async_search_by_name(meta.name, meta, group_seasons)
    )

    assert sync_result == async_result == result
    expected_calls = [
        call(
            name="测试剧",
            year="2024",
            mtype=MediaType.TV,
            season_year="2024",
            season_number=2,
            group_seasons=group_seasons,
        ),
        call(name="测试剧", mtype=MediaType.TV),
    ]
    assert module.tmdb.match.call_args_list == expected_calls
    assert module.tmdb.async_match.call_args_list == expected_calls


def test_media_search_sync_async_share_combination_and_sorting() -> None:
    """未知类型且有年份时，双入口应合并电影和电视剧并按日期倒序。"""
    module = _module_with_clients()
    meta = MetaBase("同名作品 2024")
    meta.name = "同名作品"
    meta.type = MediaType.UNKNOWN
    meta.year = "2024"
    movie = _movie_info(30)
    tv = {
        "id": 31,
        "media_type": MediaType.TV,
        "name": "同名作品",
        "first_air_date": "2024-08-01",
    }
    module.tmdb.search_movies.return_value = [movie]
    module.tmdb.search_tvs.return_value = [tv]
    module.tmdb.async_search_movies = AsyncMock(return_value=[movie])
    module.tmdb.async_search_tvs = AsyncMock(return_value=[tv])

    sync_result = module.search_medias(meta, media_source=MediaSource.TMDB)
    async_result = asyncio.run(
        module.async_search_medias(meta, media_source=MediaSource.TMDB)
    )

    assert sync_result is not None
    assert async_result is not None
    assert [item.tmdb_id for item in sync_result] == [31, 30]
    assert [item.tmdb_id for item in async_result] == [31, 30]
    module.tmdb.search_movies.assert_called_once_with("同名作品", "2024")
    module.tmdb.search_tvs.assert_called_once_with("同名作品", "2024")
    module.tmdb.async_search_movies.assert_awaited_once_with("同名作品", "2024")
    module.tmdb.async_search_tvs.assert_awaited_once_with("同名作品", "2024")


def test_obtain_images_sync_async_share_query_and_mapping() -> None:
    """图片补全双入口应选择同一接口、语言参数和最佳图片映射。"""
    module = _module_with_clients()
    images = {
        "posters": [{"file_path": "/poster.jpg", "vote_average": 8}],
        "backdrops": [{"file_path": "/backdrop.jpg", "vote_average": 7}],
        "logos": [{"file_path": "/logo.png", "vote_average": 6}],
    }
    module.tmdb.get_movie_images.return_value = images
    module.tmdb.async_get_movie_images = AsyncMock(return_value=images)
    sync_media = MediaInfo(tmdb_info=_movie_info(40))
    async_media = MediaInfo(tmdb_info=_movie_info(40))
    sync_media.original_language = "ja"
    async_media.original_language = "ja"

    sync_result = module.obtain_images(sync_media)
    async_result = asyncio.run(module.async_obtain_images(async_media))

    assert sync_result is sync_media
    assert async_result is async_media
    assert sync_media.poster_path == async_media.poster_path
    assert sync_media.backdrop_path == async_media.backdrop_path
    assert sync_media.logo_path == async_media.logo_path
    module.tmdb.get_movie_images.assert_called_once_with(40, original_language="ja")
    module.tmdb.async_get_movie_images.assert_awaited_once_with(
        40, original_language="ja"
    )
