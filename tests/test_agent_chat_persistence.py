"""AgentChat 同步短事务经有界 worker 委托的应用端口测试。"""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.application.database import DatabaseWorkerOverloadedError
from app.application.messaging.chat import AgentChatPersistenceService, AgentChatService
from app.db.models.agentchat import AgentChat
from app.db.oper.agentchat import AgentChatOper
from app.db.session import SessionFactory, async_session_scope
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
        repository=lambda: repository,
        async_executor=executor,
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
        repository=_Repository,
        async_executor=FailingExecutor(),
    )

    with pytest.raises(RuntimeError, match="worker failed"):
        await service.async_save_agent_messages(
            session_id="session-1",
            user_id="1",
            messages=[],
        )


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
        repository=_Repository,
        async_executor=executor,
        capacity=2,
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
async def test_agent_chat_persistence_uses_real_worker_and_sqlite_transaction() -> None:
    """真实 AgentChat Oper 经 worker 写入后可被 native async 查询恢复。"""
    worker = DatabaseWorker(max_workers=1, capacity=4)
    await worker.start()
    session_id = f"worker-{uuid4().hex}"
    persistence = AgentChatPersistenceService(
        repository=AgentChatOper,
        async_executor=worker,
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
        repository=AgentChatOper,
        async_executor=worker,
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
