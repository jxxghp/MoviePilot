import asyncio
from typing import Generator
from unittest.mock import AsyncMock, patch

import pytest

from app.application.orchestration.recommend import RecommendChain
from app.runtime.cache import TTLCache
from app.domain.context import MusicInfo
from app.schemas.types import MUSIC_ENTITY_ALBUM

SYNC_EMPTY_CACHE_CASES = [
    ("tmdb_movies", "app.application.orchestration.recommend.TmdbChain", "tmdb_discover"),
    ("tmdb_tvs", "app.application.orchestration.recommend.TmdbChain", "tmdb_discover"),
    ("tmdb_trending", "app.application.orchestration.recommend.TmdbChain", "tmdb_trending"),
    ("bangumi_calendar", "app.application.orchestration.recommend.BangumiChain", "calendar"),
    ("douban_movie_showing", "app.application.orchestration.recommend.DoubanChain", "movie_showing"),
    ("douban_movies", "app.application.orchestration.recommend.DoubanChain", "douban_discover"),
    ("douban_tvs", "app.application.orchestration.recommend.DoubanChain", "douban_discover"),
    ("douban_movie_top250", "app.application.orchestration.recommend.DoubanChain", "movie_top250"),
    ("douban_tv_weekly_chinese", "app.application.orchestration.recommend.DoubanChain", "tv_weekly_chinese"),
    ("douban_tv_weekly_global", "app.application.orchestration.recommend.DoubanChain", "tv_weekly_global"),
    ("douban_tv_animation", "app.application.orchestration.recommend.DoubanChain", "tv_animation"),
    ("douban_movie_hot", "app.application.orchestration.recommend.DoubanChain", "movie_hot"),
    ("douban_tv_hot", "app.application.orchestration.recommend.DoubanChain", "tv_hot"),
]

ASYNC_EMPTY_CACHE_CASES = [
    ("async_tmdb_movies", "app.application.orchestration.recommend.TmdbChain", "async_tmdb_discover"),
    ("async_tmdb_tvs", "app.application.orchestration.recommend.TmdbChain", "async_tmdb_discover"),
    ("async_tmdb_trending", "app.application.orchestration.recommend.TmdbChain", "async_tmdb_trending"),
    ("async_bangumi_calendar", "app.application.orchestration.recommend.BangumiChain", "async_calendar"),
    ("async_douban_movie_showing", "app.application.orchestration.recommend.DoubanChain", "async_movie_showing"),
    ("async_douban_movies", "app.application.orchestration.recommend.DoubanChain", "async_douban_discover"),
    ("async_douban_tvs", "app.application.orchestration.recommend.DoubanChain", "async_douban_discover"),
    ("async_douban_movie_top250", "app.application.orchestration.recommend.DoubanChain", "async_movie_top250"),
    ("async_douban_tv_weekly_chinese", "app.application.orchestration.recommend.DoubanChain", "async_tv_weekly_chinese"),
    ("async_douban_tv_weekly_global", "app.application.orchestration.recommend.DoubanChain", "async_tv_weekly_global"),
    ("async_douban_tv_animation", "app.application.orchestration.recommend.DoubanChain", "async_tv_animation"),
    ("async_douban_movie_hot", "app.application.orchestration.recommend.DoubanChain", "async_movie_hot"),
    ("async_douban_tv_hot", "app.application.orchestration.recommend.DoubanChain", "async_tv_hot"),
]


def clear_recommend_cache() -> None:
    """清理推荐缓存，避免缓存装饰器状态影响用例。"""
    TTLCache(region=RecommendChain.recommend_cache_region).clear()


@pytest.fixture(autouse=True)
def isolated_recommend_cache() -> Generator[None, None, None]:
    """每个用例前后都清空推荐缓存。"""
    clear_recommend_cache()
    yield
    clear_recommend_cache()


@pytest.mark.parametrize(
    ("method_name", "chain_target", "backend_method"),
    SYNC_EMPTY_CACHE_CASES,
)
def test_sync_recommend_methods_do_not_cache_empty_result(
    method_name: str,
    chain_target: str,
    backend_method: str,
) -> None:
    """同步推荐来源返回空列表时不应缓存。"""
    chain = RecommendChain()
    recommend_method = getattr(chain, method_name)

    with patch(chain_target) as backend_chain:
        backend_call = getattr(backend_chain.return_value, backend_method)
        backend_call.side_effect = [[], []]

        assert recommend_method(page=1) == []
        assert recommend_method(page=1) == []

    assert backend_call.call_count == 2


@pytest.mark.parametrize(
    ("method_name", "chain_target", "backend_method"),
    ASYNC_EMPTY_CACHE_CASES,
)
def test_async_recommend_methods_do_not_cache_empty_result(
    method_name: str,
    chain_target: str,
    backend_method: str,
) -> None:
    """异步推荐来源返回空列表时不应缓存。"""
    chain = RecommendChain()
    recommend_method = getattr(chain, method_name)

    with patch(chain_target) as backend_chain:
        backend_call = AsyncMock(side_effect=[[], []])
        setattr(backend_chain.return_value, backend_method, backend_call)

        assert asyncio.run(recommend_method(page=1)) == []
        assert asyncio.run(recommend_method(page=1)) == []

    assert backend_call.call_count == 2


def test_music_weekly_uses_music_chart():
    """同步推荐缓存应从本周音乐榜单生成通用媒体字典。"""
    chain = RecommendChain()
    with patch("app.application.orchestration.recommend.ListenBrainzChain") as source_chain:
        source_chain.return_value.music_chart.return_value = [
            MusicInfo(media_source="musicbrainz", media_id="recording-1", title="晴天")
        ]

        result = chain.music_weekly(page=2, count=10)

    assert result[0]["media_id"] == "recording-1"
    source_chain.return_value.music_chart.assert_called_once_with(
        range_name="this_week",
        page=2,
        count=10,
        entity="recording",
    )


def test_async_music_weekly_uses_music_chart():
    """异步推荐接口应从本周音乐榜单返回统一媒体字典。"""
    chain = RecommendChain()
    with patch("app.application.orchestration.recommend.ListenBrainzChain") as source_chain:
        source_chain.return_value.async_music_chart = AsyncMock(
            return_value=[
                MusicInfo(media_source="musicbrainz", media_id="recording-1", title="晴天")
            ]
        )

        result = asyncio.run(chain.async_music_weekly(page=1, count=30))

    assert result[0]["type"] == "音乐"
    source_chain.return_value.async_music_chart.assert_awaited_once_with(
        range_name="this_week",
        page=1,
        count=30,
        entity="recording",
    )


def test_music_douban_recommendations_use_discover():
    """豆瓣音乐推荐入口应保留来源与实体，并输出统一媒体字典。"""
    chain = RecommendChain()
    with patch("app.application.orchestration.recommend.DoubanChain") as source_chain:
        source_chain.return_value.music_discover.return_value = [
            MusicInfo(
                media_source="doubanmusic",
                media_id="music-1",
                music_type=MUSIC_ENTITY_ALBUM,
                title="Music",
            )
        ]

        result = chain.music_douban(page=2, count=10)

    assert result[0]["media_source"] == "doubanmusic"
    source_chain.return_value.music_discover.assert_called_once_with(
        page=2,
        count=10,
        entity=MUSIC_ENTITY_ALBUM,
        mode="chart",
        tags="",
        sort="U",
    )


def test_async_music_douban_recommendations_use_discover():
    """异步豆瓣音乐推荐入口应调用统一发现链并保留来源。"""
    chain = RecommendChain()
    with patch("app.application.orchestration.recommend.DoubanChain") as source_chain:
        source_chain.return_value.async_music_discover = AsyncMock(
            return_value=[
                MusicInfo(media_source="doubanmusic", media_id="music-1", title="Music")
            ]
        )

        result = asyncio.run(chain.async_music_douban(page=1, count=30))

    assert result[0]["media_source"] == "doubanmusic"
    source_chain.return_value.async_music_discover.assert_awaited_once_with(
        page=1,
        count=30,
        entity=MUSIC_ENTITY_ALBUM,
        mode="chart",
        tags="",
        sort="U",
    )


def test_music_chart_applies_filter_and_sort() -> None:
    """音乐榜单应在 RecommendChain 统一执行热度、封面和排序约束。"""
    chain = RecommendChain()
    candidates = [
        MusicInfo(
            media_source="musicbrainz",
            media_id="low",
            title="Low",
            listen_count=10,
            cover_url="cover-low",
        ),
        MusicInfo(
            media_source="musicbrainz",
            media_id="high",
            title="High",
            listen_count=30,
            cover_url="cover-high",
        ),
        MusicInfo(
            media_source="musicbrainz",
            media_id="no-cover",
            title="No Cover",
            listen_count=40,
        ),
    ]
    with patch("app.application.orchestration.recommend.ListenBrainzChain") as source_chain:
        source_chain.return_value.music_chart.return_value = candidates

        result = chain.music_chart(
            range_name="this_month",
            page=1,
            count=10,
            sort_by="listen_count.desc",
            min_listen_count=20,
            with_cover=True,
        )

    assert [item.media_id for item in result] == ["high"]


def test_async_music_fresh_releases_uses_listenbrainz_source() -> None:
    """新发行推荐应委派 ListenBrainz 来源链并保留分页参数。"""
    chain = RecommendChain()
    with patch("app.application.orchestration.recommend.ListenBrainzChain") as source_chain:
        source_chain.return_value.async_music_fresh_releases = AsyncMock(
            return_value=[
                MusicInfo(
                    media_source="musicbrainz",
                    media_id="album-1",
                    music_type=MUSIC_ENTITY_ALBUM,
                    title="Album",
                )
            ]
        )

        result = asyncio.run(chain.async_music_fresh_releases(page=2, count=12))

    assert result[0].media_id == "album-1"
    source_chain.return_value.async_music_fresh_releases.assert_awaited_once_with(
        days=14,
        sort="release_date",
        past=True,
        future=True,
        page=2,
        count=12,
    )
