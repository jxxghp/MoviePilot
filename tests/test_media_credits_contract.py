"""media_credits 契约的归一行为测试。

覆盖 TMDB、豆瓣、Bangumi、AniList 四个提供者：非本来源让出、mtype 缺省落到电影接口、
media_id 为空或无法转换为本来源ID类型时让出、参数按各自签名下传、契约方法委托原实现
而非重新实现。
"""
import asyncio
from unittest.mock import AsyncMock, Mock

from app.modules.anilist import AniListModule
from app.modules.bangumi import BangumiModule
from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule
from app.runtime.extensions.contract.module_method import (
    get_module_method_contract,
    get_multi_source_contract,
)
from app.schemas.types import MediaSource, MediaType


# ---------------------------------------------------------------------------
# mtype=None 落到电影接口，这是本族最易写反的地方
# ---------------------------------------------------------------------------

def test_tmdb_media_credits_none_mtype_uses_movie_credits():
    """TMDB 契约在 mtype 缺省时必须落到 tmdb_movie_credits，而非剧集接口。"""
    module = TheMovieDbModule()
    module.tmdb_movie_credits = Mock(return_value=[{"id": 1}])
    module.tmdb_tv_credits = Mock(return_value=[{"id": 999}])

    result = module.media_credits(source=MediaSource.TMDB, media_id=550)

    assert result == [{"id": 1}]
    module.tmdb_movie_credits.assert_called_once_with(tmdbid=550, page=1)
    module.tmdb_tv_credits.assert_not_called()


def test_async_tmdb_media_credits_none_mtype_uses_movie_credits():
    """异步 TMDB 契约同样在 mtype 缺省时落到电影接口。"""
    module = TheMovieDbModule()
    module.async_tmdb_movie_credits = AsyncMock(return_value=[{"id": 1}])
    module.async_tmdb_tv_credits = AsyncMock(return_value=[{"id": 999}])

    result = asyncio.run(module.async_media_credits(source=MediaSource.TMDB, media_id=550))

    assert result == [{"id": 1}]
    module.async_tmdb_movie_credits.assert_called_once_with(tmdbid=550, page=1)
    module.async_tmdb_tv_credits.assert_not_called()


def test_douban_media_credits_none_mtype_uses_movie_credits():
    """豆瓣契约在 mtype 缺省时必须落到 douban_movie_credits，而非剧集接口。"""
    module = DoubanModule()
    module.douban_movie_credits = Mock(return_value=[{"id": 2}])
    module.douban_tv_credits = Mock(return_value=[{"id": 999}])

    result = module.media_credits(source=MediaSource.Douban, media_id="123")

    assert result == [{"id": 2}]
    module.douban_movie_credits.assert_called_once_with(doubanid="123")
    module.douban_tv_credits.assert_not_called()


def test_async_douban_media_credits_none_mtype_uses_movie_credits():
    """异步豆瓣契约同样在 mtype 缺省时落到电影接口。"""
    module = DoubanModule()
    module.async_douban_movie_credits = AsyncMock(return_value=[{"id": 2}])
    module.async_douban_tv_credits = AsyncMock(return_value=[{"id": 999}])

    result = asyncio.run(module.async_media_credits(source=MediaSource.Douban, media_id="123"))

    assert result == [{"id": 2}]
    module.async_douban_movie_credits.assert_called_once_with(doubanid="123")
    module.async_douban_tv_credits.assert_not_called()


# ---------------------------------------------------------------------------
# mtype=MediaType.TV 落到剧集接口
# ---------------------------------------------------------------------------

def test_tmdb_media_credits_tv_mtype_uses_tv_credits():
    """TMDB 契约在 mtype=TV 时必须落到 tmdb_tv_credits。"""
    module = TheMovieDbModule()
    module.tmdb_movie_credits = Mock(return_value=[{"id": 999}])
    module.tmdb_tv_credits = Mock(return_value=[{"id": 1}])

    result = module.media_credits(source=MediaSource.TMDB, media_id=1399, mtype=MediaType.TV, page=2)

    assert result == [{"id": 1}]
    module.tmdb_tv_credits.assert_called_once_with(tmdbid=1399, page=2)
    module.tmdb_movie_credits.assert_not_called()


def test_douban_media_credits_tv_mtype_uses_tv_credits():
    """豆瓣契约在 mtype=TV 时必须落到 douban_tv_credits。"""
    module = DoubanModule()
    module.douban_movie_credits = Mock(return_value=[{"id": 999}])
    module.douban_tv_credits = Mock(return_value=[{"id": 2}])

    result = module.media_credits(source=MediaSource.Douban, media_id="456", mtype=MediaType.TV)

    assert result == [{"id": 2}]
    module.douban_tv_credits.assert_called_once_with(doubanid="456")
    module.douban_movie_credits.assert_not_called()


# ---------------------------------------------------------------------------
# Bangumi：不下传 mtype/page/count，kwargs 键集合精确相等
# ---------------------------------------------------------------------------

def test_bangumi_media_credits_does_not_forward_mtype_page_or_count():
    """Bangumi 契约必须只下传 bangumiid，不能下传 mtype/page/count。"""
    module = BangumiModule()
    module.bangumi_credits = Mock(return_value=[{"id": 3}])

    result = module.media_credits(
        source=MediaSource.Bangumi, media_id=42, mtype=MediaType.MOVIE, page=3, count=10
    )

    assert result == [{"id": 3}]
    module.bangumi_credits.assert_called_once_with(bangumiid=42)
    _, kwargs = module.bangumi_credits.call_args
    assert set(kwargs.keys()) == {"bangumiid"}


def test_async_bangumi_media_credits_does_not_forward_mtype_page_or_count():
    """异步 Bangumi 契约同样只下传 bangumiid。"""
    module = BangumiModule()
    module.async_bangumi_credits = AsyncMock(return_value=[{"id": 3}])

    result = asyncio.run(
        module.async_media_credits(
            source=MediaSource.Bangumi, media_id=42, mtype=MediaType.TV, page=3, count=10
        )
    )

    assert result == [{"id": 3}]
    module.async_bangumi_credits.assert_called_once_with(bangumiid=42)
    _, kwargs = module.async_bangumi_credits.call_args
    assert set(kwargs.keys()) == {"bangumiid"}


# ---------------------------------------------------------------------------
# AniList：count=None 时底层收到本源缺省值 20
# ---------------------------------------------------------------------------

def test_anilist_media_credits_defaults_missing_count_to_twenty():
    """AniList 契约收到 count=None 时必须把本源缺省值 20 下传，而不是 None。"""
    module = AniListModule()
    module.anilist_credits = Mock(return_value=[])

    module.media_credits(source=MediaSource.AniList, media_id=7, page=2, count=None)

    module.anilist_credits.assert_called_once_with(7, page=2, count=20)


def test_async_anilist_media_credits_defaults_missing_count_to_twenty():
    """异步 AniList 契约同样把 count=None 转换为 20。"""
    module = AniListModule()
    module.async_anilist_credits = AsyncMock(return_value=[])

    asyncio.run(module.async_media_credits(source=MediaSource.AniList, media_id=7, page=2, count=None))

    module.async_anilist_credits.assert_called_once_with(7, page=2, count=20)


def test_anilist_media_credits_forwards_explicit_count():
    """AniList 契约显式传入 count 时原样下传。"""
    module = AniListModule()
    module.anilist_credits = Mock(return_value=[])

    module.media_credits(source=MediaSource.AniList, media_id=7, page=1, count=5)

    module.anilist_credits.assert_called_once_with(7, page=1, count=5)


# ---------------------------------------------------------------------------
# TMDB：count 不是本源签名的一部分，必须就地丢弃；豆瓣：page/count 均不支持
# ---------------------------------------------------------------------------

def test_tmdb_media_credits_drops_unsupported_count():
    """TMDB 契约收到 count 时必须丢弃，不下传给 tmdb_movie_credits。"""
    module = TheMovieDbModule()
    module.tmdb_movie_credits = Mock(return_value=[{"id": 1}])

    result = module.media_credits(source=MediaSource.TMDB, media_id=550, page=2, count=99)

    assert result == [{"id": 1}]
    module.tmdb_movie_credits.assert_called_once_with(tmdbid=550, page=2)
    _, kwargs = module.tmdb_movie_credits.call_args
    assert "count" not in kwargs


def test_douban_media_credits_drops_unsupported_page_and_count():
    """豆瓣契约收到 page/count 时必须丢弃，均不下传给 douban_movie_credits。"""
    module = DoubanModule()
    module.douban_movie_credits = Mock(return_value=[{"id": 2}])

    result = module.media_credits(source=MediaSource.Douban, media_id="123", page=3, count=99)

    assert result == [{"id": 2}]
    module.douban_movie_credits.assert_called_once_with(doubanid="123")
    _, kwargs = module.douban_movie_credits.call_args
    assert "page" not in kwargs
    assert "count" not in kwargs


# ---------------------------------------------------------------------------
# media_id 为 None 或转换失败时返回 None，不抛异常
# ---------------------------------------------------------------------------

def test_tmdb_media_credits_returns_none_for_none_media_id():
    module = TheMovieDbModule()
    module.tmdb_movie_credits = Mock(return_value=[{"id": 1}])

    assert module.media_credits(source=MediaSource.TMDB, media_id=None) is None
    module.tmdb_movie_credits.assert_not_called()


def test_tmdb_media_credits_returns_none_for_unconvertible_media_id():
    module = TheMovieDbModule()
    module.tmdb_movie_credits = Mock(return_value=[{"id": 1}])

    assert module.media_credits(source=MediaSource.TMDB, media_id="abc") is None
    module.tmdb_movie_credits.assert_not_called()


def test_douban_media_credits_returns_none_for_none_media_id():
    """豆瓣契约必须在 media_id 为 None 时提前让出，不能得到字符串 "None"。"""
    module = DoubanModule()
    module.douban_movie_credits = Mock(return_value=[{"id": 2}])

    assert module.media_credits(source=MediaSource.Douban, media_id=None) is None
    module.douban_movie_credits.assert_not_called()


def test_bangumi_media_credits_returns_none_for_none_media_id():
    module = BangumiModule()
    module.bangumi_credits = Mock(return_value=[{"id": 3}])

    assert module.media_credits(source=MediaSource.Bangumi, media_id=None) is None
    module.bangumi_credits.assert_not_called()


def test_bangumi_media_credits_returns_none_for_unconvertible_media_id():
    module = BangumiModule()
    module.bangumi_credits = Mock(return_value=[{"id": 3}])

    assert module.media_credits(source=MediaSource.Bangumi, media_id="abc") is None
    module.bangumi_credits.assert_not_called()


def test_anilist_media_credits_returns_none_for_none_media_id():
    module = AniListModule()
    module.anilist_credits = Mock(return_value=[])

    assert module.media_credits(source=MediaSource.AniList, media_id=None) is None
    module.anilist_credits.assert_not_called()


def test_anilist_media_credits_returns_none_for_unconvertible_media_id():
    module = AniListModule()
    module.anilist_credits = Mock(return_value=[])

    assert module.media_credits(source=MediaSource.AniList, media_id="abc") is None
    module.anilist_credits.assert_not_called()


# ---------------------------------------------------------------------------
# 非本来源必须返回 None（而非 falsy 的 []）
# ---------------------------------------------------------------------------

def test_tmdb_media_credits_returns_none_for_other_source():
    module = TheMovieDbModule()
    module.tmdb_movie_credits = Mock(return_value=[{"id": 1}])

    result = module.media_credits(source=MediaSource.Douban, media_id=550)

    assert result is None
    module.tmdb_movie_credits.assert_not_called()


def test_douban_media_credits_returns_none_for_other_source():
    module = DoubanModule()
    module.douban_movie_credits = Mock(return_value=[{"id": 2}])

    result = module.media_credits(source=MediaSource.TMDB, media_id="123")

    assert result is None
    module.douban_movie_credits.assert_not_called()


def test_bangumi_media_credits_returns_none_for_other_source():
    module = BangumiModule()
    module.bangumi_credits = Mock(return_value=[{"id": 3}])

    result = module.media_credits(source=MediaSource.TMDB, media_id=42)

    assert result is None
    module.bangumi_credits.assert_not_called()


def test_anilist_media_credits_returns_none_for_other_source():
    module = AniListModule()
    module.anilist_credits = Mock(return_value=[{"id": 4}])

    result = module.media_credits(source=MediaSource.TMDB, media_id=7)

    assert result is None
    module.anilist_credits.assert_not_called()


def test_tmdb_media_credits_returns_none_when_source_missing():
    """未指定来源时 TMDB 契约必须让出，返回 None。"""
    module = TheMovieDbModule()
    module.tmdb_movie_credits = Mock(return_value=[{"id": 1}])

    assert module.media_credits(media_id=550) is None


# ---------------------------------------------------------------------------
# 别名字符串来源（如 "tmdb"）必须能命中，裸 != 比较对别名不成立
# ---------------------------------------------------------------------------

def test_tmdb_media_credits_accepts_alias_string_source():
    module = TheMovieDbModule()
    module.tmdb_movie_credits = Mock(return_value=[{"id": 1}])

    result = module.media_credits(source="tmdb", media_id=550)

    assert result == [{"id": 1}]
    module.tmdb_movie_credits.assert_called_once_with(tmdbid=550, page=1)


def test_douban_media_credits_accepts_alias_string_source():
    module = DoubanModule()
    module.douban_movie_credits = Mock(return_value=[{"id": 2}])

    result = module.media_credits(source="douban", media_id="123")

    assert result == [{"id": 2}]
    module.douban_movie_credits.assert_called_once_with(doubanid="123")


def test_bangumi_media_credits_accepts_alias_string_source():
    module = BangumiModule()
    module.bangumi_credits = Mock(return_value=[{"id": 3}])

    result = module.media_credits(source="bangumi", media_id=42)

    assert result == [{"id": 3}]
    module.bangumi_credits.assert_called_once_with(bangumiid=42)


def test_anilist_media_credits_accepts_alias_string_source():
    module = AniListModule()
    module.anilist_credits = Mock(return_value=[{"id": 4}])

    result = module.media_credits(source="anilist", media_id=7)

    assert result == [{"id": 4}]
    module.anilist_credits.assert_called_once_with(7, page=1, count=20)


# ---------------------------------------------------------------------------
# 契约是委托而非重新实现
# ---------------------------------------------------------------------------

def test_tmdb_media_credits_delegates_instead_of_reimplementing():
    module = TheMovieDbModule()
    module.tmdb_movie_credits = Mock(return_value=[{"id": 99}])

    result = module.media_credits(source=MediaSource.TMDB, media_id=550)

    assert result == [{"id": 99}]
    module.tmdb_movie_credits.assert_called_once()


def test_douban_media_credits_delegates_instead_of_reimplementing():
    module = DoubanModule()
    module.douban_movie_credits = Mock(return_value=[{"id": 98}])

    result = module.media_credits(source=MediaSource.Douban, media_id="123")

    assert result == [{"id": 98}]
    module.douban_movie_credits.assert_called_once()


def test_bangumi_media_credits_delegates_instead_of_reimplementing():
    module = BangumiModule()
    module.bangumi_credits = Mock(return_value=[{"id": 97}])

    result = module.media_credits(source=MediaSource.Bangumi, media_id=42)

    assert result == [{"id": 97}]
    module.bangumi_credits.assert_called_once()


def test_anilist_media_credits_delegates_instead_of_reimplementing():
    module = AniListModule()
    module.anilist_credits = Mock(return_value=[{"id": 96}])

    result = module.media_credits(source=MediaSource.AniList, media_id=7)

    assert result == [{"id": 96}]
    module.anilist_credits.assert_called_once()


# ---------------------------------------------------------------------------
# 能力契约登记
# ---------------------------------------------------------------------------

def test_media_credits_is_registered_as_media_metadata_family():
    assert get_module_method_contract("media_credits").family == "media-metadata"
    assert get_module_method_contract("async_media_credits").family == "media-metadata"


def test_media_credits_declares_its_multi_source_protocol():
    contract = get_multi_source_contract("media_credits")

    assert contract is not None
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"source"}
    assert "首个非空答案" in contract.arbitration
