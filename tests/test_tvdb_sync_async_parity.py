"""TVDB 附加信息同步异步共享决策回归测试。"""

import asyncio
from typing import Any

from app.domain.context import MediaInfo
from app.modules import thetvdb as tvdb_module
from app.modules.thetvdb import TheTvDbModule
from app.schemas.types import MediaSource, MediaType


def _module() -> TheTvDbModule:
    """绕过模块运行时构造可局部注入的 TVDB provider。"""
    return object.__new__(TheTvDbModule)


def _media(
    media_source: MediaSource = MediaSource.Douban,
    media_id: str = "100",
) -> MediaInfo:
    """构造用于 TVDB 别名匹配的电视剧媒体。"""
    return MediaInfo(
        media_source=media_source,
        media_id=media_id,
        type=MediaType.TV,
        title="测试剧集",
        year="2026",
        names=["Test Series"],
    )


def test_tvdb_sync_async_share_title_lookup_and_candidate_resolution(
    monkeypatch,
) -> None:
    """标题查询双入口应只切换 I/O 外壳并复用同一候选解析。"""
    module = _module()
    calls: list[tuple[str, object]] = []
    threaded: list[str] = []
    candidates = [
        {"type": "series", "tvdb_id": "series-404", "name": "Other"},
        {
            "type": "series",
            "tvdb_id": "series-123",
            "name": "测试剧集",
            "year": "2026",
            "translations": [{"name": "Test Series"}],
        },
    ]

    def search_tvdb(title: str) -> list[dict[str, Any]]:
        """记录阻塞标题查询并返回稳定候选。"""
        calls.append(("search_tvdb", title))
        return candidates

    async def run_blocking_call(function, *args, **kwargs):
        """记录异步入口实际提交到线程池的函数边界。"""
        threaded.append(function.__name__)
        return function(*args, **kwargs)

    module.search_tvdb = search_tvdb
    monkeypatch.setattr(tvdb_module, "run_in_threadpool", run_blocking_call)

    sync_result = module.get_media_auxiliary_info(
        _media(), media_source=(MediaSource.TVDB,)
    )
    async_result = asyncio.run(
        module.async_get_media_auxiliary_info(
            _media(), media_source=(MediaSource.TVDB,)
        )
    )

    assert [item.to_dict() for item in sync_result] == [
        item.to_dict() for item in async_result
    ]
    assert calls == [
        ("search_tvdb", "测试剧集"),
        ("search_tvdb", "测试剧集"),
    ]
    assert threaded == ["search_tvdb"]
    assert sync_result[0].media_id == "123"
    assert sync_result[0].names == ["测试剧集", "Test Series"]


def test_tvdb_sync_async_share_native_id_lookup(monkeypatch) -> None:
    """TVDB 原生身份双入口应选择相同详情查询，线程池不包业务方法。"""
    module = _module()
    calls: list[int] = []
    threaded: list[str] = []
    info = {
        "id": 123,
        "name": "测试剧集",
        "year": "2026",
        "aliases": ["Test Series"],
    }

    def tvdb_info(tvdb_id: int) -> dict[str, Any]:
        """记录阻塞详情查询。"""
        calls.append(tvdb_id)
        return info

    async def run_blocking_call(function, *args, **kwargs):
        """验证异步入口只提交详情 I/O。"""
        threaded.append(function.__name__)
        return function(*args, **kwargs)

    module.tvdb_info = tvdb_info
    monkeypatch.setattr(tvdb_module, "run_in_threadpool", run_blocking_call)
    media = _media(media_source=MediaSource.TVDB, media_id="123")

    sync_result = module.get_media_auxiliary_info(
        media, media_source=(MediaSource.TVDB,)
    )
    async_result = asyncio.run(
        module.async_get_media_auxiliary_info(
            media, media_source=(MediaSource.TVDB,)
        )
    )

    assert [item.to_dict() for item in sync_result] == [
        item.to_dict() for item in async_result
    ]
    assert calls == [123, 123]
    assert threaded == ["tvdb_info"]


def test_tvdb_disabled_source_short_circuits_before_sync_and_async_io(
    monkeypatch,
) -> None:
    """未启用 TVDB 时双入口都不得触发网络或线程池。"""
    module = _module()

    def unexpected_io(*_args, **_kwargs):
        """标记禁用来源错误触发了同步 I/O。"""
        raise AssertionError("禁用 TVDB 时不应触发 I/O")

    async def unexpected_threadpool(*_args, **_kwargs):
        """标记禁用来源错误触发了线程池。"""
        raise AssertionError("禁用 TVDB 时不应触发线程池")

    module.search_tvdb = unexpected_io
    module.tvdb_info = unexpected_io
    monkeypatch.setattr(tvdb_module, "run_in_threadpool", unexpected_threadpool)

    assert module.get_media_auxiliary_info(
        _media(), media_source=(MediaSource.TMDB,)
    ) == []
    assert asyncio.run(
        module.async_get_media_auxiliary_info(
            _media(), media_source=(MediaSource.TMDB,)
        )
    ) == []
