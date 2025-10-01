"""
Agent 系统测试脚本
"""
import sys
from agent_core import Agent
from movie_tools import create_movie_tools
import json


def test_tool_registration():
    """测试工具注册"""
    print("=" * 60)
    print("测试 1: 工具注册")
    print("=" * 60)
    
    agent = Agent(name="TestAgent")
    tools = create_movie_tools()
    agent.register_tools(tools)
    
    print(f"✓ 成功注册 {len(agent.tools)} 个工具")
    for tool_name in agent.tools:
        print(f"  - {tool_name}")
    print()


def test_search_functionality():
    """测试搜索功能"""
    print("=" * 60)
    print("测试 2: 电影搜索功能")
    print("=" * 60)
    
    from movie_tools import search_movies
    
    # 测试搜索
    result = search_movies(keyword="盗梦空间")
    print("搜索 '盗梦空间':")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 测试按类型搜索
    result = search_movies(genre="科幻")
    print("\n搜索科幻电影:")
    print(f"找到 {result['count']} 部电影")
    
    # 测试按评分搜索
    result = search_movies(min_rating=9.5)
    print(f"\n搜索评分 >= 9.5 的电影:")
    print(f"找到 {result['count']} 部电影")
    print()


def test_subscription_workflow():
    """测试订阅流程"""
    print("=" * 60)
    print("测试 3: 订阅流程")
    print("=" * 60)
    
    from movie_tools import subscribe_movie, list_subscriptions, unsubscribe_movie
    
    # 订阅电影
    print("1. 订阅电影 ID=1:")
    result = subscribe_movie("1", "4K")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 查看订阅列表
    print("\n2. 查看订阅列表:")
    result = list_subscriptions()
    print(f"共有 {result['count']} 个订阅")
    
    # 重复订阅（应该失败）
    print("\n3. 尝试重复订阅:")
    result = subscribe_movie("1", "1080p")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # 取消订阅
    print("\n4. 取消订阅:")
    result = unsubscribe_movie("1")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print()


def test_download_workflow():
    """测试下载流程"""
    print("=" * 60)
    print("测试 4: 下载流程")
    print("=" * 60)
    
    from movie_tools import (
        download_movie,
        list_downloads,
        check_download_status,
        cancel_download
    )
    
    # 创建下载任务
    print("1. 创建下载任务:")
    result = download_movie("2", "1080p")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    download_id = result.get('download', {}).get('id')
    
    # 查看下载列表
    print("\n2. 查看下载列表:")
    result = list_downloads()
    print(f"共有 {result['count']} 个下载任务")
    
    # 检查下载状态
    if download_id:
        print(f"\n3. 检查下载状态 ({download_id}):")
        result = check_download_status(download_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        
        # 再次检查（进度应该更新）
        print(f"\n4. 再次检查下载状态:")
        result = check_download_status(download_id)
        print(f"进度: {result['download']['progress']}%")
    print()


def test_agent_conversation():
    """测试智能体对话"""
    print("=" * 60)
    print("测试 5: 智能体对话（无 LLM）")
    print("=" * 60)
    
    agent = Agent(
        name="MovieAgent",
        system_prompt="你是一个电影资源管理助手"
    )
    tools = create_movie_tools()
    agent.register_tools(tools)
    
    test_messages = [
        "你好",
        "搜索电影",
        "下载电影",
        "查看订阅",
    ]
    
    for msg in test_messages:
        print(f"\n用户: {msg}")
        response = agent.chat(msg)
        print(f"助手: {response}")
    print()


def test_message_handler():
    """测试消息处理器"""
    print("=" * 60)
    print("测试 6: 消息处理器")
    print("=" * 60)
    
    from message_handler import MessageHandler
    
    handler = MessageHandler()
    
    # 测试消息处理
    result = handler.handle_message(
        user_id="test_user",
        message="搜索科幻电影",
        session_id="test_session"
    )
    
    print("请求:")
    print(f"  User ID: {result['user_id']}")
    print(f"  Message: {result['message']}")
    print(f"  Session ID: {result['session_id']}")
    print(f"\n响应:")
    print(f"  Success: {result['success']}")
    print(f"  Response: {result['response']}")
    print(f"  Timestamp: {result['timestamp']}")
    print()


def run_all_tests():
    """运行所有测试"""
    print("\n")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 15 + "Agent 系统测试" + " " * 29 + "║")
    print("╚" + "═" * 58 + "╝")
    print()
    
    tests = [
        test_tool_registration,
        test_search_functionality,
        test_subscription_workflow,
        test_download_workflow,
        test_agent_conversation,
        test_message_handler,
    ]
    
    passed = 0
    failed = 0
    
    for test_func in tests:
        try:
            test_func()
            passed += 1
            print(f"✓ {test_func.__name__} 通过\n")
        except Exception as e:
            failed += 1
            print(f"✗ {test_func.__name__} 失败: {str(e)}\n")
    
    print("=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("=" * 60)
    
    return failed == 0


def interactive_test():
    """交互式测试"""
    print("\n")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 15 + "交互式测试模式" + " " * 27 + "║")
    print("╚" + "═" * 58 + "╝")
    print()
    print("提示: 输入 'quit' 或 'exit' 退出")
    print()
    
    agent = Agent(
        name="MovieAgent",
        system_prompt="你是一个专业的电影资源管理助手，可以帮助用户搜索、订阅和下载电影。"
    )
    tools = create_movie_tools()
    agent.register_tools(tools)
    
    print("助手: 你好！我是电影智能助手，我可以帮你搜索、订阅和下载电影。请告诉我你需要什么帮助？\n")
    
    while True:
        try:
            user_input = input("你: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ['quit', 'exit', '退出']:
                print("\n再见！👋")
                break
            
            response = agent.chat(user_input)
            print(f"\n助手: {response}\n")
            
        except KeyboardInterrupt:
            print("\n\n再见！👋")
            break
        except Exception as e:
            print(f"\n错误: {str(e)}\n")


def main():
    """主函数"""
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        if mode == 'interactive' or mode == 'i':
            interactive_test()
        elif mode == 'test' or mode == 't':
            success = run_all_tests()
            sys.exit(0 if success else 1)
        else:
            print("用法:")
            print("  python test_agent.py            # 运行所有测试")
            print("  python test_agent.py test       # 运行所有测试")
            print("  python test_agent.py interactive # 交互式测试")
    else:
        # 默认运行所有测试
        success = run_all_tests()
        
        # 询问是否进入交互模式
        print("\n是否进入交互式测试模式? (y/n): ", end='')
        choice = input().strip().lower()
        if choice == 'y':
            interactive_test()


if __name__ == "__main__":
    main()
