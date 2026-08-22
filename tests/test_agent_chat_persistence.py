"""AgentChat 同步短事务经有界 worker 委托的应用端口测试。"""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4
from types import SimpleNamespace

import pytest

from app.application.messaging.chat import AgentChatPersistenceService
from app.db.oper.agentchat import AgentChatOper
from app.db.models.agentchat import AgentChat
from app.db.worker import DatabaseWorker


class _Executor:
    """用独立线程模拟 G2B worker，验证调用方不会直接执行同步仓储。"""

    def __init__(self) -> None:
        self.calls = 0
        self.worker_thread_id: int | None = None

    async def run(self, operation):
        """在线程中执行一个完整的同步操作。"""
        self.calls += 1

        def invoke():
            self.worker_thread_id = threading.get_ident()
            return operation()

        return await asyncio.to_thread(invoke)


class _Repository:
    """记录 AgentChat 端口调用的同步仓储替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return SimpleNamespace(agent_messages=[])

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
    """同步查询和写入都必须经过一次 worker admission。"""
    executor = _Executor()
    repository = _Repository()
    service = AgentChatPersistenceService(
        repository=lambda: repository,
        async_executor=executor,
    )
    caller_thread_id = threading.get_ident()

    await service.async_get("session-1", user_id="1")
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

    assert executor.calls == 5
    assert executor.worker_thread_id != caller_thread_id
    assert [name for name, _kwargs in repository.calls] == [
        "get",
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
async def test_agent_chat_persistence_uses_real_worker_and_sqlite_transaction() -> None:
    """真实 AgentChat Oper 经 worker 写入后可被后续 worker 查询恢复。"""
    worker = DatabaseWorker(max_workers=1, capacity=4)
    await worker.start()
    session_id = f"worker-{uuid4().hex}"
    service = AgentChatPersistenceService(
        repository=AgentChatOper,
        async_executor=worker,
    )

    try:
        await service.async_save_display_messages(
            session_id=session_id,
            user_id="worker-user",
            username="worker-user",
            channel="WebAgent",
            source="worker-test",
            messages=[{"role": "user", "content": "worker"}],
        )
        chat = await service.async_get(session_id, user_id="worker-user")
        assert chat is not None
        assert chat.message_count == 1
        assert chat.display_messages[0]["content"] == "worker"
    finally:
        chat = AgentChatOper().get(session_id=session_id, user_id="worker-user")
        if chat is not None:
            AgentChat.delete(rid=chat.id)
        await worker.shutdown()
