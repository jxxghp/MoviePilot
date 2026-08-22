"""Agent 会话历史的查询、授权与删除应用服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from app.schemas.agent import AgentChatSessionDetail, AgentChatSessionSummary


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

    async def get(self, session_id: str) -> Optional[AgentChatRecord]:
        """读取不附带授权判断的会话投影。"""
        record = await self._repository.async_get(session_id=session_id)
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
        )


_configured_agent_chat_service: AgentChatService | None = None


def configure_agent_chat_service(service: AgentChatService) -> None:
    """由启动组合根登记同步 Agent 会话服务。"""
    global _configured_agent_chat_service
    _configured_agent_chat_service = service


def get_configured_agent_chat_service() -> AgentChatService:
    """返回启动阶段登记的 Agent 会话服务。"""
    if _configured_agent_chat_service is None:
        raise RuntimeError("Agent 会话服务尚未配置")
    return _configured_agent_chat_service
