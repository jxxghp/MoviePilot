"""match_media 契约的归一行为测试。

覆盖 TMDB 与豆瓣两个提供者：非本来源让出、参数按各自签名下传、
契约方法委托原实现而非重新实现、以及 match_doubaninfo 的限流/重试装饰器
未被新契约方法的插入位置破坏。
"""
from unittest.mock import AsyncMock, Mock

from app.modules.douban import DoubanModule
from app.modules.themoviedb import TheMovieDbModule
from app.runtime.extensions.contract.module_method import (
    get_module_method_contract,
    get_multi_source_contract,
)
from app.schemas.types import MediaSource, MediaType


# ---------------------------------------------------------------------------
# TMDB：imdbid/raise_exception 本源不支持，必须就地丢弃
# ---------------------------------------------------------------------------

def test_tmdb_match_media_drops_unsupported_kwargs():
    """TMDB 契约收到 imdbid/raise_exception 时必须丢弃，不下传给 match_tmdbinfo。"""
    module = TheMovieDbModule()
    module.match_tmdbinfo = Mock(return_value={"id": 1})

    result = module.match_media(
        source=MediaSource.TMDB,
        name="Foo",
        mtype=MediaType.MOVIE,
        year="2024",
        season=1,
        imdbid="tt123",
        raise_exception=True,
    )

    assert result == {"id": 1}
    module.match_tmdbinfo.assert_called_once_with(
        name="Foo", mtype=MediaType.MOVIE, year="2024", season=1
    )


def test_async_tmdb_match_media_drops_unsupported_kwargs():
    """异步 TMDB 契约同样必须丢弃 imdbid/raise_exception。"""
    import asyncio

    module = TheMovieDbModule()
    module.async_match_tmdbinfo = AsyncMock(return_value={"id": 1})

    result = asyncio.run(
        module.async_match_media(
            source=MediaSource.TMDB,
            name="Foo",
            mtype=MediaType.MOVIE,
            year="2024",
            season=1,
            imdbid="tt123",
            raise_exception=True,
        )
    )

    assert result == {"id": 1}
    module.async_match_tmdbinfo.assert_called_once_with(
        name="Foo", mtype=MediaType.MOVIE, year="2024", season=1
    )


# ---------------------------------------------------------------------------
# 豆瓣：六个参数全部下传
# ---------------------------------------------------------------------------

def test_douban_match_media_forwards_all_kwargs():
    """豆瓣契约把 name/imdbid/mtype/year/season/raise_exception 六个参数全部下传。"""
    module = DoubanModule()
    module.match_doubaninfo = Mock(return_value={"id": 2})

    result = module.match_media(
        source=MediaSource.Douban,
        name="Foo",
        mtype=MediaType.TV,
        year="2023",
        season=2,
        imdbid="tt456",
        raise_exception=True,
    )

    assert result == {"id": 2}
    module.match_doubaninfo.assert_called_once_with(
        name="Foo", imdbid="tt456", mtype=MediaType.TV, year="2023",
        season=2, raise_exception=True,
    )


def test_async_douban_match_media_forwards_all_kwargs():
    """异步豆瓣契约同样把六个参数全部下传。"""
    import asyncio

    module = DoubanModule()
    module.async_match_doubaninfo = AsyncMock(return_value={"id": 2})

    result = asyncio.run(
        module.async_match_media(
            source=MediaSource.Douban,
            name="Foo",
            mtype=MediaType.TV,
            year="2023",
            season=2,
            imdbid="tt456",
            raise_exception=True,
        )
    )

    assert result == {"id": 2}
    module.async_match_doubaninfo.assert_called_once_with(
        name="Foo", imdbid="tt456", mtype=MediaType.TV, year="2023",
        season=2, raise_exception=True,
    )


def test_douban_match_media_default_raise_exception_is_false():
    """契约缺省 raise_exception 为 False，未显式传入时按 False 下传。"""
    module = DoubanModule()
    module.match_doubaninfo = Mock(return_value={"id": 3})

    module.match_media(source=MediaSource.Douban, name="Foo")

    module.match_doubaninfo.assert_called_once_with(
        name="Foo", imdbid=None, mtype=None, year=None, season=None,
        raise_exception=False,
    )


# ---------------------------------------------------------------------------
# 非本来源必须返回 None（而非 falsy 的 []/{}/False）
# ---------------------------------------------------------------------------

def test_tmdb_match_media_returns_none_for_other_source():
    """TMDB 契约对非本来源必须返回 None，不能返回其他 falsy 值。"""
    module = TheMovieDbModule()
    module.match_tmdbinfo = Mock(return_value={"id": 1})

    result = module.match_media(source=MediaSource.Douban, name="Foo")

    assert result is None
    module.match_tmdbinfo.assert_not_called()


def test_douban_match_media_returns_none_for_other_source():
    """豆瓣契约对非本来源必须返回 None，不能返回其他 falsy 值。"""
    module = DoubanModule()
    module.match_doubaninfo = Mock(return_value={"id": 2})

    result = module.match_media(source=MediaSource.TMDB, name="Foo")

    assert result is None
    module.match_doubaninfo.assert_not_called()


def test_tmdb_match_media_returns_none_when_source_missing():
    """未指定来源时 TMDB 契约必须让出，返回 None。"""
    module = TheMovieDbModule()
    module.match_tmdbinfo = Mock(return_value={"id": 1})

    assert module.match_media(name="Foo") is None


# ---------------------------------------------------------------------------
# 别名字符串来源（如 "tmdb"）必须能命中，裸 != 比较对别名不成立
# ---------------------------------------------------------------------------

def test_tmdb_match_media_accepts_alias_string_source():
    """字符串别名 "tmdb" 必须规范化为 MediaSource.TMDB 后命中。"""
    module = TheMovieDbModule()
    module.match_tmdbinfo = Mock(return_value={"id": 1})

    result = module.match_media(source="tmdb", name="Foo")

    assert result == {"id": 1}
    module.match_tmdbinfo.assert_called_once_with(
        name="Foo", mtype=None, year=None, season=None
    )


def test_douban_match_media_accepts_alias_string_source():
    """字符串别名 "douban" 必须规范化为 MediaSource.Douban 后命中。"""
    module = DoubanModule()
    module.match_doubaninfo = Mock(return_value={"id": 2})

    result = module.match_media(source="douban", name="Foo")

    assert result == {"id": 2}


# ---------------------------------------------------------------------------
# 装饰器未被劫持：match_doubaninfo 的 @retry + @rate_limit_exponential
# 必须仍然紧邻其函数定义，不能被新插入的契约方法隔开
# ---------------------------------------------------------------------------

def _unwrap_retry(fn):
    """取出 retry 装饰器闭包中真正被限流装饰器包裹的函数。"""
    index = fn.__code__.co_freevars.index("f")
    return fn.__closure__[index].cell_contents


def test_douban_match_doubaninfo_retains_rate_limit_and_retry_decorators():
    """
    match_doubaninfo 必须仍被 @retry 与 @rate_limit_exponential 包裹。

    retry() 返回的包装函数不使用 functools.wraps，因此其 __qualname__ 固定
    落在 retry.<locals>.deco_retry.<locals>.f_retry；一旦新契约方法被插进
    装饰器与 def 之间，match_doubaninfo 会变回未装饰的原始函数，
    __qualname__ 会变成 "DoubanModule.match_doubaninfo"。
    rate_limit_exponential 的包装函数使用了 functools.wraps，因此其
    __wrapped__ 属性可用于确认限流装饰器仍在生效。
    """
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
# 契约是委托而非重新实现
# ---------------------------------------------------------------------------

def test_tmdb_match_media_delegates_instead_of_reimplementing():
    """mock 掉 match_tmdbinfo 后，契约必须调用它而不是自行实现匹配逻辑。"""
    module = TheMovieDbModule()
    module.match_tmdbinfo = Mock(return_value={"id": 99})

    result = module.match_media(source=MediaSource.TMDB, name="Foo")

    assert result == {"id": 99}
    module.match_tmdbinfo.assert_called_once()


def test_douban_match_media_delegates_instead_of_reimplementing():
    """mock 掉 match_doubaninfo 后，契约必须调用它而不是自行实现匹配逻辑。"""
    module = DoubanModule()
    module.match_doubaninfo = Mock(return_value={"id": 98})

    result = module.match_media(source=MediaSource.Douban, name="Foo")

    assert result == {"id": 98}
    module.match_doubaninfo.assert_called_once()


# ---------------------------------------------------------------------------
# 能力契约登记
# ---------------------------------------------------------------------------

def test_match_media_is_registered_as_media_metadata_family():
    """match_media 与 async_match_media 都必须显式登记为 media-metadata 能力族。"""
    assert get_module_method_contract("match_media").family == "media-metadata"
    assert get_module_method_contract("async_match_media").family == "media-metadata"


def test_match_media_declares_its_multi_source_protocol():
    """match_media 必须登记多来源应答协议：让出方式、收窄键与仲裁规则。"""
    contract = get_multi_source_contract("match_media")

    assert contract is not None
    assert "None" in contract.abstain
    assert dict(contract.narrowing).keys() == {"source"}
    assert "首个非空答案" in contract.arbitration
