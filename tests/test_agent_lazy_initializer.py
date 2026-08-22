from __future__ import annotations

import asyncio
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.runtime.capabilities.errors import CapabilityRuntimeClosedError
from app.startup import agent_initializer


@pytest.mark.anyio
async def test_disabled_initializer_does_not_materialize_manager(monkeypatch) -> None:
    """功能关闭时启动阶段不得解析完整 Agent 模块。"""
    activate = AsyncMock(return_value=None)
    monkeypatch.setattr(
        agent_initializer,
        "activate_agent_service",
        activate,
    )
    initializer = agent_initializer.AgentInitializer()

    assert await initializer.initialize() is True
    assert initializer._initialized is False
    activate.assert_awaited_once_with()


@pytest.mark.anyio
async def test_cleanup_without_initialized_manager_does_not_query(monkeypatch) -> None:
    """清理空状态只能关闭已持有资源，不能为清理而触发首次导入。"""
    activate = AsyncMock(side_effect=AssertionError("service activated"))
    monkeypatch.setattr(agent_initializer, "activate_agent_service", activate)

    await agent_initializer.AgentInitializer().cleanup()

    activate.assert_not_awaited()


@pytest.mark.anyio
async def test_failed_initialize_keeps_manager_for_shutdown_cleanup(
    monkeypatch,
) -> None:
    """初始化中途失败时仍须保留实际 manager，供应用关闭释放部分资源。"""
    manager = AsyncMock()
    manager.initialize.side_effect = RuntimeError("partial initialization")
    monkeypatch.setattr(agent_initializer.settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr(agent_initializer, "agent_manager", manager)
    initializer = agent_initializer.AgentInitializer()

    assert await initializer.initialize() is False
    await initializer.cleanup()

    manager.close.assert_awaited_once_with()
    assert initializer._manager is None


@pytest.mark.anyio
async def test_compat_stop_closes_injected_manager_without_building_runtime(
    monkeypatch,
) -> None:
    """显式注入对象由兼容路径关闭，不为其构建空 Capability Runtime。"""
    events = []
    manager = AsyncMock()
    manager.initialize.side_effect = lambda: events.append("initialize")
    manager.close.side_effect = lambda: events.append("close")
    monkeypatch.setattr(agent_initializer.settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr(agent_initializer, "agent_manager", manager)
    shutdown = AsyncMock(side_effect=lambda: events.append("shutdown_gate"))
    monkeypatch.setattr(agent_initializer, "begin_agent_shutdown", shutdown)
    monkeypatch.setattr(
        agent_initializer,
        "agent_initializer",
        agent_initializer.AgentInitializer(),
    )

    assert await agent_initializer.init_agent() is True
    await agent_initializer.stop_agent()

    assert events == ["initialize", "close"]
    manager.initialize.assert_awaited_once_with()
    manager.close.assert_awaited_once_with()
    shutdown.assert_not_awaited()


@pytest.mark.anyio
async def test_production_initializer_delegates_lifecycle_to_runtime(
    monkeypatch,
) -> None:
    """生产路径只协调 service，不得再次手工 initialize 或 close canonical manager。"""
    manager = AsyncMock()
    activate = AsyncMock(return_value=manager)
    monkeypatch.setattr(agent_initializer, "agent_manager", None)
    monkeypatch.setattr(agent_initializer, "activate_agent_service", activate)
    initializer = agent_initializer.AgentInitializer()

    assert await initializer.initialize() is True
    await initializer.cleanup()

    activate.assert_awaited_once_with()
    manager.initialize.assert_not_awaited()
    manager.close.assert_not_awaited()
    assert initializer._manager is None


@pytest.mark.anyio
async def test_production_stop_seals_runtime_without_manually_closing_manager(
    monkeypatch,
) -> None:
    """生产关闭由 Runtime 关闸并释放 service，initializer 只清理自身引用。"""
    manager = AsyncMock()
    initializer = agent_initializer.AgentInitializer()
    initializer._manager = manager
    initializer._initialized = True
    initializer._compat_injected = False
    shutdown = AsyncMock()
    monkeypatch.setattr(agent_initializer, "begin_agent_shutdown", shutdown)
    monkeypatch.setattr(agent_initializer, "agent_initializer", initializer)
    monkeypatch.setattr(
        agent_initializer,
        "is_tool_factory_materialized",
        lambda: False,
    )

    await agent_initializer.stop_agent()

    shutdown.assert_awaited_once_with()
    manager.close.assert_not_awaited()
    assert initializer._manager is None


@pytest.mark.anyio
async def test_config_listener_delegates_watch_filter_to_runtime(monkeypatch) -> None:
    """配置监听器只转交 changed keys，不维护第二份启用开关。"""
    manager = AsyncMock()
    reconcile = AsyncMock(return_value=manager)
    monkeypatch.setattr(agent_initializer, "reconcile_agent_service", reconcile)
    initializer = agent_initializer.AgentInitializer()
    event = agent_initializer.Event(
        agent_initializer.EventType.ConfigChanged,
        {"key": {"AI_AGENT_ENABLE"}},
    )

    await initializer.handle_config_changed(event)

    reconcile.assert_awaited_once_with(
        reason="agent_service_config_changed",
        changed_keys={"AI_AGENT_ENABLE"},
        retry=True,
    )
    assert initializer._manager is manager
    assert initializer._initialized is True


def test_config_listener_registration_is_idempotent_and_instance_free() -> None:
    """重复构造 initializer 不得累积监听器或持有过期实例。"""
    subscribers = getattr(
        agent_initializer.eventmanager,
        "_EventManager__broadcast_subscribers",
    )
    AgentInitializer = agent_initializer.AgentInitializer
    AgentInitializer()
    AgentInitializer()

    listeners = tuple(
        subscribers.get(agent_initializer.EventType.ConfigChanged, {}).values()
    )
    matching = [
        listener
        for listener in listeners
        if listener is agent_initializer._handle_agent_config_changed
    ]
    assert len(matching) == 1
    assert agent_initializer._handle_agent_config_changed.__closure__ is None


@pytest.mark.anyio
async def test_config_event_after_shutdown_is_fail_closed(monkeypatch) -> None:
    """关闭后的配置事件不得把 service 重新标为初始化成功。"""
    reconcile = AsyncMock(side_effect=CapabilityRuntimeClosedError("closed"))
    monkeypatch.setattr(agent_initializer, "reconcile_agent_service", reconcile)
    initializer = agent_initializer.AgentInitializer()
    initializer._shutdown_complete = True
    event = agent_initializer.Event(
        agent_initializer.EventType.ConfigChanged,
        {"key": "AI_AGENT_ENABLE"},
    )

    await initializer.handle_config_changed(event)

    reconcile.assert_not_awaited()
    assert initializer._manager is None
    assert initializer._initialized is False


@pytest.mark.anyio
async def test_stop_skips_tool_executor_cleanup_when_factory_is_unresolved(
    monkeypatch,
) -> None:
    """工具能力从未解析时，关闭路径不得为线程池清理导入工具基础模块。"""
    fake_base = types.ModuleType("app.agent.tools.base")
    cleanup = MagicMock()
    fake_base.shutdown_blocking_executors = cleanup
    monkeypatch.setitem(sys.modules, "app.agent.tools.base", fake_base)
    monkeypatch.setattr(agent_initializer, "begin_agent_shutdown", AsyncMock())
    monkeypatch.setattr(
        agent_initializer,
        "is_tool_factory_materialized",
        lambda: False,
    )
    monkeypatch.setattr(
        agent_initializer,
        "agent_initializer",
        agent_initializer.AgentInitializer(),
    )

    await agent_initializer.stop_agent()

    cleanup.assert_not_called()


@pytest.mark.anyio
async def test_stop_closes_tool_executor_after_factory_materialization(
    monkeypatch,
) -> None:
    """工具能力已解析时，应取消仍排队的阻塞工具任务。"""
    fake_base = types.ModuleType("app.agent.tools.base")
    cleanup = MagicMock()
    fake_base.shutdown_blocking_executors = cleanup
    monkeypatch.setitem(sys.modules, "app.agent.tools.base", fake_base)
    monkeypatch.setattr(agent_initializer, "begin_agent_shutdown", AsyncMock())
    monkeypatch.setattr(
        agent_initializer,
        "is_tool_factory_materialized",
        lambda: True,
    )
    monkeypatch.setattr(
        agent_initializer,
        "agent_initializer",
        agent_initializer.AgentInitializer(),
    )

    await agent_initializer.stop_agent()

    cleanup.assert_called_once_with(wait=False, cancel_futures=True)


@pytest.mark.anyio
async def test_stop_does_not_wait_for_running_blocking_tool(monkeypatch) -> None:
    """应用关闭不得等待已经进入线程池且尚未返回的工具调用。"""
    from app.agent.tools.base import MoviePilotTool

    started = threading.Event()
    release = threading.Event()

    def _blocking_call() -> str:
        started.set()
        release.wait()
        return "done"

    worker = asyncio.create_task(
        MoviePilotTool.run_blocking("web", _blocking_call)
    )
    assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
    monkeypatch.setattr(agent_initializer, "begin_agent_shutdown", AsyncMock())
    monkeypatch.setattr(
        agent_initializer,
        "is_tool_factory_materialized",
        lambda: True,
    )
    monkeypatch.setattr(
        agent_initializer,
        "agent_initializer",
        agent_initializer.AgentInitializer(),
    )

    try:
        await asyncio.wait_for(agent_initializer.stop_agent(), timeout=0.2)
        assert worker.done() is False
    finally:
        release.set()
        assert await asyncio.wait_for(worker, timeout=1) == "done"
