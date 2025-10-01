"""
消息处理模块
使用智能体替代传统的 message 交互功能
"""
from typing import Dict, Any, Optional, List
from datetime import datetime
from agent_core import Agent, Message, MessageRole
from movie_tools import create_movie_tools
import json


class MessageHandler:
    """消息处理器 - 基于智能体的消息交互"""
    
    def __init__(self, llm_client: Any = None):
        """
        初始化消息处理器
        
        Args:
            llm_client: LLM 客户端（可选，如果不提供则使用规则匹配）
        """
        self.system_prompt = """你是一个智能电影资源管理助手。你可以帮助用户：

1. **搜索电影**：根据关键词、年份、类型、评分等条件搜索电影
2. **获取详情**：查看电影的详细信息
3. **订阅电影**：订阅感兴趣的电影，自动监控更新
4. **管理订阅**：查看和取消订阅
5. **下载电影**：下载电影资源到本地
6. **管理下载**：查看下载进度、取消下载任务

使用工具处理用户请求时：
- 优先理解用户意图
- 选择合适的工具完成任务
- 将结果以友好的方式呈现给用户
- 如果需要多步操作，先获取必要信息再执行

请用中文与用户交流，态度友好专业。"""
        
        # 创建智能体
        self.agent = Agent(
            name="MovieAgent",
            system_prompt=self.system_prompt,
            llm_client=llm_client
        )
        
        # 注册工具
        tools = create_movie_tools()
        self.agent.register_tools(tools)
        
        # 会话管理
        self.sessions: Dict[str, Agent] = {}
        
        print("[MessageHandler] 初始化完成，已注册 {} 个工具".format(len(tools)))
    
    def handle_message(self, 
                      user_id: str,
                      message: str,
                      session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        处理用户消息
        
        Args:
            user_id: 用户 ID
            message: 用户消息内容
            session_id: 会话 ID（可选，用于多轮对话）
        
        Returns:
            响应结果
        """
        try:
            # 获取或创建会话
            if session_id and session_id in self.sessions:
                agent = self.sessions[session_id]
            else:
                # 创建新会话（复用主 agent 的配置）
                agent = self.agent
                if session_id:
                    self.sessions[session_id] = agent
            
            # 调用智能体处理消息
            response = agent.chat(message)
            
            return {
                "success": True,
                "user_id": user_id,
                "message": message,
                "response": response,
                "timestamp": datetime.now().isoformat(),
                "session_id": session_id
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "user_id": user_id,
                "message": message,
                "timestamp": datetime.now().isoformat()
            }
    
    def reset_session(self, session_id: str) -> bool:
        """
        重置会话
        
        Args:
            session_id: 会话 ID
        
        Returns:
            是否成功
        """
        if session_id in self.sessions:
            self.sessions[session_id].reset_conversation()
            return True
        return False
    
    def delete_session(self, session_id: str) -> bool:
        """
        删除会话
        
        Args:
            session_id: 会话 ID
        
        Returns:
            是否成功
        """
        if session_id in self.sessions:
            del self.sessions[session_id]
            return True
        return False
    
    def get_session_history(self, session_id: str) -> Optional[List[Dict[str, Any]]]:
        """
        获取会话历史
        
        Args:
            session_id: 会话 ID
        
        Returns:
            消息历史列表
        """
        if session_id in self.sessions:
            history = self.sessions[session_id].get_conversation_history()
            return [
                {
                    "role": msg.role.value,
                    "content": msg.content,
                    "tool_calls": msg.tool_calls,
                    "tool_call_id": msg.tool_call_id,
                    "name": msg.name
                }
                for msg in history
            ]
        return None


class WebSocketMessageHandler(MessageHandler):
    """WebSocket 消息处理器"""
    
    def __init__(self, llm_client: Any = None):
        super().__init__(llm_client)
        self.connections: Dict[str, Any] = {}
    
    async def handle_websocket_message(self,
                                      websocket: Any,
                                      user_id: str,
                                      message: str,
                                      session_id: Optional[str] = None) -> None:
        """
        处理 WebSocket 消息（异步）
        
        Args:
            websocket: WebSocket 连接
            user_id: 用户 ID
            message: 消息内容
            session_id: 会话 ID
        """
        try:
            # 发送处理中状态
            await websocket.send_json({
                "type": "processing",
                "message": "正在处理您的请求..."
            })
            
            # 处理消息
            result = self.handle_message(user_id, message, session_id)
            
            # 发送响应
            await websocket.send_json({
                "type": "response",
                "data": result
            })
            
        except Exception as e:
            await websocket.send_json({
                "type": "error",
                "error": str(e)
            })
    
    def register_connection(self, user_id: str, websocket: Any) -> None:
        """注册 WebSocket 连接"""
        self.connections[user_id] = websocket
    
    def unregister_connection(self, user_id: str) -> None:
        """注销 WebSocket 连接"""
        if user_id in self.connections:
            del self.connections[user_id]


class HTTPMessageHandler(MessageHandler):
    """HTTP API 消息处理器"""
    
    def handle_http_request(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理 HTTP 请求
        
        Args:
            request_data: 请求数据，格式：
                {
                    "user_id": "用户ID",
                    "message": "消息内容",
                    "session_id": "会话ID（可选）"
                }
        
        Returns:
            响应数据
        """
        user_id = request_data.get("user_id")
        message = request_data.get("message")
        session_id = request_data.get("session_id")
        
        if not user_id or not message:
            return {
                "success": False,
                "error": "缺少必要参数：user_id 或 message"
            }
        
        return self.handle_message(user_id, message, session_id)
    
    def handle_session_reset(self, session_id: str) -> Dict[str, Any]:
        """处理会话重置请求"""
        success = self.reset_session(session_id)
        return {
            "success": success,
            "message": "会话已重置" if success else "会话不存在"
        }
    
    def handle_session_history(self, session_id: str) -> Dict[str, Any]:
        """处理获取会话历史请求"""
        history = self.get_session_history(session_id)
        if history is not None:
            return {
                "success": True,
                "history": history
            }
        else:
            return {
                "success": False,
                "error": "会话不存在"
            }
