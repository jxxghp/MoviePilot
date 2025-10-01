#!/usr/bin/env python
"""
快速启动脚本
"""
import sys
import os


def print_banner():
    """打印横幅"""
    print("\n")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 10 + "🎬 电影智能助手 - Agent 系统" + " " * 18 + "║")
    print("╚" + "═" * 58 + "╝")
    print()


def print_menu():
    """打印菜单"""
    print("请选择操作：")
    print()
    print("  [1] 🚀 启动 API 服务器（REST + WebSocket）")
    print("  [2] 🧪 运行测试")
    print("  [3] 💬 交互式测试")
    print("  [4] 📖 查看示例")
    print("  [5] 📊 查看配置")
    print("  [6] 📚 查看文档")
    print("  [0] 🚪 退出")
    print()


def start_server():
    """启动服务器"""
    print("\n正在启动 API 服务器...\n")
    import api_server
    # 服务器会在 api_server.py 的 main 部分启动


def run_tests():
    """运行测试"""
    print("\n正在运行测试...\n")
    from test_agent import run_all_tests
    run_all_tests()


def interactive_test():
    """交互式测试"""
    print("\n启动交互式测试...\n")
    from test_agent import interactive_test
    interactive_test()


def show_examples():
    """显示示例"""
    print("\n正在启动示例程序...\n")
    import example_usage
    example_usage.main()


def show_config():
    """显示配置"""
    print("\n")
    from config import Config
    Config.print_config()
    print()


def show_documentation():
    """显示文档"""
    print("\n" + "=" * 60)
    print("📚 快速文档")
    print("=" * 60)
    print("""
1. 项目结构:
   - agent_core.py          # Agent 智能体核心
   - movie_tools.py         # 电影管理工具集
   - message_handler.py     # 消息处理器
   - api_server.py         # API 服务器
   - example_usage.py      # 使用示例
   - test_agent.py         # 测试脚本
   - client_example.py     # 客户端示例

2. 快速开始:
   # 安装依赖
   pip install -r requirements.txt
   
   # 运行测试
   python test_agent.py
   
   # 启动服务器
   python api_server.py
   
   # 访问演示界面
   http://localhost:5000/demo

3. API 接口:
   REST API: http://localhost:5000/api/message
   WebSocket: ws://localhost:5000
   
4. 配置 LLM:
   复制 .env.example 为 .env
   配置 LLM_PROVIDER 和 LLM_API_KEY
   
5. 更多信息:
   查看 README.md 获取完整文档
    """)
    print("=" * 60)


def main():
    """主函数"""
    print_banner()
    
    while True:
        print_menu()
        
        try:
            choice = input("请输入选项 (0-6): ").strip()
            
            if choice == "0":
                print("\n再见！👋\n")
                break
            elif choice == "1":
                start_server()
                break
            elif choice == "2":
                run_tests()
                input("\n按 Enter 键继续...")
            elif choice == "3":
                interactive_test()
                input("\n按 Enter 键继续...")
            elif choice == "4":
                show_examples()
                input("\n按 Enter 键继续...")
            elif choice == "5":
                show_config()
                input("\n按 Enter 键继续...")
            elif choice == "6":
                show_documentation()
                input("\n按 Enter 键继续...")
            else:
                print("\n❌ 无效的选项，请重试\n")
        
        except KeyboardInterrupt:
            print("\n\n再见！👋\n")
            break
        except Exception as e:
            print(f"\n❌ 错误: {str(e)}\n")
            input("按 Enter 键继续...")


if __name__ == "__main__":
    main()
