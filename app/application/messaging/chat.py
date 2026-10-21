"""
Agent 会话历史的查询、授权与删除应用服务。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol
from weakref import WeakValueDictionary

from app.application.database import (
    AsyncDatabaseExecutor,
)
from app.application.messaging.recall import RecallService
from app.runtime.observability import record_metric
from app.runtime.priority import INTERACTIVE_DATABASE_CAPACITY, is_interactive
from app.schemas.agent import AgentChatSessionDetail, AgentChatSessionSummary
from app.schemas.exception import AgentChatPersistenceUnavailableError

DEFAULT_AGENT_CHAT_WRITE_CAPACITY = 32
DEFAULT_AGENT_CHAT_SESSION_CAPACITY = 4


def has_custom_agent_chat_title(value: Optional[str]) -> bool:
    """
    判断会话标题是否已经脱离默认占位标题。
    """
    return bool(value and value.strip() and value.strip() != "未命名会话")


class AgentChatPrincipal(Protocol):
    """
    会话访问控制所需的最小用户身份。
    """

    @property
    def id(self) -> Any:
        """
        返回稳定用户标识。

        :return: 用于会话归属校验的稳定用户标识
        """
        ...

    @property
    def name(self) -> Optional[str]:
        """
        返回可选用户显示名。

        :return: 用于展示及旧会话兼容查询的用户名称，未提供时为 None
        """
        ...

    @property
    def is_superuser(self) -> bool:
        """
        返回是否具备超级管理员权限。

        :return: 用户具备全局会话管理权限时为 True
        """
        ...


class AsyncAgentChatRepository(Protocol):
    """
    Agent 会话用例需要的最小异步持久化端口。
    """

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> list[Any]:
        """
        分页读取用户可见的会话。

        :param page: 页码，从 1 开始
        :param count: 每页最多返回的会话数，默认 30
        :param user_id: 按用户身份过滤；与 username 同时提供时还兼容同名且未绑定身份的旧记录
        :param username: 旧记录兼容名称；单独提供时按名称筛选，两项均为 None 时不限定用户
        :return: 按最近更新时间降序排列的会话记录
        """
        ...

    async def async_get(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[Any]:
        """
        按服务端会话 ID 读取记录。

        :param session_id: 服务端会话标识
        :param user_id: 限定记录归属的用户标识；None 表示不按用户过滤，授权由上层完成
        :return: 匹配的持久化记录，不存在时为 None
        """
        ...

    async def async_delete(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> bool:
        """
        删除指定服务端会话。

        :param session_id: 服务端会话标识
        :param user_id: 限定记录归属的用户标识；None 表示不按用户过滤，授权由上层完成
        :return: 找到并删除记录时为 True，不存在时为 False
        """
        ...

    async def async_stage_delete(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> bool:
        """
        暂存删除指定服务端会话，不提交调用方事务。

        :param session_id: 服务端会话标识
        :param user_id: 限定记录归属的用户标识；None 表示不按用户过滤，授权由上层完成
        :return: 找到并删除记录时为 True，不存在时为 False
        """
        ...

    def get(self, session_id: str, user_id: Optional[str] = None) -> Optional[Any]:
        """
        同步读取服务端会话。

        :param session_id: 服务端会话标识
        :param user_id: 限定记录归属的用户标识；None 表示不按用户过滤，授权由上层完成
        :return: 匹配的持久化记录，不存在时为 None
        """
        ...


class SyncAgentChatRepository(Protocol):
    """
    仅包含 Agent 编排所需同步持久化方法的适配器端口。
    """

    def append_display_messages(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict[str, object]]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> Optional[Any]:
        """
        追加用户可见消息。

        暂存展示消息及会话元数据，事务由调用方提交。

        :param session_id: 服务端会话标识
        :param user_id: 会话归属的可信用户标识，None 表示不更新已有归属
        :param messages: 本批追加的展示消息，None 或空列表不增加消息
        :param username: 会话用户显示名称，None 表示不更新已有名称
        :param channel: 会话通知渠道，None 表示不更新已有渠道
        :param source: 渠道实例标识，None 表示不更新已有来源
        :param original_chat_id: 外部渠道聊天标识，None 表示不更新已有标识
        :param client_session_id: 客户端会话标识，None 表示不更新已有标识
        :return: 更新后的会话记录，无法建立会话时为 None
        """
        ...

    def save_display_messages(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict[str, object]]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> Optional[Any]:
        """
        保存用户可见消息快照。

        暂存展示消息及会话元数据，事务由调用方提交。

        :param session_id: 服务端会话标识
        :param user_id: 会话归属的可信用户标识，None 表示不更新已有归属
        :param messages: 替换当前快照的全部展示消息，None 表示清空快照
        :param username: 会话用户显示名称，None 表示不更新已有名称
        :param channel: 会话通知渠道，None 表示不更新已有渠道
        :param source: 渠道实例标识，None 表示不更新已有来源
        :param original_chat_id: 外部渠道聊天标识，None 表示不更新已有标识
        :param client_session_id: 客户端会话标识，None 表示不更新已有标识
        :return: 更新后的会话记录，无法建立会话时为 None
        """
        ...

    def save_agent_messages(
        self,
        session_id: str,
        user_id: Optional[str],
        messages: list[dict[str, object]],
    ) -> None:
        """
        保存可恢复的原始 Agent 消息。

        暂存原始消息快照，事务由调用方提交；不替代用户展示消息。

        :param session_id: 服务端会话标识
        :param user_id: 会话所属用户标识，None 表示未提供身份
        :param messages: 用于恢复 Agent 上下文的完整原始消息快照，空列表清空快照
        """
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
        """
        在会话尚无标题时写入标题。

        仅更新未设置自定义标题的会话；修改暂存于调用方事务。

        :param session_id: 服务端会话标识
        :param user_id: 会话归属的可信用户标识，None 表示不更新已有归属
        :param title: 候选标题；空值或默认占位标题不覆盖现有标题
        :param username: 会话用户显示名称，None 表示不更新已有名称
        :param channel: 会话通知渠道，None 表示不更新已有渠道
        :param source: 渠道实例标识，None 表示不更新已有来源
        :param original_chat_id: 外部渠道聊天标识，None 表示不更新已有标识
        :param client_session_id: 客户端会话标识，None 表示不更新已有标识
        """
        ...


SyncAgentChatRepositoryFactory = Callable[[object], SyncAgentChatRepository]
SyncAgentChatTransaction = Callable[[Callable[[object], object]], object]


@dataclass(frozen=True, slots=True)
class AgentChatRecord:
    """
    脱离 ORM 会话的 Agent 会话持久化投影。

    :param id: 业务数据库记录主键，尚无主键时为 None
    :param session_id: 宿主分配的服务端会话标识
    :param client_session_id: 客户端持有的会话标识，旧记录可能为 None
    :param title: 会话展示标题，尚未生成时可为 None
    :param channel: 会话所属通知渠道，未记录时为 None
    :param source: 会话所属渠道实例，未记录时为 None
    :param user_id: 持久化的用户身份标识，旧记录可能缺失
    :param username: 用户显示名称，用于展示及兼容未绑定身份的旧会话
    :param original_chat_id: 外部消息平台的原始聊天标识，无渠道上下文时为 None
    :param message_count: 用户可见消息的条数
    :param created_at: 存储层提供的会话创建时间
    :param updated_at: 存储层提供的会话最近更新时间
    :param messages: 供界面展示的消息快照
    :param agent_messages: 供 Agent 恢复上下文的原始消息快照
    """

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
    messages: list[dict[str, object]]
    agent_messages: list[dict[str, object]]


class AsyncUnitOfWork(Protocol):
    """
    Agent 会话异步写用例所需的最小事务端口。
    """

    async def commit(self) -> None:
        """
        提交当前请求事务。
        """
        ...

    async def rollback(self) -> None:
        """
        回滚当前请求事务。
        """
        ...


class AgentChatService:
    """
    统一执行 Agent 会话查询、访问控制和删除。
    """

    def __init__(
        self,
        repository: AsyncAgentChatRepository,
        unit_of_work: Optional[AsyncUnitOfWork] = None,
        recall: RecallService | None = None,
    ) -> None:
        """
        保存会话持久化端口和可选请求级事务。
        """
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._recall = recall

    async def list(
        self,
        principal: AgentChatPrincipal,
        *,
        page: int = 1,
        count: int = 30,
    ) -> list[AgentChatSessionSummary]:
        """
        分页返回当前用户可见的会话摘要。
        """
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
        """
        读取会话并在应用边界执行访问控制。
        """
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
        """
        读取不附带授权判断的会话投影。
        """
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
        """
        仅在当前用户可访问时删除会话。
        """
        record = await self.get_accessible(session_id, principal)
        if record is None:
            return False
        # 两个数据库没有共享事务：先收回证据并落删除墓碑，主库失败可安全重试。
        if self._recall is not None and record.user_id:
            await self._recall.delete(record.user_id, session_id)
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

    def get_sync(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[AgentChatRecord]:
        """
        同步读取会话投影，供同步 Agent 编排路径使用。
        """
        record = self._repository.get(session_id=session_id, user_id=user_id)
        return self._project(record) if record is not None else None

    def save_agent_messages(
        self,
        session_id: str,
        user_id: str,
        messages: List[dict[str, Any]],
    ) -> None:
        """
        同步保存可恢复消息，兼容非协程 Agent 调用路径。
        """
        sync_repository = self._repository
        save = getattr(sync_repository, "save_agent_messages", None)
        if not callable(save):
            raise RuntimeError("Agent 会话仓储不支持同步消息保存")
        save(session_id=session_id, user_id=user_id, messages=messages)

    @staticmethod
    def can_access(
        record: AgentChatRecord,
        principal: AgentChatPrincipal,
    ) -> bool:
        """
        优先按稳定用户 ID 授权，仅无 ID 旧记录回退到用户名。
        """
        if getattr(principal, "is_superuser", False):
            return True
        user_id = str(principal.id)
        username = str(principal.name or "")
        if record.user_id:
            return str(record.user_id) == user_id
        return bool(username) and record.username == username

    @staticmethod
    def to_summary(record: AgentChatRecord) -> AgentChatSessionSummary:
        """
        把持久化投影转换为会话摘要 DTO。
        """
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
        """
        把持久化投影转换为会话详情 DTO。
        """
        return AgentChatSessionDetail.model_validate({
                **cls.to_summary(record).model_dump(),
                "messages": record.messages,
            })

    @staticmethod
    def _project(record: Any) -> AgentChatRecord:
        """
        立即复制 ORM 字段，避免对象越过请求级会话边界。
        """
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
    """
    把 Agent 编排所需的同步持久化操作委托给有界数据库 worker。
    """

    def __init__(
        self,
        repository: SyncAgentChatRepositoryFactory,
        async_executor: AsyncDatabaseExecutor,
        sync_transaction: SyncAgentChatTransaction,
        capacity: int = DEFAULT_AGENT_CHAT_WRITE_CAPACITY,
        session_capacity: int = DEFAULT_AGENT_CHAT_SESSION_CAPACITY,
    ) -> None:
        """
        保存同步仓储工厂、事务端口和两级写入容量。
        """
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
        self._pending_interactive_writes = 0
        self._pending_by_session: dict[str, int] = {}
        self._active_tasks: set[asyncio.Task[object]] = set()
        self._closing = False
        # append_display_messages 属于读取旧快照后整列写回的复合操作；按会话串行化，
        # 才能在 worker 并发下保持首次建行和既有会话追加的完整性。弱引用避免长期运行
        # 中为一次性会话永久保留锁对象，不限制不同会话之间的 worker 并行度。
        self._session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        """
        返回当前进程内指定会话的写锁。
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def begin_shutdown(self) -> None:
        """
        停止接受新的 AgentChat 持久化任务。
        """
        self._closing = True

    async def _run_write(
        self,
        session_id: str,
        operation: Callable[[SyncAgentChatRepository], object],
    ) -> None:
        """
        在线程 worker 内完成同步写入并丢弃仓储对象返回值。
        """
        # 同时限制全局和单会话等待量，避免一个热点会话占满总 admission 后饿死其他会话。
        if self._closing:
            raise AgentChatPersistenceUnavailableError(
                "AgentChat 持久化服务当前不可接收任务"
            )
        session_pending = self._pending_by_session.get(session_id, 0)
        interactive = is_interactive()
        pending = self._pending_interactive_writes if interactive else self._pending_writes - self._pending_interactive_writes
        capacity = INTERACTIVE_DATABASE_CAPACITY if interactive else self._capacity
        if (
            pending >= capacity
            or session_pending >= self._session_capacity
        ):
            record_metric("agent.chat.persistence.rejected")
            raise AgentChatPersistenceUnavailableError(
                f"AgentChat 写入容量已用尽（当前执行组上限 {capacity}，"
                f"单会话上限 {self._session_capacity}）"
            )
        self._pending_writes += 1
        if interactive:
            self._pending_interactive_writes += 1
        self._pending_by_session[session_id] = session_pending + 1
        current = asyncio.current_task()
        if current is not None:
            self._active_tasks.add(current)
        record_metric("agent.chat.persistence.pending", 1)
        try:
            async with self._session_lock(session_id):
                def execute() -> None:
                    """
                    在单一同步事务中执行写入，不让 ORM 对象越过 worker 边界。
                    """
                    self._sync_transaction(
                        lambda session: operation(self._repository(session))
                    )

                await self._async_executor.run(execute)
        finally:
            self._pending_writes -= 1
            if interactive:
                self._pending_interactive_writes -= 1
            remaining = self._pending_by_session.get(session_id, 1) - 1
            if remaining:
                self._pending_by_session[session_id] = remaining
            else:
                self._pending_by_session.pop(session_id, None)
            if current is not None:
                self._active_tasks.discard(current)
            record_metric("agent.chat.persistence.pending", -1)

    async def shutdown(self) -> None:
        """
        拒绝新写入并等待当前会话锁和 worker 操作取得终态。
        """
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
        messages: Optional[list[dict[str, object]]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> None:
        """
        异步追加展示消息，等待同步事务取得确定终态。
        """
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
        messages: Optional[list[dict[str, object]]] = None,
        username: Optional[str] = None,
        channel: Optional[Any] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> None:
        """
        异步保存展示消息快照，实际写入由有界 worker 承接。
        """
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
        messages: list[dict[str, object]],
    ) -> None:
        """
        异步保存可恢复的原始消息。
        """
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
        """
        异步写入首次生成的会话标题。
        """
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
    """
    由启动组合根登记同步 Agent 会话服务。
    """
    global _configured_agent_chat_service
    _configured_agent_chat_service = service


def get_configured_agent_chat_service() -> AgentChatService:
    """
    返回启动阶段登记的 Agent 会话服务。
    """
    if _configured_agent_chat_service is None:
        raise RuntimeError("Agent 会话服务尚未配置")
    return _configured_agent_chat_service


def configure_agent_chat_persistence(
    service: AgentChatPersistenceService,
) -> None:
    """
    由启动组合根登记 Agent 编排所需的同步持久化端口。
    """
    global _configured_agent_chat_persistence
    _configured_agent_chat_persistence = service


def reset_agent_chat_service() -> None:
    """
    清除当前 lifespan 的 Agent 会话查询服务。
    """
    global _configured_agent_chat_service
    _configured_agent_chat_service = None


def reset_agent_chat_persistence() -> None:
    """
    清除当前 lifespan 的 Agent 会话持久化服务。
    """
    global _configured_agent_chat_persistence
    _configured_agent_chat_persistence = None


def get_configured_agent_chat_persistence() -> AgentChatPersistenceService:
    """
    返回由启动组合根登记的 AgentChat worker 端口。
    """
    if _configured_agent_chat_persistence is None:
        raise RuntimeError("Agent 会话持久化服务尚未配置")
    return _configured_agent_chat_persistence
