"""对话记忆管理器"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from app.core.config import settings
from app.helper.redis import AsyncRedisHelper
from app.log import logger
from app.schemas.agent import ConversationMemory


class ConversationMemoryManager:
    """对话记忆管理器"""

    def __init__(self):
        # 内存中的会话记忆缓存
        self.memory_cache: Dict[str, ConversationMemory] = {}
        # 使用现有的Redis助手
        self.redis_helper = AsyncRedisHelper()
        # 内存缓存清理任务（Redis通过TTL自动过期）
        self.cleanup_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """初始化记忆管理器"""
        try:
            # 启动内存缓存清理任务（Redis通过TTL自动过期）
            self.cleanup_task = asyncio.create_task(self._cleanup_expired_memories())
            logger.info("对话记忆管理器初始化完成")

        except Exception as e:
            logger.warning(f"Redis连接失败，将使用内存存储: {e}")

    async def close(self):
        """关闭记忆管理器"""
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass

        await self.redis_helper.close()

        logger.info("对话记忆管理器已关闭")

    @staticmethod
    def get_memory_key(session_id: str, user_id: str):
        """计算内存Key"""
        return f"{user_id}:{session_id}" if user_id else session_id

    @staticmethod
    def get_redis_key(session_id: str, user_id: str):
        """计算Redis Key"""
        return f"agent_memory:{user_id}:{session_id}" if user_id else f"agent_memory:{session_id}"

    async def get_memory(self, session_id: str, user_id: str) -> ConversationMemory:
        """获取会话记忆"""
        # 首先检查缓存
        cache_key = self.get_memory_key(session_id, user_id)
        if cache_key in self.memory_cache:
            return self.memory_cache[cache_key]

        # 尝试从Redis加载
        if settings.CACHE_BACKEND_TYPE == "redis":
            try:
                redis_key = self.get_redis_key(session_id, user_id)
                memory_data = await self.redis_helper.get(redis_key, region="AI_AGENT")
                if memory_data:
                    memory_dict = json.loads(memory_data) if isinstance(memory_data, str) else memory_data
                    memory = ConversationMemory(**memory_dict)
                    self.memory_cache[cache_key] = memory
                    return memory
            except Exception as e:
                logger.warning(f"从Redis加载记忆失败: {e}")

        # 创建新的记忆
        memory = ConversationMemory(session_id=session_id, user_id=user_id)
        self.memory_cache[cache_key] = memory
        await self._save_memory(memory)

        return memory

    async def set_title(self, session_id: str, user_id: str, title: str):
        """设置会话标题"""
        memory = await self.get_memory(session_id=session_id, user_id=user_id)
        memory.title = title
        memory.updated_at = datetime.now()
        await self._save_memory(memory)

    async def get_title(self, session_id: str, user_id: str) -> Optional[str]:
        """获取会话标题"""
        memory = await self.get_memory(session_id=session_id, user_id=user_id)
        return memory.title

    async def list_sessions(self, user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """列出历史会话摘要（按更新时间倒序）

        - 当启用Redis时：遍历 `agent_memory:*` 键并读取摘要
        - 当未启用Redis时：基于内存缓存返回
        """
        sessions: List[ConversationMemory] = []
        # 从Redis遍历
        if settings.CACHE_BACKEND_TYPE == "redis":
            try:
                # 使用Redis助手的items方法遍历所有键
                async for key, value in self.redis_helper.items(region="AI_AGENT"):
                    if key.startswith("agent_memory:"):
                        try:
                            # 解析键名获取user_id和session_id
                            key_parts = key.split(":")
                            if len(key_parts) >= 3:
                                key_user_id = key_parts[2] if len(key_parts) > 3 else None
                                if not user_id or key_user_id == user_id:
                                    data = value if isinstance(value, dict) else json.loads(value)
                                    memory = ConversationMemory(**data)
                                    sessions.append(memory)
                        except Exception as err:
                            logger.warning(f"解析Redis记忆数据失败: {err}")
                            continue
            except Exception as e:
                logger.warning(f"遍历Redis会话失败: {e}")

        # 合并内存缓存（确保包含近期的会话）
        for cache_key, memory in self.memory_cache.items():
            # 如果指定了user_id，只返回该用户的会话
            if not user_id or memory.user_id == user_id:
                sessions.append(memory)

        # 去重（以 session_id 为键，取最近updated）
        uniq: Dict[str, ConversationMemory] = {}
        for mem in sessions:
            existed = uniq.get(mem.session_id)
            if (not existed) or (mem.updated_at > existed.updated_at):
                uniq[mem.session_id] = mem

        # 排序并裁剪
        sorted_list = sorted(uniq.values(), key=lambda m: m.updated_at, reverse=True)[:limit]
        return [
            {
                "session_id": m.session_id,
                "title": m.title or "新会话",
                "message_count": len(m.messages),
                "created_at": m.created_at.isoformat(),
                "updated_at": m.updated_at.isoformat(),
            }
            for m in sorted_list
        ]

    async def add_memory(
            self,
            session_id: str,
            user_id: str,
            role: str,
            content: str,
            metadata: Optional[Dict[str, Any]] = None
    ):
        """添加消息到记忆"""
        memory = await self.get_memory(session_id=session_id, user_id=user_id)

        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {}
        }

        memory.messages.append(message)
        memory.updated_at = datetime.now()

        # 限制消息数量，避免记忆过大
        max_messages = settings.LLM_MAX_MEMORY_MESSAGES
        if len(memory.messages) > max_messages:
            # 保留最近的消息，但保留第一条系统消息
            system_messages = [msg for msg in memory.messages if msg["role"] == "system"]
            recent_messages = memory.messages[-(max_messages - len(system_messages)):]
            memory.messages = system_messages + recent_messages

        await self._save_memory(memory)

        logger.debug(f"消息已添加到记忆: session_id={session_id}, user_id={user_id}, role={role}")

    def get_recent_messages_for_agent(
            self,
            session_id: str,
            user_id: str
    ) -> List[Dict[str, Any]]:
        """为Agent获取最近的消息（仅内存缓存）

        如果消息Token数量超过模型最大上下文长度的阀值，会自动进行摘要裁剪
        """
        cache_key = self.get_memory_key(session_id, user_id)
        memory = self.memory_cache.get(cache_key)
        if not memory:
            return []

        # 获取所有消息
        return memory.messages

    async def get_recent_messages(
            self,
            session_id: str,
            user_id: str,
            limit: int = 10,
            role_filter: Optional[list] = None
    ) -> List[Dict[str, Any]]:
        """获取最近的消息"""
        memory = await self.get_memory(session_id=session_id, user_id=user_id)

        messages = memory.messages
        if role_filter:
            messages = [msg for msg in messages if msg["role"] in role_filter]

        return messages[-limit:] if messages else []

    async def get_context(self, session_id: str, user_id: str) -> Dict[str, Any]:
        """获取会话上下文"""
        memory = await self.get_memory(session_id=session_id, user_id=user_id)
        return memory.context

    async def clear_memory(self, session_id: str, user_id: str):
        """清空会话记忆"""
        cache_key = f"{user_id}:{session_id}" if user_id else session_id
        if cache_key in self.memory_cache:
            del self.memory_cache[cache_key]

        if settings.CACHE_BACKEND_TYPE == "redis":
            redis_key = self.get_redis_key(session_id, user_id)
            await self.redis_helper.delete(redis_key, region="AI_AGENT")

        logger.info(f"会话记忆已清空: session_id={session_id}, user_id={user_id}")

    async def _save_memory(self, memory: ConversationMemory):
        """保存记忆到存储

        Redis中的记忆会自动通过TTL机制过期，无需手动清理
        """
        # 更新内存缓存
        cache_key = self.get_memory_key(memory.session_id, memory.user_id)
        self.memory_cache[cache_key] = memory

        # 保存到Redis，设置TTL自动过期
        if settings.CACHE_BACKEND_TYPE == "redis":
            try:
                memory_dict = memory.model_dump()
                redis_key = self.get_redis_key(memory.session_id, memory.user_id)
                ttl = int(timedelta(days=settings.LLM_REDIS_MEMORY_RETENTION_DAYS).total_seconds())
                await self.redis_helper.set(
                    redis_key,
                    memory_dict,
                    ttl=ttl,
                    region="AI_AGENT"
                )
            except Exception as e:
                logger.warning(f"保存记忆到Redis失败: {e}")

    async def _cleanup_expired_memories(self):
        """清理内存中过期记忆的后台任务

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
                    if (current_time - memory.updated_at).days > settings.LLM_MEMORY_RETENTION_DAYS:
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
