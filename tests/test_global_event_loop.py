import asyncio

import pytest

from app.runtime.config import GlobalVar, global_vars
from app.runtime.loop import MainLoopRegistry, main_loop_registry


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
        previous_owner = runtime.set_loop(previous)
        current_owner = runtime.set_loop(current)
        runtime.clear_loop(previous_owner)
        assert runtime.loop is current

        runtime.clear_loop(current_owner)
        assert runtime.CURRENT_EVENT_LOOP is None

    try:
        asyncio.run(verify())
    finally:
        previous.close()


def test_nested_owner_release_restores_same_event_loop() -> None:
    """同一循环上的内层生命周期退出后，外层 owner 仍保持登记。"""
    runtime = GlobalVar()

    async def verify() -> None:
        loop = asyncio.get_running_loop()
        outer_owner = runtime.set_loop(loop)
        inner_owner = runtime.set_loop(loop)

        runtime.clear_loop(inner_owner)
        assert runtime.loop is loop

        runtime.clear_loop(outer_owner)
        assert runtime.CURRENT_EVENT_LOOP is None

    asyncio.run(verify())


def test_global_var_loop_property_is_only_a_compatibility_facade() -> None:
    """旧属性赋值与 canonical registry 必须共享事实源而不复制状态。"""
    previous = main_loop_registry.current
    loop = asyncio.new_event_loop()
    try:
        global_vars.CURRENT_EVENT_LOOP = loop
        assert main_loop_registry.current is loop
        assert global_vars.CURRENT_EVENT_LOOP is loop
    finally:
        global_vars.CURRENT_EVENT_LOOP = previous
        loop.close()


def test_main_loop_registry_rejects_stale_owner_release() -> None:
    """独立 registry 中旧 owner 的迟到释放不得覆盖更新循环。"""
    registry = MainLoopRegistry()
    first = asyncio.new_event_loop()
    second = asyncio.new_event_loop()
    try:
        first_owner = registry.register(first)
        second_owner = registry.register(second)

        registry.release(first_owner)

        assert registry.current is second
        registry.release(second_owner)
        assert registry.current is None
    finally:
        first.close()
        second.close()
