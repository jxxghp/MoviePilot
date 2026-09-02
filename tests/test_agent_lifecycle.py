import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.agent.orchestrator as agent_module
import app.agent.session as agent_session
from app.agent.memory import MemoryManager
from app.agent.orchestrator import (
    AGENT_SESSION_QUEUE_MAX_SIZE,
    AgentManager,
    AgentManagerQueueFullError,
    AgentManagerUnavailableError,
)
from app.agent.tools.base import reopen_blocking_executors
from app.application import query as query_application
from app.application.messaging.agent import (
    create_web_agent_background_task,
    shutdown_web_agent_background_tasks,
)
from app.sdk import queries as query_sdk
from app.startup.initializers import agent as agent_initializer
from app.startup.initializers import modules as modules_initializer


@pytest.mark.anyio
async def test_custom_moviepilot_agent_factory_receives_runtime_context() -> None:
    """Web/OpenAI Agent 子类必须复用 manager 装配的数据与记忆上下文。"""
    data = MagicMock()
    memory = MagicMock()
    manager = AgentManager(data=data, memory=memory)

    class CustomMoviePilotAgent(agent_module.MoviePilotAgent):
        async def process(self, message: str, **kwargs: object) -> str:
            return message

    result = await manager._process_message_internal(
        agent_session._MessageTask(
            session_id="custom-context",
            user_id="1",
            message="ok",
            agent_factory=CustomMoviePilotAgent,
        )
    )

    agent = manager.active_agents["custom-context"]
    assert result == "ok"
    assert agent._data is data
    assert agent._memory is memory


@pytest.mark.anyio
async def test_web_agent_background_tasks_are_cancelled_and_drained() -> None:
    """Web Agent 任务关闭后不得继续占用循环或提交晚到的快照。"""
    started = asyncio.Event()
    finished = asyncio.Event()

    async def blocked_task() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    task = create_web_agent_background_task(blocked_task())
    await started.wait()
    await shutdown_web_agent_background_tasks()

    assert task.done()
    assert task.cancelled()
    assert finished.is_set()


@pytest.mark.anyio
async def test_web_agent_shutdown_timeout_does_not_cancel_task_cleanup() -> None:
    """关闭超时时保留仍在执行取消收尾的 Web Agent 任务。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def task_with_slow_cleanup() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise

    task = create_web_agent_background_task(task_with_slow_cleanup())
    await started.wait()
    shutdown = asyncio.create_task(shutdown_web_agent_background_tasks())
    await asyncio.sleep(0)
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert task.done() is False
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_agent_entrypoint_initializes_on_calling_loop(monkeypatch) -> None:
    """Agent 启动入口必须在应用主循环完成初始化。"""
    current_loop = asyncio.get_running_loop()
    initialized_loops = []
    manager = AsyncMock()

    async def initialize() -> None:
        initialized_loops.append(asyncio.get_running_loop())

    manager.initialize.side_effect = initialize
    _patch_agent_settings(monkeypatch, True)
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
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
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

    assert await manager.close() is True
    assert await manager.close() is True
    assert manager._idle_cleanup_task is None
    assert memory_manager.cleanup_task is None
    assert idle_cleanup_task.done()
    assert memory_cleanup_task.done()


@pytest.mark.anyio
async def test_agent_entrypoint_reuses_tasks_and_closes_idempotently(
    monkeypatch,
) -> None:
    """全局启停入口重复调用时必须复用任务并安全收口。"""
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
    initializer = agent_initializer.AgentInitializer()
    _patch_agent_settings(monkeypatch, True)
    monkeypatch.setattr(agent_initializer, "agent_manager", manager)
    monkeypatch.setattr(agent_initializer, "agent_initializer", initializer)

    assert await agent_initializer.init_agent() is True
    idle_cleanup_task = manager._idle_cleanup_task
    memory_cleanup_task = memory_manager.cleanup_task
    assert await agent_initializer.init_agent() is True
    assert manager._idle_cleanup_task is idle_cleanup_task
    assert memory_manager.cleanup_task is memory_cleanup_task

    try:
        await agent_initializer.stop_agent()
        await agent_initializer.stop_agent()
        assert initializer._initialized is False
        assert manager._idle_cleanup_task is None
        assert memory_manager.cleanup_task is None
    finally:
        assert reopen_blocking_executors() is True


@pytest.mark.anyio
async def test_agent_initialization_failure_does_not_stop_module_startup(
    monkeypatch,
) -> None:
    """Agent 初始化异常只关闭该能力，基础模块仍继续完成启动。"""
    manager = AsyncMock()
    manager.initialize.side_effect = RuntimeError("agent init failed")
    _patch_agent_settings(monkeypatch, True)
    monkeypatch.setattr(agent_initializer, "agent_manager", manager)
    monkeypatch.setattr(
        agent_initializer,
        "agent_initializer",
        agent_initializer.AgentInitializer(),
    )
    monkeypatch.setattr(modules_initializer, "init_agent", agent_initializer.init_agent)

    for name in (
        "SitesHelper",
        "ModuleManager",
    ):
        monkeypatch.setattr(modules_initializer, name, MagicMock())
    monkeypatch.setattr(modules_initializer, "configure_doh_composition", MagicMock())
    monkeypatch.setattr(modules_initializer, "update_resources", MagicMock())
    monkeypatch.setattr(modules_initializer, "init_managed_resources", MagicMock())
    monkeypatch.setattr(modules_initializer, "user_auth", MagicMock())
    monkeypatch.setattr(modules_initializer.EventManager, "start", MagicMock())
    for name in (
        "async_init_plugin_report",
        "async_init_subscribe_report",
        "get_user_uuid",
        "get_github_user",
    ):
        monkeypatch.setattr(
            modules_initializer.MoviePilotServerHelper,
            name,
            AsyncMock() if name.startswith("async_") else MagicMock(),
        )
    start_frontend = MagicMock()
    check_auth = MagicMock()
    monkeypatch.setattr(modules_initializer, "start_frontend", start_frontend)
    monkeypatch.setattr(modules_initializer, "check_auth", check_auth)
    monkeypatch.setattr(query_application, "_configured_data_query_service", None)

    try:
        runtime = await modules_initializer.init_modules()
        assert runtime.workflow.system_config() is (modules_initializer.get_configured_system_config())
        query_page = await query_sdk.async_list_subscriptions({"ids": [-1]})
        assert query_page.items == []
        assert query_page.total == 0
        history_repository = runtime.agent.subscription_history
        assert (
            await history_repository.async_list_by_type(
                "不存在的订阅类型",
                page=1,
                count=1,
            )
            == []
        )
    finally:
        await modules_initializer.stop_database_runtime()

    manager.initialize.assert_awaited_once_with()
    start_frontend.assert_called_once_with()
    check_auth.assert_called_once_with()


@pytest.mark.anyio
async def test_disabled_agent_does_not_create_background_tasks(monkeypatch) -> None:
    """Agent 未启用时启动入口不得创建运行时任务。"""
    manager = AsyncMock()
    _patch_agent_settings(monkeypatch, False)
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
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)

    with pytest.raises(AgentManagerUnavailableError):
        await manager.process_message("before-init", "1", "hello")

    await manager.initialize()
    manager._process_message_internal = AsyncMock(return_value="accepted")
    assert (
        await manager.process_message(
            "running",
            "1",
            "hello",
            wait_for_completion=True,
        )
        == "accepted"
    )
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
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
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
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
    started = asyncio.Event()

    async def process(_task):
        started.set()
        return "done"

    manager._process_message_internal = process
    await manager.initialize()
    assert (
        await manager.process_message(
            "queue-observe",
            "1",
            "message",
            wait_for_completion=True,
        )
        == "done"
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    status = manager.get_session_status("queue-observe")
    assert status["last_queue_wait_ms"] >= 0
    await manager.close()


@pytest.mark.anyio
async def test_agent_manager_rejects_new_messages_while_worker_shutdown_is_pending(
    monkeypatch,
) -> None:
    """worker 未在关停上限内收敛时，同一会话必须保持停止态。"""
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
    manager._shutdown_timeout = 0.01
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
    memory_manager = MemoryManager()
    memory_manager.clear_memory = MagicMock()
    manager = AgentManager(memory=memory_manager)
    manager._shutdown_timeout = 0.01
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
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
    manager._shutdown_timeout = 0.01
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

    assert await manager.close() is False
    assert await manager.close() is False
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
    assert await manager.close() is True


@pytest.mark.anyio
async def test_close_retains_agent_until_detached_subagent_converges(
    monkeypatch,
) -> None:
    """Agent cleanup 返回 False 时 manager 必须保留 agent 和共享记忆 owner。"""
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)

    class PendingSubagentOwner:
        """首次清理未收敛、第二次清理成功的最小 Agent 替身。"""

        def __init__(self) -> None:
            """初始化封口计数与可重试清理结果。"""
            self.seal_count = 0
            self.cleanup_results = iter((False, True))

        def begin_shutdown(self) -> None:
            """记录 manager 在首个 await 前封住了子代理提交。"""
            self.seal_count += 1

        async def cleanup(self) -> bool:
            """按测试序列返回 detached owner 的收敛状态。"""
            return next(self.cleanup_results)

    owner = PendingSubagentOwner()
    await manager.initialize()
    manager.active_agents["detached-owner"] = owner

    assert await manager.close() is False
    assert manager.active_agents == {"detached-owner": owner}
    assert owner.seal_count == 1
    assert memory_manager.cleanup_task is not None

    assert await manager.close() is True
    assert manager.active_agents == {}
    assert owner.seal_count == 2
    assert memory_manager.cleanup_task is None


@pytest.mark.anyio
async def test_agent_manager_close_serializes_racing_enqueue_and_clear(
    monkeypatch,
) -> None:
    """关闭、临时会话清理和迟到请求必须串行收口且只清理一次。"""
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
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
    late_enqueue = asyncio.create_task(manager.process_message("late", "1", "hello"))
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
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
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
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
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


@pytest.mark.anyio
async def test_stop_current_task_handles_worker_cancelled_before_first_run() -> None:
    """worker 尚未首次运行时停止也必须完成收口。"""
    manager = AgentManager()
    session_id = "stop-before-worker-start"
    worker = None
    await manager.initialize()

    try:
        await manager.process_message(session_id, "1", "message")
        worker = manager._session_workers[session_id]
        assert worker.done() is False

        assert await manager.stop_current_task(session_id) is True
        assert worker.done() is True
        assert session_id not in manager._session_workers
        assert session_id not in manager._session_queues
    finally:
        if worker is not None:
            if not worker.done():
                worker.cancel()
            try:
                await worker
            except BaseException:
                pass
            if manager._session_workers.get(session_id) is worker:
                manager._session_workers.pop(session_id, None)
        if manager._accepting_tasks:
            await manager.close()


@pytest.mark.anyio
async def test_clear_session_cancellation_does_not_stick_cleanup_pending(
    monkeypatch,
) -> None:
    """clear_session 被调用方取消后必须能重试或已转交延迟清理。"""
    memory_manager = MemoryManager()
    memory_manager.clear_memory = MagicMock()
    manager = AgentManager(memory=memory_manager)
    session_id = "clear-caller-cancelled"
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()
    execution = None
    clear_request = None

    class BlockingAgent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        async def process(self, _message, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
                raise

        async def cleanup(self):
            return True

        def get_session_status(self):
            return {}

    await manager.initialize()
    try:
        execution = asyncio.create_task(
            manager.process_message(
                session_id,
                "1",
                "message",
                agent_factory=BlockingAgent,
                wait_for_completion=True,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        clear_request = asyncio.create_task(manager.clear_session(session_id, "1"))
        await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
        assert clear_request.done() is False
        clear_request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await clear_request

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await execution

        # 取消安全的延迟清理可能异步完成；若未发生转交，重试仍必须完成清理。
        await asyncio.wait_for(
            manager.clear_session(session_id, "1"),
            timeout=1,
        )
        for _ in range(100):
            if session_id not in manager._session_cleanup_pending and session_id not in manager.active_agents:
                break
            await asyncio.sleep(0)

        assert session_id not in manager._session_cleanup_pending
        assert session_id not in manager._session_shutdown_pending
        assert session_id not in manager._session_deferred_cleanup_tasks
        assert session_id not in manager._session_workers
        assert session_id not in manager._session_queues
        assert session_id not in manager.active_agents
        assert memory_manager.clear_memory.call_count == 1
    finally:
        release.set()
        if clear_request is not None and not clear_request.done():
            clear_request.cancel()
            try:
                await clear_request
            except BaseException:
                pass
        if execution is not None and not execution.done():
            execution.cancel()
            try:
                await execution
            except BaseException:
                pass
        for deferred in list(manager._session_deferred_cleanup_tasks.values()):
            if not deferred.done():
                deferred.cancel()
            try:
                await deferred
            except BaseException:
                pass
        if manager._accepting_tasks:
            await manager.close()


@pytest.mark.anyio
async def test_session_worker_restarts_after_idle_timeout_races_with_full_enqueue(
    monkeypatch,
) -> None:
    """空闲退出与满队列入队交错时必须保留会话消费者。"""
    memory_manager = MemoryManager()
    manager = AgentManager(memory=memory_manager)
    session_id = "idle-timeout-full-queue"
    processed = []
    first_processed = asyncio.Event()
    idle_waiting = asyncio.Event()
    release_timeout = asyncio.Event()
    timeout_intercepted = False
    real_wait_for = asyncio.wait_for

    async def process(task):
        processed.append(task.message)
        if task.message == "initial":
            first_processed.set()
        return task.message

    async def controlled_wait_for(awaitable, timeout):
        nonlocal timeout_intercepted
        if timeout == 60.0 and processed and not timeout_intercepted:
            timeout_intercepted = True
            idle_waiting.set()
            await release_timeout.wait()
            awaitable.close()
            raise asyncio.TimeoutError
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(agent_module.asyncio, "wait_for", controlled_wait_for)
    await manager.initialize()
    try:
        manager._process_message_internal = process
        await manager.process_message(session_id, "1", "initial")
        await real_wait_for(first_processed.wait(), timeout=1)
        await real_wait_for(idle_waiting.wait(), timeout=1)

        for index in range(AGENT_SESSION_QUEUE_MAX_SIZE):
            await manager.process_message(
                session_id,
                "1",
                f"queued-{index}",
            )

        assert manager._session_queues[session_id].full()
        release_timeout.set()

        async def all_messages_processed() -> None:
            while len(processed) < AGENT_SESSION_QUEUE_MAX_SIZE + 1:
                await asyncio.sleep(0)

        await real_wait_for(all_messages_processed(), timeout=1)
        assert processed == [
            "initial",
            *[f"queued-{index}" for index in range(AGENT_SESSION_QUEUE_MAX_SIZE)],
        ]
        worker = manager._session_workers.get(session_id)
        assert worker is not None
        assert worker.done() is False
    finally:
        release_timeout.set()
        if manager._accepting_tasks:
            await manager.clear_session(session_id, "1")
            await manager.close()


def _patch_agent_settings(monkeypatch, enabled: bool) -> None:
    """注入 Agent 启动测试所需的只读配置。"""
    monkeypatch.setattr(
        agent_initializer,
        "get_runtime_setting",
        lambda key: enabled if key == "AI_AGENT_ENABLE" else None,
    )
