# -*- coding: utf-8 -*-
"""
覆盖TMDB识别链路两个已核实缺陷的回归测试。

§6.1 零退避重试：
    同步 `TMDb.request` 在连接失败重建会话后立即重试，两次尝试间隔为0；
    异步 `TMDb.async_request` 遇到连接失败时完全没有重试。
    NAS+FUSE网盘环境下TMDB连接偶发的都是数秒内可自愈的瞬时抖动，零退避/不重试
    基本无法覆盖这类抖动窗口。

§6.2 网络故障与"条目不存在"文案混淆：
    `TmdbApi.__get_movie_detail`/`__get_tv_detail` 对所有异常（含真实网络故障与
    TMDB返回的404"资源不存在"）一律 `except Exception: return None`，两类完全不同
    性质的失败被折叠成同一个 None；`TheMovieDbModule._get_info_by_tmdbid` 又用
    `info_tv or info_movie or None` 把结果进一步折叠，最终识别失败统一报
    "无法确定媒体类型，识别失败"，用户无法判断该等网络恢复还是该确认条目不存在。
"""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

import app.modules.themoviedb as themoviedb_module
from app.core.metainfo import MetaInfo
from app.modules.themoviedb import TheMovieDbModule
from app.modules.themoviedb.tmdb_cache import TmdbCache
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.modules.themoviedb.tmdbv3api import tmdb as tmdb_module
from app.modules.themoviedb.tmdbv3api.exceptions import TMDbConnectionError, TMDbException
from app.modules.themoviedb.tmdbv3api.tmdb import TMDb
from app.schemas.types import MediaType


class _FakeResponse:
    """测试用响应对象，模拟 requests/httpx 响应的最小接口。"""

    def __init__(self, payload, headers: dict = None):
        """初始化响应内容。"""
        self._payload = payload
        self.headers = headers or {}
        self.status_code = 200
        self.text = ""

    def json(self):
        """返回预置JSON内容。"""
        return self._payload


# ---------------------------------------------------------------------------
# §6.1 零退避重试
# ---------------------------------------------------------------------------

def test_tmdb_connection_error_is_tmdb_exception_subclass():
    """
    TMDbConnectionError必须是TMDbException的子类，
    否则现有 `except TMDbException` 代码路径会因新增异常类型而失效。
    """
    assert issubclass(TMDbConnectionError, TMDbException)


def test_request_sleeps_before_retry_after_connection_failure(monkeypatch):
    """
    同步请求失败后，重建会话重试前应等待 RETRY_BACKOFF_SECONDS 秒，
    而不是零间隔立即重试。
    """
    tmdb = TMDb()
    response = _FakeResponse(payload={"id": 1})
    request_results = [None, response]
    tmdb._request_once = lambda method, url, data, json: request_results.pop(0)

    sleep_calls = []
    monkeypatch.setattr(tmdb_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    result = TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)

    assert result["json"] == {"id": 1}
    assert sleep_calls == [tmdb_module.RETRY_BACKOFF_SECONDS]
    assert 1 <= tmdb_module.RETRY_BACKOFF_SECONDS <= 3


def test_request_raises_connection_error_after_retry_exhausted(monkeypatch):
    """
    重试后依旧失败时，应抛出更具体的 TMDbConnectionError（而不仅是笼统的 TMDbException），
    供上层区分"网络故障"与TMDB业务层错误；对外仍保持"最终失败抛异常"的语义不变。
    """
    tmdb = TMDb()
    tmdb._request_once = lambda method, url, data, json: None
    monkeypatch.setattr(tmdb_module.time, "sleep", lambda seconds: None)

    with pytest.raises(TMDbConnectionError, match="无法连接TheMovieDb"):
        TMDb.request.__wrapped__(tmdb, "GET", "https://example.com", None, None)


def test_async_request_retries_once_after_connection_failure(monkeypatch):
    """
    异步请求当前完全没有重试；修复后首次失败应等待 RETRY_BACKOFF_SECONDS 秒
    后重试一次，重试成功则返回正常结果。
    """
    tmdb = TMDb()
    response = _FakeResponse(payload={"id": 2})
    outcomes = [None, response]

    async def _fake_once(method, url, data, json):
        return outcomes.pop(0)

    tmdb._async_request_once = _fake_once

    sleep_calls = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(tmdb_module.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(
        TMDb.async_request.__wrapped__(tmdb, "GET", "https://example.com", None, None)
    )

    assert result["json"] == {"id": 2}
    assert sleep_calls == [tmdb_module.RETRY_BACKOFF_SECONDS]


def test_async_request_raises_connection_error_after_retry_exhausted(monkeypatch):
    """异步请求重试一次后仍失败，应抛出 TMDbConnectionError，保持最终失败语义。"""
    tmdb = TMDb()

    async def _fake_once(method, url, data, json):
        return None

    tmdb._async_request_once = _fake_once

    async def _fake_sleep(seconds):
        return None

    monkeypatch.setattr(tmdb_module.asyncio, "sleep", _fake_sleep)

    with pytest.raises(TMDbConnectionError, match="无法连接TheMovieDb"):
        asyncio.run(TMDb.async_request.__wrapped__(tmdb, "GET", "https://example.com", None, None))


def test_request_exception_path_is_not_cached(monkeypatch):
    """
    `request`/`async_request` 带 `@cached(skip_none=True)`；抛异常时函数没有
    正常返回值，缓存装饰器的 `cache_backend.set` 分支不会被执行到，天然不会缓存。
    这里通过真实缓存路径（不使用 __wrapped__）验证：连续两次同参数请求都会真实
    触发底层请求，而不是第二次直接命中一个"异常快照"。
    """
    tmdb = TMDb()
    call_count = {"n": 0}

    def _always_fail(method, url, data, json):
        call_count["n"] += 1
        return None

    tmdb._request_once = _always_fail
    tmdb._reset_owned_session = lambda: None
    monkeypatch.setattr(tmdb_module.time, "sleep", lambda seconds: None)

    for _ in range(2):
        with pytest.raises(TMDbConnectionError):
            tmdb.request("GET", "https://example.com/exc-not-cached", None, None)

    # 两次调用都真实触发了底层请求（各自都经历了一次重试），说明异常没有被当作
    # 缓存命中而跳过；即 2 次调用 × 2 次尝试 = 4 次底层请求。
    assert call_count["n"] == 4


# ---------------------------------------------------------------------------
# §6.2 网络故障与"条目不存在"文案混淆
# ---------------------------------------------------------------------------

def test_request_obj_raises_plain_tmdb_exception_for_not_found_status():
    """
    调研结论：TMDB对404"资源不存在"的响应体形如
    `{"success": false, "status_code": 34, "status_message": "..."}`，
    `TMDb._handle_errors` 据此抛出的是普通 TMDbException，而不是表示传输层失败的
    TMDbConnectionError。这与`request`/`async_request`在请求彻底失败（返回None）
    时抛出的 TMDbConnectionError是两类不同的异常，不应混淆处理。
    """
    tmdb = TMDb()
    snapshot = {
        TMDb._RESPONSE_SNAPSHOT_MARKER: True,
        "headers": {},
        "json": {
            "success": False,
            "status_code": 34,
            "status_message": "The resource you requested could not be found.",
        },
    }
    tmdb.request = lambda *args, **kwargs: snapshot

    with pytest.raises(TMDbException) as exc_info:
        tmdb._request_obj("/movie/999999999")
    assert not isinstance(exc_info.value, TMDbConnectionError)


def test_get_movie_detail_propagates_connection_error_when_requested():
    """
    `TmdbApi.get_info(..., raise_on_connection_error=True)` 遇到TMDB连接失败时
    应该把 TMDbConnectionError 传播出去，而不是像默认那样吞掉返回 None。
    """
    tmdb_api = TmdbApi()
    tmdb_api.movie.details = Mock(
        side_effect=TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
    )

    with pytest.raises(TMDbConnectionError):
        tmdb_api.get_info(mtype=MediaType.MOVIE, tmdbid=1, raise_on_connection_error=True)


def test_get_movie_detail_swallows_connection_error_by_default():
    """
    不传 `raise_on_connection_error` 时（默认False），行为必须与修复前完全一致：
    任何异常都吞掉返回 None，不破坏现有调用方。
    """
    tmdb_api = TmdbApi()
    tmdb_api.movie.details = Mock(
        side_effect=TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
    )

    assert tmdb_api.get_info(mtype=MediaType.MOVIE, tmdbid=1) is None


def test_get_tv_detail_not_found_error_still_returns_none_even_with_flag():
    """
    即使显式要求 `raise_on_connection_error=True`，TMDB业务层返回的"资源不存在"
    （普通TMDbException，非TMDbConnectionError）也不应被当作连接失败传播出去，
    应继续按现有语义返回 None。
    """
    tmdb_api = TmdbApi()
    tmdb_api.tv.details = Mock(
        side_effect=TMDbException("The resource you requested could not be found.")
    )

    result = tmdb_api.get_info(mtype=MediaType.TV, tmdbid=1, raise_on_connection_error=True)

    assert result is None


def test_async_get_info_propagates_connection_error_when_requested():
    """异步版本的详情查询同样要支持连接失败的显式传播。"""
    tmdb_api = TmdbApi()
    tmdb_api.movie.async_details = AsyncMock(
        side_effect=TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
    )

    with pytest.raises(TMDbConnectionError):
        asyncio.run(
            tmdb_api.async_get_info(mtype=MediaType.MOVIE, tmdbid=1, raise_on_connection_error=True)
        )


def test_async_get_info_swallows_connection_error_by_default():
    """异步默认行为同样必须保持向后兼容。"""
    tmdb_api = TmdbApi()
    tmdb_api.movie.async_details = AsyncMock(
        side_effect=TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
    )

    result = asyncio.run(tmdb_api.async_get_info(mtype=MediaType.MOVIE, tmdbid=1))

    assert result is None


class _FakeTmdbApi:
    """
    模拟 TmdbApi.get_info/async_get_info，用给定结果驱动
    `TheMovieDbModule._get_info_by_tmdbid` 的三种分支：确定命中、确认不存在、连接失败。
    """

    def __init__(self, tv_outcome, movie_outcome):
        """使用电视剧、电影两路各自的结果初始化。"""
        self.tv_outcome = tv_outcome
        self.movie_outcome = movie_outcome
        self.calls = []

    def _resolve(self, outcome, raise_on_connection_error):
        if isinstance(outcome, Exception):
            if raise_on_connection_error and isinstance(outcome, TMDbConnectionError):
                raise outcome
            return None
        return outcome

    def get_info(self, mtype, tmdbid, raise_on_connection_error=False):
        """同步查询：按mtype返回预置结果，忠实模拟 raise_on_connection_error 语义。"""
        self.calls.append((mtype, raise_on_connection_error))
        outcome = self.tv_outcome if mtype == MediaType.TV else self.movie_outcome
        return self._resolve(outcome, raise_on_connection_error)

    async def async_get_info(self, mtype, tmdbid, raise_on_connection_error=False):
        """异步查询：委托同步实现。"""
        return self.get_info(mtype, tmdbid, raise_on_connection_error=raise_on_connection_error)


class _CacheProbe:
    """记录 update 调用参数，get 恒返回未命中，用于验证网络故障路径不会触碰缓存。"""

    def __init__(self):
        """初始化调用记录列表。"""
        self.update_calls = []

    def get(self, meta):
        """始终返回缓存未命中。"""
        return {}

    def update(self, meta, info):
        """记录写入尝试，供测试断言从未被调用或调用了哪些内容。"""
        self.update_calls.append(info)


def _build_module(tv_outcome, movie_outcome) -> TheMovieDbModule:
    """构造绕开真实HTTP/缓存的 TheMovieDbModule 测试实例。"""
    module = TheMovieDbModule()
    module.tmdb = _FakeTmdbApi(tv_outcome, movie_outcome)
    module.cache = _CacheProbe()
    return module


def _build_meta(tmdbid: int = 98865) -> MetaInfo:
    """构造识别所需的最小元数据。"""
    meta = MetaInfo(title="测试标题")
    meta.tmdbid = tmdbid
    return meta


def test_get_info_by_tmdbid_raises_connection_error_when_both_types_fail_to_connect():
    """
    电影、电视剧两路查询都因TMDB连接失败而没有得到确定结果时，
    不能断言"条目不存在"，应向上抛出 TMDbConnectionError。
    """
    conn_err = TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
    module = _build_module(tv_outcome=conn_err, movie_outcome=conn_err)

    with pytest.raises(TMDbConnectionError):
        module._get_info_by_tmdbid(tmdbid=98865, mtype=None, meta=_build_meta())


def test_get_info_by_tmdbid_returns_none_when_both_confirmed_not_found():
    """
    电影、电视剧两路查询都明确返回"未查询到"（空dict，非连接异常）时，
    应保持原有"无法确定媒体类型"语义，返回 None 且不抛异常。
    """
    module = _build_module(tv_outcome={}, movie_outcome={})

    result = module._get_info_by_tmdbid(tmdbid=98865, mtype=None, meta=_build_meta())

    assert result is None


def test_get_info_by_tmdbid_prefers_positive_result_over_partial_connection_error():
    """
    电视剧一路连接失败，但电影一路查到了确定结果时，应直接返回电影结果，
    不能因为另一路的瞬时抖动就误判为整体失败。
    """
    conn_err = TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
    movie_info = {
        "id": 98865,
        "media_type": MediaType.MOVIE,
        "title": "测试电影",
        "release_date": "2020-01-01",
        "genres": [{"id": 28, "name": "动作"}],
    }
    module = _build_module(tv_outcome=conn_err, movie_outcome=movie_info)

    result = module._get_info_by_tmdbid(tmdbid=98865, mtype=None, meta=_build_meta())

    assert result is movie_info


def test_async_get_info_by_tmdbid_raises_connection_error_when_both_types_fail_to_connect():
    """异步版本同样要在两路都判定为连接失败时抛出 TMDbConnectionError。"""
    conn_err = TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
    module = _build_module(tv_outcome=conn_err, movie_outcome=conn_err)

    with pytest.raises(TMDbConnectionError):
        asyncio.run(
            module._async_get_info_by_tmdbid(tmdbid=98865, mtype=None, meta=_build_meta())
        )


def test_recognize_media_reports_network_error_message_and_skips_cache_on_connection_failure(
    monkeypatch,
):
    """
    端到端回归：复现因果链中的场景（tmdb_id:98865 双路查询均连接失败）。
    识别应返回 None，日志应给出明确的网络故障文案（而不是"无法确定媒体类型"），
    且绝不能写入任何缓存（正缓存、负缓存都不能写）。
    """
    conn_err = TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
    module = _build_module(tv_outcome=conn_err, movie_outcome=conn_err)

    mock_logger = Mock()
    monkeypatch.setattr(themoviedb_module, "logger", mock_logger)

    meta = MetaInfo(title="测试标题")
    result = module.recognize_media(meta=meta, tmdbid=98865, cache=True)

    assert result is None
    # 网络故障场景不写入任何缓存条目（正缓存/负缓存皆不写）
    assert module.cache.update_calls == []

    logged_messages = " ".join(
        str(call.args[0]) if call.args else "" for call in mock_logger.error.call_args_list
    )
    assert "连接TheMovieDb失败" in logged_messages or "连接 TheMovieDb 失败" in logged_messages
    assert "无法确定媒体类型" not in logged_messages


def test_recognize_media_keeps_not_found_message_when_both_types_confirmed_absent(monkeypatch):
    """
    电影、电视剧都确认查无此项（非连接失败）时，应维持原有的
    "无法确定媒体类型，识别失败"文案，不能被网络错误文案顶替。
    """
    module = _build_module(tv_outcome={}, movie_outcome={})

    mock_logger = Mock()
    monkeypatch.setattr(themoviedb_module, "logger", mock_logger)

    meta = MetaInfo(title="测试标题")
    result = module.recognize_media(meta=meta, tmdbid=98865, cache=True)

    assert result is None
    assert module.cache.update_calls == []

    logged_messages = " ".join(
        str(call.args[0]) if call.args else "" for call in mock_logger.warn.call_args_list
    )
    assert "无法确定媒体类型" in logged_messages


def test_async_recognize_media_reports_network_error_message_and_skips_cache_on_connection_failure(
    monkeypatch,
):
    """异步识别路径同样要区分网络故障文案，且不得写入缓存。"""
    conn_err = TMDbConnectionError("无法连接TheMovieDb，请检查网络连接！")
    module = _build_module(tv_outcome=conn_err, movie_outcome=conn_err)

    mock_logger = Mock()
    monkeypatch.setattr(themoviedb_module, "logger", mock_logger)

    meta = MetaInfo(title="测试标题")
    result = asyncio.run(
        module.async_recognize_media(meta=meta, tmdbid=98865, cache=True)
    )

    assert result is None
    assert module.cache.update_calls == []

    logged_messages = " ".join(
        str(call.args[0]) if call.args else "" for call in mock_logger.error.call_args_list
    )
    assert "连接TheMovieDb失败" in logged_messages or "连接 TheMovieDb 失败" in logged_messages
    assert "无法确定媒体类型" not in logged_messages


def test_cache_update_writes_nothing_for_connection_error_sentinel():
    """
    回归锁定 `TmdbCache.update` 的既有安全语义：info=None（网络错误场景对应的
    取值）不应写入任何缓存条目，正、负缓存都不写。
    """
    cache = object.__new__(TmdbCache)
    cache._cache = Mock()
    meta = MetaInfo(title="测试标题")
    meta.tmdbid = 98865

    cache.update(meta, None)

    cache._cache.set.assert_not_called()


def test_cache_update_still_writes_negative_cache_for_confirmed_empty_result():
    """
    对照组：info={}（确认查无此项）时仍应写入负缓存 `{"id": 0}`，
    与 info=None（网络错误）的行为形成对照，证明二者语义未被本次修复混淆。
    """
    cache = object.__new__(TmdbCache)
    cache._cache = Mock()
    meta = MetaInfo(title="测试标题")
    meta.tmdbid = 98865

    cache.update(meta, {})

    cache._cache.set.assert_called_once()
    args, _kwargs = cache._cache.set.call_args
    assert args[1] == {"id": 0}
