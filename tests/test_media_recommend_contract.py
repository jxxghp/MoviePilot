"""media_recommend 与 media_similar 契约的归一行为测试。

media_recommend 覆盖 TMDB、豆瓣、Bangumi、AniList 四个提供者：非本来源让出、mtype 缺省落到
电影接口、media_id 为空或无法转换为本来源ID类型时让出、参数按各自签名下传（TMDB/豆瓣/Bangumi
本源均不支持分页，AniList 支持并有缺省 count）、契约方法委托原实现而非重新实现。

media_similar 只有 TMDB 实现，豆瓣、Bangumi、AniList 均未声明该方法，不会进入能力索引。
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
# TMDB / 豆瓣：本源不支持分页，下传 kwargs 键集合必须精确
# ---------------------------------------------------------------------------

def test_tmdb_media_recommend_does_not_forward_page_or_count():
    """TMDB 推荐契约必须只下传 tmdbid，不能下传 page/count。"""
    module = TheMovieDbModule()
    module.tmdb_movie_recommend = Mock(return_value=[{"id": 1}])

    result = module.media_recommend(source=MediaSource.TMDB, media_id=550, page=3, count=99)

    assert result == [{"id": 1}]
    module.tmdb_movie_recommend.assert_called_once_with(tmdbid=550)
    _, kwargs = module.tmdb_movie_recommend.call_args
    assert set(kwargs.keys()) == {"tmdbid"}


def test_async_tmdb_media_recommend_does_not_forward_page_or_count():
    """异步 TMDB 推荐契约同样只下传 tmdbid。"""
    module = TheMovieDbModule()
    module.async_tmdb_movie_recommend = AsyncMock(return_value=[{"id": 1}])

    result = asyncio.run(
        module.async_media_recommend(source=MediaSource.TMDB, media_id=550, page=3, count=99)
    )

    assert result == [{"id": 1}]
    module.async_tmdb_movie_recommend.assert_called_once_with(tmdbid=550)
    _, kwargs = module.async_tmdb_movie_recommend.call_args
    assert set(kwargs.keys()) == {"tmdbid"}


def test_douban_media_recommend_does_not_forward_page_or_count():
    """豆瓣推荐契约必须只下传 doubanid，不能下传 page/count。"""
    module = DoubanModule()
    module.douban_movie_recommend = Mock(return_value=[{"id": 2}])

    result = module.media_recommend(source=MediaSource.Douban, media_id="123", page=3, count=99)

    assert result == [{"id": 2}]
    module.douban_movie_recommend.assert_called_once_with(doubanid="123")
    _, kwargs = module.douban_movie_recommend.call_args
    assert set(kwargs.keys()) == {"doubanid"}


def test_async_douban_media_recommend_does_not_forward_page_or_count():
    """异步豆瓣推荐契约同样只下传 doubanid。"""
    module = DoubanModule()
    module.async_douban_movie_recommend = AsyncMock(return_value=[{"id": 2}])

    result = asyncio.run(
        module.async_media_recommend(source=MediaSource.Douban, media_id="123", page=3, count=99)
    )

    assert result == [{"id": 2}]
    module.async_douban_movie_recommend.assert_called_once_with(doubanid="123")
    _, kwargs = module.async_douban_movie_recommend.call_args
    assert set(kwargs.keys()) == {"doubanid"}


# ---------------------------------------------------------------------------
# AniList：count=None 时底层收到本源缺省值 20，且传了 page
# ---------------------------------------------------------------------------

def test_anilist_media_recommend_defaults_missing_count_to_twenty():
    """AniList 推荐契约收到 count=None 时必须把本源缺省值 20 下传，而不是 None。"""
    module = AniListModule()
    module.anilist_recommendations = Mock(return_value=[])

    module.media_recommend(source=MediaSource.AniList, media_id=7, page=2, count=None)

    module.anilist_recommendations.assert_called_once_with(7, page=2, count=20)


def test_async_anilist_media_recommend_defaults_missing_count_to_twenty():
    """异步 AniList 推荐契约同样把 count=None 转换为 20。"""
    module = AniListModule()
    module.async_anilist_recommendations = AsyncMock(return_value=[])

    asyncio.run(
        module.async_media_recommend(source=MediaSource.AniList, media_id=7, page=2, count=None)
    )

    module.async_anilist_recommendations.assert_called_once_with(7, page=2, count=20)


def test_anilist_media_recommend_forwards_explicit_count():
    """AniList 推荐契约显式传入 count 时原样下传。"""
    module = AniListModule()
    module.anilist_recommendations = Mock(return_value=[])

    module.media_recommend(source=MediaSource.AniList, media_id=7, page=1, count=5)

    module.anilist_recommendations.assert_called_once_with(7, page=1, count=5)


# ---------------------------------------------------------------------------
# Bangumi：不下传 mtype/page/count，只下传媒体标识
# ---------------------------------------------------------------------------

def test_bangumi_media_recommend_only_forwards_media_identifier():
    """Bangumi 推荐契约必须只下传 bangumiid，不能下传 mtype/page/count。"""
    module = BangumiModule()
    module.bangumi_recommend = Mock(return_value=[{"id": 3}])

    result = module.media_recommend(
        source=MediaSource.Bangumi, media_id=42, mtype=MediaType.MOVIE, page=3, count=10
    )

    assert result == [{"id": 3}]
    module.bangumi_recommend.assert_called_once_with(bangumiid=42)
    _, kwargs = module.bangumi_recommend.call_args
    assert set(kwargs.keys()) == {"bangumiid"}


def test_async_bangumi_media_recommend_only_forwards_media_identifier():
    """异步 Bangumi 推荐契约同样只下传 bangumiid。"""
    module = BangumiModule()
    module.async_bangumi_recommend = AsyncMock(return_value=[{"id": 3}])

    result = asyncio.run(
        module.async_media_recommend(
            source=MediaSource.Bangumi, media_id=42, mtype=MediaType.TV, page=3, count=10
        )
    )

    assert result == [{"id": 3}]
    module.async_bangumi_recommend.assert_called_once_with(bangumiid=42)
    _, kwargs = module.async_bangumi_recommend.call_args
    assert set(kwargs.keys()) == {"bangumiid"}


# ---------------------------------------------------------------------------
# mtype=None 落到电影接口，这是本族最易写反的地方
# ---------------------------------------------------------------------------

def test_tmdb_media_recommend_none_mtype_uses_movie_recommend():
    """TMDB 推荐契约在 mtype 缺省时必须落到 tmdb_movie_recommend，而非剧集接口。"""
    module = TheMovieDbModule()
    module.tmdb_movie_recommend = Mock(return_value=[{"id": 1}])
    module.tmdb_tv_recommend = Mock(return_value=[{"id": 999}])

    result = module.media_recommend(source=MediaSource.TMDB, media_id=550)

    assert result == [{"id": 1}]
    module.tmdb_movie_recommend.assert_called_once_with(tmdbid=550)
    module.tmdb_tv_recommend.assert_not_called()


def test_douban_media_recommend_none_mtype_uses_movie_recommend():
    """豆瓣推荐契约在 mtype 缺省时必须落到 douban_movie_recommend，而非剧集接口。"""
    module = DoubanModule()
    module.douban_movie_recommend = Mock(return_value=[{"id": 2}])
    module.douban_tv_recommend = Mock(return_value=[{"id": 999}])

    result = module.media_recommend(source=MediaSource.Douban, media_id="123")

    assert result == [{"id": 2}]
    module.douban_movie_recommend.assert_called_once_with(doubanid="123")
    module.douban_tv_recommend.assert_not_called()


# ---------------------------------------------------------------------------
# mtype=MediaType.TV 落到剧集接口
# ---------------------------------------------------------------------------

def test_tmdb_media_recommend_tv_mtype_uses_tv_recommend():
    """TMDB 推荐契约在 mtype=TV 时必须落到 tmdb_tv_recommend。"""
    module = TheMovieDbModule()
    module.tmdb_movie_recommend = Mock(return_value=[{"id": 999}])
    module.tmdb_tv_recommend = Mock(return_value=[{"id": 1}])

    result = module.media_recommend(source=MediaSource.TMDB, media_id=1399, mtype=MediaType.TV)

    assert result == [{"id": 1}]
    module.tmdb_tv_recommend.assert_called_once_with(tmdbid=1399)
    module.tmdb_movie_recommend.assert_not_called()


def test_douban_media_recommend_tv_mtype_uses_tv_recommend():
    """豆瓣推荐契约在 mtype=TV 时必须落到 douban_tv_recommend。"""
    module = DoubanModule()
    module.douban_movie_recommend = Mock(return_value=[{"id": 999}])
    module.douban_tv_recommend = Mock(return_value=[{"id": 2}])

    result = module.media_recommend(source=MediaSource.Douban, media_id="456", mtype=MediaType.TV)

    assert result == [{"id": 2}]
    module.douban_tv_recommend.assert_called_once_with(doubanid="456")
    module.douban_movie_recommend.assert_not_called()


# ---------------------------------------------------------------------------
# media_id 为 None 或转换失败时返回 None，不抛异常
# ---------------------------------------------------------------------------

def test_tmdb_media_recommend_returns_none_for_none_media_id():
    module = TheMovieDbModule()
    module.tmdb_movie_recommend = Mock(return_value=[{"id": 1}])

    assert module.media_recommend(source=MediaSource.TMDB, media_id=None) is None
    module.tmdb_movie_recommend.assert_not_called()


def test_tmdb_media_recommend_returns_none_for_unconvertible_media_id():
    module = TheMovieDbModule()
    module.tmdb_movie_recommend = Mock(return_value=[{"id": 1}])

    assert module.media_recommend(source=MediaSource.TMDB, media_id="abc") is None
    module.tmdb_movie_recommend.assert_not_called()


def test_douban_media_recommend_returns_none_for_none_media_id():
    """豆瓣推荐契约必须在 media_id 为 None 时提前让出，不能得到字符串 "None"。"""
    module = DoubanModule()
    module.douban_movie_recommend = Mock(return_value=[{"id": 2}])

    assert module.media_recommend(source=MediaSource.Douban, media_id=None) is None
    module.douban_movie_recommend.assert_not_called()


def test_bangumi_media_recommend_returns_none_for_none_media_id():
    module = BangumiModule()
    module.bangumi_recommend = Mock(return_value=[{"id": 3}])

    assert module.media_recommend(source=MediaSource.Bangumi, media_id=None) is None
    module.bangumi_recommend.assert_not_called()


def test_bangumi_media_recommend_returns_none_for_unconvertible_media_id():
    module = BangumiModule()
    module.bangumi_recommend = Mock(return_value=[{"id": 3}])

    assert module.media_recommend(source=MediaSource.Bangumi, media_id="abc") is None
    module.bangumi_recommend.assert_not_called()


def test_anilist_media_recommend_returns_none_for_none_media_id():
    module = AniListModule()
    module.anilist_recommendations = Mock(return_value=[])

    assert module.media_recommend(source=MediaSource.AniList, media_id=None) is None
    module.anilist_recommendations.assert_not_called()


def test_anilist_media_recommend_returns_none_for_unconvertible_media_id():
    module = AniListModule()
    module.anilist_recommendations = Mock(return_value=[])

    assert module.media_recommend(source=MediaSource.AniList, media_id="abc") is None
    module.anilist_recommendations.assert_not_called()


# ---------------------------------------------------------------------------
# 非本来源必须返回 None（而非 falsy 的 []）
# ---------------------------------------------------------------------------

def test_tmdb_media_recommend_returns_none_for_other_source():
    module = TheMovieDbModule()
    module.tmdb_movie_recommend = Mock(return_value=[{"id": 1}])

    result = module.media_recommend(source=MediaSource.Douban, media_id=550)

    assert result is None
    module.tmdb_movie_recommend.assert_not_called()


def test_douban_media_recommend_returns_none_for_other_source():
    module = DoubanModule()
    module.douban_movie_recommend = Mock(return_value=[{"id": 2}])

    result = module.media_recommend(source=MediaSource.TMDB, media_id="123")

    assert result is None
    module.douban_movie_recommend.assert_not_called()


def test_bangumi_media_recommend_returns_none_for_other_source():
    module = BangumiModule()
    module.bangumi_recommend = Mock(return_value=[{"id": 3}])

    result = module.media_recommend(source=MediaSource.TMDB, media_id=42)

    assert result is None
    module.bangumi_recommend.assert_not_called()


def test_anilist_media_recommend_returns_none_for_other_source():
    module = AniListModule()
    module.anilist_recommendations = Mock(return_value=[{"id": 4}])

    result = module.media_recommend(source=MediaSource.TMDB, media_id=7)

    assert result is None
    module.anilist_recommendations.assert_not_called()


def test_tmdb_media_recommend_returns_none_when_source_missing():
    """未指定来源时 TMDB 推荐契约必须让出，返回 None。"""
    module = TheMovieDbModule()
    module.tmdb_movie_recommend = Mock(return_value=[{"id": 1}])

    assert module.media_recommend(media_id=550) is None


# ---------------------------------------------------------------------------
# 别名字符串来源（如 "tmdb"）必须能命中，裸 != 比较对别名不成立
# ---------------------------------------------------------------------------

def test_tmdb_media_recommend_accepts_alias_string_source():
    module = TheMovieDbModule()
    module.tmdb_movie_recommend = Mock(return_value=[{"id": 1}])

    result = module.media_recommend(source="tmdb", media_id=550)

    assert result == [{"id": 1}]
    module.tmdb_movie_recommend.assert_called_once_with(tmdbid=550)


def test_douban_media_recommend_accepts_alias_string_source():
    module = DoubanModule()
    module.douban_movie_recommend = Mock(return_value=[{"id": 2}])

    result = module.media_recommend(source="douban", media_id="123")

    assert result == [{"id": 2}]
    module.douban_movie_recommend.assert_called_once_with(doubanid="123")


def test_bangumi_media_recommend_accepts_alias_string_source():
    module = BangumiModule()
    module.bangumi_recommend = Mock(return_value=[{"id": 3}])

    result = module.media_recommend(source="bangumi", media_id=42)

    assert result == [{"id": 3}]
    module.bangumi_recommend.assert_called_once_with(bangumiid=42)


def test_anilist_media_recommend_accepts_alias_string_source():
    module = AniListModule()
    module.anilist_recommendations = Mock(return_value=[{"id": 4}])

    result = module.media_recommend(source="anilist", media_id=7)

    assert result == [{"id": 4}]
    module.anilist_recommendations.assert_called_once_with(7, page=1, count=20)


# ---------------------------------------------------------------------------
# 契约是委托而非重新实现
# ---------------------------------------------------------------------------

def test_tmdb_media_recommend_delegates_instead_of_reimplementing():
    module = TheMovieDbModule()
    module.tmdb_movie_recommend = Mock(return_value=[{"id": 99}])

    result = module.media_recommend(source=MediaSource.TMDB, media_id=550)

    assert result == [{"id": 99}]
    module.tmdb_movie_recommend.assert_called_once()


def test_douban_media_recommend_delegates_instead_of_reimplementing():
    module = DoubanModule()
    module.douban_movie_recommend = Mock(return_value=[{"id": 98}])

    result = module.media_recommend(source=MediaSource.Douban, media_id="123")

    assert result == [{"id": 98}]
    module.douban_movie_recommend.assert_called_once()


def test_bangumi_media_recommend_delegates_instead_of_reimplementing():
    module = BangumiModule()
    module.bangumi_recommend = Mock(return_value=[{"id": 97}])

    result = module.media_recommend(source=MediaSource.Bangumi, media_id=42)

    assert result == [{"id": 97}]
    module.bangumi_recommend.assert_called_once()


def test_anilist_media_recommend_delegates_instead_of_reimplementing():
    module = AniListModule()
    module.anilist_recommendations = Mock(return_value=[{"id": 96}])

    result = module.media_recommend(source=MediaSource.AniList, media_id=7)

    assert result == [{"id": 96}]
    module.anilist_recommendations.assert_called_once()


# ---------------------------------------------------------------------------
# media_recommend 能力契约登记
# ---------------------------------------------------------------------------

def test_media_recommend_is_registered_as_media_metadata_family():
    assert get_module_method_contract("media_recommend").family == "media-metadata"
    assert get_module_method_contract("async_media_recommend").family == "media-metadata"


def test_media_recommend_declares_its_multi_source_protocol():
    contract = get_multi_source_contract("media_recommend")

    assert contract is not None
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"source"}
    assert "首个非空答案" in contract.arbitration


# ===========================================================================
# media_similar：只有 TMDB 实现
# ===========================================================================

def test_media_similar_only_exists_on_tmdb_module():
    """豆瓣、Bangumi、AniList 均不得实现 media_similar，否则会被错误地纳入能力索引。"""
    assert hasattr(DoubanModule, "media_similar") is False
    assert hasattr(DoubanModule, "async_media_similar") is False
    assert hasattr(BangumiModule, "media_similar") is False
    assert hasattr(BangumiModule, "async_media_similar") is False
    assert hasattr(AniListModule, "media_similar") is False
    assert hasattr(AniListModule, "async_media_similar") is False
    assert hasattr(TheMovieDbModule, "media_similar") is True
    assert hasattr(TheMovieDbModule, "async_media_similar") is True


def test_tmdb_media_similar_none_mtype_uses_movie_similar():
    """TMDB 相似契约在 mtype 缺省时必须落到 tmdb_movie_similar，而非剧集接口。"""
    module = TheMovieDbModule()
    module.tmdb_movie_similar = Mock(return_value=[{"id": 1}])
    module.tmdb_tv_similar = Mock(return_value=[{"id": 999}])

    result = module.media_similar(source=MediaSource.TMDB, media_id=550)

    assert result == [{"id": 1}]
    module.tmdb_movie_similar.assert_called_once_with(tmdbid=550)
    module.tmdb_tv_similar.assert_not_called()


def test_async_tmdb_media_similar_none_mtype_uses_movie_similar():
    module = TheMovieDbModule()
    module.async_tmdb_movie_similar = AsyncMock(return_value=[{"id": 1}])
    module.async_tmdb_tv_similar = AsyncMock(return_value=[{"id": 999}])

    result = asyncio.run(module.async_media_similar(source=MediaSource.TMDB, media_id=550))

    assert result == [{"id": 1}]
    module.async_tmdb_movie_similar.assert_called_once_with(tmdbid=550)
    module.async_tmdb_tv_similar.assert_not_called()


def test_tmdb_media_similar_tv_mtype_uses_tv_similar():
    """TMDB 相似契约在 mtype=TV 时必须落到 tmdb_tv_similar。"""
    module = TheMovieDbModule()
    module.tmdb_movie_similar = Mock(return_value=[{"id": 999}])
    module.tmdb_tv_similar = Mock(return_value=[{"id": 1}])

    result = module.media_similar(source=MediaSource.TMDB, media_id=1399, mtype=MediaType.TV)

    assert result == [{"id": 1}]
    module.tmdb_tv_similar.assert_called_once_with(tmdbid=1399)
    module.tmdb_movie_similar.assert_not_called()


def test_tmdb_media_similar_does_not_forward_extra_kwargs():
    """TMDB 相似契约只下传 tmdbid，无分页参数。"""
    module = TheMovieDbModule()
    module.tmdb_movie_similar = Mock(return_value=[{"id": 1}])

    module.media_similar(source=MediaSource.TMDB, media_id=550)

    _, kwargs = module.tmdb_movie_similar.call_args
    assert set(kwargs.keys()) == {"tmdbid"}


def test_tmdb_media_similar_returns_none_for_other_source():
    module = TheMovieDbModule()
    module.tmdb_movie_similar = Mock(return_value=[{"id": 1}])

    result = module.media_similar(source=MediaSource.Douban, media_id=550)

    assert result is None
    module.tmdb_movie_similar.assert_not_called()


def test_tmdb_media_similar_returns_none_when_source_missing():
    module = TheMovieDbModule()
    module.tmdb_movie_similar = Mock(return_value=[{"id": 1}])

    assert module.media_similar(media_id=550) is None
    module.tmdb_movie_similar.assert_not_called()


def test_tmdb_media_similar_returns_none_for_none_media_id():
    module = TheMovieDbModule()
    module.tmdb_movie_similar = Mock(return_value=[{"id": 1}])

    assert module.media_similar(source=MediaSource.TMDB, media_id=None) is None
    module.tmdb_movie_similar.assert_not_called()


def test_tmdb_media_similar_returns_none_for_unconvertible_media_id():
    module = TheMovieDbModule()
    module.tmdb_movie_similar = Mock(return_value=[{"id": 1}])

    assert module.media_similar(source=MediaSource.TMDB, media_id="abc") is None
    module.tmdb_movie_similar.assert_not_called()


def test_tmdb_media_similar_accepts_alias_string_source():
    module = TheMovieDbModule()
    module.tmdb_movie_similar = Mock(return_value=[{"id": 1}])

    result = module.media_similar(source="tmdb", media_id=550)

    assert result == [{"id": 1}]
    module.tmdb_movie_similar.assert_called_once_with(tmdbid=550)


def test_tmdb_media_similar_delegates_instead_of_reimplementing():
    module = TheMovieDbModule()
    module.tmdb_movie_similar = Mock(return_value=[{"id": 99}])

    result = module.media_similar(source=MediaSource.TMDB, media_id=550)

    assert result == [{"id": 99}]
    module.tmdb_movie_similar.assert_called_once()


def test_async_tmdb_media_similar_delegates_instead_of_reimplementing():
    module = TheMovieDbModule()
    module.async_tmdb_movie_similar = AsyncMock(return_value=[{"id": 99}])

    result = asyncio.run(module.async_media_similar(source=MediaSource.TMDB, media_id=550))

    assert result == [{"id": 99}]
    module.async_tmdb_movie_similar.assert_called_once()


# ---------------------------------------------------------------------------
# media_similar 能力契约登记
# ---------------------------------------------------------------------------

def test_media_similar_is_registered_as_media_metadata_family():
    assert get_module_method_contract("media_similar").family == "media-metadata"
    assert get_module_method_contract("async_media_similar").family == "media-metadata"


def test_media_similar_declares_its_multi_source_protocol_with_only_tmdb_source():
    contract = get_multi_source_contract("media_similar")

    assert contract is not None
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"source"}
    assert "首个非空答案" in contract.arbitration
    # 只有 TMDB 与插件自认领，不写成四源
    assert len(contract.sources) == 2
    assert any("TMDB" in source for source in contract.sources)
    assert not any("豆瓣" in source for source in contract.sources)
    assert not any("Bangumi" in source for source in contract.sources)
    assert not any("AniList" in source for source in contract.sources)
