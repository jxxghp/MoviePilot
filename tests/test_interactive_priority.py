"""
交互执行隔离、HTTP 传播、有限避让与关闭合同的离线回归。
"""

import asyncio
import threading
import time
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from anyio.to_thread import current_default_thread_limiter, run_sync

from app.adapters.web.priority import ExecutionPriorityMiddleware
from app.agent.api import executor as api_module
from app.agent.api.executor import ApiExecutionContext, MoviePilotApiExecutor
from app.agent.middleware.config import RuntimeConfigMiddleware
from app.agent.orchestrator import MoviePilotAgent
from app.agent.terminal.ownership import TerminalScope
from app.agent.tools import base as tool_module
from app.application.messaging.chat import AgentChatPersistenceService
from app.db.worker import DatabaseWorker
from app.runtime import priority
from app.runtime.execution import run_in_threadpool
from app.runtime.thread import ThreadHelper
from app.scheduler import execution as scheduler_execution
from app.scheduler.admission import defer_coroutine, invoke_background
from app.schemas.exception import DatabaseWorkerOverloadedError


@pytest.fixture(autouse=True)
def execution_settings(monkeypatch):
    """
    固定本文件的执行策略，避免依赖真实配置或等待生产默认预算。
    """
    settings = {"AGENT_INTERACTIVE_PRIORITY": True, "AGENT_BACKGROUND_DEFER_SECONDS": 0.1}
    monkeypatch.setattr(priority, "get_runtime_setting", lambda key, default=None: settings.get(key, default))
    return settings


async def _entered(event: threading.Event) -> None:
    """
    有界等待线程开始；超时明确失败，避免挂住测试进程。
    """
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.001)


def test_scope_restoration_expiry_and_disabled_policy(execution_settings) -> None:
    """
    轮次结束撤销复制上下文和 HTTP 凭据，禁用策略后不再取得保留容量。
    """
    assert not priority.is_interactive()
    with priority.interactive_execution():
        key = priority.interactive_key()
        copied = copy_context()
        with priority.interactive_execution():
            assert priority.interactive_key() == key
        with priority.interactive_execution(enabled=False):
            assert not priority.is_interactive()
            with priority.propagated_execution(key):
                assert priority.is_interactive()
            with priority.propagated_execution("forged"):
                assert not priority.is_interactive()
        assert priority.background_defer_seconds() == 0
        execution_settings["AGENT_INTERACTIVE_PRIORITY"] = False
        assert priority.interactive_key() is None
        assert priority.background_defer_seconds() == 0
    assert copied.run(priority.is_interactive) is False
    with priority.propagated_execution(key):
        assert not priority.is_interactive()


@pytest.mark.asyncio
async def test_http_only_restores_live_process_registered_key(monkeypatch) -> None:
    """
    仅真实宿主轮次可经 API 传播优先级，普通请求、过期和伪造头均无效。
    """
    monkeypatch.setattr(api_module, "create_access_token", lambda **_kwargs: "test-auth")
    executor = MoviePilotApiExecutor(context=ApiExecutionContext("1", "admin", True))
    observed = []

    async def app(_scope, _receive, _send):
        """
        观察下游请求实际看到的执行归属。
        """
        observed.append(priority.is_interactive())

    middleware = ExecutionPriorityMiddleware(app)
    with priority.interactive_execution():
        headers = executor._build_headers()
        key = headers[priority.PRIORITY_HEADER]
        assert headers["Authorization"] == "Bearer test-auth"
        with priority.interactive_execution(enabled=False):
            await middleware({"type": "http", "headers": [(b"x-moviepilot-execution", key.encode())]}, None, None)
            assert not priority.is_interactive()
    assert priority.PRIORITY_HEADER not in executor._build_headers()
    for value in (key, "forged", ""):
        await middleware({"type": "http", "headers": [(b"x-moviepilot-execution", value.encode())]}, None, None)
    await middleware({"type": "lifespan"}, None, None)
    assert observed == [True, False, False, False, False]


@pytest.mark.asyncio
async def test_anyio_interactive_and_gateway_capacity_survive_background_saturation() -> None:
    """
    普通宿主令牌用尽时框架入口仍执行，两组均忙时交互工具仍可取得保留容量。
    """
    limiter = current_default_thread_limiter()
    original = limiter.total_tokens
    limiter.total_tokens = 1
    entered = threading.Event()
    framework_entered = threading.Event()
    release = threading.Event()
    task = asyncio.create_task(run_in_threadpool(lambda: entered.set() or release.wait(3)))
    framework = None
    try:
        await _entered(entered)
        # FastAPI 的同步依赖和端点走库默认入口，也不能等待宿主后台调用归还令牌。
        assert await asyncio.wait_for(run_sync(lambda: "gateway"), 1) == "gateway"
        framework = asyncio.create_task(run_sync(lambda: framework_entered.set() or release.wait(3)))
        await _entered(framework_entered)
        with priority.interactive_execution():
            assert priority.interactive_thread_limiter() is priority.interactive_thread_limiter()
            assert await asyncio.wait_for(run_in_threadpool(priority.is_interactive), 1) is True
        assert priority.background_thread_limiter() is priority.background_thread_limiter()
        assert not task.done()
    finally:
        release.set()
        await task
        if framework is not None:
            await framework
        limiter.total_tokens = original


@pytest.mark.asyncio
async def test_database_reservation_is_bounded_and_cancelled_transaction_keeps_owner() -> None:
    """
    普通 DB 队列满时交互仍执行，交互容量也有上限，取消等待真实事务结束。
    """
    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    background_entered = threading.Event()
    interactive_entered = threading.Event()
    release = threading.Event()
    interactive_release = threading.Event()
    background = asyncio.create_task(worker.run(lambda: background_entered.set() or release.wait(3)))
    tasks = []
    try:
        await _entered(background_entered)
        with pytest.raises(DatabaseWorkerOverloadedError):
            await worker.run(lambda: None)
        with priority.interactive_execution():
            assert await asyncio.wait_for(worker.run(priority.is_interactive), 1) is True
            active = asyncio.create_task(worker.run(lambda: interactive_entered.set() or interactive_release.wait(3)))
            tasks.append(active)
            await _entered(interactive_entered)
            for _ in range(priority.INTERACTIVE_DATABASE_CAPACITY - 1):
                tasks.append(asyncio.create_task(worker.run(lambda: None)))
            await asyncio.sleep(0)
            with pytest.raises(DatabaseWorkerOverloadedError):
                await worker.run(lambda: None)
            active.cancel()
            await asyncio.sleep(0)
            assert not active.done()
            interactive_release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert await worker.run(lambda: "reused") == "reused"
            assert worker.interactive_snapshot().running == 0
            assert worker._interactive_worker.interactive_snapshot() is None
        assert not background.done()
    finally:
        release.set()
        interactive_release.set()
        await asyncio.gather(background, *tasks, return_exceptions=True)
        await worker.shutdown()


def test_thread_helper_reservation_shutdown_and_reopen(monkeypatch) -> None:
    """
    普通池占满时使用交互池；关闭先共同封口，未收敛时不得创建替代 owner。
    """
    from app.runtime import thread as thread_module

    monkeypatch.setattr(thread_module, "get_runtime_setting", lambda _key: SimpleNamespace(threadpool=1))
    helper = object.__new__(ThreadHelper)
    helper.__init__()
    entered = threading.Event()
    release = threading.Event()
    future = helper.submit(lambda: entered.set() or release.wait(3))
    try:
        assert entered.wait(1)
        with priority.interactive_execution():
            assert helper.submit(priority.is_interactive).result(1) is True
            assert helper.shutdown(timeout=0.01) is False
            with pytest.raises(RuntimeError):
                helper.submit(lambda: None)
        assert helper.reopen() is False
    finally:
        release.set()
        future.result(1)
        assert helper.shutdown(timeout=1)
    assert helper.reopen()
    with priority.interactive_execution():
        assert helper.submit(lambda: "restored").result(1) == "restored"
    assert helper.shutdown(timeout=1)


@pytest.mark.asyncio
async def test_tool_bucket_background_saturation_does_not_block_interactive() -> None:
    """
    自主任务占满某工具桶后，交互工具仍获得独立额度并保留归属。
    """
    release = threading.Event()
    entered = [threading.Event() for _ in range(tool_module._BLOCKING_BUCKET_LIMITS["web"])]
    background = [asyncio.create_task(tool_module.run_agent_blocking("web", lambda e=e: e.set() or release.wait(3))) for e in entered]
    try:
        for event in entered:
            await _entered(event)
        with priority.interactive_execution():
            assert await asyncio.wait_for(tool_module.run_agent_blocking("web", priority.is_interactive), 1) is True
        assert all(not task.done() for task in background)
    finally:
        release.set()
        await asyncio.gather(*background)
        assert await tool_module.close_blocking_executors(timeout_seconds=1)
        assert tool_module.reopen_blocking_executors()


@pytest.mark.asyncio
async def test_background_defer_resumes_and_does_not_starve(execution_settings) -> None:
    """
    活跃轮次使后台等待，交互结束立即恢复；持续对话只等待配置预算。
    """
    job = {"owner": "subscription"}
    def accepting():
        """
        保持调度器运行，用于验证避让期限。
        """
        return True
    with priority.interactive_execution():
        with priority.interactive_execution(enabled=False):
            task = asyncio.create_task(defer_coroutine(job, accepting))
        await asyncio.sleep(0.02)
        assert job["deferred"] and not task.done()
    await asyncio.wait_for(task, 1)
    assert job["deferred"] is False
    execution_settings["AGENT_BACKGROUND_DEFER_SECONDS"] = 0.01
    with priority.interactive_execution():
        with priority.interactive_execution(enabled=False):
            await asyncio.wait_for(defer_coroutine(job, accepting), 1)
            priority.wait_for_interactive_idle(0.01, accepting)
            for exempt in ({"owner": "outbox"}, {"manual": True}):
                await asyncio.wait_for(defer_coroutine(exempt, accepting), 1)
            with pytest.raises(asyncio.CancelledError):
                await defer_coroutine(job, lambda: False)
    assert not job["deferred"]


@pytest.mark.asyncio
async def test_cancelling_deferred_job_clears_state() -> None:
    """
    等待交互期间取消任务会复原展示状态，不会继续调用业务函数。
    """
    job = {"owner": "plugin:test"}
    with priority.interactive_execution():
        with priority.interactive_execution(enabled=False):
            task = asyncio.create_task(defer_coroutine(job, lambda: True))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert job["deferred"] is False


def test_sync_deferred_job_does_not_start_after_shutdown() -> None:
    """
    关闭期间尚未进入业务函数的同步任务不得继续执行。
    """
    with pytest.raises(RuntimeError, match="尚未开始"):
        invoke_background({}, lambda: False, lambda: pytest.fail("任务不应执行"))


def test_sync_background_wait_is_bounded_and_resets_inherited_priority() -> None:
    """
    同步任务按预算避让，并在真正执行时撤销意外继承的交互归属。
    """
    job = {"owner": "site"}
    with priority.interactive_execution():
        assert invoke_background(job, lambda: True, priority.is_interactive) is False
        with priority.interactive_execution(enabled=False):
            started = time.monotonic()
            assert invoke_background(job, lambda: True, lambda: "completed") == "completed"
            assert time.monotonic() - started >= 0.05
    assert job["deferred"] is False


@pytest.mark.asyncio
async def test_sync_entry_on_loop_does_not_block_for_defer() -> None:
    """
    兼容的主循环同步入口不能再同步等待交互结束。
    """
    with priority.interactive_execution():
        assert invoke_background({}, lambda: True, priority.is_interactive) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_agent_process_binds_interactive_only_for_foreground(monkeypatch, scheduled) -> None:
    """
    真实 Agent 入口在交互轮次绑定优先级，定时任务即使带回传渠道也不升级。
    """
    agent = MoviePilotAgent("priority-test", user_id="1", channel="Web", source="web-agent")
    observed = []

    async def process(_message, **_kwargs):
        """
        代替模型推理记录实际调用上下文。
        """
        observed.append(priority.is_interactive())
        return "done"

    monkeypatch.setattr(agent, "_process", process)
    scope = TerminalScope("1", "run", "scheduled") if scheduled else None
    assert await agent.process("message", terminal_scope=scope) == "done"
    assert observed == [not scheduled]
    assert not priority.is_interactive()
    assert await agent.cleanup()


@pytest.mark.asyncio
async def test_model_request_timeout_and_disabled_timeout(monkeypatch) -> None:
    """
    模型请求超时包含内部重试，取消真正传入 handler；0 保留无限等待语义。
    """
    from app.agent.middleware import config as config_module

    middleware = RuntimeConfigMiddleware()
    monkeypatch.setattr(middleware, "modify_request", lambda request: request)
    monkeypatch.setattr(config_module, "get_runtime_setting", lambda *_args: 0.01)
    cancelled = asyncio.Event()

    async def handler(_request):
        """
        持有模型请求直到超时，并记录取消传播。
        """
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(TimeoutError):
        await middleware.awrap_model_call(None, handler)
    assert cancelled.is_set()
    monkeypatch.setattr(config_module, "get_runtime_setting", lambda *_args: 0)
    assert await middleware.awrap_model_call(None, AsyncMock(return_value="done")) == "done"


def test_timeout_notice_requires_checking_existing_write_status() -> None:
    """
    超时提示必须说明操作仍可能收尾，避免用户直接重复执行写入。
    """
    message = MoviePilotAgent._friendly_execution_error_message(TimeoutError())
    assert "超时" in message
    assert "先确认状态" in message


@pytest.mark.asyncio
async def test_chat_admission_keeps_foreground_capacity_when_background_is_full() -> None:
    """
    聊天写入的上层准入同样隔离，避免请求到达保留 DB worker 前就被后台挤出。
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    class Executor:
        """
        用异步屏障隔离数据库 I/O，观察聊天服务自己的准入。
        """

        async def run(self, operation):
            """
            后台持有容量，交互直接完成；不访问真实仓储。
            """
            if not priority.is_interactive():
                entered.set()
                await release.wait()

    service = AgentChatPersistenceService(
        repository=lambda _session: None, async_executor=Executor(),
        sync_transaction=lambda operation: None, capacity=1,
    )
    background = asyncio.create_task(service._run_write("background", lambda _repository: None))
    try:
        await entered.wait()
        with priority.interactive_execution():
            await service._run_write("interactive", lambda _repository: None)
        assert not background.done()
        assert service._pending_writes == 1
        assert service._pending_interactive_writes == 0
    finally:
        release.set()
        await background
        await service.shutdown()
    assert service._pending_writes == 0


@pytest.mark.asyncio
async def test_scheduler_applies_default_timeout_and_finishes_timed_out_job(monkeypatch) -> None:
    """
    默认超时只填补协程任务的空声明，超时取消后由原 owner 发布失败终态。
    """
    owner = scheduler_execution.SchedulerExecutionOwner()
    owner._registry = SimpleNamespace(assign_generation=Mock())
    owner._lifecycle_state = "running"
    owner._finish_job = AsyncMock()
    owner._handle_job_error = Mock()
    monkeypatch.setattr(scheduler_execution, "get_runtime_setting", lambda *_args: 7)
    cancelled = asyncio.Event()

    async def task():
        """
        模拟需要收到取消才能结束的自动任务。
        """
        assert not priority.is_interactive()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    job = {"func": task, "owner": "site", "timeout_seconds": None}
    owner._assign_job_generation("job", job)
    assert job["timeout_seconds"] == 7
    job["timeout_seconds"] = 0.01
    owner._assign_job_generation("job", job)
    assert job["timeout_seconds"] == 0.01
    with priority.interactive_execution():
        await owner._run_coro_job(task, "job", job)
        assert priority.is_interactive()
    assert cancelled.is_set()
    assert owner._finish_job.await_args.kwargs["success"] is False
    assert "超时" in owner._finish_job.await_args.kwargs["error"]
    monkeypatch.setattr(scheduler_execution, "get_runtime_setting", lambda *_args: 0)
    unlimited = {"func": task}
    owner._assign_job_generation("unlimited", unlimited)
    assert unlimited["timeout_seconds"] is None
