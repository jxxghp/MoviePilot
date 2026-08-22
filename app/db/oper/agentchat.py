import time
from typing import Any, Optional, Union

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.agentchat import AgentChat
from app.schemas.notification import ChannelRef, channel_identity

DEFAULT_AGENT_CHAT_TITLE = "未命名会话"


class AgentChatOper(DbOper):
    """
    Agent 会话历史数据管理。
    """

    def __init__(self, db: Optional[Union[Session, AsyncSession]] = None):
        super().__init__(db)

    @staticmethod
    def _now() -> str:
        """返回数据库统一使用的当前时间字符串。"""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    @staticmethod
    def _normalize_messages(messages: Optional[list[dict]]) -> list[dict]:
        """规范化展示消息列表，避免 JSON 字段存入 None。"""
        return messages if isinstance(messages, list) else []

    @staticmethod
    def _normalize_title(value: Optional[str], messages: list[dict]) -> str:
        """生成会话标题。"""
        if value and value.strip():
            return value.strip()[:120]
        for message in messages:
            if message.get("role") != "user":
                continue
            content = str(message.get("content") or "").strip()
            if content:
                return content.replace("\n", " ")[:120]
            attachments = message.get("attachments")
            if isinstance(attachments, list) and attachments:
                name = attachments[0].get("name") or "附件消息"
                return str(name)[:120]
        return DEFAULT_AGENT_CHAT_TITLE

    @staticmethod
    def has_custom_title(value: Optional[str]) -> bool:
        """判断会话是否已有真实标题。"""
        return bool(value and value.strip() and value.strip() != DEFAULT_AGENT_CHAT_TITLE)

    @staticmethod
    def _normalize_preview(messages: list[dict]) -> str:
        """生成会话预览文本。"""
        for message in reversed(messages):
            content = str(message.get("content") or "").strip()
            if content:
                return content.replace("\n", " ")[:240]
            attachments = message.get("attachments")
            if isinstance(attachments, list) and attachments:
                name = attachments[0].get("name") or "附件消息"
                return str(name)[:240]
        return ""

    def get(
        self, session_id: str, user_id: Optional[str] = None
    ) -> Optional[AgentChat]:
        """
        获取 Agent 会话。
        """
        return AgentChat.get_by_session(self._db, session_id, user_id)

    async def async_get(
        self, session_id: str, user_id: Optional[str] = None
    ) -> Optional[AgentChat]:
        """
        异步获取 Agent 会话。
        """
        return await AgentChat.async_get_by_session(self._db, session_id, user_id)

    def ensure_session(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        channel: Optional[ChannelRef] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> Optional[AgentChat]:
        """
        确保 Agent 会话记录存在，并刷新基础渠道信息。
        """
        now = self._now()
        chat = self.get(session_id=session_id, user_id=user_id)
        if not chat:
            chat = self.get(session_id=session_id)
        payload = {
            "user_id": user_id,
            "username": username,
            "channel": channel_identity(channel),
            "source": source,
            "original_chat_id": original_chat_id,
            "client_session_id": client_session_id,
            "updated_at": now,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        if chat:
            self._stage_update(chat, payload)
            return self.get(session_id=session_id, user_id=user_id) or self.get(session_id=session_id)

        chat = AgentChat(
            session_id=session_id,
            user_id=user_id,
            username=username,
            channel=channel_identity(channel),
            source=source,
            original_chat_id=original_chat_id,
            client_session_id=client_session_id,
            title=DEFAULT_AGENT_CHAT_TITLE,
            preview="",
            agent_messages=[],
            display_messages=[],
            message_count=0,
            created_at=now,
            updated_at=now,
        )
        self._stage_create(chat)
        return self.get(session_id=session_id, user_id=user_id) or self.get(session_id=session_id)

    def save_agent_messages(
        self,
        session_id: str,
        user_id: Optional[str],
        messages: list[dict],
    ) -> None:
        """
        保存可恢复 Agent 上下文的原始消息。
        """
        chat = self.get(session_id=session_id, user_id=user_id)
        if not chat:
            chat = self.get(session_id=session_id)
        if not chat:
            chat = self.ensure_session(session_id=session_id, user_id=user_id)
        if not chat:
            return
        self._stage_update(
            chat,
            {
                "agent_messages": messages or [],
                "updated_at": self._now(),
            },
        )

    def update_title_if_empty(
        self,
        session_id: str,
        user_id: Optional[str],
        title: Optional[str],
        username: Optional[str] = None,
        channel: Optional[ChannelRef] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> None:
        """
        在会话尚未生成标题时写入标题。
        """
        normalized_title = self._normalize_title(title, [])
        if normalized_title == DEFAULT_AGENT_CHAT_TITLE:
            return

        chat = self.ensure_session(
            session_id=session_id,
            user_id=user_id,
            username=username,
            channel=channel,
            source=source,
            original_chat_id=original_chat_id,
            client_session_id=client_session_id,
        )
        if not chat:
            return
        if self.has_custom_title(chat.title):
            return
        self._stage_update(
            chat,
            {
                "title": normalized_title,
                "updated_at": self._now(),
            },
        )

    def save_display_messages(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        username: Optional[str] = None,
        channel: Optional[ChannelRef] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Optional[AgentChat]:
        """
        保存用户可见的 Agent 会话消息。
        """
        normalized_messages = self._normalize_messages(messages)
        chat = self.ensure_session(
            session_id=session_id,
            user_id=user_id,
            username=username,
            channel=channel,
            source=source,
            original_chat_id=original_chat_id,
            client_session_id=client_session_id,
        )
        if not chat:
            return None
        normalized_title = (
            chat.title
            if self.has_custom_title(chat.title)
            else self._normalize_title(title, normalized_messages)
        )
        self._stage_update(
            chat,
            {
                "title": normalized_title,
                "preview": self._normalize_preview(normalized_messages),
                "display_messages": normalized_messages,
                "message_count": len(normalized_messages),
                "updated_at": self._now(),
            },
        )
        return self.get(session_id=session_id, user_id=user_id) or self.get(session_id=session_id)

    def append_display_messages(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        username: Optional[str] = None,
        channel: Optional[ChannelRef] = None,
        source: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
    ) -> Optional[AgentChat]:
        """
        追加一组用户可见的 Agent 会话消息。
        """
        chat = self.ensure_session(
            session_id=session_id,
            user_id=user_id,
            username=username,
            channel=channel,
            source=source,
            original_chat_id=original_chat_id,
            client_session_id=client_session_id,
        )
        if not chat:
            return None
        display_messages = self._normalize_messages(chat.display_messages)
        display_messages.extend(self._normalize_messages(messages))
        title = chat.title if self.has_custom_title(chat.title) else None
        return self.save_display_messages(
            session_id=session_id,
            user_id=user_id,
            messages=display_messages,
            username=username or chat.username,
            channel=channel or chat.channel,
            source=source or chat.source,
            original_chat_id=original_chat_id or chat.original_chat_id,
            client_session_id=client_session_id or chat.client_session_id,
            title=title,
        )

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> list[AgentChat]:
        """
        异步分页获取 Agent 会话历史。
        """
        return await AgentChat.async_list_by_page(
            self._db,
            page=page,
            count=count,
            user_id=user_id,
            username=username,
        )

    async def async_delete(
        self, session_id: str, user_id: Optional[str] = None
    ) -> bool:
        """
        异步删除 Agent 会话历史。
        """
        chat = await self.async_get(session_id=session_id, user_id=user_id)
        if not chat:
            return False
        await self._stage_async_delete(AgentChat, chat.id)
        return True

    async def async_stage_delete(
        self,
        session_id: str,
        user_id: Optional[str] = None,
    ) -> bool:
        """暂存 Agent 会话删除并 flush，不提交请求级事务。"""
        if not isinstance(self._db, AsyncSession):
            raise RuntimeError("Agent 会话暂存删除需要调用方提供 AsyncSession")
        chat = await self.async_get(session_id=session_id, user_id=user_id)
        if not chat:
            return False
        await self._db.delete(chat)
        await self._db.flush()
        return True

    @staticmethod
    def to_summary(chat: AgentChat) -> dict[str, Any]:
        """
        转换为历史会话摘要。
        """
        return {
            "id": chat.id,
            "session_id": chat.session_id,
            "client_session_id": chat.client_session_id,
            "title": chat.title,
            "channel": chat.channel,
            "source": chat.source,
            "user_id": chat.user_id,
            "username": chat.username,
            "original_chat_id": chat.original_chat_id,
            "message_count": chat.message_count or 0,
            "created_at": chat.created_at,
            "updated_at": chat.updated_at,
        }

    @classmethod
    def to_detail(cls, chat: AgentChat) -> dict[str, Any]:
        """
        转换为历史会话详情。
        """
        data = cls.to_summary(chat)
        data["messages"] = chat.display_messages or []
        return data
