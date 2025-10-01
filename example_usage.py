"""
使用示例
展示如何使用智能体进行电影资源管理
"""
from agent_core import Agent
from movie_tools import create_movie_tools
from message_handler import MessageHandler
import json


def print_section(title: str):
    """打印分隔线"""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60 + "\n")


def example_1_basic_agent():
    """示例 1: 基础智能体使用"""
    print_section("示例 1: 基础智能体使用")
    
    # 创建智能体
    agent = Agent(
        name="MovieAgent",
        system_prompt="你是一个专业的电影资源管理助手，可以帮助用户搜索、订阅和下载电影。"
    )
    
    # 注册工具
    tools = create_movie_tools()
    agent.register_tools(tools)
    
    # 对话示例
    print("用户: 帮我搜索一些评分9分以上的电影")
    response = agent.chat("帮我搜索一些评分9分以上的电影")
    print(f"助手: {response}\n")
    
    print("用户: 告诉我肖申克的救赎的详细信息")
    response = agent.chat("告诉我肖申克的救赎的详细信息")
    print(f"助手: {response}\n")


def example_2_search_and_subscribe():
    """示例 2: 搜索和订阅电影"""
    print_section("示例 2: 搜索和订阅电影")
    
    agent = Agent(name="MovieAgent")
    tools = create_movie_tools()
    agent.register_tools(tools)
    
    # 搜索科幻电影
    print("用户: 搜索科幻电影")
    response = agent.chat("搜索科幻电影")
    print(f"助手: {response}\n")
    
    # 订阅电影
    print("用户: 订阅《盗梦空间》，要4K画质的")
    response = agent.chat("订阅《盗梦空间》，要4K画质的")
    print(f"助手: {response}\n")
    
    # 查看订阅列表
    print("用户: 查看我的订阅列表")
    response = agent.chat("查看我的订阅列表")
    print(f"助手: {response}\n")


def example_3_download_management():
    """示例 3: 下载管理"""
    print_section("示例 3: 下载管理")
    
    agent = Agent(name="MovieAgent")
    tools = create_movie_tools()
    agent.register_tools(tools)
    
    # 下载电影
    print("用户: 下载《星际穿越》，1080p画质")
    response = agent.chat("下载《星际穿越》，1080p画质")
    print(f"助手: {response}\n")
    
    # 查看下载任务
    print("用户: 查看所有下载任务")
    response = agent.chat("查看所有下载任务")
    print(f"助手: {response}\n")
    
    # 检查下载进度
    print("用户: 检查下载进度")
    response = agent.chat("检查下载进度")
    print(f"助手: {response}\n")


def example_4_message_handler():
    """示例 4: 使用消息处理器"""
    print_section("示例 4: 使用消息处理器（HTTP 风格）")
    
    # 创建消息处理器
    handler = MessageHandler()
    
    # 模拟用户请求
    requests = [
        {
            "user_id": "user123",
            "message": "搜索评分最高的5部电影",
            "session_id": "session001"
        },
        {
            "user_id": "user123",
            "message": "订阅第一部电影",
            "session_id": "session001"
        },
        {
            "user_id": "user123",
            "message": "查看我的订阅",
            "session_id": "session001"
        }
    ]
    
    for req in requests:
        print(f"\n用户 ({req['user_id']}): {req['message']}")
        result = handler.handle_message(
            req['user_id'],
            req['message'],
            req.get('session_id')
        )
        
        if result['success']:
            print(f"助手: {result['response']}")
        else:
            print(f"错误: {result['error']}")
        
        print(f"时间: {result['timestamp']}")


def example_5_complex_workflow():
    """示例 5: 复杂工作流"""
    print_section("示例 5: 复杂工作流 - 完整的电影管理流程")
    
    agent = Agent(
        name="MovieAgent",
        system_prompt="""你是一个专业的电影资源管理助手。
        当用户询问时，你应该：
        1. 理解用户意图
        2. 调用合适的工具
        3. 用友好的方式呈现结果
        4. 如有需要，主动提供相关建议
        """
    )
    
    tools = create_movie_tools()
    agent.register_tools(tools)
    
    # 完整流程
    conversations = [
        "你好，我想找一些好看的电影",
        "帮我搜索克里斯托弗·诺兰导演的电影",
        "盗梦空间这部电影怎么样？给我详细信息",
        "不错！帮我订阅这部电影吧",
        "再帮我下载《星际穿越》，要高清的",
        "查看一下所有的下载任务状态"
    ]
    
    for i, message in enumerate(conversations, 1):
        print(f"\n[对话 {i}]")
        print(f"用户: {message}")
        response = agent.chat(message)
        print(f"助手: {response}")
        print("-" * 60)


def example_6_direct_tool_calls():
    """示例 6: 直接工具调用（演示工具功能）"""
    print_section("示例 6: 直接工具调用")
    
    from movie_tools import (
        search_movies,
        get_movie_details,
        subscribe_movie,
        download_movie,
        list_subscriptions,
        list_downloads
    )
    
    # 搜索电影
    print("1. 搜索科幻类型的电影：")
    result = search_movies(genre="科幻")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 获取电影详情
    print("\n2. 获取电影详情（ID: 1）：")
    result = get_movie_details("1")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 订阅电影
    print("\n3. 订阅电影：")
    result = subscribe_movie("2", "1080p")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 下载电影
    print("\n4. 下载电影：")
    result = download_movie("3", "4K")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 查看订阅列表
    print("\n5. 查看订阅列表：")
    result = list_subscriptions()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 查看下载任务
    print("\n6. 查看下载任务：")
    result = list_downloads()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    """主函数"""
    print("\n")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 10 + "电影智能助手 - 使用示例" + " " * 24 + "║")
    print("╚" + "═" * 58 + "╝")
    
    examples = [
        ("1", "基础智能体使用", example_1_basic_agent),
        ("2", "搜索和订阅电影", example_2_search_and_subscribe),
        ("3", "下载管理", example_3_download_management),
        ("4", "消息处理器", example_4_message_handler),
        ("5", "复杂工作流", example_5_complex_workflow),
        ("6", "直接工具调用", example_6_direct_tool_calls),
        ("0", "运行所有示例", None),
    ]
    
    print("\n请选择要运行的示例：")
    for num, name, _ in examples:
        print(f"  [{num}] {name}")
    
    choice = input("\n请输入选项 (0-6): ").strip()
    
    if choice == "0":
        for num, name, func in examples:
            if func:
                func()
                input("\n按 Enter 键继续下一个示例...")
    else:
        for num, name, func in examples:
            if num == choice and func:
                func()
                break
        else:
            print("无效的选项！")
    
    print_section("示例运行完成")
    print("提示：运行 api_server.py 启动 Web 服务和演示界面")
    print("命令：python api_server.py\n")


if __name__ == "__main__":
    main()
