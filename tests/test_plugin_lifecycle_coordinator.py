"""
插件生命周期协调器的启动 owner/token 契约测试。
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from unittest.mock import Mock

import pytest

from app.application.plugin.lifecycle import PluginLifecycleCoordinator
from app.schemas.exception import PluginMutationRejectedError


async def _assert_event_waits(event: asyncio.Event) -> None:
    """
    确认事件在短预算内仍未发生，避免测试依赖固定 sleep 时序。
    """
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(event.wait()), timeout=0.03)


async def _cancel_task(task: asyncio.Task) -> None:
    """
    取消仍在等待生命周期资格的任务并消费其终态。
    """
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_startup_scope_yields_opaque_token_for_matching_plugin_hold() -> None:
    """
    启动 owner 取得的 token 可让内部取得逐插件资格。
    """
    coordinator = PluginLifecycleCoordinator()

    async with coordinator.hold_startup() as startup_token:
        assert startup_token is not None
        assert not isinstance(startup_token, (str, bytes, int, bool))

        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_plugin() -> None:
            """
            持有效启动令牌进入插件租约，并等待测试明确释放。
            """
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
    """
    启动内部的逐插件资格不向外部调用放行，且同插件仍保持互斥。
    """
    coordinator = PluginLifecycleCoordinator()
    internal_release = asyncio.Event()
    duplicate_release = asyncio.Event()
    internal_entered = asyncio.Event()
    duplicate_entered = asyncio.Event()
    external_entered = asyncio.Event()

    async with coordinator.hold_startup() as startup_token:

        async def internal_hold() -> None:
            """
            持启动令牌占用插件租约，作为重复与外部请求的阻塞参照。
            """
            async with coordinator.hold("DemoPlugin", startup_token):
                internal_entered.set()
                await internal_release.wait()

        async def duplicate_hold() -> None:
            """
            使用大小写不同的同一插件名申请租约，检查身份归一化后的互斥。
            """
            async with coordinator.hold("demoplugin", startup_token):
                duplicate_entered.set()
                await duplicate_release.wait()

        async def external_hold() -> None:
            """
            不携带启动令牌申请租约，记录外部调用实际获准进入的时机。
            """
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
    """
    其他 coordinator 或旧 lease 的 token 不得绕过当前启动 owner。
    """
    first = PluginLifecycleCoordinator()
    second = PluginLifecycleCoordinator()

    async with first.hold_startup() as foreign_token:
        async with second.hold_startup() as current_token:
            assert foreign_token is not current_token
            foreign_entered = asyncio.Event()

            async def foreign_hold() -> None:
                """
                将另一协调器签发的令牌用于当前租约，观察其是否被阻止越权进入。
                """
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
            """
            使用过期启动令牌申请租约，记录当前启动屏障释放前后的进入情况。
            """
            async with second.hold("DemoPlugin", expired_token):
                expired_entered.set()

        expired_task = asyncio.create_task(expired_hold())
        await _assert_event_waits(expired_entered)
        await _cancel_task(expired_task)


@pytest.mark.asyncio
async def test_startup_token_can_cross_threads_without_contextvar() -> None:
    """
    显式 token 可跨线程传递，资格判断不依赖隐式 ContextVar。
    """
    coordinator = PluginLifecycleCoordinator()
    main_thread = threading.current_thread().name

    def run_in_thread(startup_token: object) -> tuple[str, set[str]]:
        """
        在独立事件循环中验证启动令牌的显式跨线程授权。
        """
        async def hold_plugin() -> tuple[str, set[str]]:
            """
            在工作线程内取得逐插件资格并返回持有事实。
            """
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
    """
    无参数调用继续遵守启动全局资格的等待语义。
    """
    coordinator = PluginLifecycleCoordinator()
    async with coordinator.hold_startup():
        entered = asyncio.Event()

        async def external_hold() -> None:
            """
            以旧式无令牌调用申请租约，检查其仍等待启动流程结束。
            """
            async with coordinator.hold("DemoPlugin"):
                entered.set()

        task = asyncio.create_task(external_hold())
        await _assert_event_waits(entered)
        await _cancel_task(task)


@pytest.mark.asyncio
async def test_admission_deadline_does_not_release_plugin_or_startup_owner() -> None:
    """
    等待预算耗尽后现有租约仍有效，后续请求不能越权进入。
    """
    coordinator = PluginLifecycleCoordinator()
    async with coordinator.hold("SlowPlugin"):
        with pytest.raises(PluginMutationRejectedError, match="slowplugin.*秒"):
            async with coordinator.hold("SLOWPLUGIN", timeout=0.01):
                pytest.fail("重复插件资格被放行")
        with pytest.raises(PluginMutationRejectedError, match="slowplugin"):
            async with coordinator.hold_startup(timeout=0.01):
                pytest.fail("启动资格被错误放行")
        assert coordinator._active_plugins == {"slowplugin"}
        async with coordinator.hold("OtherPlugin", timeout=0):
            assert coordinator._active_plugins == {"slowplugin", "otherplugin"}
    async with coordinator.hold_startup() as token:
        with pytest.raises(PluginMutationRejectedError, match="启动同步"):
            async with coordinator.hold("OtherPlugin", timeout=0):
                pytest.fail("外部请求穿过启动屏障")
        assert coordinator._startup_token is token
    async with coordinator.hold("SlowPlugin", timeout=0):
        assert coordinator._active_plugins == {"slowplugin"}


@pytest.mark.asyncio
@pytest.mark.parametrize("startup", [False, True])
async def test_admission_does_not_acquire_after_wait_budget_expires(
    startup: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    即使持有者在等待预算耗尽时释放，也不能在截止时间之后取得新资格。
    """
    coordinator = PluginLifecycleCoordinator()
    acquire = Mock(side_effect=[None, object()] if startup else [False, True])
    monkeypatch.setattr(
        coordinator, "_try_acquire_startup" if startup else "_try_acquire_plugin", acquire,
    )
    lease = coordinator.hold_startup(timeout=0.01) if startup else coordinator.hold("DemoPlugin", timeout=0.01)
    with pytest.raises(PluginMutationRejectedError, match="准入正忙"):
        async with lease:
            pytest.fail("预算耗尽后仍然取得执行权")
    acquire.assert_called_once()
