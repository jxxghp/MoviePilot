"""media_detail 契约的归一行为测试。

覆盖 TMDB、豆瓣、Bangumi、AniList、TVDB 五个提供者：非本来源让出、各源只下传自己
支持的参数、media_id 为空或无法转换为本来源ID类型时让出、契约方法委托原实现而非
重新实现。重点覆盖两个高风险点：豆瓣契约的 raise_exception 缺省必须是 False（而非
douban_info 自身的 True 缺省），以及 TVDB 没有原生异步实现、async_media_detail 必须
经 run_in_threadpool 包装同步方法。
"""
import asyncio
from unittest.mock import AsyncMock, Mock

import app.modules.thetvdb as thetvdb_module
from app.modules.anilist import AniListModule
from app.modules.bangumi import BangumiModule
from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule
from app.modules.thetvdb import TheTvDbModule
from app.runtime.extensions.contract.module_method import (
    get_module_method_contract,
    get_multi_source_contract,
)
from app.schemas.types import MediaSource, MediaType


# ---------------------------------------------------------------------------
# TMDB：mtype/season 下传，raise_exception 本源不支持须丢弃
# ---------------------------------------------------------------------------

def test_tmdb_media_detail_forwards_mtype_and_season_drops_raise_exception():
    module = TheMovieDbModule()
    module.tmdb_info = Mock(return_value={"id": 1})

    result = module.media_detail(
        source=MediaSource.TMDB, media_id=550, mtype=MediaType.MOVIE, season=2, raise_exception=True
    )

    assert result == {"id": 1}
    module.tmdb_info.assert_called_once_with(tmdbid=550, mtype=MediaType.MOVIE, season=2)
    _, kwargs = module.tmdb_info.call_args
    assert set(kwargs.keys()) == {"tmdbid", "mtype", "season"}


def test_async_tmdb_media_detail_forwards_mtype_and_season_drops_raise_exception():
    module = TheMovieDbModule()
    module.async_tmdb_info = AsyncMock(return_value={"id": 1})

    result = asyncio.run(
        module.async_media_detail(
            source=MediaSource.TMDB, media_id=550, mtype=MediaType.MOVIE, season=2, raise_exception=True
        )
    )

    assert result == {"id": 1}
    module.async_tmdb_info.assert_called_once_with(tmdbid=550, mtype=MediaType.MOVIE, season=2)
    _, kwargs = module.async_tmdb_info.call_args
    assert set(kwargs.keys()) == {"tmdbid", "mtype", "season"}


def test_tmdb_media_detail_returns_none_for_none_media_id():
    module = TheMovieDbModule()
    module.tmdb_info = Mock(return_value={"id": 1})

    assert module.media_detail(source=MediaSource.TMDB, media_id=None) is None
    module.tmdb_info.assert_not_called()


def test_tmdb_media_detail_returns_none_for_unconvertible_media_id():
    module = TheMovieDbModule()
    module.tmdb_info = Mock(return_value={"id": 1})

    assert module.media_detail(source=MediaSource.TMDB, media_id="abc") is None
    module.tmdb_info.assert_not_called()


def test_tmdb_media_detail_returns_none_for_other_source():
    module = TheMovieDbModule()
    module.tmdb_info = Mock(return_value={"id": 1})

    result = module.media_detail(source=MediaSource.Douban, media_id=550)

    assert result is None
    module.tmdb_info.assert_not_called()


def test_tmdb_media_detail_accepts_alias_string_source():
    module = TheMovieDbModule()
    module.tmdb_info = Mock(return_value={"id": 1})

    result = module.media_detail(source="tmdb", media_id=550, mtype=MediaType.MOVIE)

    assert result == {"id": 1}
    module.tmdb_info.assert_called_once_with(tmdbid=550, mtype=MediaType.MOVIE, season=None)


def test_tmdb_media_detail_delegates_instead_of_reimplementing():
    module = TheMovieDbModule()
    module.tmdb_info = Mock(return_value={"id": 99})

    result = module.media_detail(source=MediaSource.TMDB, media_id=550, mtype=MediaType.MOVIE)

    assert result == {"id": 99}
    module.tmdb_info.assert_called_once()


# ---------------------------------------------------------------------------
# 豆瓣：mtype 下传，season 本源不支持须丢弃；raise_exception 契约缺省 False，
# 与 douban_info 自身缺省 True 不同——这是本族最容易写反的地方
# ---------------------------------------------------------------------------

def test_douban_media_detail_forwards_mtype_drops_season():
    module = DoubanModule()
    module.douban_info = Mock(return_value={"id": 2})

    result = module.media_detail(
        source=MediaSource.Douban, media_id="123", mtype=MediaType.TV, season=3, raise_exception=True
    )

    assert result == {"id": 2}
    module.douban_info.assert_called_once_with(doubanid="123", mtype=MediaType.TV, raise_exception=True)
    _, kwargs = module.douban_info.call_args
    assert set(kwargs.keys()) == {"doubanid", "mtype", "raise_exception"}


def test_douban_media_detail_default_raise_exception_is_false():
    """契约缺省 raise_exception 为 False；douban_info 自身缺省是 True，
    不传该参数时底层必须收到 False，而不是 douban_info 自己的缺省值。"""
    module = DoubanModule()
    module.douban_info = Mock(return_value={"id": 3})

    module.media_detail(source=MediaSource.Douban, media_id="123")

    module.douban_info.assert_called_once_with(doubanid="123", mtype=None, raise_exception=False)


def test_async_douban_media_detail_default_raise_exception_is_false():
    module = DoubanModule()
    module.async_douban_info = AsyncMock(return_value={"id": 3})

    asyncio.run(module.async_media_detail(source=MediaSource.Douban, media_id="123"))

    module.async_douban_info.assert_called_once_with(doubanid="123", mtype=None, raise_exception=False)


def test_douban_media_detail_returns_none_for_none_media_id():
    module = DoubanModule()
    module.douban_info = Mock(return_value={"id": 2})

    assert module.media_detail(source=MediaSource.Douban, media_id=None) is None
    module.douban_info.assert_not_called()


def test_douban_media_detail_returns_none_for_other_source():
    module = DoubanModule()
    module.douban_info = Mock(return_value={"id": 2})

    result = module.media_detail(source=MediaSource.TMDB, media_id="123")

    assert result is None
    module.douban_info.assert_not_called()


def test_douban_media_detail_accepts_alias_string_source():
    module = DoubanModule()
    module.douban_info = Mock(return_value={"id": 2})

    result = module.media_detail(source="douban", media_id="123")

    assert result == {"id": 2}
    module.douban_info.assert_called_once_with(doubanid="123", mtype=None, raise_exception=False)


def test_douban_media_detail_delegates_instead_of_reimplementing():
    module = DoubanModule()
    module.douban_info = Mock(return_value={"id": 98})

    result = module.media_detail(source=MediaSource.Douban, media_id="123")

    assert result == {"id": 98}
    module.douban_info.assert_called_once()


def test_douban_douban_info_retains_rate_limit_decorator():
    """douban_info 必须仍被 @rate_limit_exponential 包裹（functools.wraps 留下 __wrapped__）；
    一旦新契约方法被插进装饰器与 def 之间，douban_info 会变回未装饰的原始函数，
    同步限流即彻底失效。"""
    assert hasattr(DoubanModule.douban_info, "__wrapped__")
    assert hasattr(DoubanModule.async_douban_info, "__wrapped__")


# ---------------------------------------------------------------------------
# Bangumi：mtype/season/raise_exception 均不支持，kwargs 键集合精确相等
# ---------------------------------------------------------------------------

def test_bangumi_media_detail_drops_mtype_season_raise_exception():
    module = BangumiModule()
    module.bangumi_info = Mock(return_value={"id": 3})

    result = module.media_detail(
        source=MediaSource.Bangumi, media_id=42, mtype=MediaType.MOVIE, season=1, raise_exception=True
    )

    assert result == {"id": 3}
    module.bangumi_info.assert_called_once_with(bangumiid=42)
    _, kwargs = module.bangumi_info.call_args
    assert set(kwargs.keys()) == {"bangumiid"}


def test_async_bangumi_media_detail_drops_mtype_season_raise_exception():
    module = BangumiModule()
    module.async_bangumi_info = AsyncMock(return_value={"id": 3})

    result = asyncio.run(
        module.async_media_detail(
            source=MediaSource.Bangumi, media_id=42, mtype=MediaType.TV, season=1, raise_exception=True
        )
    )

    assert result == {"id": 3}
    module.async_bangumi_info.assert_called_once_with(bangumiid=42)
    _, kwargs = module.async_bangumi_info.call_args
    assert set(kwargs.keys()) == {"bangumiid"}


def test_bangumi_media_detail_returns_none_for_none_media_id():
    module = BangumiModule()
    module.bangumi_info = Mock(return_value={"id": 3})

    assert module.media_detail(source=MediaSource.Bangumi, media_id=None) is None
    module.bangumi_info.assert_not_called()


def test_bangumi_media_detail_returns_none_for_unconvertible_media_id():
    module = BangumiModule()
    module.bangumi_info = Mock(return_value={"id": 3})

    assert module.media_detail(source=MediaSource.Bangumi, media_id="abc") is None
    module.bangumi_info.assert_not_called()


def test_bangumi_media_detail_returns_none_for_other_source():
    module = BangumiModule()
    module.bangumi_info = Mock(return_value={"id": 3})

    result = module.media_detail(source=MediaSource.TMDB, media_id=42)

    assert result is None
    module.bangumi_info.assert_not_called()


def test_bangumi_media_detail_accepts_alias_string_source():
    module = BangumiModule()
    module.bangumi_info = Mock(return_value={"id": 3})

    result = module.media_detail(source="bangumi", media_id=42)

    assert result == {"id": 3}
    module.bangumi_info.assert_called_once_with(bangumiid=42)


def test_bangumi_media_detail_delegates_instead_of_reimplementing():
    module = BangumiModule()
    module.bangumi_info = Mock(return_value={"id": 97})

    result = module.media_detail(source=MediaSource.Bangumi, media_id=42)

    assert result == {"id": 97}
    module.bangumi_info.assert_called_once()


# ---------------------------------------------------------------------------
# AniList：mtype/season/raise_exception 均不支持，kwargs 键集合精确相等
# ---------------------------------------------------------------------------

def test_anilist_media_detail_drops_mtype_season_raise_exception():
    module = AniListModule()
    module.anilist_info = Mock(return_value={"id": 4})

    result = module.media_detail(
        source=MediaSource.AniList, media_id=7, mtype=MediaType.TV, season=1, raise_exception=True
    )

    assert result == {"id": 4}
    module.anilist_info.assert_called_once_with(anilist_id=7)
    _, kwargs = module.anilist_info.call_args
    assert set(kwargs.keys()) == {"anilist_id"}


def test_async_anilist_media_detail_drops_mtype_season_raise_exception():
    module = AniListModule()
    module.async_anilist_info = AsyncMock(return_value={"id": 4})

    result = asyncio.run(
        module.async_media_detail(
            source=MediaSource.AniList, media_id=7, mtype=MediaType.MOVIE, season=1, raise_exception=True
        )
    )

    assert result == {"id": 4}
    module.async_anilist_info.assert_called_once_with(anilist_id=7)
    _, kwargs = module.async_anilist_info.call_args
    assert set(kwargs.keys()) == {"anilist_id"}


def test_anilist_media_detail_returns_none_for_none_media_id():
    module = AniListModule()
    module.anilist_info = Mock(return_value={"id": 4})

    assert module.media_detail(source=MediaSource.AniList, media_id=None) is None
    module.anilist_info.assert_not_called()


def test_anilist_media_detail_returns_none_for_unconvertible_media_id():
    module = AniListModule()
    module.anilist_info = Mock(return_value={"id": 4})

    assert module.media_detail(source=MediaSource.AniList, media_id="abc") is None
    module.anilist_info.assert_not_called()


def test_anilist_media_detail_returns_none_for_other_source():
    module = AniListModule()
    module.anilist_info = Mock(return_value={"id": 4})

    result = module.media_detail(source=MediaSource.TMDB, media_id=7)

    assert result is None
    module.anilist_info.assert_not_called()


def test_anilist_media_detail_accepts_alias_string_source():
    module = AniListModule()
    module.anilist_info = Mock(return_value={"id": 4})

    result = module.media_detail(source="anilist", media_id=7)

    assert result == {"id": 4}
    module.anilist_info.assert_called_once_with(anilist_id=7)


def test_anilist_media_detail_delegates_instead_of_reimplementing():
    module = AniListModule()
    module.anilist_info = Mock(return_value={"id": 96})

    result = module.media_detail(source=MediaSource.AniList, media_id=7)

    assert result == {"id": 96}
    module.anilist_info.assert_called_once()


# ---------------------------------------------------------------------------
# TVDB：mtype/season/raise_exception 均不支持；没有原生异步实现，
# async_media_detail 必须经 run_in_threadpool 包装同步方法
# ---------------------------------------------------------------------------

def test_tvdb_media_detail_drops_mtype_season_raise_exception():
    module = TheTvDbModule()
    module.tvdb_info = Mock(return_value={"id": 81189})

    result = module.media_detail(
        source=MediaSource.TVDB, media_id=81189, mtype=MediaType.TV, season=1, raise_exception=True
    )

    assert result == {"id": 81189}
    module.tvdb_info.assert_called_once_with(tvdbid=81189)
    _, kwargs = module.tvdb_info.call_args
    assert set(kwargs.keys()) == {"tvdbid"}


def test_tvdb_media_detail_returns_none_for_none_media_id():
    module = TheTvDbModule()
    module.tvdb_info = Mock(return_value={"id": 1})

    assert module.media_detail(source=MediaSource.TVDB, media_id=None) is None
    module.tvdb_info.assert_not_called()


def test_tvdb_media_detail_returns_none_for_unconvertible_media_id():
    module = TheTvDbModule()
    module.tvdb_info = Mock(return_value={"id": 1})

    assert module.media_detail(source=MediaSource.TVDB, media_id="abc") is None
    module.tvdb_info.assert_not_called()


def test_tvdb_media_detail_returns_none_for_other_source():
    module = TheTvDbModule()
    module.tvdb_info = Mock(return_value={"id": 1})

    result = module.media_detail(source=MediaSource.TMDB, media_id=81189)

    assert result is None
    module.tvdb_info.assert_not_called()


def test_tvdb_media_detail_accepts_alias_string_source():
    module = TheTvDbModule()
    module.tvdb_info = Mock(return_value={"id": 1})

    result = module.media_detail(source="tvdb", media_id=81189)

    assert result == {"id": 1}
    module.tvdb_info.assert_called_once_with(tvdbid=81189)


def test_tvdb_media_detail_delegates_instead_of_reimplementing():
    module = TheTvDbModule()
    module.tvdb_info = Mock(return_value={"id": 99})

    result = module.media_detail(source=MediaSource.TVDB, media_id=81189)

    assert result == {"id": 99}
    module.tvdb_info.assert_called_once()


def test_tvdb_async_media_detail_returns_sync_method_result():
    """TVDB 没有原生异步实现，async_media_detail 必须能拿到同步方法的结果。"""
    module = TheTvDbModule()
    module.tvdb_info = Mock(return_value={"id": 81189})

    result = asyncio.run(module.async_media_detail(source=MediaSource.TVDB, media_id=81189))

    assert result == {"id": 81189}
    module.tvdb_info.assert_called_once_with(tvdbid=81189)


def test_tvdb_async_media_detail_uses_run_in_threadpool(monkeypatch):
    """async_media_detail 必须经 run_in_threadpool 把同步方法移出事件循环，
    而不是直接同步调用或自行重新实现。"""
    module = TheTvDbModule()
    module.tvdb_info = Mock(return_value={"id": 1})
    calls = []

    async def fake_run_in_threadpool(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(thetvdb_module, "run_in_threadpool", fake_run_in_threadpool)

    result = asyncio.run(module.async_media_detail(source=MediaSource.TVDB, media_id=1))

    assert result == {"id": 1}
    assert len(calls) == 1
    func, args, kwargs = calls[0]
    assert func is module.tvdb_info
    assert args == ()
    assert kwargs == {"tvdbid": 1}


def test_tvdb_async_media_detail_returns_none_for_other_source():
    module = TheTvDbModule()
    module.tvdb_info = Mock(return_value={"id": 1})

    result = asyncio.run(module.async_media_detail(source=MediaSource.TMDB, media_id=1))

    assert result is None
    module.tvdb_info.assert_not_called()


# ---------------------------------------------------------------------------
# 能力契约登记
# ---------------------------------------------------------------------------

def test_media_detail_is_registered_as_media_metadata_family():
    assert get_module_method_contract("media_detail").family == "media-metadata"
    assert get_module_method_contract("async_media_detail").family == "media-metadata"


def test_media_detail_declares_its_multi_source_protocol():
    contract = get_multi_source_contract("media_detail")

    assert contract is not None
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"source"}
    assert "首个非空答案" in contract.arbitration
