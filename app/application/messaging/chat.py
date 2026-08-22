"""Agent 会话历史的查询、授权与删除应用服务。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Optional, Protocol
from weakref import WeakValueDictionary

from app.application.database import (
    AsyncDatabaseExecutor,
    DatabaseWorkerClosedError,
    DatabaseWorkerOverloadedError,
)
from app.schemas.agent import AgentChatSessionDetail, AgentChatSessionSummary
from app.runtime.observability import record_metric


DEFAULT_AGENT_CHAT_WRITE_CAPACITY = 32
DEFAULT_AGENT_CHAT_SESSION_CAPACITY = 4


def has_custom_agent_chat_title(value: Optional[str]) -> bool:
    """判断会话标题是否已经脱离默认占位标题。"""
    return bool(value and value.strip() and value.strip() != "未命名会话")


class AgentChatPrincipal(Protocol):
    """会话访问控制所需的最小用户身份。"""

    id: Any
    name: Optional[str]
    is_superuser: bool


class AsyncAgentChatRepository(Protocol):
    """Agent 会话用例需要的最小异步持久化端口。"""

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> list[Any]:
        """分页读取用户可见的会话。"""
        ...

    async def async_get(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[Any]:
        """按服务端会话 ID 读取记录。"""
        ...

    async def async_delete(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> bool:
        """删除指定服务端会话。"""
        ...

    async def async_stage_delete(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> bool:
        """暂存删除指定服务端会话，不提交调用方事务。"""
        ...

    def get(self, session_id: str, user_id: Optional[str] = None) -> Optional[Any]:
        """同步读取服务端会话。"""
        ...

    def save_display_messages(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> Optional[Any]:
        """同步保存用户可见会话消息。"""
        ...


class SyncAgentChatRepository(Protocol):
    """仅包含 Agent 编排所需同步持久化方法的适配器端口。"""

    def append_display_messages(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> Optional[Any]:
        """追加用户可见消息。"""
        ...

    def save_display_messages(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> Optional[Any]:
        """保存用户可见消息快照。"""
        ...

    def save_agent_messages(
        self,
        session_id: str,
        user_id: Optional[str],
        messages: list[dict],
    ) -> None:
        """保存可恢复的原始 Agent 消息。"""
        ...

    def update_title_if_empty(
        self,
        session_id: str,
        user_id: Optional[str],
        title: Optional[str],
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> None:
        """在会话尚无标题时写入标题。"""
        ...


SyncAgentChatRepositoryFactory = Callable[[object], SyncAgentChatRepository]
SyncAgentChatTransaction = Callable[[Callable[[object], object]], object]


@dataclass(frozen=True, slots=True)
class AgentChatRecord:
    """脱离 ORM 会话的 Agent 会话持久化投影。"""

    id: Optional[int]
    session_id: str
    client_session_id: Optional[str]
    title: Optional[str]
    channel: Optional[str]
    source: Optional[str]
    user_id: Optional[str]
    username: Optional[str]
    original_chat_id: Optional[str]
    message_count: int
    created_at: Any
    updated_at: Any
    messages: list[dict]
    agent_messages: list[dict]


class AsyncUnitOfWork(Protocol):
    """Agent 会话异步写用例所需的最小事务端口。"""

    async def commit(self) -> None:
        """提交当前请求事务。"""
        ...

    async def rollback(self) -> None:
        """回滚当前请求事务。"""
        ...


class AgentChatService:
    """统一执行 Agent 会话查询、访问控制和删除。"""

    def __init__(
        self,
        repository: AsyncAgentChatRepository,
        unit_of_work: Optional[AsyncUnitOfWork] = None,
    ) -> None:
        """保存会话持久化端口和可选请求级事务。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    async def list(
        self,
        principal: AgentChatPrincipal,
        *,
        page: int = 1,
        count: int = 30,
    ) -> list[AgentChatSessionSummary]:
        """分页返回当前用户可见的会话摘要。"""
        user_id = None if principal.is_superuser else str(principal.id)
        username = None if principal.is_superuser else principal.name
        records = await self._repository.async_list_by_page(
            page=page,
            count=count,
            user_id=user_id,
            username=username,
        )
        return [self.to_summary(self._project(record)) for record in records]

    async def get_accessible(
        self,
        session_id: str,
        principal: AgentChatPrincipal,
    ) -> Optional[AgentChatRecord]:
        """读取会话并在应用边界执行访问控制。"""
        projected = await self.get(session_id)
        if projected is None:
            return None
        if not self.can_access(projected, principal):
            return None
        return projected

    async def get(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[AgentChatRecord]:
        """读取不附带授权判断的会话投影。"""
        record = await self._repository.async_get(
            session_id=session_id,
            user_id=user_id,
        )
        if record is None:
            return None
        return self._project(record)

    async def delete(
        self,
        session_id: str,
        principal: AgentChatPrincipal,
    ) -> bool:
        """仅在当前用户可访问时删除会话。"""
        record = await self.get_accessible(session_id, principal)
        if record is None:
            return False
        if self._unit_of_work is None:
            return await self._repository.async_delete(session_id=session_id)
        try:
            deleted = await self._repository.async_stage_delete(
                session_id=session_id
            )
            if deleted:
                await self._unit_of_work.commit()
            return deleted
        except Exception:
            await self._unit_of_work.rollback()
            raise

    def get_sync(self, session_id: str) -> Optional[AgentChatRecord]:
        """同步读取会话投影，供同步 Agent 编排路径使用。"""
        record = self._repository.get(session_id=session_id)
        return self._project(record) if record is not None else None

    def save_display_sync(
        self,
        *,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> Optional[AgentChatRecord]:
        """同步保存用户可见消息并返回最新投影。"""
        record = self._repository.save_display_messages(
            session_id=session_id,
            user_id=user_id,
            messages=messages,
            username=username,
            channel=channel,
            source=source,
            original_chat_id=original_chat_id,
            client_session_id=client_session_id,
        )
        return self._project(record) if record is not None else None

    @staticmethod
    def can_access(
        record: AgentChatRecord,
        principal: AgentChatPrincipal,
    ) -> bool:
        """判断用户是否拥有会话访问权。"""
        if principal.is_superuser:
            return True
        user_id = str(principal.id)
        username = str(principal.name or "")
        return record.user_id == user_id or (
            bool(username) and record.username == username
        )

    @staticmethod
    def to_summary(record: AgentChatRecord) -> AgentChatSessionSummary:
        """把持久化投影转换为会话摘要 DTO。"""
        return AgentChatSessionSummary(
            id=record.id,
            session_id=record.session_id,
            client_session_id=record.client_session_id,
            title=record.title,
            channel=record.channel,
            source=record.source,
            user_id=record.user_id,
            username=record.username,
            original_chat_id=record.original_chat_id,
            message_count=record.message_count,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @classmethod
    def to_detail(cls, record: AgentChatRecord) -> AgentChatSessionDetail:
        """把持久化投影转换为会话详情 DTO。"""
        return AgentChatSessionDetail(
            **cls.to_summary(record).model_dump(),
            messages=record.messages,
        )

    @staticmethod
    def _project(record: Any) -> AgentChatRecord:
        """立即复制 ORM 字段，避免对象越过请求级会话边界。"""
        return AgentChatRecord(
            id=record.id,
            session_id=record.session_id,
            client_session_id=record.client_session_id,
            title=record.title,
            channel=record.channel,
            source=record.source,
            user_id=record.user_id,
            username=record.username,
            original_chat_id=record.original_chat_id,
            message_count=record.message_count or 0,
            created_at=record.created_at,
            updated_at=record.updated_at,
            messages=list(record.display_messages or []),
            agent_messages=list(record.agent_messages or []),
        )


class AgentChatPersistenceService:
    """把 Agent 编排所需的同步持久化操作委托给有界数据库 worker。"""

    def __init__(
        self,
        repository: SyncAgentChatRepositoryFactory,
        async_executor: AsyncDatabaseExecutor,
        sync_transaction: SyncAgentChatTransaction,
        capacity: int = DEFAULT_AGENT_CHAT_WRITE_CAPACITY,
        session_capacity: int = DEFAULT_AGENT_CHAT_SESSION_CAPACITY,
    ) -> None:
        """保存同步仓储工厂、事务端口和两级写入容量。"""
        if capacity < 1:
            raise ValueError("AgentChat 写入容量必须大于 0")
        if session_capacity < 1:
            raise ValueError("AgentChat 单会话写入容量必须大于 0")
        self._repository = repository
        self._async_executor = async_executor
        self._sync_transaction = sync_transaction
        self._capacity = capacity
        self._session_capacity = session_capacity
        self._pending_writes = 0
        self._pending_by_session: dict[str, int] = {}
        self._active_tasks: set[asyncio.Task[object]] = set()
        self._closing = False
        # append_display_messages 属于读取旧快照后整列写回的复合操作；按会话串行化，
        # 才能在 worker 并发下保持首次建行和既有会话追加的完整性。弱引用避免长期运行
        # 中为一次性会话永久保留锁对象，不限制不同会话之间的 worker 并行度。
        self._session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        """返回当前进程内指定会话的写锁。"""
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def begin_shutdown(self) -> None:
        """停止接受新的 AgentChat 持久化任务。"""
        self._closing = True

    async def _run_write(
        self,
        session_id: str,
        operation: Callable[[SyncAgentChatRepository], object],
    ) -> None:
        """在线程 worker 内完成同步写入并丢弃仓储对象返回值。"""
        # 同时限制全局和单会话等待量，避免一个热点会话占满总 admission 后饿死其他会话。
        if self._closing:
            raise DatabaseWorkerClosedError("AgentChat 持久化服务当前不可接收任务")
        session_pending = self._pending_by_session.get(session_id, 0)
        if (
            self._pending_writes >= self._capacity
            or session_pending >= self._session_capacity
        ):
            record_metric("agent.chat.persistence.rejected")
            raise DatabaseWorkerOverloadedError(
                f"AgentChat 写入容量已用尽（全局上限 {self._capacity}，"
                f"单会话上限 {self._session_capacity}）"
            )
        self._pending_writes += 1
        self._pending_by_session[session_id] = session_pending + 1
        current = asyncio.current_task()
        if current is not None:
            self._active_tasks.add(current)
        record_metric("agent.chat.persistence.pending", 1)
        try:
            async with self._session_lock(session_id):
                def execute() -> None:
                    """在单一同步事务中执行写入，不让 ORM 对象越过 worker 边界。"""
                    self._sync_transaction(
                        lambda session: operation(self._repository(session))
                    )

                await self._async_executor.run(execute)
        finally:
            self._pending_writes -= 1
            remaining = self._pending_by_session.get(session_id, 1) - 1
            if remaining:
                self._pending_by_session[session_id] = remaining
            else:
                self._pending_by_session.pop(session_id, None)
            if current is not None:
                self._active_tasks.discard(current)
            record_metric("agent.chat.persistence.pending", -1)

    async def shutdown(self) -> None:
        """拒绝新写入并等待当前会话锁和 worker 操作取得终态。"""
        self.begin_shutdown()
        current = asyncio.current_task()
        tasks = tuple(task for task in self._active_tasks if task is not current)
        if tasks:
            # wait 不会在生命周期超时时取消实际写入；外层可及时返回并保留
            # 数据库 worker owner，已开始的事务继续由 worker 收口。
            await asyncio.wait(tasks)

    async def async_append_display_messages(
        self,
        *,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> None:
        """异步追加展示消息，等待同步事务取得确定终态。"""
        await self._run_write(
            session_id,
            lambda repository: repository.append_display_messages(
                session_id=session_id,
                user_id=user_id,
                messages=messages,
                username=username,
                channel=channel,
                source=source,
                original_chat_id=original_chat_id,
                client_session_id=client_session_id,
            )
        )
        return None

    async def async_save_display_messages(
        self,
        *,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> None:
        """异步保存展示消息快照，实际写入由有界 worker 承接。"""
        await self._run_write(
            session_id,
            lambda repository: repository.save_display_messages(
                session_id=session_id,
                user_id=user_id,
                messages=messages,
                username=username,
                channel=channel,
                source=source,
                original_chat_id=original_chat_id,
                client_session_id=client_session_id,
            )
        )
        return None

    async def async_save_agent_messages(
        self,
        *,
        session_id: str,
        user_id: str,
        messages: list[dict],
    ) -> None:
        """异步保存可恢复的原始消息。"""
        await self._run_write(
            session_id,
            lambda repository: repository.save_agent_messages(
                session_id=session_id,
                user_id=user_id,
                messages=messages,
            )
        )

    async def async_update_title_if_empty(
        self,
        *,
        session_id: str,
        user_id: Optional[str],
        title: Optional[str],
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> None:
        """异步写入首次生成的会话标题。"""
        await self._run_write(
            session_id,
            lambda repository: repository.update_title_if_empty(
                session_id=session_id,
                user_id=user_id,
                title=title,
                username=username,
                channel=channel,
                source=source,
                original_chat_id=original_chat_id,
                client_session_id=client_session_id,
            )
        )


_configured_agent_chat_service: AgentChatService | None = None
_configured_agent_chat_persistence: AgentChatPersistenceService | None = None


def configure_agent_chat_service(service: AgentChatService) -> None:
    """由启动组合根登记同步 Agent 会话服务。"""
    global _configured_agent_chat_service
    _configured_agent_chat_service = service


def get_configured_agent_chat_service() -> AgentChatService:
    """返回启动阶段登记的 Agent 会话服务。"""
    if _configured_agent_chat_service is None:
        raise RuntimeError("Agent 会话服务尚未配置")
    return _configured_agent_chat_service


def configure_agent_chat_persistence(
    service: AgentChatPersistenceService,
) -> None:
    """由启动组合根登记 Agent 编排所需的同步持久化端口。"""
    global _configured_agent_chat_persistence
    _configured_agent_chat_persistence = service


def get_configured_agent_chat_persistence() -> AgentChatPersistenceService:
    """返回由启动组合根登记的 AgentChat worker 端口。"""
    if _configured_agent_chat_persistence is None:
        raise RuntimeError("Agent 会话持久化服务尚未配置")
    return _configured_agent_chat_persistence
