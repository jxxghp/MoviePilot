"""
TMDB 回退匹配的年份软校验回归测试（#7504）。

Issue 背景：标题以纯数字结尾的作品（``Example.Show.0.S01.2018``）经解析层丢掉
尾数字后只剩 ``Example Show``；电视剧分支的第一步（带年份 + 季信息）找不到可用
条目后，回退步骤完全不带年份，于是同名前作 ``tv/42509``（2011）仅凭标题/别名
匹配就胜出，而 ``meta.year`` 明明是 2018。2018 年的正片 ``tv/78102`` 原名是日文，
带结尾数字的英文名只是别名，直接标题匹配不到它。

本文件用离线 mock 复现该场景：搜索返回同时包含两部作品的响应集，详情按
``get_info`` 的真实返回结构补 ``names``。断言分两层：

- 排序降权：年份相符的候选（标题或别名匹配）应胜过年份不符的标题直达候选；
- 可见标注：年份不符的候选最终胜出时，识别日志必须显式警告，而不是静默成功。

解析层（``moviepilot_rust`` 是外部编译依赖）不在本仓修复范围，本文件不覆盖它。
"""

import asyncio
import logging
from copy import deepcopy
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.runtime.log import LoggerManager
from app.schemas.types import MediaType


@pytest.fixture
def captured_tmdb_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """
    把 caplog 处理器挂到 themoviedb 控制台日志器上。

    该日志器 ``propagate=False``，根日志器上的 caplog 处理器收不到它的记录，
    须按 ``test_log_exception.py`` 的做法直接挂处理器。
    """
    console_logger = LoggerManager._get_console_logger(LoggerManager._default_log_file)
    console_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=console_logger.name):
            yield caplog
    finally:
        console_logger.removeHandler(caplog.handler)

_QUERY = "Example Show"
_META_YEAR = "2018"

# 搜索列表里的两部同名作品：前作标题正好等于查询名，正片原名是日文。
_PREQUEL_SEARCH = {
    "id": 42509,
    "name": "Example Show",
    "original_name": "Example Show",
    "first_air_date": "2011-04-01",
    "media_type": "tv",
}
_SEQUEL_SEARCH = {
    "id": 78102,
    "name": "サンプル番組0",
    "original_name": "サンプル番組0",
    "first_air_date": "2018-01-05",
    "media_type": "tv",
}


def _details(sequel_aliases: list) -> dict:
    """
    按 ``get_info`` 的真实返回结构构造两部作品的详情。

    :param sequel_aliases: 正片详情里的别名清单；是否含查询名决定降权后谁胜出
    """
    return {
        42509: {
            "id": 42509,
            "name": "Example Show",
            "original_name": "Example Show",
            "first_air_date": "2011-04-01",
            "media_type": "tv",
            "names": ["Example Show"],
            "genres": [{"id": 18}],
            "seasons": [{"season_number": 1, "air_date": "2011-04-01"}],
        },
        78102: {
            "id": 78102,
            "name": "サンプル番組0",
            "original_name": "サンプル番組0",
            "first_air_date": "2018-01-05",
            "media_type": "tv",
            "names": list(sequel_aliases),
            "genres": [{"id": 18}],
            "seasons": [{"season_number": 1, "air_date": "2018-01-05"}],
        },
    }


def _search_tv_shows(details: dict) -> Mock:
    """
    模拟 TMDB 电视剧搜索：不带年份返回两部作品，带年份只返回该年作品。
    """
    def _side_effect(term: str, release_year: Optional[str] = None) -> list:
        assert term == _QUERY
        if release_year is None:
            return deepcopy([_PREQUEL_SEARCH, _SEQUEL_SEARCH])
        return deepcopy([_SEQUEL_SEARCH]) if release_year == _META_YEAR else []

    return Mock(side_effect=_side_effect)


async def _async_search_tv_shows(term: str, release_year: Optional[str] = None) -> list:
    """
    同步搜索替身的异步版本。
    """
    if release_year is None:
        return deepcopy([_PREQUEL_SEARCH, _SEQUEL_SEARCH])
    return deepcopy([_SEQUEL_SEARCH]) if release_year == _META_YEAR else []


def _api(sequel_aliases: list) -> TmdbApi:
    """
    构造不初始化真实 TMDB 客户端的最小 API 实例。
    """
    details = _details(sequel_aliases)
    api = object.__new__(TmdbApi)
    api.tmdb = SimpleNamespace(language="en")
    api.search = SimpleNamespace(
        total_results=2,
        movies=Mock(),
        async_movies=AsyncMock(),
        tv_shows=_search_tv_shows(details),
        async_tv_shows=AsyncMock(side_effect=_async_search_tv_shows),
        multi=Mock(),
        async_multi=AsyncMock(),
        collections=Mock(),
        async_collections=AsyncMock(),
    )
    api.movie = SimpleNamespace(details=Mock(), async_details=AsyncMock())
    api.tv = SimpleNamespace(details=Mock(), async_details=AsyncMock())
    api.get_info = Mock(side_effect=lambda mtype, tmdbid: deepcopy(details[tmdbid]))
    api.async_get_info = AsyncMock(side_effect=lambda mtype, tmdbid: deepcopy(details[tmdbid]))
    return api


def _meta() -> SimpleNamespace:
    """
    构造 issue 场景的解析元数据：电视剧、2018 年、第 1 季。
    """
    return SimpleNamespace(
        type=MediaType.TV,
        year=_META_YEAR,
        begin_season=1,
        name=_QUERY,
        cn_name=None,
        en_name=_QUERY,
    )


def _module(api: TmdbApi) -> TheMovieDbModule:
    """
    构造只挂载给定 TMDB 客户端的模块实例，不初始化真实缓存与客户端。
    """
    module = object.__new__(TheMovieDbModule)
    module._tmdb = api
    return module


@pytest.mark.parametrize("asynchronous", [False, True])
def test_soft_year_prefers_year_matching_alias_over_mismatched_direct(asynchronous: bool) -> None:
    """
    回退搜索带软年份时，年份相符的别名候选应胜过年份不符的标题直达候选。

    未修复前：标题直达短路直接返回 2011 年前作，年份相符的 2018 年正片
    连别名比对的机会都没有。
    """
    api = _api(["Example Show 0", "Example Show"])
    if asynchronous:
        result = asyncio.run(api.async_match(_QUERY, MediaType.TV, soft_year=_META_YEAR))
    else:
        result = api.match(_QUERY, MediaType.TV, soft_year=_META_YEAR)
    assert result is not None
    assert result["id"] == 78102


@pytest.mark.parametrize("asynchronous", [False, True])
def test_soft_year_keeps_mismatched_match_when_no_year_match_exists(asynchronous: bool) -> None:
    """
    没有任何年份相符的候选时，回退仍应返回年份不符的标题直达候选，不丢失召回。
    """
    api = _api(["Example Show 0"])
    if asynchronous:
        result = asyncio.run(api.async_match(_QUERY, MediaType.TV, soft_year=_META_YEAR))
    else:
        result = api.match(_QUERY, MediaType.TV, soft_year=_META_YEAR)
    assert result is not None
    assert result["id"] == 42509


def test_no_soft_year_keeps_first_title_match() -> None:
    """
    不传软年份时保持原有行为：标题直达候选直接胜出，不额外查详情。

    对照用例：证明降权只在调用方给出软年份时才生效。
    """
    api = _api(["Example Show 0", "Example Show"])
    result = api.match(_QUERY, MediaType.TV)
    assert result is not None
    assert result["id"] == 42509
    api.get_info.assert_not_called()


def test_build_match_plan_tv_fallback_carries_soft_year() -> None:
    """
    电视剧分支的回退步骤应携带软年份，但不把它当作 TMDB 查询过滤条件。
    """
    plan = TheMovieDbModule._build_match_plan(_QUERY, _meta())
    assert len(plan) == 2
    fallback = plan[1]
    assert fallback.soft_year == _META_YEAR
    assert fallback.include_year is False
    assert fallback.year is None
    assert fallback.mtype is MediaType.TV
    yearless_meta = _meta()
    yearless_meta.year = None
    yearless_plan = TheMovieDbModule._build_match_plan(_QUERY, yearless_meta)
    assert yearless_plan[1].soft_year is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_search_by_name_warns_on_year_mismatch(
    asynchronous: bool, captured_tmdb_logs: pytest.LogCaptureFixture
) -> None:
    """
    年份不符的候选最终胜出时，识别日志必须显式警告元数据年份与结果年份。

    用 issue 的真实数据形状：正片别名里没有去掉结尾数字的写法，第一步
    （年份 + 季信息）找不到它，回退只能返回 2011 年前作——但不得静默成功。
    """
    api = _api(["Example Show 0"])
    module = _module(api)
    if asynchronous:
        result = asyncio.run(module._async_search_by_name(_QUERY, _meta(), []))
    else:
        result = module._search_by_name(_QUERY, _meta(), [])
    assert result is not None
    assert result["id"] == 42509
    assert "2018" in captured_tmdb_logs.text
    assert "2011" in captured_tmdb_logs.text


@pytest.mark.parametrize("asynchronous", [False, True])
def test_search_by_name_quiet_when_year_matches(
    asynchronous: bool, captured_tmdb_logs: pytest.LogCaptureFixture
) -> None:
    """
    年份相符的候选胜出时不应产生年份不符警告（对照用例）。
    """
    api = _api(["Example Show 0", "Example Show"])
    module = _module(api)
    if asynchronous:
        result = asyncio.run(module._async_search_by_name(_QUERY, _meta(), []))
    else:
        result = module._search_by_name(_QUERY, _meta(), [])
    assert result is not None
    assert result["id"] == 78102
    assert "年份不符" not in captured_tmdb_logs.text
