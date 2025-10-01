"""
FastAPI 集成完整示例
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import os
import logging

from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 创建 FastAPI 应用
app = FastAPI(
    title="电影智能助手 API",
    description="基于 Agent 的智能电影资源搜索、订阅和下载系统",
    version="1.0.0"
)

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 初始化 Agent
config = AgentConfig(
    openai_api_key=os.getenv("OPENAI_API_KEY", ""),
    openai_base_url=os.getenv("OPENAI_BASE_URL"),
    openai_model=os.getenv("OPENAI_MODEL", "gpt-4-turbo-preview")
)

agent_handler = AgentMessageHandler(config)


# 请求/响应模型
class ChatRequest(BaseModel):
    """对话请求"""
    message: str
    user_id: str


class ChatResponse(BaseModel):
    """对话响应"""
    response: str
    session_id: str


class HistoryItem(BaseModel):
    """历史记录项"""
    role: str
    content: str


class HistoryResponse(BaseModel):
    """历史记录响应"""
    session_id: str
    history: List[HistoryItem]


# API 路由
@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "电影智能助手 API",
        "version": "1.0.0",
        "docs": "/docs"
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok"}


@app.post("/api/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    普通对话接口
    
    发送消息给智能体并获取回复
    """
    try:
        session_id = f"user_{request.user_id}"
        logger.info(f"收到对话请求 - User: {request.user_id}, Message: {request.message}")
        
        response = await agent_handler.handle_message(
            message=request.message,
            session_id=session_id,
            user_id=request.user_id
        )
        
        return ChatResponse(
            response=response,
            session_id=session_id
        )
    
    except Exception as e:
        logger.error(f"对话处理失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws/chat/{user_id}")
async def websocket_chat(websocket: WebSocket, user_id: str):
    """
    WebSocket 流式对话接口
    
    支持实时流式返回智能体回复
    """
    await websocket.accept()
    session_id = f"user_{user_id}"
    logger.info(f"WebSocket 连接建立 - User: {user_id}")
    
    try:
        while True:
            # 接收消息
            message = await websocket.receive_text()
            logger.info(f"WebSocket 收到消息 - User: {user_id}, Message: {message}")
            
            # 发送开始标记
            await websocket.send_text("[START]")
            
            # 流式返回
            async for chunk in agent_handler.handle_message_stream(
                message=message,
                session_id=session_id,
                user_id=user_id
            ):
                await websocket.send_text(chunk)
            
            # 发送结束标记
            await websocket.send_text("[DONE]")
    
    except WebSocketDisconnect:
        logger.info(f"WebSocket 连接断开 - User: {user_id}")
    except Exception as e:
        logger.error(f"WebSocket 错误: {str(e)}", exc_info=True)
        await websocket.close()


@app.get("/api/v1/chat/history/{user_id}", response_model=HistoryResponse)
async def get_chat_history(user_id: str):
    """
    获取对话历史
    
    返回用户的对话历史记录
    """
    try:
        session_id = f"user_{user_id}"
        history = agent_handler.get_conversation_history(session_id)
        
        return HistoryResponse(
            session_id=session_id,
            history=[
                HistoryItem(role=item["role"], content=item["content"])
                for item in history
            ]
        )
    
    except Exception as e:
        logger.error(f"获取历史失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/v1/chat/history/{user_id}")
async def clear_chat_history(user_id: str):
    """
    清空对话历史
    
    删除用户的所有对话历史记录
    """
    try:
        session_id = f"user_{user_id}"
        agent_handler.clear_history(session_id)
        logger.info(f"已清空对话历史 - User: {user_id}")
        
        return {"message": "对话历史已清空", "session_id": session_id}
    
    except Exception as e:
        logger.error(f"清空历史失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/tools")
async def list_tools():
    """
    列出所有可用工具
    
    返回智能体可以使用的所有工具及其描述
    """
    try:
        tools = agent_handler.agent.tool_registry.get_all_tools()
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": [
                        {
                            "name": param.name,
                            "type": param.type,
                            "description": param.description,
                            "required": param.required,
                            "enum": param.enum
                        }
                        for param in tool.parameters
                    ]
                }
                for tool in tools
            ]
        }
    
    except Exception as e:
        logger.error(f"获取工具列表失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    
    # 检查 API Key
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("未设置 OPENAI_API_KEY 环境变量")
    
    # 启动服务
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
