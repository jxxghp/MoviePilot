"""媒体识别 Chain 同步、异步入口的业务决策对称性测试。"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.chain.base import ChainBase
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.runtime.events import Event
from app.schemas.types import ChainEventType, MediaSource, MediaType


async def _keep_async_candidate(**kwargs):
    """异步插件补充替身保持原生候选不变。"""
    return kwargs["mediainfo"]


def _keep_candidate(**kwargs):
    """同步插件补充替身保持原生候选不变。"""
    return kwargs["mediainfo"]


def _fallback(title: str) -> MediaInfo:
    """构造不带远端身份的可观察回退结果。"""
    return MediaInfo(title=title, type=MediaType.MOVIE)


@pytest.mark.parametrize(
    ("request_kwargs", "expected_identity"),
    [
        (
            {"media_source": "themoviedb", "cache": False},
            (MediaSource.TMDB, "23155"),
        ),
        (
            {
                "media_source": MediaSource.TMDB,
                "media_id": "603",
                "music_type": "recording",
                "cache": False,
            },
            (MediaSource.TMDB, "603"),
        ),
    ],
)
def test_sync_async_requests_build_identical_module_plans(
        request_kwargs,
        expected_identity,
) -> None:
    """同一请求经双入口规范化后必须得到完全相同的模块参数。"""
    chain = ChainBase()
    meta = MetaInfo("空之境界 第五章 矛盾螺旋 (2008) {tmdbid=23155}")
    meta.episode_group = "group-1"
    sync_candidate = _fallback("同步候选")
    async_candidate = _fallback("异步候选")
    sync_native = Mock(return_value=sync_candidate)
    async_native = AsyncMock(return_value=async_candidate)

    with patch.object(chain, "_run_native_media_recognize", sync_native), patch.object(
        chain, "_async_run_native_media_recognize", async_native
    ), patch.object(
        chain, "_supplement_media_recognize", side_effect=_keep_candidate
    ), patch.object(
        chain, "_async_supplement_media_recognize", side_effect=_keep_async_candidate
    ):
        sync_result = chain.recognize_media(meta=meta, **request_kwargs)
        async_result = asyncio.run(
            chain.async_recognize_media(meta=meta, **request_kwargs)
        )

    sync_kwargs = sync_native.call_args.args[0]
    async_kwargs = async_native.await_args.args[0]
    assert sync_kwargs == async_kwargs
    assert (sync_kwargs["media_source"], sync_kwargs["media_id"]) == expected_identity
    assert sync_kwargs["episode_group"] == "group-1"
    assert sync_result is sync_candidate
    assert async_result is async_candidate


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"media_id": "123"},
        {"media_source": MediaSource.TMDB, "media_id": "0"},
    ],
)
def test_sync_async_reject_the_same_invalid_explicit_identity(request_kwargs) -> None:
    """显式身份缺半或为零时，双入口都必须在任何 I/O 前拒绝。"""
    chain = ChainBase()
    sync_native = Mock()
    async_native = AsyncMock()

    with patch.object(chain, "_run_native_media_recognize", sync_native), patch.object(
        chain, "_async_run_native_media_recognize", async_native
    ):
        assert chain.recognize_media(meta=MetaInfo("任意标题"), **request_kwargs) is None
        assert asyncio.run(
            chain.async_recognize_media(meta=MetaInfo("任意标题"), **request_kwargs)
        ) is None

    sync_native.assert_not_called()
    async_native.assert_not_awaited()


def test_sync_async_share_miss_preserves_the_same_first_fallback() -> None:
    """共享身份二次识别仍无身份时，双入口都保留最早的本地回退结果。"""
    chain = ChainBase()
    chain.runtime_config = replace(
        chain.runtime_config,
        media_recognize_share=True,
    )
    meta = MetaInfo("共享回退电影 (2026)")
    sync_first = _fallback("同步首选")
    sync_second = _fallback("同步共享候选")
    async_first = _fallback("异步首选")
    async_second = _fallback("异步共享候选")
    sync_native = Mock(side_effect=[sync_first, sync_second])
    async_native = AsyncMock(side_effect=[async_first, async_second])
    share_port = Mock()
    share_port.query_recognize_share.return_value = {"media_id": "603"}
    share_port.async_query_recognize_share = AsyncMock(
        return_value={"media_id": "603"}
    )
    share_port.to_recognize_params.return_value = {
        "mtype": MediaType.MOVIE,
        "media_source": MediaSource.TMDB,
        "media_id": "603",
    }

    with patch.object(chain, "_run_native_media_recognize", sync_native), patch.object(
        chain, "_async_run_native_media_recognize", async_native
    ), patch.object(
        chain, "_supplement_media_recognize", side_effect=_keep_candidate
    ), patch.object(
        chain, "_async_supplement_media_recognize", side_effect=_keep_async_candidate
    ), patch(
        "app.chain._recognition._recognition_share_snapshot",
        return_value=share_port,
    ):
        sync_result = chain.recognize_media(meta=meta, cache=False)
        async_result = asyncio.run(chain.async_recognize_media(meta=meta, cache=False))

    assert sync_result is sync_first
    assert async_result is async_first
    assert [call.args[0] for call in sync_native.call_args_list] == [
        call.args[0] for call in async_native.await_args_list
    ]
    assert share_port.query_recognize_share.call_args.kwargs == (
        share_port.async_query_recognize_share.await_args.kwargs
    )


def test_sync_async_share_hit_uses_same_second_plan_and_cache_backfill() -> None:
    """共享身份命中后，双入口应按同一二次计划识别并回填各自本地缓存。"""
    chain = ChainBase()
    chain.runtime_config = replace(
        chain.runtime_config,
        media_recognize_share=True,
    )
    sync_meta = MetaInfo("共享命中电影 (2026)")
    async_meta = MetaInfo("共享命中电影 (2026)")
    sync_fallback = _fallback("同步回退")
    async_fallback = _fallback("异步回退")
    sync_hit = MediaInfo(
        title="共享命中",
        type=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="603",
    )
    async_hit = MediaInfo(
        title="共享命中",
        type=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="603",
    )
    sync_native = Mock(side_effect=[sync_fallback, sync_hit])
    async_native = AsyncMock(side_effect=[async_fallback, async_hit])
    sync_cache = Mock()
    async_cache = AsyncMock()
    sync_counter = Mock()
    async_counter = AsyncMock()
    share_port = Mock()
    share_port.query_recognize_share.return_value = {"media_id": "603"}
    share_port.async_query_recognize_share = AsyncMock(
        return_value={"media_id": "603"}
    )
    share_port.to_recognize_params.return_value = {
        "mtype": MediaType.MOVIE,
        "media_source": MediaSource.TMDB,
        "media_id": "603",
    }

    with patch.object(chain, "_run_native_media_recognize", sync_native), patch.object(
        chain, "_async_run_native_media_recognize", async_native
    ), patch.object(
        chain, "_supplement_media_recognize", side_effect=_keep_candidate
    ), patch.object(
        chain, "_async_supplement_media_recognize", side_effect=_keep_async_candidate
    ), patch.object(
        chain, "_update_local_recognize_cache", sync_cache
    ), patch.object(
        chain, "_async_update_local_recognize_cache", async_cache
    ), patch.object(
        chain, "_record_media_recognize_share_hit", sync_counter
    ), patch(
        "app.chain._recognition.run_in_threadpool", async_counter
    ), patch(
        "app.chain._recognition._recognition_share_snapshot",
        return_value=share_port,
    ):
        sync_result = chain.recognize_media(meta=sync_meta, cache=False)
        async_result = asyncio.run(
            chain.async_recognize_media(meta=async_meta, cache=False)
        )

    assert sync_result is sync_hit
    assert async_result is async_hit
    assert sync_native.call_args_list[1].args[0] == async_native.await_args_list[1].args[0]
    assert sync_cache.call_args.kwargs["meta"] is not sync_meta
    assert async_cache.await_args.kwargs["meta"] is not async_meta
    assert sync_cache.call_args.kwargs["mediainfo"] is sync_hit
    assert async_cache.await_args.kwargs["mediainfo"] is async_hit
    sync_counter.assert_called_once_with()
    async_counter.assert_awaited_once_with(sync_counter)


def test_sync_async_plugin_failures_use_one_payload_and_fallback_policy() -> None:
    """插件返回无身份结果时，双入口发送同一载荷并保留各自原候选。"""
    chain = ChainBase()
    meta = MetaInfo("插件回退电影 (2026)")
    sync_fallback = _fallback("同步回退")
    async_fallback = _fallback("异步回退")
    invalid_event = Event(
        ChainEventType.MediaRecognize,
        {"mediainfo": {"media_source": "themoviedb", "title": "非法结果"}},
    )
    sync_sender = Mock(return_value=invalid_event)
    async_sender = AsyncMock(return_value=invalid_event)

    with patch.object(chain.eventmanager, "check", return_value=True), patch.object(
        chain.eventmanager, "send_event", sync_sender
    ), patch.object(
        chain.eventmanager, "async_send_event", async_sender
    ):
        sync_result = chain._supplement_media_recognize(
            meta=meta,
            mtype=MediaType.MOVIE,
            media_source=None,
            media_id=None,
            mediainfo=sync_fallback,
        )
        async_result = asyncio.run(
            chain._async_supplement_media_recognize(
                meta=meta,
                mtype=MediaType.MOVIE,
                media_source=None,
                media_id=None,
                mediainfo=async_fallback,
            )
        )

    assert sync_result is sync_fallback
    assert async_result is async_fallback
    assert sync_sender.call_args.args == async_sender.await_args.args
