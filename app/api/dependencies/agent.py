"""Agent 与消息查询依赖。"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.context import get_agent_chat_repository, get_agent_chat_transaction
from app.api.data import get_async_db
from app.api.dependencies.data import repository
from app.application.messaging.chat import (
    AgentChatService,
    AsyncAgentChatRepository,
    AsyncUnitOfWork,
)
from app.application.messaging.message import MessageQueryService


def get_agent_chat_service(
    chat_repository: AsyncAgentChatRepository = Depends(get_agent_chat_repository),
    unit_of_work: AsyncUnitOfWork = Depends(get_agent_chat_transaction),
) -> AgentChatService:
    """组装类型化 Agent 会话历史查询和删除服务。"""
    return AgentChatService(chat_repository, unit_of_work)


def get_message_query_service(
    db: AsyncSession = Depends(get_async_db),
) -> MessageQueryService:
    """组装消息历史异步查询服务。"""
    return MessageQueryService(repository=repository("message", db))
