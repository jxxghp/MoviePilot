"""AgentChat 同步短事务经有界 worker 委托的应用端口测试。"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.application.database import (
    DatabaseWorkerClosedError,
    DatabaseWorkerOverloadedError,
)
from app.application.messaging.chat import AgentChatPersistenceService, AgentChatService
from app.api.endpoints.agent import save_agent_chat_display
from app.db.models.agentchat import AgentChat
from app.db.oper.agentchat import AgentChatOper
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import run_sync_transaction
from app.schemas.agent import AgentChatDisplaySaveRequest
from app.db.worker import DatabaseWorker


class _Executor:
    """用独立线程模拟 G2B worker，验证调用方不会直接执行同步仓储。"""

    def __init__(self) -> None:
        self.calls = 0
        self.worker_thread_id: int | None = None
        self.results: list[object] = []

    async def run(self, operation):
        """在线程中执行一个完整的同步操作。"""
        self.calls += 1

        def invoke():
            self.worker_thread_id = threading.get_ident()
            result = operation()
            self.results.append(result)
            return result

        return await asyncio.to_thread(invoke)


class _Repository:
    """记录 AgentChat 端口调用的同步仓储替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def append_display_messages(self, **kwargs):
        self.calls.append(("append_display_messages", kwargs))
        return None

    def save_display_messages(self, **kwargs):
        self.calls.append(("save_display_messages", kwargs))
        return None

    def save_agent_messages(self, **kwargs):
        self.calls.append(("save_agent_messages", kwargs))

    def update_title_if_empty(self, **kwargs):
        self.calls.append(("update_title_if_empty", kwargs))


@pytest.mark.asyncio
async def test_agent_chat_persistence_runs_sync_repository_inside_worker() -> None:
    """同步 AgentChat 写入必须经过一次 worker admission。"""
    executor = _Executor()
    repository = _Repository()
    service = AgentChatPersistenceService(
        repository=lambda _session: repository,
        async_executor=executor,
        sync_transaction=lambda operation: operation(object()),
    )
    caller_thread_id = threading.get_ident()

    await service.async_append_display_messages(
        session_id="session-1",
        user_id="1",
        messages=[{"role": "user", "content": "hello"}],
    )
    await service.async_save_display_messages(
        session_id="session-1",
        user_id="1",
        messages=[],
    )
    await service.async_save_agent_messages(
        session_id="session-1",
        user_id="1",
        messages=[],
    )
    await service.async_update_title_if_empty(
        session_id="session-1",
        user_id="1",
        title="标题",
    )

    assert executor.calls == 4
    assert executor.results == [None, None, None, None]
    assert executor.worker_thread_id != caller_thread_id
    assert [name for name, _kwargs in repository.calls] == [
        "append_display_messages",
        "save_display_messages",
        "save_agent_messages",
        "update_title_if_empty",
    ]


@pytest.mark.asyncio
async def test_agent_chat_persistence_propagates_worker_failure() -> None:
    """worker admission 或事务异常必须原样返回给 async 应用调用方。"""

    class FailingExecutor:
        async def run(self, _operation):
            raise RuntimeError("worker failed")

    service = AgentChatPersistenceService(
        repository=lambda _session: _Repository(),
        async_executor=FailingExecutor(),
        sync_transaction=lambda operation: operation(object()),
    )

    with pytest.raises(RuntimeError, match="worker failed"):
        await service.async_save_agent_messages(
            session_id="session-1",
            user_id="1",
            messages=[],
        )


@pytest.mark.asyncio
async def test_agent_chat_persistence_pending_metric_uses_deltas() -> None:
    """pending 是 UpDownCounter，准入和释放必须分别记录增减量。"""
    service = AgentChatPersistenceService(
        repository=lambda _session: _Repository(),
        async_executor=_Executor(),
        sync_transaction=lambda operation: operation(object()),
    )
    with patch("app.application.messaging.chat.record_metric") as record_metric:
        await service.async_save_agent_messages(
            session_id="metric-session",
            user_id="1",
            messages=[],
        )
    record_metric.assert_has_calls(
        [
            call("agent.chat.persistence.pending", 1),
            call("agent.chat.persistence.pending", -1),
        ]
    )


@pytest.mark.asyncio
async def test_authoritative_display_save_propagates_worker_overload() -> None:
    """权威 PUT 保存不能把 worker 背压吞成成功或普通业务失败。"""
    repository = AsyncMock()
    repository.async_get.return_value = None
    service = AgentChatService(repository=repository)

    class OverloadedPersistence:
        async def async_save_display_messages(self, **_kwargs):
            raise DatabaseWorkerOverloadedError("busy")

    with pytest.raises(DatabaseWorkerOverloadedError, match="busy"):
        await save_agent_chat_display(
            session_id="overloaded-session",
            payload=AgentChatDisplaySaveRequest(messages=[]),
            current_user=SimpleNamespace(id=1, name="admin", is_superuser=True),
            service=service,
            persistence=OverloadedPersistence(),
        )


@pytest.mark.asyncio
async def test_authoritative_display_save_reads_fresh_projection_after_worker_write(
        monkeypatch,
) -> None:
    """权威展示保存的响应必须读取 worker 提交后的最新投影。"""
    existing_chat = SimpleNamespace(
        user_id="1",
        username="admin",
        channel="WebAgent",
        source="web-agent",
        original_chat_id=None,
        client_session_id="client-1",
    )
    updated_chat = SimpleNamespace(
        session_id="fresh-session",
        message_count=2,
    )
    request_service = SimpleNamespace(
        get_accessible=AsyncMock(return_value=existing_chat),
        get=AsyncMock(return_value=existing_chat),
    )
    canonical_service = SimpleNamespace(
        get_accessible=AsyncMock(return_value=updated_chat),
        to_summary=MagicMock(return_value="fresh-summary"),
    )
    persistence = SimpleNamespace(async_save_display_messages=AsyncMock())
    current_user = SimpleNamespace(id=1, name="admin", is_superuser=True)
    monkeypatch.setattr(
        "app.api.endpoints.agent.get_configured_agent_chat_service",
        MagicMock(return_value=canonical_service),
    )

    response = await save_agent_chat_display(
        session_id="fresh-session",
        payload=AgentChatDisplaySaveRequest(messages=[]),
        current_user=current_user,
        service=request_service,
        persistence=persistence,
    )

    assert response.success is True
    assert response.data == "fresh-summary"
    canonical_service.get_accessible.assert_awaited_once_with(
        "fresh-session", current_user
    )


@pytest.mark.asyncio
async def test_agent_chat_persistence_rolls_back_compound_write(monkeypatch) -> None:
    """复合写入中途失败时，创建或更新不能留下半成品。"""
    worker = DatabaseWorker(max_workers=1, capacity=4)
    await worker.start()
    session_id = f"worker-rollback-{uuid4().hex}"
    persistence = AgentChatPersistenceService(
        repository=lambda session: AgentChatOper(session),
        async_executor=worker,
        sync_transaction=run_sync_transaction,
    )
    original = AgentChatOper.save_display_messages

    def fail_after_stage(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("display snapshot failed")

    monkeypatch.setattr(AgentChatOper, "save_display_messages", fail_after_stage)
    try:
        with pytest.raises(RuntimeError, match="display snapshot failed"):
            await persistence.async_append_display_messages(
                session_id=session_id,
                user_id="rollback-user",
                messages=[{"role": "user", "content": "not committed"}],
            )
        async with async_session_scope() as session:
            result = await session.execute(
                select(AgentChat).where(AgentChat.session_id == session_id)
            )
            assert result.scalars().first() is None
    finally:
        await worker.shutdown()


@pytest.mark.asyncio
async def test_agent_chat_persistence_bounds_session_waiters_and_releases_cancelled() -> None:
    """同会话锁等待受总量限制，取消等待不会遗留 admission。"""

    class BlockingExecutor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, operation):
            self.started.set()
            await self.release.wait()
            return operation()

    executor = BlockingExecutor()
    service = AgentChatPersistenceService(
        repository=lambda _session: _Repository(),
        async_executor=executor,
        sync_transaction=lambda operation: operation(object()),
        capacity=2,
        session_capacity=2,
    )
    first = asyncio.create_task(
        service.async_save_agent_messages(
            session_id="session-admission",
            user_id="1",
            messages=[],
        )
    )
    await executor.started.wait()
    second = asyncio.create_task(
        service.async_save_agent_messages(
            session_id="session-admission",
            user_id="1",
            messages=[],
        )
    )
    await asyncio.sleep(0)
    third = asyncio.create_task(
        service.async_save_agent_messages(
            session_id="session-admission",
            user_id="1",
            messages=[],
        )
    )
    with pytest.raises(DatabaseWorkerOverloadedError):
        await third
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert service._pending_writes == 1
    executor.release.set()
    await first
    assert service._pending_writes == 0


@pytest.mark.asyncio
async def test_agent_chat_persistence_session_admission_is_fair() -> None:
    """热点会话的锁等待不能占满全局容量并拒绝其他会话。"""

    class BlockingExecutor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, operation):
            self.started.set()
            await self.release.wait()
            return operation()

    executor = BlockingExecutor()
    service = AgentChatPersistenceService(
        repository=lambda _session: _Repository(),
        async_executor=executor,
        sync_transaction=lambda operation: operation(object()),
        capacity=4,
        session_capacity=2,
    )
    first = asyncio.create_task(
        service.async_save_agent_messages(
            session_id="hot-session", user_id="1", messages=[]
        )
    )
    await executor.started.wait()
    second = asyncio.create_task(
        service.async_save_agent_messages(
            session_id="hot-session", user_id="1", messages=[]
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(DatabaseWorkerOverloadedError):
        await service.async_save_agent_messages(
            session_id="hot-session", user_id="1", messages=[]
        )
    other = asyncio.create_task(
        service.async_save_agent_messages(
            session_id="other-session", user_id="1", messages=[]
        )
    )
    await asyncio.sleep(0)
    assert not other.done()
    executor.release.set()
    await first
    await second
    await other


@pytest.mark.asyncio
async def test_agent_chat_persistence_shutdown_drains_active_writes() -> None:
    """关闭持久化端口时拒绝新写入并等待现有会话写入收口。"""

    class BlockingExecutor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, operation):
            self.started.set()
            await self.release.wait()
            return operation()

    executor = BlockingExecutor()
    service = AgentChatPersistenceService(
        repository=lambda _session: _Repository(),
        async_executor=executor,
        sync_transaction=lambda operation: operation(object()),
    )
    write = asyncio.create_task(
        service.async_save_agent_messages(
            session_id="shutdown-session", user_id="1", messages=[]
        )
    )
    await executor.started.wait()
    shutdown = asyncio.create_task(service.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    with pytest.raises(DatabaseWorkerClosedError):
        await service.async_save_agent_messages(
            session_id="new-session", user_id="1", messages=[]
        )
    executor.release.set()
    await write
    await shutdown


@pytest.mark.asyncio
async def test_agent_chat_shutdown_timeout_keeps_worker_owner_until_write_finishes() -> None:
    """持久化关闭超时时保留运行中的写入和数据库 worker owner。"""
    started = threading.Event()
    release = threading.Event()

    class BlockingRepository(_Repository):
        def save_agent_messages(self, **kwargs):
            started.set()
            release.wait(1)
            super().save_agent_messages(**kwargs)

    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    service = AgentChatPersistenceService(
        repository=lambda _session: BlockingRepository(),
        async_executor=worker,
        sync_transaction=lambda operation: operation(object()),
    )
    write = asyncio.create_task(
        service.async_save_agent_messages(
            session_id="shutdown-timeout-session",
            user_id="1",
            messages=[],
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    shutdown = asyncio.create_task(service.shutdown())
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(shutdown, timeout=0.01)
        assert service._closing is True
        assert write.done() is False
        assert worker._executor is not None
    finally:
        release.set()
        await write
        await worker.shutdown()

    assert worker._executor is None


@pytest.mark.asyncio
async def test_agent_chat_persistence_uses_real_worker_and_sqlite_transaction() -> None:
    """真实 AgentChat Oper 经 worker 写入后可被 native async 查询恢复。"""
    worker = DatabaseWorker(max_workers=1, capacity=4)
    await worker.start()
    session_id = f"worker-{uuid4().hex}"
    persistence = AgentChatPersistenceService(
        repository=lambda session: AgentChatOper(session),
        async_executor=worker,
        sync_transaction=run_sync_transaction,
    )
    query = AgentChatService(repository=AgentChatOper())

    try:
        await persistence.async_save_display_messages(
            session_id=session_id,
            user_id="worker-user",
            username="worker-user",
            channel="WebAgent",
            source="worker-test",
            messages=[{"role": "user", "content": "worker"}],
        )
        chat = await query.get(
            session_id,
            user_id="worker-user",
        )
        assert chat is not None
        assert chat.message_count == 1
        assert chat.messages[0]["content"] == "worker"
    finally:
        await AgentChatOper().async_delete(
            session_id=session_id,
            user_id="worker-user",
        )
        await worker.shutdown()


@pytest.mark.asyncio
async def test_agent_chat_persistence_serializes_same_session_writes() -> None:
    """同一会话的首次创建和既有快照追加都必须串行。"""
    worker = DatabaseWorker(max_workers=4, capacity=16)
    await worker.start()
    session_id = f"worker-race-{uuid4().hex}"
    existing_session_id = f"worker-race-existing-{uuid4().hex}"
    persistence = AgentChatPersistenceService(
        repository=lambda session: AgentChatOper(session),
        async_executor=worker,
        sync_transaction=run_sync_transaction,
    )

    async def append(content: str) -> None:
        await persistence.async_append_display_messages(
            session_id=session_id,
            user_id="worker-race-user",
            messages=[{"role": "user", "content": content}],
        )

    async def append_existing(content: str) -> None:
        await persistence.async_append_display_messages(
            session_id=existing_session_id,
            user_id="worker-race-user",
            messages=[{"role": "user", "content": content}],
        )

    try:
        await asyncio.gather(*(append(f"message-{index}") for index in range(4)))
        await persistence.async_save_display_messages(
            session_id=existing_session_id,
            user_id="worker-race-user",
            messages=[{"role": "user", "content": "seed"}],
        )
        await asyncio.gather(
            *(append_existing(f"existing-{index}") for index in range(4))
        )
        async with async_session_scope() as session:
            result = await session.execute(
                select(AgentChat).where(
                    AgentChat.session_id.in_((session_id, existing_session_id))
                )
            )
            rows = list(result.scalars().all())
        assert len(rows) == 2
        row_by_session = {row.session_id: row for row in rows}
        assert {
            message["content"]
            for message in row_by_session[session_id].display_messages
        } == {f"message-{index}" for index in range(4)}
        assert {
            message["content"]
            for message in row_by_session[existing_session_id].display_messages
        } == {"seed"} | {f"existing-{index}" for index in range(4)}
    finally:
        with SessionFactory() as session:
            session.execute(
                delete(AgentChat).where(
                    AgentChat.session_id.in_((session_id, existing_session_id))
                )
            )
            session.commit()
        await worker.shutdown()
