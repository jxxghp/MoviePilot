"""Agent 渠道流式输出的 start/stop owner 生命周期测试。"""

import asyncio

import pytest

from app.agent.callback import StreamingHandler
from app.schemas.types import NotificationChannel


@pytest.mark.asyncio
async def test_repeated_streaming_start_retains_previous_flush_owner() -> None:
    """重复启动必须先等待旧 flush owner 结束，再发布新一轮上下文。"""
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._source = "old-source"
    handler._streaming_enabled = True
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    observed_sources: list[str | None] = []

    async def old_flush_owner() -> None:
        """在退出前读取 handler 上下文，暴露被新一轮提前覆盖的风险。"""
        old_started.set()
        await release_old.wait()
        observed_sources.append(handler._source)

    old_task = asyncio.create_task(old_flush_owner(), name="old.flush")
    handler._flush_task = old_task
    await old_started.wait()

    restart = asyncio.create_task(
        handler.start_streaming(
            channel=NotificationChannel.Feishu.value,
            source="new-source",
        ),
        name="agent.streaming.restart",
    )
    await asyncio.sleep(0)

    assert restart.done() is False
    assert handler._source == "old-source"
    assert handler._flush_task is old_task

    release_old.set()
    await asyncio.wait_for(restart, timeout=1)
    new_task = handler._flush_task

    assert old_task.done()
    assert observed_sources == ["old-source"]
    assert new_task is not None
    assert new_task is not old_task
    assert handler._source == "new-source"

    await asyncio.wait_for(handler.stop_streaming(), timeout=1)
    assert new_task.done()


@pytest.mark.asyncio
async def test_streaming_start_waits_for_concurrent_stop_final_flush() -> None:
    """停止阶段的最终刷新未完成时，新启动不得改写消息上下文。"""
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._source = "stopping-source"
    handler._streaming_enabled = True
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()

    async def blocking_flush() -> None:
        """把最终刷新停留在生命周期锁内，验证新启动等待。"""
        flush_started.set()
        await release_flush.wait()

    handler._flush = blocking_flush
    stop_task = asyncio.create_task(
        handler.stop_streaming(),
        name="agent.streaming.stop",
    )
    await flush_started.wait()
    start_task = asyncio.create_task(
        handler.start_streaming(
            channel=NotificationChannel.Feishu.value,
            source="next-source",
        ),
        name="agent.streaming.next",
    )
    await asyncio.sleep(0)

    assert start_task.done() is False
    assert handler._source == "stopping-source"

    release_flush.set()
    await asyncio.wait_for(stop_task, timeout=1)
    await asyncio.wait_for(start_task, timeout=1)
    next_flush_task = handler._flush_task

    assert handler._source == "next-source"
    assert next_flush_task is not None

    await asyncio.wait_for(handler.stop_streaming(), timeout=1)
    assert next_flush_task.done()
