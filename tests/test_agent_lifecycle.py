import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import agent as agent_module
from app.agent import AgentManager
from app.agent.memory import MemoryManager
from app.startup import agent_initializer, modules_initializer


@pytest.mark.anyio
async def test_agent_entrypoint_initializes_on_calling_loop(monkeypatch) -> None:
    """Agent 启动入口必须在应用主循环完成初始化。"""
    current_loop = asyncio.get_running_loop()
    initialized_loops = []
    manager = AsyncMock()

    async def initialize() -> None:
        initialized_loops.append(asyncio.get_running_loop())

    manager.initialize.side_effect = initialize
    monkeypatch.setattr(agent_initializer.settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr(agent_initializer, "agent_manager", manager)
    monkeypatch.setattr(
        agent_initializer,
        "agent_initializer",
        agent_initializer.AgentInitializer(),
    )

    assert await agent_initializer.init_agent() is True
    assert initialized_loops == [current_loop]
    manager.initialize.assert_awaited_once_with()


@pytest.mark.anyio
async def test_agent_manager_background_tasks_share_owner_loop(monkeypatch) -> None:
    """长期清理任务必须在同一循环创建、复用并完成关闭。"""
    manager = AgentManager()
    memory_manager = MemoryManager()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    current_loop = asyncio.get_running_loop()

    await manager.initialize()
    idle_cleanup_task = manager._idle_cleanup_task
    memory_cleanup_task = memory_manager.cleanup_task

    assert idle_cleanup_task is not None
    assert memory_cleanup_task is not None
    assert idle_cleanup_task.get_loop() is current_loop
    assert memory_cleanup_task.get_loop() is current_loop
    assert not idle_cleanup_task.done()
    assert not memory_cleanup_task.done()

    await manager.initialize()
    assert manager._idle_cleanup_task is idle_cleanup_task
    assert memory_manager.cleanup_task is memory_cleanup_task

    await manager.close()
    await manager.close()
    assert manager._idle_cleanup_task is None
    assert memory_manager.cleanup_task is None
    assert idle_cleanup_task.done()
    assert memory_cleanup_task.done()


@pytest.mark.anyio
async def test_agent_entrypoint_reuses_tasks_and_closes_idempotently(
        monkeypatch,
) -> None:
    """全局启停入口重复调用时必须复用任务并安全收口。"""
    manager = AgentManager()
    memory_manager = MemoryManager()
    initializer = agent_initializer.AgentInitializer()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    monkeypatch.setattr(agent_initializer.settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr(agent_initializer, "agent_manager", manager)
    monkeypatch.setattr(agent_initializer, "agent_initializer", initializer)

    assert await agent_initializer.init_agent() is True
    idle_cleanup_task = manager._idle_cleanup_task
    memory_cleanup_task = memory_manager.cleanup_task
    assert await agent_initializer.init_agent() is True
    assert manager._idle_cleanup_task is idle_cleanup_task
    assert memory_manager.cleanup_task is memory_cleanup_task

    await agent_initializer.stop_agent()
    await agent_initializer.stop_agent()
    assert initializer._initialized is False
    assert manager._idle_cleanup_task is None
    assert memory_manager.cleanup_task is None


@pytest.mark.anyio
async def test_agent_initialization_failure_does_not_stop_module_startup(
        monkeypatch,
) -> None:
    """Agent 初始化异常只关闭该能力，基础模块仍继续完成启动。"""
    manager = AsyncMock()
    manager.initialize.side_effect = RuntimeError("agent init failed")
    monkeypatch.setattr(agent_initializer.settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr(agent_initializer, "agent_manager", manager)
    monkeypatch.setattr(
        agent_initializer,
        "agent_initializer",
        agent_initializer.AgentInitializer(),
    )
    monkeypatch.setattr(modules_initializer, "init_agent", agent_initializer.init_agent)

    for name in (
        "DisplayHelper",
        "DohHelper",
        "SitesHelper",
        "ResourceHelper",
        "ModuleManager",
    ):
        monkeypatch.setattr(modules_initializer, name, MagicMock())
    monkeypatch.setattr(modules_initializer, "user_auth", MagicMock())
    monkeypatch.setattr(modules_initializer.EventManager, "start", MagicMock())
    for name in (
        "init_plugin_report",
        "init_subscribe_report",
        "get_user_uuid",
        "get_github_user",
    ):
        monkeypatch.setattr(
            modules_initializer.MoviePilotServerHelper,
            name,
            MagicMock(),
        )
    start_frontend = MagicMock()
    check_auth = MagicMock()
    monkeypatch.setattr(modules_initializer, "start_frontend", start_frontend)
    monkeypatch.setattr(modules_initializer, "check_auth", check_auth)

    await modules_initializer.init_modules()

    manager.initialize.assert_awaited_once_with()
    start_frontend.assert_called_once_with()
    check_auth.assert_called_once_with()


@pytest.mark.anyio
async def test_disabled_agent_does_not_create_background_tasks(monkeypatch) -> None:
    """Agent 未启用时启动入口不得创建运行时任务。"""
    manager = AsyncMock()
    monkeypatch.setattr(agent_initializer.settings, "AI_AGENT_ENABLE", False)
    monkeypatch.setattr(agent_initializer, "agent_manager", manager)
    monkeypatch.setattr(
        agent_initializer,
        "agent_initializer",
        agent_initializer.AgentInitializer(),
    )

    assert await agent_initializer.init_agent() is True
    manager.initialize.assert_not_awaited()
