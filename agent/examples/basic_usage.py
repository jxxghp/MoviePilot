"""
基础使用示例
"""
import asyncio
import os
from agent import MovieAgent
from agent.config import AgentConfig


async def main():
    """基础使用示例"""
    
    # 配置
    config = AgentConfig(
        openai_api_key=os.getenv("OPENAI_API_KEY", "your-api-key"),
        openai_base_url=os.getenv("OPENAI_BASE_URL"),
        openai_model="gpt-4-turbo-preview"
    )
    
    # 创建智能体
    agent = MovieAgent(
        api_key=config.openai_api_key,
        base_url=config.openai_base_url,
        model=config.openai_model
    )
    
    # 示例对话
    print("=" * 60)
    print("电影智能体示例")
    print("=" * 60)
    
    # 示例1: 搜索电影
    print("\n示例1: 搜索电影")
    print("-" * 60)
    response = await agent.chat("帮我搜索2023年的科幻电影")
    print(f"用户: 帮我搜索2023年的科幻电影")
    print(f"智能体: {response}")
    
    # 示例2: 订阅电影
    print("\n示例2: 订阅电影")
    print("-" * 60)
    response = await agent.chat("帮我订阅《沙丘2》，要1080P的")
    print(f"用户: 帮我订阅《沙丘2》，要1080P的")
    print(f"智能体: {response}")
    
    # 示例3: 查看订阅
    print("\n示例3: 查看订阅")
    print("-" * 60)
    response = await agent.chat("查看我的订阅列表")
    print(f"用户: 查看我的订阅列表")
    print(f"智能体: {response}")
    
    # 示例4: 流式对话
    print("\n示例4: 流式对话")
    print("-" * 60)
    print(f"用户: 帮我找一部好看的动作电影")
    print(f"智能体: ", end="", flush=True)
    async for chunk in agent.chat_stream("帮我找一部好看的动作电影"):
        print(chunk, end="", flush=True)
    print()
    
    print("\n" + "=" * 60)


async def interactive_mode():
    """交互模式"""
    config = AgentConfig(
        openai_api_key=os.getenv("OPENAI_API_KEY", "your-api-key"),
        openai_base_url=os.getenv("OPENAI_BASE_URL"),
        openai_model="gpt-4-turbo-preview"
    )
    
    agent = MovieAgent(
        api_key=config.openai_api_key,
        base_url=config.openai_base_url,
        model=config.openai_model
    )
    
    print("=" * 60)
    print("电影智能体 - 交互模式")
    print("输入 'quit' 或 'exit' 退出")
    print("=" * 60)
    
    history = []
    
    while True:
        user_input = input("\n你: ").strip()
        
        if user_input.lower() in ['quit', 'exit', '退出']:
            print("再见!")
            break
        
        if not user_input:
            continue
        
        print("智能体: ", end="", flush=True)
        
        response_parts = []
        async for chunk in agent.chat_stream(user_input, history=history):
            print(chunk, end="", flush=True)
            response_parts.append(chunk)
        
        print()
        
        # 更新历史
        full_response = "".join(response_parts)
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": full_response})


if __name__ == "__main__":
    # 运行示例
    # asyncio.run(main())
    
    # 或运行交互模式
    asyncio.run(interactive_mode())
