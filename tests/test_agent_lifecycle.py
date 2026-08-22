import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.agent.orchestrator as agent_module
from app.agent import AgentManager
from app.agent.orchestrator import (
    AGENT_SESSION_QUEUE_MAX_SIZE,
    AgentManagerQueueFullError,
    AgentManagerUnavailableError,
)
from app.agent.memory import MemoryManager
from app.startup import agent_initializer, modules_initializer


@pytest.fixture
def anyio_backend():
    """使用 asyncio 后端运行 anyio 异步测试。

    AgentManager 的启动/关闭路径直接使用 ``asyncio.create_task`` /
    ``asyncio.get_running_loop`` 等仅限 asyncio 的原语，在 trio 后端下没有
    running asyncio loop，必然以 ``RuntimeError: no running event loop``
    失败；与业务逻辑无关，故不参数化到 trio。
    """
    return "asyncio"


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
async def test_agent_manager_rejects_messages_when_session_queue_is_full(
        monkeypatch,
) -> None:
    """会话达到待处理容量后应立即拒绝，不得在生命周期锁内无限等待。"""
    manager = AgentManager()
    memory_manager = MemoryManager()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    started = asyncio.Event()

    async def block_current(_task):
        started.set()
        await asyncio.Event().wait()

    manager._process_message_internal = block_current
    await manager.initialize()
    current = asyncio.create_task(
        manager.process_message(
            "bounded",
            "1",
            "current",
            wait_for_completion=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    queued = [
        asyncio.create_task(
            manager.process_message(
                "bounded",
                "1",
                f"queued-{index}",
                wait_for_completion=True,
            )
        )
        for index in range(AGENT_SESSION_QUEUE_MAX_SIZE)
    ]
    await asyncio.sleep(0)

    with pytest.raises(AgentManagerQueueFullError) as error_info:
        await manager.process_message(
            "bounded",
            "1",
            "rejected",
            wait_for_completion=True,
        )
    assert error_info.value.code == "agent_manager_queue_full"

    status = manager.get_session_status("bounded")
    assert status["pending_messages"] == AGENT_SESSION_QUEUE_MAX_SIZE
    assert status["queue_capacity"] == AGENT_SESSION_QUEUE_MAX_SIZE
    assert status["queue_saturated"] is True
    assert status["queue_rejections"] == 1

    await manager.close()
    results = await asyncio.gather(current, *queued, return_exceptions=True)
    assert all(isinstance(result, AgentManagerUnavailableError) for result in results)


@pytest.mark.anyio
async def test_agent_manager_records_queue_wait_time(monkeypatch) -> None:
    """任务开始执行后应保留最近一次排队等待的可观测值。"""
    manager = AgentManager()
    memory_manager = MemoryManager()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    started = asyncio.Event()

    async def process(_task):
        started.set()
        return "done"

    manager._process_message_internal = process
    await manager.initialize()
    assert await manager.process_message(
        "queue-observe",
        "1",
        "message",
        wait_for_completion=True,
    ) == "done"
    await asyncio.wait_for(started.wait(), timeout=1)

    status = manager.get_session_status("queue-observe")
    assert status["last_queue_wait_ms"] >= 0
    await manager.close()


@pytest.mark.anyio
async def test_agent_manager_rejects_new_messages_while_worker_shutdown_is_pending(
        monkeypatch,
) -> None:
    """worker 未在关停上限内收敛时，同一会话必须保持停止态。"""
    manager = AgentManager()
    manager._shutdown_timeout = 0.01
    memory_manager = MemoryManager()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    started = asyncio.Event()
    release = asyncio.Event()

    async def ignore_cancellation(_task):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    manager._process_message_internal = ignore_cancellation
    await manager.initialize()
    execution = asyncio.create_task(
        manager.process_message(
            "shutdown-boundary",
            "1",
            "current",
            wait_for_completion=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await manager.stop_current_task("shutdown-boundary") is True
    status = manager.get_session_status("shutdown-boundary")
    assert status["shutdown_pending"] is True
    with pytest.raises(AgentManagerUnavailableError):
        await manager.process_message(
            "shutdown-boundary",
            "1",
            "late",
        )

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=1)
    for _ in range(20):
        if not manager.get_session_status("shutdown-boundary")["shutdown_pending"]:
            break
        await asyncio.sleep(0)
    assert manager.get_session_status("shutdown-boundary")["shutdown_pending"] is False
    await manager.close()


@pytest.mark.anyio
async def test_clear_session_defers_agent_cleanup_until_worker_finishes(
        monkeypatch,
) -> None:
    """clear_session 超时期间不得清理仍被 worker 使用的 Agent 和记忆。"""
    manager = AgentManager()
    manager._shutdown_timeout = 0.01
    memory_manager = MemoryManager()
    memory_manager.clear_memory = MagicMock()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    started = asyncio.Event()
    release = asyncio.Event()
    cleanup_called = asyncio.Event()

    class BlockingAgent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def process(self, _message, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        async def cleanup(self):
            cleanup_called.set()

        def get_session_status(self):
            return {}

    await manager.initialize()
    execution = asyncio.create_task(
        manager.process_message(
            "clear-timeout",
            "1",
            "current",
            agent_factory=BlockingAgent,
            wait_for_completion=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    await manager.clear_session("clear-timeout", "1")
    assert not cleanup_called.is_set()
    assert memory_manager.clear_memory.call_count == 0
    assert "clear-timeout" in manager.active_agents
    assert manager.get_session_status("clear-timeout")["shutdown_pending"] is True

    release.set()
    await asyncio.wait_for(cleanup_called.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await execution
    for _ in range(20):
        if "clear-timeout" not in manager.active_agents:
            break
        await asyncio.sleep(0)
    assert "clear-timeout" not in manager.active_agents
    assert memory_manager.clear_memory.call_count == 1
    await manager.close()


@pytest.mark.anyio
async def test_close_defers_shared_agent_teardown_after_worker_timeout(
        monkeypatch,
) -> None:
    """管理器关闭超时后，旧 worker 收敛前不得拆除共享 Agent 资源。"""
    manager = AgentManager()
    manager._shutdown_timeout = 0.01
    memory_manager = MemoryManager()
    monkeypatch.setattr(agent_module, "memory_manager", memory_manager)
    started = asyncio.Event()
    release = asyncio.Event()
    cleanup_called = asyncio.Event()

    class BlockingAgent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def process(self, _message, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        async def cleanup(self):
            cleanup_called.set()

    await manager.initialize()
    execution = asyncio.create_task(
        manager.process_message(
            "close-timeout",
            "1",
            "current",
            agent_factory=BlockingAgent,
            wait_for_completion=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    await manager.close()
    assert not cleanup_called.is_set()
    assert "close-timeout" in manager.active_agents
    assert manager._close_finalizer_task is not None

    release.set()
    await asyncio.wait_for(cleanup_called.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await execution
    for _ in range(20):
        if manager._close_finalizer_task is None:
            break
        await asyncio.sleep(0)
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
