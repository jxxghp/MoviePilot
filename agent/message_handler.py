"""
消息处理器 - 将原有的 message 交互替换为 Agent 交互
"""
import logging
from typing import Dict, Any, List, Optional
from .agent import MovieAgent
from .config import AgentConfig

logger = logging.getLogger(__name__)


class AgentMessageHandler:
    """Agent 消息处理器"""
    
    def __init__(self, config: AgentConfig):
        """
        初始化消息处理器
        
        Args:
            config: Agent 配置
        """
        self.agent = MovieAgent(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
            model=config.openai_model,
            system_prompt=config.system_prompt
        )
        self.conversation_history: Dict[str, List[Dict[str, Any]]] = {}
    
    def _get_history(self, session_id: str) -> List[Dict[str, Any]]:
        """获取会话历史"""
        if session_id not in self.conversation_history:
            self.conversation_history[session_id] = []
        return self.conversation_history[session_id]
    
    def _add_to_history(self, session_id: str, role: str, content: str):
        """添加到会话历史"""
        history = self._get_history(session_id)
        history.append({"role": role, "content": content})
        
        # 限制历史长度，保留最近20条
        if len(history) > 20:
            self.conversation_history[session_id] = history[-20:]
    
    async def handle_message(
        self,
        message: str,
        session_id: str,
        user_id: Optional[str] = None
    ) -> str:
        """
        处理用户消息
        
        Args:
            message: 用户消息
            session_id: 会话ID
            user_id: 用户ID
            
        Returns:
            智能体回复
        """
        try:
            logger.info(f"处理消息 - Session: {session_id}, User: {user_id}, Message: {message}")
            
            # 获取历史对话
            history = self._get_history(session_id)
            
            # 调用智能体
            response = await self.agent.chat(
                user_message=message,
                history=history,
                stream=False
            )
            
            # 更新历史
            self._add_to_history(session_id, "user", message)
            self._add_to_history(session_id, "assistant", response)
            
            logger.info(f"消息处理完成 - Session: {session_id}, Response: {response}")
            
            return response
            
        except Exception as e:
            logger.error(f"消息处理失败: {str(e)}", exc_info=True)
            return f"抱歉，处理您的消息时出现错误: {str(e)}"
    
    async def handle_message_stream(
        self,
        message: str,
        session_id: str,
        user_id: Optional[str] = None
    ):
        """
        流式处理用户消息
        
        Args:
            message: 用户消息
            session_id: 会话ID
            user_id: 用户ID
            
        Yields:
            智能体回复片段
        """
        try:
            logger.info(f"流式处理消息 - Session: {session_id}, User: {user_id}, Message: {message}")
            
            # 获取历史对话
            history = self._get_history(session_id)
            
            # 收集完整回复
            response_parts = []
            
            # 调用智能体流式接口
            async for chunk in self.agent.chat_stream(
                user_message=message,
                history=history
            ):
                response_parts.append(chunk)
                yield chunk
            
            # 更新历史
            full_response = "".join(response_parts)
            self._add_to_history(session_id, "user", message)
            self._add_to_history(session_id, "assistant", full_response)
            
            logger.info(f"流式消息处理完成 - Session: {session_id}")
            
        except Exception as e:
            logger.error(f"流式消息处理失败: {str(e)}", exc_info=True)
            yield f"抱歉，处理您的消息时出现错误: {str(e)}"
    
    def clear_history(self, session_id: str):
        """清空会话历史"""
        if session_id in self.conversation_history:
            del self.conversation_history[session_id]
            logger.info(f"已清空会话历史 - Session: {session_id}")
    
    def get_conversation_history(self, session_id: str) -> List[Dict[str, Any]]:
        """获取会话历史记录"""
        return self._get_history(session_id)
