import asyncio
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.runtime.config import global_vars
from app.startup import lifecycle


@pytest.mark.asyncio
async def test_runtime_ready_waits_for_scheduler_and_command_refresh(monkeypatch) -> None:
    """插件 ready 只在调度任务和命令注册完成后对外可见。"""
    order: list[str] = []
    manager = MagicMock()
    command_future = Future()

    async def sync_plugins() -> bool:
        order.append("plugins")
        return True

    async def execute_task(_loop, task_func, _task_name):
        task_func()
        return []

    monkeypatch.setattr(lifecycle.settings, "MOVIEPILOT_SAFE_MODE", False)
    monkeypatch.setattr(
        global_vars,
        "CURRENT_EVENT_LOOP",
        asyncio.get_running_loop(),
    )
    monkeypatch.setattr(lifecycle, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(lifecycle, "sync_plugins", sync_plugins)
    monkeypatch.setattr(lifecycle, "execute_task", execute_task)
    monkeypatch.setattr(
        lifecycle,
        "init_plugin_scheduler",
        lambda: order.append("scheduler"),
    )
    monkeypatch.setattr(
        lifecycle,
        "restart_command",
        lambda: (order.append("commands"), command_future)[1],
    )
    monkeypatch.setattr(lifecycle, "SystemHelper", MagicMock())
    monkeypatch.setattr(lifecycle, "SystemChain", MagicMock())
    monkeypatch.setattr(
        lifecycle.MoviePilotServerHelper,
        "async_report_usage",
        AsyncMock(),
    )
    manager.set_plugin_settling.side_effect = lambda value: order.append(
        f"settling:{value}"
    )
    manager.start_monitor.side_effect = lambda: order.append("monitor")

    settle_task = asyncio.create_task(lifecycle.init_extra())
    await asyncio.sleep(0)

    assert order == ["plugins", "scheduler", "commands"]
    manager.set_plugin_settling.assert_not_called()

    command_future.set_result(None)
    await settle_task

    assert order == [
        "plugins",
        "scheduler",
        "commands",
        "settling:False",
        "monitor",
    ]
