"""
客户端调用示例
"""
import asyncio
import aiohttp
import json


class MovieAgentClient:
    """电影智能体客户端"""
    
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        self.user_id = "test_user_123"
    
    async def chat(self, message: str):
        """发送消息"""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/v1/chat",
                json={
                    "message": message,
                    "user_id": self.user_id
                }
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["response"]
                else:
                    error = await response.text()
                    raise Exception(f"请求失败: {error}")
    
    async def get_history(self):
        """获取对话历史"""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/api/v1/chat/history/{self.user_id}"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["history"]
                else:
                    error = await response.text()
                    raise Exception(f"请求失败: {error}")
    
    async def clear_history(self):
        """清空对话历史"""
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{self.base_url}/api/v1/chat/history/{self.user_id}"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["message"]
                else:
                    error = await response.text()
                    raise Exception(f"请求失败: {error}")
    
    async def list_tools(self):
        """获取工具列表"""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/api/v1/tools"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["tools"]
                else:
                    error = await response.text()
                    raise Exception(f"请求失败: {error}")
    
    async def chat_stream_websocket(self, message: str):
        """WebSocket 流式对话"""
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"{self.base_url}/ws/chat/{self.user_id}"
            ) as ws:
                # 发送消息
                await ws.send_str(message)
                
                # 接收响应
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        text = msg.data
                        if text == "[START]":
                            print("开始接收...", flush=True)
                        elif text == "[DONE]":
                            print("\n完成", flush=True)
                            break
                        else:
                            print(text, end="", flush=True)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        break


async def demo_rest_api():
    """演示 REST API 调用"""
    client = MovieAgentClient()
    
    print("=" * 60)
    print("REST API 调用示例")
    print("=" * 60)
    
    # 1. 查看可用工具
    print("\n1. 查看可用工具")
    print("-" * 60)
    try:
        tools = await client.list_tools()
        print(f"可用工具数量: {len(tools)}")
        for tool in tools:
            print(f"  - {tool['name']}: {tool['description']}")
    except Exception as e:
        print(f"错误: {e}")
    
    # 2. 发送消息
    print("\n2. 发送消息")
    print("-" * 60)
    try:
        message = "帮我搜索2024年上映的科幻电影"
        print(f"用户: {message}")
        response = await client.chat(message)
        print(f"智能体: {response}")
    except Exception as e:
        print(f"错误: {e}")
    
    # 3. 查看历史
    print("\n3. 查看对话历史")
    print("-" * 60)
    try:
        history = await client.get_history()
        print(f"历史记录数: {len(history)}")
        for item in history[-4:]:  # 显示最近4条
            print(f"  {item['role']}: {item['content'][:50]}...")
    except Exception as e:
        print(f"错误: {e}")
    
    # 4. 继续对话
    print("\n4. 继续对话")
    print("-" * 60)
    try:
        message = "订阅第一部电影"
        print(f"用户: {message}")
        response = await client.chat(message)
        print(f"智能体: {response}")
    except Exception as e:
        print(f"错误: {e}")
    
    # 5. 清空历史
    print("\n5. 清空对话历史")
    print("-" * 60)
    try:
        result = await client.clear_history()
        print(result)
    except Exception as e:
        print(f"错误: {e}")
    
    print("\n" + "=" * 60)


async def demo_websocket():
    """演示 WebSocket 调用"""
    client = MovieAgentClient()
    
    print("\n\n" + "=" * 60)
    print("WebSocket 流式对话示例")
    print("=" * 60)
    
    try:
        print("\n用户: 推荐一部评分高的科幻电影给我")
        print("智能体: ", end="", flush=True)
        await client.chat_stream_websocket("推荐一部评分高的科幻电影给我")
    except Exception as e:
        print(f"\n错误: {e}")
    
    print("\n" + "=" * 60)


async def interactive_mode():
    """交互模式"""
    client = MovieAgentClient()
    
    print("=" * 60)
    print("电影智能助手 - 交互模式")
    print("输入 'quit' 或 'exit' 退出")
    print("输入 'history' 查看历史")
    print("输入 'clear' 清空历史")
    print("=" * 60)
    
    while True:
        try:
            user_input = input("\n你: ").strip()
            
            if user_input.lower() in ['quit', 'exit', '退出']:
                print("再见!")
                break
            
            if not user_input:
                continue
            
            if user_input.lower() == 'history':
                history = await client.get_history()
                print(f"\n对话历史 (共 {len(history)} 条):")
                for item in history:
                    print(f"  {item['role']}: {item['content']}")
                continue
            
            if user_input.lower() == 'clear':
                await client.clear_history()
                print("对话历史已清空")
                continue
            
            # 发送消息
            print("智能体: ", end="", flush=True)
            response = await client.chat(user_input)
            print(response)
        
        except KeyboardInterrupt:
            print("\n\n再见!")
            break
        except Exception as e:
            print(f"\n错误: {e}")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        if mode == "interactive":
            asyncio.run(interactive_mode())
        elif mode == "websocket":
            asyncio.run(demo_websocket())
        else:
            print("未知模式。可用模式: interactive, websocket")
    else:
        # 默认运行所有演示
        asyncio.run(demo_rest_api())
        asyncio.run(demo_websocket())
        
        print("\n\n提示: 运行 'python client_example.py interactive' 进入交互模式")
