"""person_detail / person_credits 契约的归一行为测试。

覆盖 TMDB、豆瓣、Bangumi、AniList 四个提供者：非本来源让出、参数按各自签名下传、
契约方法委托原实现而非重新实现、以及原方法的限流装饰器未被新契约方法的插入位置破坏。
"""
from unittest.mock import AsyncMock, Mock

from app.modules.anilist import AniListModule
from app.modules.bangumi import BangumiModule
from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule
from app.runtime.extensions.contract.module_method import (
    get_module_method_contract,
    get_multi_source_contract,
)
from app.schemas.types import MediaSource


# ---------------------------------------------------------------------------
# Bangumi：person_credits 不下传 page/count，传了会 TypeError
# ---------------------------------------------------------------------------

def test_bangumi_person_credits_does_not_forward_page_or_count():
    """Bangumi 契约必须只下传 person_id，不能下传 page/count。"""
    module = BangumiModule()
    module.bangumi_person_credits = Mock(return_value=[{"id": 1}])

    result = module.person_credits(
        source=MediaSource.Bangumi, person_id=42, page=3, count=10
    )

    assert result == [{"id": 1}]
    module.bangumi_person_credits.assert_called_once_with(person_id=42)


def test_async_bangumi_person_credits_does_not_forward_page_or_count():
    """异步 Bangumi 契约同样只下传 person_id。"""
    import asyncio

    module = BangumiModule()
    module.async_bangumi_person_credits = AsyncMock(return_value=[{"id": 1}])

    result = asyncio.run(
        module.async_person_credits(
            source=MediaSource.Bangumi, person_id=42, page=3, count=10
        )
    )

    assert result == [{"id": 1}]
    module.async_bangumi_person_credits.assert_called_once_with(person_id=42)


# ---------------------------------------------------------------------------
# AniList：count=None 时底层收到本源缺省值 20
# ---------------------------------------------------------------------------

def test_anilist_person_credits_defaults_missing_count_to_twenty():
    """AniList 契约收到 count=None 时必须把本源缺省值 20 下传，而不是 None。"""
    module = AniListModule()
    module.anilist_person_credits = Mock(return_value=[])

    module.person_credits(source=MediaSource.AniList, person_id=7, page=2, count=None)

    module.anilist_person_credits.assert_called_once_with(person_id=7, page=2, count=20)


def test_async_anilist_person_credits_defaults_missing_count_to_twenty():
    """异步 AniList 契约同样把 count=None 转换为 20。"""
    import asyncio

    module = AniListModule()
    module.async_anilist_person_credits = AsyncMock(return_value=[])

    asyncio.run(
        module.async_person_credits(source=MediaSource.AniList, person_id=7, page=2, count=None)
    )

    module.async_anilist_person_credits.assert_called_once_with(person_id=7, page=2, count=20)


def test_anilist_person_credits_forwards_explicit_count():
    """AniList 契约显式传入 count 时原样下传。"""
    module = AniListModule()
    module.anilist_person_credits = Mock(return_value=[])

    module.person_credits(source=MediaSource.AniList, person_id=7, page=1, count=5)

    module.anilist_person_credits.assert_called_once_with(person_id=7, page=1, count=5)


# ---------------------------------------------------------------------------
# TMDB / 豆瓣：count 不是本源签名的一部分，必须就地丢弃
# ---------------------------------------------------------------------------

def test_tmdb_person_credits_drops_unsupported_count():
    """TMDB 契约收到 count 时必须丢弃，不下传给 tmdb_person_credits。"""
    module = TheMovieDbModule()
    module.tmdb_person_credits = Mock(return_value=[{"id": 1}])

    result = module.person_credits(source=MediaSource.TMDB, person_id=1, page=2, count=99)

    assert result == [{"id": 1}]
    module.tmdb_person_credits.assert_called_once_with(person_id=1, page=2)
    _, kwargs = module.tmdb_person_credits.call_args
    assert "count" not in kwargs


def test_douban_person_credits_drops_unsupported_count():
    """豆瓣契约收到 count 时必须丢弃，不下传给 douban_person_credits。"""
    module = DoubanModule()
    module.douban_person_credits = Mock(return_value=[{"id": 2}])

    result = module.person_credits(source=MediaSource.Douban, person_id=2, page=1, count=99)

    assert result == [{"id": 2}]
    module.douban_person_credits.assert_called_once_with(person_id=2, page=1)
    _, kwargs = module.douban_person_credits.call_args
    assert "count" not in kwargs


# ---------------------------------------------------------------------------
# 非本来源必须返回 None（而非 falsy 的 []）
# ---------------------------------------------------------------------------

def test_tmdb_person_detail_returns_none_for_other_source():
    module = TheMovieDbModule()
    module.tmdb_person_detail = Mock(return_value={"id": 1})

    result = module.person_detail(source=MediaSource.Douban, person_id=1)

    assert result is None
    module.tmdb_person_detail.assert_not_called()


def test_tmdb_person_credits_returns_none_for_other_source():
    module = TheMovieDbModule()
    module.tmdb_person_credits = Mock(return_value=[{"id": 1}])

    result = module.person_credits(source=MediaSource.Douban, person_id=1)

    assert result is None
    module.tmdb_person_credits.assert_not_called()


def test_douban_person_detail_returns_none_for_other_source():
    module = DoubanModule()
    module.douban_person_detail = Mock(return_value={"id": 2})

    result = module.person_detail(source=MediaSource.TMDB, person_id=1)

    assert result is None
    module.douban_person_detail.assert_not_called()


def test_douban_person_credits_returns_none_for_other_source():
    module = DoubanModule()
    module.douban_person_credits = Mock(return_value=[{"id": 2}])

    result = module.person_credits(source=MediaSource.TMDB, person_id=1)

    assert result is None
    module.douban_person_credits.assert_not_called()


def test_bangumi_person_detail_returns_none_for_other_source():
    module = BangumiModule()
    module.bangumi_person_detail = Mock(return_value={"id": 3})

    result = module.person_detail(source=MediaSource.TMDB, person_id=1)

    assert result is None
    module.bangumi_person_detail.assert_not_called()


def test_bangumi_person_credits_returns_none_for_other_source():
    module = BangumiModule()
    module.bangumi_person_credits = Mock(return_value=[{"id": 3}])

    result = module.person_credits(source=MediaSource.TMDB, person_id=1)

    assert result is None
    module.bangumi_person_credits.assert_not_called()


def test_anilist_person_detail_returns_none_for_other_source():
    module = AniListModule()
    module.anilist_person_detail = Mock(return_value={"id": 4})

    result = module.person_detail(source=MediaSource.TMDB, person_id=1)

    assert result is None
    module.anilist_person_detail.assert_not_called()


def test_anilist_person_credits_returns_none_for_other_source():
    module = AniListModule()
    module.anilist_person_credits = Mock(return_value=[{"id": 4}])

    result = module.person_credits(source=MediaSource.TMDB, person_id=1)

    assert result is None
    module.anilist_person_credits.assert_not_called()


def test_tmdb_person_detail_returns_none_when_source_missing():
    """未指定来源时 TMDB 契约必须让出，返回 None。"""
    module = TheMovieDbModule()
    module.tmdb_person_detail = Mock(return_value={"id": 1})

    assert module.person_detail(person_id=1) is None


# ---------------------------------------------------------------------------
# 别名字符串来源（如 "tmdb"）必须能命中，裸 != 比较对别名不成立
# ---------------------------------------------------------------------------

def test_tmdb_person_detail_accepts_alias_string_source():
    module = TheMovieDbModule()
    module.tmdb_person_detail = Mock(return_value={"id": 1})

    result = module.person_detail(source="tmdb", person_id=1)

    assert result == {"id": 1}
    module.tmdb_person_detail.assert_called_once_with(person_id=1)


def test_douban_person_detail_accepts_alias_string_source():
    module = DoubanModule()
    module.douban_person_detail = Mock(return_value={"id": 2})

    result = module.person_detail(source="douban", person_id=1)

    assert result == {"id": 2}
    module.douban_person_detail.assert_called_once_with(person_id=1)


def test_bangumi_person_detail_accepts_alias_string_source():
    module = BangumiModule()
    module.bangumi_person_detail = Mock(return_value={"id": 3})

    result = module.person_detail(source="bangumi", person_id=1)

    assert result == {"id": 3}
    module.bangumi_person_detail.assert_called_once_with(person_id=1)


def test_anilist_person_detail_accepts_alias_string_source():
    module = AniListModule()
    module.anilist_person_detail = Mock(return_value={"id": 4})

    result = module.person_detail(source="anilist", person_id=1)

    assert result == {"id": 4}
    module.anilist_person_detail.assert_called_once_with(person_id=1)


# ---------------------------------------------------------------------------
# 契约是委托而非重新实现
# ---------------------------------------------------------------------------

def test_tmdb_person_detail_delegates_instead_of_reimplementing():
    module = TheMovieDbModule()
    module.tmdb_person_detail = Mock(return_value={"id": 99})

    result = module.person_detail(source=MediaSource.TMDB, person_id=1)

    assert result == {"id": 99}
    module.tmdb_person_detail.assert_called_once()


def test_douban_person_credits_delegates_instead_of_reimplementing():
    module = DoubanModule()
    module.douban_person_credits = Mock(return_value=[{"id": 98}])

    result = module.person_credits(source=MediaSource.Douban, person_id=1)

    assert result == [{"id": 98}]
    module.douban_person_credits.assert_called_once()


def test_bangumi_person_detail_delegates_instead_of_reimplementing():
    module = BangumiModule()
    module.bangumi_person_detail = Mock(return_value={"id": 97})

    result = module.person_detail(source=MediaSource.Bangumi, person_id=1)

    assert result == {"id": 97}
    module.bangumi_person_detail.assert_called_once()


def test_anilist_person_credits_delegates_instead_of_reimplementing():
    module = AniListModule()
    module.anilist_person_credits = Mock(return_value=[{"id": 96}])

    result = module.person_credits(source=MediaSource.AniList, person_id=1)

    assert result == [{"id": 96}]
    module.anilist_person_credits.assert_called_once()


# ---------------------------------------------------------------------------
# 装饰器未被劫持：douban_info / match_doubaninfo 的限流装饰器必须仍紧邻其 def，
# 未被新插入的 person_detail/person_credits 契约方法隔开
# ---------------------------------------------------------------------------

def _unwrap_retry(fn):
    """取出 retry 装饰器闭包中真正被限流装饰器包裹的函数。"""
    index = fn.__code__.co_freevars.index("f")
    return fn.__closure__[index].cell_contents


def test_douban_info_retains_rate_limit_decorator():
    """douban_info 必须仍被 @rate_limit_exponential 包裹（functools.wraps 留下 __wrapped__）。"""
    assert hasattr(DoubanModule.douban_info, "__wrapped__")


def test_async_douban_info_retains_rate_limit_decorator():
    """async_douban_info 必须仍被 @rate_limit_exponential 包裹。"""
    assert hasattr(DoubanModule.async_douban_info, "__wrapped__")


def test_douban_match_doubaninfo_retains_rate_limit_and_retry_decorators():
    """match_doubaninfo 必须仍被 @retry 与 @rate_limit_exponential 包裹。"""
    fn = DoubanModule.match_doubaninfo
    assert fn.__qualname__.endswith("f_retry")
    rate_limited = _unwrap_retry(fn)
    assert hasattr(rate_limited, "__wrapped__")


def test_async_douban_match_doubaninfo_retains_rate_limit_and_retry_decorators():
    """async_match_doubaninfo 必须仍被 @retry 与 @rate_limit_exponential 包裹。"""
    fn = DoubanModule.async_match_doubaninfo
    assert fn.__qualname__.endswith("async_f_retry")
    rate_limited = _unwrap_retry(fn)
    assert hasattr(rate_limited, "__wrapped__")


# ---------------------------------------------------------------------------
# 能力契约登记
# ---------------------------------------------------------------------------

def test_person_detail_is_registered_as_media_metadata_family():
    assert get_module_method_contract("person_detail").family == "media-metadata"
    assert get_module_method_contract("async_person_detail").family == "media-metadata"


def test_person_credits_is_registered_as_media_metadata_family():
    assert get_module_method_contract("person_credits").family == "media-metadata"
    assert get_module_method_contract("async_person_credits").family == "media-metadata"


def test_person_detail_declares_its_multi_source_protocol():
    contract = get_multi_source_contract("person_detail")

    assert contract is not None
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"source"}
    assert "首个非空答案" in contract.arbitration


def test_person_credits_declares_its_multi_source_protocol():
    contract = get_multi_source_contract("person_credits")

    assert contract is not None
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"source"}
    assert "首个非空答案" in contract.arbitration
