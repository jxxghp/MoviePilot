"""插件生命周期协调器的启动 owner/token 契约测试。"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import pytest

from app.application.plugin.lifecycle import PluginLifecycleCoordinator


async def _assert_event_waits(event: asyncio.Event) -> None:
    """确认事件在短预算内仍未发生，避免测试依赖固定 sleep 时序。"""
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(event.wait()), timeout=0.03)


async def _cancel_task(task: asyncio.Task) -> None:
    """取消仍在等待生命周期资格的任务并消费其终态。"""
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_startup_scope_yields_opaque_token_for_matching_plugin_hold() -> None:
    """启动 owner 取得的 token 可让内部取得逐插件资格。"""
    coordinator = PluginLifecycleCoordinator()

    async with coordinator.hold_startup() as startup_token:
        assert startup_token is not None
        assert not isinstance(startup_token, (str, bytes, int, bool))

        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_plugin() -> None:
            async with coordinator.hold("DemoPlugin", startup_token):
                entered.set()
                await release.wait()

        task = asyncio.create_task(hold_plugin())
        await entered.wait()
        assert coordinator._active_plugins == {"demoplugin"}
        release.set()
        await task

    assert coordinator._active_plugins == set()


@pytest.mark.asyncio
async def test_external_and_duplicate_plugin_holds_remain_blocked() -> None:
    """启动内部的逐插件资格不向外部调用放行，且同插件仍保持互斥。"""
    coordinator = PluginLifecycleCoordinator()
    internal_release = asyncio.Event()
    duplicate_release = asyncio.Event()
    internal_entered = asyncio.Event()
    duplicate_entered = asyncio.Event()
    external_entered = asyncio.Event()

    async with coordinator.hold_startup() as startup_token:

        async def internal_hold() -> None:
            async with coordinator.hold("DemoPlugin", startup_token):
                internal_entered.set()
                await internal_release.wait()

        async def duplicate_hold() -> None:
            async with coordinator.hold("demoplugin", startup_token):
                duplicate_entered.set()
                await duplicate_release.wait()

        async def external_hold() -> None:
            async with coordinator.hold("DemoPlugin"):
                external_entered.set()

        internal_task = asyncio.create_task(internal_hold())
        await internal_entered.wait()
        duplicate_task = asyncio.create_task(duplicate_hold())
        external_task = asyncio.create_task(external_hold())

        await _assert_event_waits(duplicate_entered)
        await _assert_event_waits(external_entered)

        internal_release.set()
        await internal_task
        await duplicate_entered.wait()
        await _assert_event_waits(external_entered)
        duplicate_release.set()
        await duplicate_task

    await external_entered.wait()
    await external_task


@pytest.mark.asyncio
async def test_foreign_and_expired_tokens_cannot_bypass_current_startup_lease() -> None:
    """其他 coordinator 或旧 lease 的 token 不得绕过当前启动 owner。"""
    first = PluginLifecycleCoordinator()
    second = PluginLifecycleCoordinator()

    async with first.hold_startup() as foreign_token:
        async with second.hold_startup() as current_token:
            assert foreign_token is not current_token
            foreign_entered = asyncio.Event()

            async def foreign_hold() -> None:
                async with second.hold("DemoPlugin", foreign_token):
                    foreign_entered.set()

            foreign_task = asyncio.create_task(foreign_hold())
            await _assert_event_waits(foreign_entered)
            await _cancel_task(foreign_task)

    async with second.hold_startup() as expired_token:
        pass

    async with second.hold_startup() as current_token:
        assert expired_token is not current_token
        expired_entered = asyncio.Event()

        async def expired_hold() -> None:
            async with second.hold("DemoPlugin", expired_token):
                expired_entered.set()

        expired_task = asyncio.create_task(expired_hold())
        await _assert_event_waits(expired_entered)
        await _cancel_task(expired_task)


@pytest.mark.asyncio
async def test_startup_token_can_cross_threads_without_contextvar() -> None:
    """显式 token 可跨线程传递，资格判断不依赖隐式 ContextVar。"""
    coordinator = PluginLifecycleCoordinator()
    main_thread = threading.current_thread().name

    def run_in_thread(startup_token: object) -> tuple[str, set[str]]:
        async def hold_plugin() -> tuple[str, set[str]]:
            async with coordinator.hold("DemoPlugin", startup_token):
                return threading.current_thread().name, set(coordinator._active_plugins)

        return asyncio.run(hold_plugin())

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="plugin-startup") as executor:
        async with coordinator.hold_startup() as startup_token:
            result = await asyncio.wrap_future(
                executor.submit(run_in_thread, startup_token)
            )

    assert result[0] != main_thread
    assert result[1] == {"demoplugin"}


@pytest.mark.asyncio
async def test_hold_without_token_retains_startup_waiting_compatibility() -> None:
    """无参数调用继续遵守启动全局资格的等待语义。"""
    coordinator = PluginLifecycleCoordinator()
    async with coordinator.hold_startup():
        entered = asyncio.Event()

        async def external_hold() -> None:
            async with coordinator.hold("DemoPlugin"):
                entered.set()

        task = asyncio.create_task(external_hold())
        await _assert_event_waits(entered)
        await _cancel_task(task)
