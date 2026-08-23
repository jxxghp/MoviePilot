import asyncio

import pytest

from app.runtime.config import GlobalVar


def test_global_loop_requires_lifecycle_owner() -> None:
    """启动前读取主循环不得隐式创建一个无法执行任务的循环。"""
    runtime = GlobalVar()
    runtime.CURRENT_EVENT_LOOP = None

    with pytest.raises(RuntimeError, match="主事件循环尚未启动或已经停止"):
        _ = runtime.loop

    assert runtime.CURRENT_EVENT_LOOP is None


def test_global_loop_rejects_closed_owner() -> None:
    """已关闭的生命周期 owner 不得继续接收跨线程任务。"""
    runtime = GlobalVar()
    loop = asyncio.new_event_loop()
    runtime.set_loop(loop)
    loop.close()

    with pytest.raises(RuntimeError, match="主事件循环尚未启动或已经停止"):
        _ = runtime.loop


def test_global_loop_rejects_owner_that_is_not_running() -> None:
    """未运行的循环不得成为跨线程任务投递目标。"""
    runtime = GlobalVar()
    loop = asyncio.new_event_loop()
    try:
        runtime.set_loop(loop)

        with pytest.raises(RuntimeError, match="主事件循环尚未启动或已经停止"):
            _ = runtime.loop
    finally:
        loop.close()


def test_clear_global_loop_preserves_new_owner() -> None:
    """迟到的旧生命周期清理不得清除后来登记的循环。"""
    runtime = GlobalVar()
    previous = asyncio.new_event_loop()

    async def verify() -> None:
        current = asyncio.get_running_loop()
        runtime.set_loop(current)
        runtime.clear_loop(previous)
        assert runtime.loop is current

        runtime.clear_loop(current)
        assert runtime.CURRENT_EVENT_LOOP is None

    try:
        asyncio.run(verify())
    finally:
        previous.close()
