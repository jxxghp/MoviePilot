"""影视自动识别主数据源路由回归测试。"""

import asyncio
from typing import Optional

from app.application.orchestration import ChainBase
from app.application.orchestration.media import MediaChain
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.types import MediaSource, MediaType


def _video_meta() -> MetaBase:
    """构造未指定数据源的电影元数据。"""
    meta = MetaBase("流浪地球 2019")
    meta.name = "流浪地球"
    meta.year = "2019"
    meta.type = MediaType.MOVIE
    return meta


def _module_kwargs(
    meta: MetaBase, media_source: Optional[MediaSource] = None
) -> dict:
    """构造原生识别路由使用的模块参数。"""
    return {
        "meta": meta,
        "mtype": MediaType.MOVIE,
        "media_source": media_source,
        "media_id": None,
        "episode_group": None,
        "cache": True,
    }


def test_video_auto_recognize_only_uses_tmdb(monkeypatch) -> None:
    """未指定影视来源时必须固定委托 TMDB，失败后也不切换其它来源。"""
    chain = object.__new__(MediaChain)
    calls = []

    def generic_recognize(_self, module_kwargs, cache):
        """记录父类模块分发参数并模拟 TMDB 未命中。"""
        calls.append((module_kwargs, cache))
        return None

    monkeypatch.setattr(
        ChainBase,
        "_run_native_media_recognize",
        generic_recognize,
    )

    result = chain._run_native_media_recognize(
        _module_kwargs(_video_meta()),
        cache=True,
    )

    assert result is None
    assert len(calls) == 1
    assert calls[0][0]["media_source"] == MediaSource.TMDB


def test_async_video_auto_recognize_only_uses_tmdb(monkeypatch) -> None:
    """异步未指定影视来源时也只委托 TMDB 原生识别入口。"""
    chain = object.__new__(MediaChain)
    expected = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="550",
        tmdb_id=550,
        title="流浪地球",
        year="2019",
        type=MediaType.MOVIE,
    )
    calls = []

    async def generic_recognize(_self, module_kwargs, cache):
        """记录异步父类模块分发参数并返回 TMDB 结果。"""
        calls.append((module_kwargs, cache))
        return expected

    monkeypatch.setattr(
        ChainBase,
        "_async_run_native_media_recognize",
        generic_recognize,
    )

    result = asyncio.run(chain._async_run_native_media_recognize(
        _module_kwargs(_video_meta()),
        cache=True,
    ))

    assert result is expected
    assert len(calls) == 1
    assert calls[0][0]["media_source"] == MediaSource.TMDB


def test_video_explicit_source_is_preserved(monkeypatch) -> None:
    """手工指定影视来源时必须保持严格单源分发。"""
    chain = object.__new__(MediaChain)
    expected = MediaInfo(
        media_source=MediaSource.Douban,
        media_id="26266893",
        douban_id="26266893",
        title="流浪地球",
        year="2019",
        type=MediaType.MOVIE,
    )
    calls = []

    def generic_recognize(_self, module_kwargs, cache):
        """记录显式来源并返回预设结果。"""
        calls.append((module_kwargs, cache))
        return expected

    monkeypatch.setattr(
        ChainBase,
        "_run_native_media_recognize",
        generic_recognize,
    )

    result = chain._run_native_media_recognize(
        _module_kwargs(_video_meta(), media_source=MediaSource.Douban),
        cache=True,
    )

    assert result is expected
    assert calls[0][0]["media_source"] == MediaSource.Douban
