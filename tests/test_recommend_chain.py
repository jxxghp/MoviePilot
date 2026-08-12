import asyncio
from typing import Generator
from unittest.mock import AsyncMock, patch

import pytest

from app.chain.recommend import RecommendChain
from app.core.cache import TTLCache
from app.core.context import MusicInfo
from app.schemas.types import MUSIC_ENTITY_ALBUM

SYNC_EMPTY_CACHE_CASES = [
    ("tmdb_movies", "app.chain.recommend.TmdbChain", "tmdb_discover"),
    ("tmdb_tvs", "app.chain.recommend.TmdbChain", "tmdb_discover"),
    ("tmdb_trending", "app.chain.recommend.TmdbChain", "tmdb_trending"),
    ("bangumi_calendar", "app.chain.recommend.BangumiChain", "calendar"),
    ("douban_movie_showing", "app.chain.recommend.DoubanChain", "movie_showing"),
    ("douban_movies", "app.chain.recommend.DoubanChain", "douban_discover"),
    ("douban_tvs", "app.chain.recommend.DoubanChain", "douban_discover"),
    ("douban_movie_top250", "app.chain.recommend.DoubanChain", "movie_top250"),
    ("douban_tv_weekly_chinese", "app.chain.recommend.DoubanChain", "tv_weekly_chinese"),
    ("douban_tv_weekly_global", "app.chain.recommend.DoubanChain", "tv_weekly_global"),
    ("douban_tv_animation", "app.chain.recommend.DoubanChain", "tv_animation"),
    ("douban_movie_hot", "app.chain.recommend.DoubanChain", "movie_hot"),
    ("douban_tv_hot", "app.chain.recommend.DoubanChain", "tv_hot"),
]

ASYNC_EMPTY_CACHE_CASES = [
    ("async_tmdb_movies", "app.chain.recommend.TmdbChain"),
    ("async_tmdb_tvs", "app.chain.recommend.TmdbChain"),
    ("async_tmdb_trending", "app.chain.recommend.TmdbChain"),
    ("async_bangumi_calendar", "app.chain.recommend.BangumiChain"),
    ("async_douban_movie_showing", "app.chain.recommend.DoubanChain"),
    ("async_douban_movies", "app.chain.recommend.DoubanChain"),
    ("async_douban_tvs", "app.chain.recommend.DoubanChain"),
    ("async_douban_movie_top250", "app.chain.recommend.DoubanChain"),
    ("async_douban_tv_weekly_chinese", "app.chain.recommend.DoubanChain"),
    ("async_douban_tv_weekly_global", "app.chain.recommend.DoubanChain"),
    ("async_douban_tv_animation", "app.chain.recommend.DoubanChain"),
    ("async_douban_movie_hot", "app.chain.recommend.DoubanChain"),
    ("async_douban_tv_hot", "app.chain.recommend.DoubanChain"),
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


@pytest.mark.parametrize(("method_name", "chain_target"), ASYNC_EMPTY_CACHE_CASES)
def test_async_recommend_methods_do_not_cache_empty_result(
    method_name: str,
    chain_target: str,
) -> None:
    """异步推荐来源返回空列表时不应缓存。"""
    chain = RecommendChain()
    recommend_method = getattr(chain, method_name)

    with patch(chain_target) as backend_chain:
        backend_chain.return_value.async_run_module = AsyncMock(side_effect=[[], []])

        assert asyncio.run(recommend_method(page=1)) == []
        assert asyncio.run(recommend_method(page=1)) == []

    assert backend_chain.return_value.async_run_module.call_count == 2


def test_music_weekly_uses_music_chart():
    """同步推荐缓存应从本周音乐榜单生成通用媒体字典。"""
    chain = RecommendChain()
    with patch("app.chain.recommend.MusicChain") as music_chain:
        music_chain.return_value.chart.return_value = [
            MusicInfo(media_source="musicbrainz", media_id="recording-1", title="晴天")
        ]

        result = chain.music_weekly(page=2, count=10)

    assert result[0]["media_id"] == "recording-1"
    music_chain.return_value.chart.assert_called_once_with(
        range_name="this_week",
        page=2,
        count=10,
    )


def test_async_music_weekly_uses_music_chart():
    """异步推荐接口应从本周音乐榜单返回统一媒体字典。"""
    chain = RecommendChain()
    with patch("app.chain.recommend.MusicChain") as music_chain:
        music_chain.return_value.async_chart = AsyncMock(
            return_value=[
                MusicInfo(media_source="musicbrainz", media_id="recording-1", title="晴天")
            ]
        )

        result = asyncio.run(chain.async_music_weekly(page=1, count=30))

    assert result[0]["type"] == "音乐"
    music_chain.return_value.async_chart.assert_awaited_once_with(
        range_name="this_week",
        page=1,
        count=30,
    )


def test_music_douban_recommendations_use_discover():
    """豆瓣音乐推荐入口应保留来源与实体，并输出统一媒体字典。"""
    chain = RecommendChain()
    with patch("app.chain.recommend.MusicChain") as music_chain:
        music_chain.return_value.discover.return_value = [
            MusicInfo(
                media_source="doubanmusic",
                media_id="music-1",
                music_type=MUSIC_ENTITY_ALBUM,
                title="Music",
            )
        ]

        result = chain.music_douban(page=2, count=10)

    assert result[0]["media_source"] == "doubanmusic"
    music_chain.return_value.discover.assert_called_once_with(
        media_source="doubanmusic",
        page=2,
        count=10,
        entity=MUSIC_ENTITY_ALBUM,
    )


def test_async_music_douban_recommendations_use_discover():
    """异步豆瓣音乐推荐入口应调用统一发现链并保留来源。"""
    chain = RecommendChain()
    with patch("app.chain.recommend.MusicChain") as music_chain:
        music_chain.return_value.async_discover = AsyncMock(
            return_value=[
                MusicInfo(media_source="doubanmusic", media_id="music-1", title="Music")
            ]
        )

        result = asyncio.run(chain.async_music_douban(page=1, count=30))

    assert result[0]["media_source"] == "doubanmusic"
    music_chain.return_value.async_discover.assert_awaited_once_with(
        media_source="doubanmusic",
        page=1,
        count=30,
        entity=MUSIC_ENTITY_ALBUM,
    )
