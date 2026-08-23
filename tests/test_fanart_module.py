"""Fanart 模块缓存清理生命周期测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.modules.fanart import FanartModule
from app.runtime.tasks import TaskRegistry


def test_fanart_clear_cache_registers_async_cleanup_in_running_loop() -> None:
    """同步清缓存入口应复用宿主 owner，并保持立即返回的兼容约定。"""

    async def scenario() -> None:
        """验证异步缓存清理完成前始终由宿主登记器持有。"""
        registry = TaskRegistry()
        release = asyncio.Event()

        async def clear_async_cache() -> None:
            """等待测试释放，以便观察登记中的清理任务。"""
            await release.wait()

        sync_cache = SimpleNamespace(cache_clear=Mock())
        async_cache = SimpleNamespace(
            cache_clear=Mock(side_effect=lambda: clear_async_cache())
        )
        module = FanartModule()
        with (
            patch.object(
                FanartModule,
                "_FanartModule__request_fanart",
                sync_cache,
            ),
            patch.object(
                FanartModule,
                "_FanartModule__async_request_fanart",
                async_cache,
            ),
            patch("app.modules.fanart.get_task_registry", return_value=registry),
        ):
            assert module.clear_cache() is None
            assert [record.owner for record in registry.records] == [
                "module.fanart.cache_clear"
            ]

            release.set()
            await registry.records[0].task
            await asyncio.sleep(0)

        sync_cache.cache_clear.assert_called_once_with()
        async_cache.cache_clear.assert_called_once_with()
        assert registry.records == ()

    asyncio.run(scenario())
