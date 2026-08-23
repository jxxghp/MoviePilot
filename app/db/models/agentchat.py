from typing import Any, Optional

from sqlalchemy import Integer, String, JSON, Index, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column
from app.db.decorators import legacy_async_db_query, legacy_db_query


class AgentChat(Base):
    """
    Agent 会话历史表。
    """

    id = get_id_column()
    # Agent 内部会话 ID，用于恢复 LangGraph 对话上下文
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    # 前端或渠道侧传入的原始会话标识
    client_session_id: Mapped[Optional[str]] = mapped_column(String)
    # 用户 ID
    user_id: Mapped[Optional[str]] = mapped_column(String)
    # 用户名称
    username: Mapped[Optional[str]] = mapped_column(String)
    # 消息渠道
    channel: Mapped[Optional[str]] = mapped_column(String)
    # 渠道来源配置名
    source: Mapped[Optional[str]] = mapped_column(String)
    # 原聊天 ID，用于区分群聊、频道或私聊
    original_chat_id: Mapped[Optional[str]] = mapped_column(String)
    # 会话标题
    title: Mapped[Optional[str]] = mapped_column(String)
    # 会话预览文本
    preview: Mapped[Optional[str]] = mapped_column(String)
    # 原始 LangChain messages，用于继续会话
    agent_messages: Mapped[Optional[Any]] = mapped_column(JSON)
    # 展示给用户的消息记录，包含文字、工具提示、附件与选择卡片
    display_messages: Mapped[Optional[Any]] = mapped_column(JSON)
    # 展示消息数量
    message_count: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    # 创建时间
    created_at: Mapped[Optional[str]] = mapped_column(String)
    # 更新时间
    updated_at: Mapped[Optional[str]] = mapped_column(String)

    __table_args__ = (
        Index("ix_agentchat_session_user", "session_id", "user_id"),
        Index("ix_agentchat_user_updated", "user_id", "updated_at", "id"),
        Index("ix_agentchat_channel_updated", "channel", "updated_at", "id"),
    )

    @classmethod
    @legacy_db_query
    def get_by_session(
        cls, db: Session, session_id: str, user_id: Optional[str] = None
    ) -> Optional["AgentChat"]:
        """
        根据会话 ID 获取 Agent 会话。
        """
        statement = select(cls).where(cls.session_id == session_id)
        if user_id is not None:
            statement = statement.where(cls.user_id == user_id)
        return db.execute(statement.order_by(cls.id.desc())).scalars().first()

    @classmethod
    @legacy_async_db_query
    async def async_get_by_session(
        cls, db: AsyncSession, session_id: str, user_id: Optional[str] = None
    ) -> Optional["AgentChat"]:
        """
        异步根据会话 ID 获取 Agent 会话。
        """
        statement = select(cls).where(cls.session_id == session_id)
        if user_id is not None:
            statement = statement.where(cls.user_id == user_id)
        result = await db.execute(statement.order_by(cls.id.desc()))
        return result.scalars().first()

    @classmethod
    @legacy_db_query
    def list_by_page(
        cls,
        db: Session,
        page: int = 1,
        count: int = 30,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> list["AgentChat"]:
        """
        分页获取 Agent 会话历史。
        """
        statement = select(cls)
        if user_id is not None and username is not None:
            statement = statement.where((cls.user_id == user_id) | (cls.username == username))
        elif user_id is not None:
            statement = statement.where(cls.user_id == user_id)
        elif username is not None:
            statement = statement.where(cls.username == username)
        return list(db.execute(
            statement.order_by(cls.updated_at.desc(), cls.id.desc())
            .offset((page - 1) * count)
            .limit(count)
        ).scalars().all())

    @classmethod
    @legacy_async_db_query
    async def async_list_by_page(
        cls,
        db: AsyncSession,
        page: int = 1,
        count: int = 30,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> list["AgentChat"]:
        """
        异步分页获取 Agent 会话历史。
        """
        statement = select(cls)
        if user_id is not None and username is not None:
            statement = statement.where((cls.user_id == user_id) | (cls.username == username))
        elif user_id is not None:
            statement = statement.where(cls.user_id == user_id)
        elif username is not None:
            statement = statement.where(cls.username == username)
        result = await db.execute(
            statement.order_by(cls.updated_at.desc(), cls.id.desc())
            .offset((page - 1) * count)
            .limit(count)
        )
        return list(result.scalars().all())
