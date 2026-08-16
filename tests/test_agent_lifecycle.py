import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.agent.orchestrator as agent_module
from app.agent import AgentManager
from app.agent.orchestrator import AgentManagerUnavailableError
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
        "DohHelper",
        "SitesHelper",
        "ResourceHelper",
        "ModuleManager",
    ):
        monkeypatch.setattr(modules_initializer, name, MagicMock())
    monkeypatch.setattr(modules_initializer, "init_managed_resources", MagicMock())
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


@pytest.mark.anyio
async def test_agent_manager_acceptance_gate_rejects_stale_references(
        monkeypatch,
) -> None:
    """未启动和关闭后的 manager 引用不得创建队列、worker 或 Agent。"""
    manager = AgentManager()
    memory_manager = MemoryManager()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)

    with pytest.raises(AgentManagerUnavailableError):
        await manager.process_message("before-init", "1", "hello")

    await manager.initialize()
    manager._process_message_internal = AsyncMock(return_value="accepted")
    assert await manager.process_message(
        "running",
        "1",
        "hello",
        wait_for_completion=True,
    ) == "accepted"
    await manager.close()

    with pytest.raises(AgentManagerUnavailableError):
        await manager.process_message("after-close", "1", "hello")
    assert manager._session_queues == {}
    assert manager._session_workers == {}
    assert manager.active_agents == {}


@pytest.mark.anyio
async def test_agent_manager_close_serializes_racing_enqueue_and_clear(
        monkeypatch,
) -> None:
    """关闭、临时会话清理和迟到请求必须串行收口且只清理一次。"""
    manager = AgentManager()
    memory_manager = MemoryManager()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    created = []
    cleanup_calls = []

    class BlockingAgent:
        """用于放大 close 与请求级 clear 竞态窗口。"""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            created.append(self)

        async def process(self, _message, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        async def cleanup(self):
            cleanup_calls.append(self)
            cleanup_started.set()
            await release_cleanup.wait()

    await manager.initialize()
    waiter = asyncio.create_task(
        manager.process_message(
            "closing",
            "1",
            "hello",
            agent_factory=BlockingAgent,
            wait_for_completion=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    close_task = asyncio.create_task(manager.close())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    late_enqueue = asyncio.create_task(
        manager.process_message("late", "1", "hello")
    )
    request_clear = asyncio.create_task(manager.clear_session("closing", "1"))
    await asyncio.sleep(0)
    assert not late_enqueue.done()
    assert not request_clear.done()

    release_cleanup.set()
    await asyncio.wait_for(close_task, timeout=1)
    with pytest.raises(AgentManagerUnavailableError):
        await late_enqueue
    await request_clear
    with pytest.raises(AgentManagerUnavailableError):
        await waiter

    assert len(created) == 1
    assert cleanup_calls == created
    assert manager._session_queues == {}
    assert manager._session_workers == {}
    assert manager.active_agents == {}


@pytest.mark.anyio
async def test_clear_session_settles_current_and_queued_waiters(monkeypatch) -> None:
    """清空会话必须同时结束正在执行和尚未执行的等待请求。"""
    manager = AgentManager()
    memory_manager = MemoryManager()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    started = asyncio.Event()

    async def block_current(_task):
        started.set()
        await asyncio.Event().wait()

    manager._process_message_internal = block_current
    await manager.initialize()
    current_waiter = asyncio.create_task(
        manager.process_message(
            "session-with-queue",
            "1",
            "current",
            wait_for_completion=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    queued_waiter = asyncio.create_task(
        manager.process_message(
            "session-with-queue",
            "1",
            "queued",
            wait_for_completion=True,
        )
    )
    await asyncio.sleep(0)

    await asyncio.wait_for(
        manager.clear_session("session-with-queue", "1"),
        timeout=1,
    )

    with pytest.raises(asyncio.CancelledError):
        await current_waiter
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(queued_waiter, timeout=1)
    assert "session-with-queue" not in manager._session_queues
    assert "session-with-queue" not in manager._session_workers
    await manager.close()


@pytest.mark.anyio
async def test_background_prompt_is_owned_and_cancelled_by_manager_close(
        monkeypatch,
) -> None:
    """后台 prompt 必须进入 manager worker，关闭时同步结束且不残留临时会话。"""
    manager = AgentManager()
    memory_manager = MemoryManager()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    started = asyncio.Event()

    async def block_background(task):
        assert task.session_id.startswith("__managed_background_")
        started.set()
        await asyncio.Event().wait()

    manager._process_message_internal = block_background
    await manager.initialize()
    execution = asyncio.create_task(
        manager.run_background_prompt(
            "background",
            session_prefix="__managed_background",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert len(manager._session_workers) == 1

    await manager.close()
    with pytest.raises(AgentManagerUnavailableError):
        await execution
    assert manager._session_queues == {}
    assert manager._session_workers == {}
    assert manager.active_agents == {}
