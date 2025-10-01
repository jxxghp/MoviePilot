"""
集成示例 - 展示如何将 Agent 集成到现有系统中
"""
import asyncio
import os
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig


class MockMessageSystem:
    """模拟原有的消息系统"""
    
    def __init__(self):
        # 初始化 Agent 消息处理器
        config = AgentConfig(
            openai_api_key=os.getenv("OPENAI_API_KEY", "your-api-key"),
            openai_base_url=os.getenv("OPENAI_BASE_URL"),
            openai_model="gpt-4-turbo-preview"
        )
        self.agent_handler = AgentMessageHandler(config)
    
    async def handle_user_message(self, user_id: str, message: str):
        """
        处理用户消息的接口
        这个方法可以替换原有系统中的消息处理逻辑
        """
        # 使用 user_id 作为 session_id
        session_id = f"user_{user_id}"
        
        # 使用 Agent 处理消息
        response = await self.agent_handler.handle_message(
            message=message,
            session_id=session_id,
            user_id=user_id
        )
        
        return response
    
    async def handle_user_message_stream(self, user_id: str, message: str):
        """
        流式处理用户消息的接口
        适用于需要实时返回的场景（如 WebSocket、SSE）
        """
        session_id = f"user_{user_id}"
        
        async for chunk in self.agent_handler.handle_message_stream(
            message=message,
            session_id=session_id,
            user_id=user_id
        ):
            yield chunk


async def demo_integration():
    """演示集成效果"""
    
    # 创建模拟的消息系统
    message_system = MockMessageSystem()
    
    # 模拟用户交互
    user_id = "user_123"
    
    print("=" * 60)
    print("集成演示 - 将 Agent 集成到现有消息系统")
    print("=" * 60)
    
    # 场景1: 普通消息处理
    print("\n场景1: 普通消息处理")
    print("-" * 60)
    message1 = "帮我搜索最近上映的电影"
    print(f"用户 {user_id}: {message1}")
    response1 = await message_system.handle_user_message(user_id, message1)
    print(f"系统回复: {response1}")
    
    # 场景2: 流式消息处理
    print("\n场景2: 流式消息处理")
    print("-" * 60)
    message2 = "订阅这部电影"
    print(f"用户 {user_id}: {message2}")
    print(f"系统回复: ", end="", flush=True)
    async for chunk in message_system.handle_user_message_stream(user_id, message2):
        print(chunk, end="", flush=True)
    print()
    
    # 场景3: 上下文连续对话
    print("\n场景3: 上下文连续对话")
    print("-" * 60)
    message3 = "查看我的订阅"
    print(f"用户 {user_id}: {message3}")
    response3 = await message_system.handle_user_message(user_id, message3)
    print(f"系统回复: {response3}")
    
    print("\n" + "=" * 60)


# FastAPI 集成示例
FASTAPI_EXAMPLE = '''
"""
FastAPI 集成示例
"""
from fastapi import FastAPI, WebSocket, HTTPException
from pydantic import BaseModel
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig
import os

app = FastAPI(title="电影智能助手 API")

# 初始化 Agent
config = AgentConfig(
    openai_api_key=os.getenv("OPENAI_API_KEY"),
    openai_base_url=os.getenv("OPENAI_BASE_URL"),
    openai_model="gpt-4-turbo-preview"
)
agent_handler = AgentMessageHandler(config)


class MessageRequest(BaseModel):
    message: str
    user_id: str


class MessageResponse(BaseModel):
    response: str
    session_id: str


@app.post("/api/chat", response_model=MessageResponse)
async def chat(request: MessageRequest):
    """普通对话接口"""
    session_id = f"user_{request.user_id}"
    response = await agent_handler.handle_message(
        message=request.message,
        session_id=session_id,
        user_id=request.user_id
    )
    return MessageResponse(response=response, session_id=session_id)


@app.websocket("/ws/chat/{user_id}")
async def websocket_chat(websocket: WebSocket, user_id: str):
    """WebSocket 流式对话接口"""
    await websocket.accept()
    session_id = f"user_{user_id}"
    
    try:
        while True:
            # 接收消息
            message = await websocket.receive_text()
            
            # 流式返回
            async for chunk in agent_handler.handle_message_stream(
                message=message,
                session_id=session_id,
                user_id=user_id
            ):
                await websocket.send_text(chunk)
            
            # 发送结束标记
            await websocket.send_text("[DONE]")
    
    except Exception as e:
        await websocket.close()


@app.delete("/api/chat/history/{user_id}")
async def clear_history(user_id: str):
    """清空对话历史"""
    session_id = f"user_{user_id}"
    agent_handler.clear_history(session_id)
    return {"message": "History cleared"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
'''


# Flask 集成示例
FLASK_EXAMPLE = '''
"""
Flask 集成示例
"""
from flask import Flask, request, jsonify, stream_with_context, Response
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig
import os
import asyncio

app = Flask(__name__)

# 初始化 Agent
config = AgentConfig(
    openai_api_key=os.getenv("OPENAI_API_KEY"),
    openai_base_url=os.getenv("OPENAI_BASE_URL"),
    openai_model="gpt-4-turbo-preview"
)
agent_handler = AgentMessageHandler(config)


@app.route("/api/chat", methods=["POST"])
def chat():
    """普通对话接口"""
    data = request.get_json()
    message = data.get("message")
    user_id = data.get("user_id")
    
    if not message or not user_id:
        return jsonify({"error": "Missing message or user_id"}), 400
    
    session_id = f"user_{user_id}"
    
    # 在同步环境中运行异步代码
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    response = loop.run_until_complete(
        agent_handler.handle_message(message, session_id, user_id)
    )
    loop.close()
    
    return jsonify({
        "response": response,
        "session_id": session_id
    })


@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    """流式对话接口"""
    data = request.get_json()
    message = data.get("message")
    user_id = data.get("user_id")
    
    if not message or not user_id:
        return jsonify({"error": "Missing message or user_id"}), 400
    
    session_id = f"user_{user_id}"
    
    async def generate():
        async for chunk in agent_handler.handle_message_stream(
            message, session_id, user_id
        ):
            yield f"data: {chunk}\\n\\n"
        yield "data: [DONE]\\n\\n"
    
    # 在同步环境中运行异步生成器
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    def sync_generate():
        async_gen = generate()
        while True:
            try:
                chunk = loop.run_until_complete(async_gen.__anext__())
                yield chunk
            except StopAsyncIteration:
                break
    
    return Response(
        stream_with_context(sync_generate()),
        mimetype="text/event-stream"
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
'''


if __name__ == "__main__":
    # 运行集成演示
    asyncio.run(demo_integration())
    
    # FastAPI 和 Flask 的集成代码示例已经提供在字符串中
    # 可以复制到单独的文件中使用
    print("\n\nFastAPI 集成示例代码：")
    print(FASTAPI_EXAMPLE)
    print("\n\nFlask 集成示例代码：")
    print(FLASK_EXAMPLE)
