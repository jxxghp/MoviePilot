"""discover 与 discover_board 契约的归一行为测试。

discover 覆盖 TMDB、豆瓣、Bangumi、AniList 四个提供者的条件发现：非本来源让出，筛选条件（criteria）
原样转发给各自的原方法，契约层不为任何条件补默认值，本源必填条件缺失时由被委托的原方法自身抛出异常。

discover_board 覆盖四源的榜单查询：模块内以模块级常量登记标识到方法名的映射表，discover_board 先查
表校验白名单，命中后才 getattr 调用对应方法；未登记标识让出返回 None 而不抛错。各源只下传自己认得的
分页参数，其余就地丢弃（Bangumi 的 calendar 不接受任何分页参数，TMDB 的 trending 只接受 page）。
"""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.anilist import AniListModule
from app.modules.bangumi import BangumiModule
from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule
from app.runtime.extensions.contract.module_method import (
    get_module_method_contract,
    get_multi_source_contract,
)
from app.schemas.types import MediaSource, MediaType


# ===========================================================================
# discover：筛选条件原样转发，不补默认值
# ===========================================================================

def test_tmdb_discover_forwards_criteria_exactly():
    """TMDB 发现契约必须把 criteria 原样转发给 tmdb_discover，不额外增删任何键。"""
    module = TheMovieDbModule()
    module.tmdb_discover = Mock(return_value=[{"id": 1}])
    criteria = dict(
        mtype=MediaType.MOVIE, sort_by="popularity.desc", with_genres="",
        with_original_language="", with_keywords="", with_watch_providers="",
        vote_average=0, vote_count=0, release_date="", page=2,
    )

    result = module.discover(source=MediaSource.TMDB, **criteria)

    assert result == [{"id": 1}]
    module.tmdb_discover.assert_called_once_with(**criteria)
    _, kwargs = module.tmdb_discover.call_args
    assert set(kwargs.keys()) == set(criteria.keys())


def test_async_tmdb_discover_forwards_criteria_and_raise_exception():
    """异步 TMDB 发现契约把 criteria 连同 raise_exception 原样转发给 async_tmdb_discover。"""
    module = TheMovieDbModule()
    module.async_tmdb_discover = AsyncMock(return_value=[{"id": 1}])
    criteria = dict(
        mtype=MediaType.TV, sort_by="popularity.desc", with_genres="",
        with_original_language="", with_keywords="", with_watch_providers="",
        vote_average=0, vote_count=0, release_date="", page=1,
        raise_exception=True,
    )

    result = asyncio.run(module.async_discover(source=MediaSource.TMDB, **criteria))

    assert result == [{"id": 1}]
    module.async_tmdb_discover.assert_called_once_with(**criteria)


def test_tmdb_discover_raises_when_required_criteria_missing():
    """必填条件缺失时必须由 tmdb_discover 自身抛出异常，契约层不得补默认值静默成功。"""
    module = TheMovieDbModule()

    with pytest.raises(TypeError):
        module.discover(source=MediaSource.TMDB)


def test_douban_discover_forwards_criteria_exactly():
    """豆瓣发现契约必须把 criteria 原样转发给 douban_discover，不额外增删任何键。"""
    module = DoubanModule()
    module.douban_discover = Mock(return_value=[{"id": 2}])
    criteria = dict(mtype=MediaType.MOVIE, sort="U", tags="", page=1, count=30)

    result = module.discover(source=MediaSource.Douban, **criteria)

    assert result == [{"id": 2}]
    module.douban_discover.assert_called_once_with(**criteria)
    _, kwargs = module.douban_discover.call_args
    assert set(kwargs.keys()) == set(criteria.keys())


def test_async_douban_discover_forwards_criteria_exactly():
    module = DoubanModule()
    module.async_douban_discover = AsyncMock(return_value=[{"id": 2}])
    criteria = dict(mtype=MediaType.TV, sort="T", tags="热门", page=2, count=20)

    result = asyncio.run(module.async_discover(source=MediaSource.Douban, **criteria))

    assert result == [{"id": 2}]
    module.async_douban_discover.assert_called_once_with(**criteria)


def test_bangumi_discover_forwards_criteria_exactly():
    """Bangumi 发现契约必须把任意 criteria 原样转发，不补充也不丢弃任何键。"""
    module = BangumiModule()
    module.bangumi_discover = Mock(return_value=[{"id": 3}])
    criteria = dict(sort="rank", type=2, page=1)

    result = module.discover(source=MediaSource.Bangumi, **criteria)

    assert result == [{"id": 3}]
    module.bangumi_discover.assert_called_once_with(**criteria)
    _, kwargs = module.bangumi_discover.call_args
    assert set(kwargs.keys()) == set(criteria.keys())


def test_async_bangumi_discover_forwards_criteria_exactly():
    module = BangumiModule()
    module.async_bangumi_discover = AsyncMock(return_value=[{"id": 3}])
    criteria = dict(sort="trends", type=1)

    result = asyncio.run(module.async_discover(source=MediaSource.Bangumi, **criteria))

    assert result == [{"id": 3}]
    module.async_bangumi_discover.assert_called_once_with(**criteria)


def test_anilist_discover_forwards_criteria_exactly():
    module = AniListModule()
    module.anilist_discover = Mock(return_value=[{"id": 4}])
    criteria = dict(genre="Action", season="WINTER", seasonYear=2024)

    result = module.discover(source=MediaSource.AniList, **criteria)

    assert result == [{"id": 4}]
    module.anilist_discover.assert_called_once_with(**criteria)
    _, kwargs = module.anilist_discover.call_args
    assert set(kwargs.keys()) == set(criteria.keys())


def test_async_anilist_discover_forwards_criteria_exactly():
    module = AniListModule()
    module.async_anilist_discover = AsyncMock(return_value=[{"id": 4}])
    criteria = dict(genre="Comedy")

    result = asyncio.run(module.async_discover(source=MediaSource.AniList, **criteria))

    assert result == [{"id": 4}]
    module.async_anilist_discover.assert_called_once_with(**criteria)


# ---------------------------------------------------------------------------
# 非本来源必须返回 None（is None，不是 falsy 判断）
# ---------------------------------------------------------------------------

def test_tmdb_discover_returns_none_for_other_source():
    module = TheMovieDbModule()
    module.tmdb_discover = Mock(return_value=[{"id": 1}])

    result = module.discover(source=MediaSource.Douban, mtype=MediaType.MOVIE)

    assert result is None
    module.tmdb_discover.assert_not_called()


def test_douban_discover_returns_none_for_other_source():
    module = DoubanModule()
    module.douban_discover = Mock(return_value=[{"id": 2}])

    result = module.discover(source=MediaSource.TMDB, mtype=MediaType.MOVIE)

    assert result is None
    module.douban_discover.assert_not_called()


def test_bangumi_discover_returns_none_for_other_source():
    module = BangumiModule()
    module.bangumi_discover = Mock(return_value=[{"id": 3}])

    result = module.discover(source=MediaSource.TMDB)

    assert result is None
    module.bangumi_discover.assert_not_called()


def test_anilist_discover_returns_none_for_other_source():
    module = AniListModule()
    module.anilist_discover = Mock(return_value=[{"id": 4}])

    result = module.discover(source=MediaSource.TMDB)

    assert result is None
    module.anilist_discover.assert_not_called()


def test_tmdb_discover_returns_none_when_source_missing():
    module = TheMovieDbModule()
    module.tmdb_discover = Mock(return_value=[{"id": 1}])

    assert module.discover() is None
    module.tmdb_discover.assert_not_called()


# ---------------------------------------------------------------------------
# 别名字符串来源能命中
# ---------------------------------------------------------------------------

def test_tmdb_discover_accepts_alias_string_source():
    module = TheMovieDbModule()
    module.tmdb_discover = Mock(return_value=[{"id": 1}])

    result = module.discover(source="tmdb", mtype=MediaType.MOVIE)

    assert result == [{"id": 1}]
    module.tmdb_discover.assert_called_once_with(mtype=MediaType.MOVIE)


def test_douban_discover_accepts_alias_string_source():
    module = DoubanModule()
    module.douban_discover = Mock(return_value=[{"id": 2}])

    result = module.discover(source="douban", mtype=MediaType.MOVIE)

    assert result == [{"id": 2}]
    module.douban_discover.assert_called_once_with(mtype=MediaType.MOVIE)


def test_bangumi_discover_accepts_alias_string_source():
    module = BangumiModule()
    module.bangumi_discover = Mock(return_value=[{"id": 3}])

    result = module.discover(source="bangumi", sort="rank")

    assert result == [{"id": 3}]
    module.bangumi_discover.assert_called_once_with(sort="rank")


def test_anilist_discover_accepts_alias_string_source():
    module = AniListModule()
    module.anilist_discover = Mock(return_value=[{"id": 4}])

    result = module.discover(source="anilist", genre="Action")

    assert result == [{"id": 4}]
    module.anilist_discover.assert_called_once_with(genre="Action")


# ---------------------------------------------------------------------------
# 契约是委托而非重新实现
# ---------------------------------------------------------------------------

def test_tmdb_discover_delegates_instead_of_reimplementing():
    module = TheMovieDbModule()
    module.tmdb_discover = Mock(return_value=[{"id": 99}])

    result = module.discover(source=MediaSource.TMDB, mtype=MediaType.MOVIE)

    assert result == [{"id": 99}]
    module.tmdb_discover.assert_called_once()


def test_douban_discover_delegates_instead_of_reimplementing():
    module = DoubanModule()
    module.douban_discover = Mock(return_value=[{"id": 98}])

    result = module.discover(source=MediaSource.Douban, mtype=MediaType.MOVIE)

    assert result == [{"id": 98}]
    module.douban_discover.assert_called_once()


def test_bangumi_discover_delegates_instead_of_reimplementing():
    module = BangumiModule()
    module.bangumi_discover = Mock(return_value=[{"id": 97}])

    result = module.discover(source=MediaSource.Bangumi)

    assert result == [{"id": 97}]
    module.bangumi_discover.assert_called_once()


def test_anilist_discover_delegates_instead_of_reimplementing():
    module = AniListModule()
    module.anilist_discover = Mock(return_value=[{"id": 96}])

    result = module.discover(source=MediaSource.AniList)

    assert result == [{"id": 96}]
    module.anilist_discover.assert_called_once()


# ---------------------------------------------------------------------------
# discover 能力契约登记
# ---------------------------------------------------------------------------

def test_discover_is_registered_as_media_discovery_family():
    assert get_module_method_contract("discover").family == "media-discovery"
    assert get_module_method_contract("async_discover").family == "media-discovery"


def test_discover_declares_its_multi_source_protocol():
    contract = get_multi_source_contract("discover")

    assert contract is not None
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"source"}
    assert "首个非空答案" in contract.arbitration


# ===========================================================================
# discover_board：先查白名单再 getattr，未登记标识让出而不抛错
# ===========================================================================

def test_discover_board_is_registered_as_media_discovery_family():
    assert get_module_method_contract("discover_board").family == "media-discovery"
    assert get_module_method_contract("async_discover_board").family == "media-discovery"


def test_discover_board_declares_its_multi_source_protocol():
    contract = get_multi_source_contract("discover_board")

    assert contract is not None
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"source", "board"}
    assert "首个非空答案" in contract.arbitration


# ---------------------------------------------------------------------------
# 白名单校验必须先于 getattr：真实存在的危险方法名不得被触达
# ---------------------------------------------------------------------------

def test_tmdb_discover_board_whitelist_precedes_getattr():
    module = TheMovieDbModule()
    module.clear_cache = Mock()

    result = module.discover_board(source=MediaSource.TMDB, board="clear_cache")

    assert result is None
    module.clear_cache.assert_not_called()


def test_douban_discover_board_whitelist_precedes_getattr():
    module = DoubanModule()
    module.clear_cache = Mock()

    result = module.discover_board(source=MediaSource.Douban, board="clear_cache")

    assert result is None
    module.clear_cache.assert_not_called()


def test_bangumi_discover_board_whitelist_precedes_getattr():
    module = BangumiModule()
    module.clear_cache = Mock()

    result = module.discover_board(source=MediaSource.Bangumi, board="clear_cache")

    assert result is None
    module.clear_cache.assert_not_called()


def test_anilist_discover_board_whitelist_precedes_getattr():
    module = AniListModule()
    module.clear_cache = Mock()

    result = module.discover_board(source=MediaSource.AniList, board="clear_cache")

    assert result is None
    module.clear_cache.assert_not_called()


def test_unregistered_board_returns_none_without_raising():
    """未登记榜单标识必须让出返回 None，而不是抛错——榜单本就该先枚举再取。"""
    module = DoubanModule()
    module.movie_showing = Mock(return_value=[{"id": 1}])

    result = module.discover_board(source=MediaSource.Douban, board="no_such_board")

    assert result is None
    module.movie_showing.assert_not_called()


def test_async_unregistered_board_returns_none_without_raising():
    module = DoubanModule()
    module.async_movie_showing = AsyncMock(return_value=[{"id": 1}])

    result = asyncio.run(
        module.async_discover_board(source=MediaSource.Douban, board="no_such_board")
    )

    assert result is None
    module.async_movie_showing.assert_not_called()


# ---------------------------------------------------------------------------
# Bangumi：calendar 不下传 page/count
# ---------------------------------------------------------------------------

def test_bangumi_discover_board_calendar_does_not_forward_page_or_count():
    module = BangumiModule()
    module.bangumi_calendar = Mock(return_value=[{"id": 1}])

    result = module.discover_board(source=MediaSource.Bangumi, board="calendar", page=3, count=99)

    assert result == [{"id": 1}]
    module.bangumi_calendar.assert_called_once_with()


def test_async_bangumi_discover_board_calendar_does_not_forward_page_or_count():
    module = BangumiModule()
    module.async_bangumi_calendar = AsyncMock(return_value=[{"id": 1}])

    result = asyncio.run(
        module.async_discover_board(source=MediaSource.Bangumi, board="calendar", page=3, count=99)
    )

    assert result == [{"id": 1}]
    module.async_bangumi_calendar.assert_called_once_with()


def test_bangumi_discover_board_returns_none_for_other_source():
    module = BangumiModule()
    module.bangumi_calendar = Mock(return_value=[{"id": 1}])

    result = module.discover_board(source=MediaSource.TMDB, board="calendar")

    assert result is None
    module.bangumi_calendar.assert_not_called()


def test_bangumi_discover_board_delegates_instead_of_reimplementing():
    module = BangumiModule()
    module.bangumi_calendar = Mock(return_value=[{"id": 99}])

    result = module.discover_board(source=MediaSource.Bangumi, board="calendar")

    assert result == [{"id": 99}]
    module.bangumi_calendar.assert_called_once()


# ---------------------------------------------------------------------------
# TMDB：trending 只传 page，不含 count
# ---------------------------------------------------------------------------

def test_tmdb_discover_board_trending_does_not_forward_count():
    module = TheMovieDbModule()
    module.tmdb_trending = Mock(return_value=[{"id": 1}])

    result = module.discover_board(source=MediaSource.TMDB, board="trending", page=2, count=99)

    assert result == [{"id": 1}]
    module.tmdb_trending.assert_called_once_with(page=2)
    _, kwargs = module.tmdb_trending.call_args
    assert set(kwargs.keys()) == {"page"}


def test_async_tmdb_discover_board_trending_does_not_forward_count():
    """异步版本同样不下传 count；额外支持 raise_exception，因为 async_tmdb_trending 本身接受它。"""
    module = TheMovieDbModule()
    module.async_tmdb_trending = AsyncMock(return_value=[{"id": 1}])

    result = asyncio.run(
        module.async_discover_board(source=MediaSource.TMDB, board="trending", page=2, count=99)
    )

    assert result == [{"id": 1}]
    _, kwargs = module.async_tmdb_trending.call_args
    assert "count" not in kwargs
    assert kwargs == {"page": 2, "raise_exception": False}


def test_async_tmdb_discover_board_trending_forwards_raise_exception():
    module = TheMovieDbModule()
    module.async_tmdb_trending = AsyncMock(return_value=[{"id": 1}])

    asyncio.run(
        module.async_discover_board(
            source=MediaSource.TMDB, board="trending", page=1, raise_exception=True
        )
    )

    module.async_tmdb_trending.assert_called_once_with(page=1, raise_exception=True)


def test_tmdb_discover_board_returns_none_for_other_source():
    module = TheMovieDbModule()
    module.tmdb_trending = Mock(return_value=[{"id": 1}])

    result = module.discover_board(source=MediaSource.Douban, board="trending")

    assert result is None
    module.tmdb_trending.assert_not_called()


def test_tmdb_discover_board_accepts_alias_string_source():
    module = TheMovieDbModule()
    module.tmdb_trending = Mock(return_value=[{"id": 1}])

    result = module.discover_board(source="tmdb", board="trending", page=1)

    assert result == [{"id": 1}]
    module.tmdb_trending.assert_called_once_with(page=1)


def test_tmdb_discover_board_delegates_instead_of_reimplementing():
    module = TheMovieDbModule()
    module.tmdb_trending = Mock(return_value=[{"id": 99}])

    result = module.discover_board(source=MediaSource.TMDB, board="trending")

    assert result == [{"id": 99}]
    module.tmdb_trending.assert_called_once()


# ---------------------------------------------------------------------------
# AniList：榜单标识到方法名映射正确
# ---------------------------------------------------------------------------

def test_anilist_discover_board_trending_maps_to_anilist_trending():
    module = AniListModule()
    module.anilist_trending = Mock(return_value=[{"id": 1}])
    module.anilist_popular_this_season = Mock(return_value=[{"id": 999}])

    result = module.discover_board(source=MediaSource.AniList, board="trending", page=2, count=10)

    assert result == [{"id": 1}]
    module.anilist_trending.assert_called_once_with(page=2, count=10)
    module.anilist_popular_this_season.assert_not_called()


def test_anilist_discover_board_popular_this_season_maps_to_its_own_method():
    module = AniListModule()
    module.anilist_trending = Mock(return_value=[{"id": 999}])
    module.anilist_popular_this_season = Mock(return_value=[{"id": 1}])

    result = module.discover_board(
        source=MediaSource.AniList, board="popular_this_season", page=1, count=20
    )

    assert result == [{"id": 1}]
    module.anilist_popular_this_season.assert_called_once_with(page=1, count=20)
    module.anilist_trending.assert_not_called()


def test_async_anilist_discover_board_trending_maps_to_async_anilist_trending():
    module = AniListModule()
    module.async_anilist_trending = AsyncMock(return_value=[{"id": 1}])
    module.async_anilist_popular_this_season = AsyncMock(return_value=[{"id": 999}])

    result = asyncio.run(
        module.async_discover_board(source=MediaSource.AniList, board="trending", page=1, count=20)
    )

    assert result == [{"id": 1}]
    module.async_anilist_trending.assert_called_once_with(page=1, count=20)
    module.async_anilist_popular_this_season.assert_not_called()


def test_async_anilist_discover_board_popular_this_season_maps_to_its_own_async_method():
    module = AniListModule()
    module.async_anilist_trending = AsyncMock(return_value=[{"id": 999}])
    module.async_anilist_popular_this_season = AsyncMock(return_value=[{"id": 1}])

    result = asyncio.run(
        module.async_discover_board(
            source=MediaSource.AniList, board="popular_this_season", page=1, count=20
        )
    )

    assert result == [{"id": 1}]
    module.async_anilist_popular_this_season.assert_called_once_with(page=1, count=20)
    module.async_anilist_trending.assert_not_called()


def test_anilist_discover_board_returns_none_for_other_source():
    module = AniListModule()
    module.anilist_trending = Mock(return_value=[{"id": 1}])

    result = module.discover_board(source=MediaSource.TMDB, board="trending")

    assert result is None
    module.anilist_trending.assert_not_called()


def test_anilist_discover_board_delegates_instead_of_reimplementing():
    module = AniListModule()
    module.anilist_trending = Mock(return_value=[{"id": 99}])

    result = module.discover_board(source=MediaSource.AniList, board="trending")

    assert result == [{"id": 99}]
    module.anilist_trending.assert_called_once()


# ---------------------------------------------------------------------------
# 豆瓣：7 个榜单逐个断言委托正确且下传 page/count
# ---------------------------------------------------------------------------

_DOUBAN_BOARDS_AND_METHODS = (
    ("movie_showing", "movie_showing"),
    ("movie_hot", "movie_hot"),
    ("movie_top250", "movie_top250"),
    ("tv_hot", "tv_hot"),
    ("tv_animation", "tv_animation"),
    ("tv_weekly_chinese", "tv_weekly_chinese"),
    ("tv_weekly_global", "tv_weekly_global"),
)


def test_douban_discover_board_maps_each_board_to_its_own_method():
    """豆瓣 discover_board 必须按标识精确委托到同名方法，不串台到其它榜单。"""
    all_method_names = {name for _, name in _DOUBAN_BOARDS_AND_METHODS}
    for board, method_name in _DOUBAN_BOARDS_AND_METHODS:
        module = DoubanModule()
        for _, name in _DOUBAN_BOARDS_AND_METHODS:
            setattr(module, name, Mock(return_value=[{"board": name}]))

        result = module.discover_board(source=MediaSource.Douban, board=board, page=2, count=15)

        assert result == [{"board": method_name}]
        getattr(module, method_name).assert_called_once_with(page=2, count=15)
        for other_name in all_method_names - {method_name}:
            getattr(module, other_name).assert_not_called()


def test_async_douban_discover_board_maps_each_board_to_its_own_async_method():
    all_method_names = {name for _, name in _DOUBAN_BOARDS_AND_METHODS}
    for board, method_name in _DOUBAN_BOARDS_AND_METHODS:
        module = DoubanModule()
        for _, name in _DOUBAN_BOARDS_AND_METHODS:
            setattr(module, f"async_{name}", AsyncMock(return_value=[{"board": name}]))

        result = asyncio.run(
            module.async_discover_board(source=MediaSource.Douban, board=board, page=1, count=10)
        )

        assert result == [{"board": method_name}]
        getattr(module, f"async_{method_name}").assert_called_once_with(page=1, count=10)
        for other_name in all_method_names - {method_name}:
            getattr(module, f"async_{other_name}").assert_not_called()


def test_douban_discover_board_returns_none_for_other_source():
    module = DoubanModule()
    module.movie_showing = Mock(return_value=[{"id": 1}])

    result = module.discover_board(source=MediaSource.TMDB, board="movie_showing")

    assert result is None
    module.movie_showing.assert_not_called()


def test_douban_discover_board_accepts_alias_string_source():
    module = DoubanModule()
    module.movie_top250 = Mock(return_value=[{"id": 1}])

    result = module.discover_board(source="douban", board="movie_top250", page=1, count=30)

    assert result == [{"id": 1}]
    module.movie_top250.assert_called_once_with(page=1, count=30)


def test_douban_discover_board_delegates_instead_of_reimplementing():
    module = DoubanModule()
    module.movie_top250 = Mock(return_value=[{"id": 99}])

    result = module.discover_board(source=MediaSource.Douban, board="movie_top250")

    assert result == [{"id": 99}]
    module.movie_top250.assert_called_once()
