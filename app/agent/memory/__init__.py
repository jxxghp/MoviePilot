"""对话记忆管理器"""

import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from langchain_core.messages import BaseMessage, messages_from_dict, messages_to_dict

from app.application.messaging.chat import (
    AgentChatPersistenceService,
    AgentChatService,
    get_configured_agent_chat_persistence,
    get_configured_agent_chat_service,
)
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.agent import ConversationMemory


class MemoryManager:
    """
    对话记忆管理器
    """

    def __init__(
        self,
        chat: AgentChatService | None = None,
        persistence: AgentChatPersistenceService | None = None,
    ):
        """创建记忆缓存，并保存组合根显式注入的会话能力。"""
        self._chat = chat
        self._persistence = persistence
        # 内存中的会话记忆缓存
        self.memory_cache: Dict[str, ConversationMemory] = {}
        # 内存缓存清理任务
        self.cleanup_task: Optional[asyncio.Task] = None

    def configure(
        self,
        chat: AgentChatService,
        persistence: AgentChatPersistenceService,
    ) -> None:
        """在 Agent 启动前绑定唯一会话查询与写入服务。"""
        self._chat = chat
        self._persistence = persistence

    def _chat_service(self) -> AgentChatService:
        """返回显式注入服务，兼容测试未装配时使用既有应用服务。"""
        return self._chat or get_configured_agent_chat_service()

    def _chat_persistence(self) -> AgentChatPersistenceService:
        """返回显式注入写服务，兼容测试未装配时使用既有应用服务。"""
        return self._persistence or get_configured_agent_chat_persistence()

    def initialize(self):
        """
        初始化记忆管理器
        """
        try:
            if self.cleanup_task and not self.cleanup_task.done():
                return
            # 启动内存缓存清理任务（Redis通过TTL自动过期）
            self.cleanup_task = asyncio.create_task(
                self._cleanup_expired_memories()
            )
            logger.info("对话记忆管理器初始化完成")

        except Exception as e:
            logger.warning(f"Redis连接失败，将使用内存存储: {e}")

    async def close(self):
        """
        关闭记忆管理器
        """
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
            self.cleanup_task = None

        logger.info("对话记忆管理器已关闭")

    @staticmethod
    def _get_memory_key(session_id: str, user_id: str):
        """
        计算内存Key
        """
        return f"{user_id}:{session_id}" if user_id else session_id

    def get_memory(self, session_id: str, user_id: str) -> Optional[ConversationMemory]:
        """
        获取内存中的记忆
        """
        cache_key = self._get_memory_key(session_id, user_id)
        return self.memory_cache.get(cache_key)

    def get_agent_messages(
            self, session_id: str, user_id: str
    ) -> List[BaseMessage]:
        """
        为Agent获取最近的消息。

        优先使用内存缓存，缓存不存在时从数据库恢复上一轮持久化的原始 messages。
        """
        memory = self.get_memory(session_id, user_id)
        if memory:
            return memory.messages

        try:
            chat = self._chat_service().get_sync(
                session_id=session_id,
                user_id=user_id,
            )
            if not chat:
                chat = self._chat_service().get_sync(session_id=session_id)
        except Exception as e:
            logger.debug(f"读取持久化Agent会话失败: {e}")
            return []
        if not chat or not chat.agent_messages:
            return []

        try:
            messages = messages_from_dict(chat.agent_messages)
        except Exception as e:
            logger.debug(f"恢复持久化Agent消息失败: {e}")
            return []

        memory = ConversationMemory(
            session_id=session_id,
            user_id=user_id,
            messages=messages,
        )
        self.save_memory(memory)
        return memory.messages

    async def async_get_agent_messages(
        self, session_id: str, user_id: str
    ) -> List[BaseMessage]:
        """异步恢复 Agent 消息，查询与会话应用服务保持同一异步端口。"""
        memory = self.get_memory(session_id, user_id)
        if memory:
            return memory.messages

        try:
            service = self._chat_service()
            chat = await service.get(
                session_id=session_id,
                user_id=user_id,
            )
            if not chat:
                chat = await service.get(session_id=session_id)
        except Exception as e:
            logger.debug(f"读取持久化Agent会话失败: {e}")
            return []
        if not chat or not chat.agent_messages:
            return []

        try:
            messages = messages_from_dict(chat.agent_messages)
        except Exception as e:
            logger.debug(f"恢复持久化Agent消息失败: {e}")
            return []

        memory = ConversationMemory(
            session_id=session_id,
            user_id=user_id,
            messages=messages,
        )
        self.save_memory(memory)
        return memory.messages

    def save_agent_messages(
            self, session_id: str, user_id: str, messages: List[BaseMessage]
    ):
        """
        保存Agent消息到内存缓存与持久化会话表。
        """
        memory = self.get_memory(session_id, user_id)
        if not memory:
            memory = ConversationMemory(session_id=session_id, user_id=user_id)

        memory.messages = messages
        memory.updated_at = datetime.now()

        # 更新内存缓存
        self.save_memory(memory)
        try:
            self._chat_service().save_agent_messages(
                session_id=session_id,
                user_id=user_id,
                messages=messages_to_dict(messages),
            )
        except Exception as e:
            logger.debug(f"持久化Agent消息失败: {e}")

    async def async_save_agent_messages(
        self, session_id: str, user_id: str, messages: List[BaseMessage]
    ) -> None:
        """异步保存 Agent 消息，持久化写入经有界数据库 worker 承接。"""
        memory = self.get_memory(session_id, user_id)
        if not memory:
            memory = ConversationMemory(session_id=session_id, user_id=user_id)

        memory.messages = messages
        memory.updated_at = datetime.now()
        self.save_memory(memory)
        try:
            persistence = self._chat_persistence()
            await persistence.async_save_agent_messages(
                session_id=session_id,
                user_id=user_id,
                messages=messages_to_dict(messages),
            )
        except Exception as e:
            logger.debug(f"持久化Agent消息失败: {e}")

    def save_memory(self, memory: ConversationMemory):
        """
        保存记忆到内存缓存

        注意：Redis中的记忆通过TTL机制自动过期，这里只更新内存缓存，Redis会在下次访问时自动过期
        """
        cache_key = self._get_memory_key(memory.session_id, memory.user_id)
        self.memory_cache[cache_key] = memory

    def clear_memory(self, session_id: str, user_id: str):
        """
        清空会话记忆
        """
        cache_key = self._get_memory_key(session_id, user_id)
        if cache_key in self.memory_cache:
            del self.memory_cache[cache_key]

        logger.info(f"会话记忆已清空: session_id={session_id}, user_id={user_id}")

    async def _cleanup_expired_memories(self):
        """
        清理内存中过期记忆的后台任务

        注意：Redis中的记忆通过TTL机制自动过期，这里只清理内存缓存
        """
        while True:
            try:
                # 每小时清理一次
                await asyncio.sleep(3600)

                current_time = datetime.now()
                expired_sessions = []

                # 只检查内存缓存中的过期记忆
                # Redis中的记忆会通过TTL自动过期，无需手动处理
                for cache_key, memory in self.memory_cache.items():
                    if (
                            current_time - memory.updated_at
                    ).days > get_runtime_setting('LLM_MEMORY_RETENTION_DAYS'):
                        expired_sessions.append(cache_key)

                # 只清理内存缓存，不删除Redis中的键（Redis会自动过期）
                for cache_key in expired_sessions:
                    if cache_key in self.memory_cache:
                        del self.memory_cache[cache_key]

                if expired_sessions:
                    logger.info(f"清理了{len(expired_sessions)}个过期内存会话记忆")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理记忆时发生错误: {e}")


memory_manager = MemoryManager()
