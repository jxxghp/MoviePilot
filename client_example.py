"""
客户端示例
展示如何在应用中集成电影智能助手
"""
import requests
import socketio
import json
from typing import Optional


class MovieAgentClient:
    """电影智能助手 REST API 客户端"""
    
    def __init__(self, base_url: str = "http://localhost:5000"):
        """
        初始化客户端
        
        Args:
            base_url: API 服务器地址
        """
        self.base_url = base_url.rstrip('/')
        self.session_id: Optional[str] = None
    
    def send_message(self, 
                    user_id: str, 
                    message: str, 
                    session_id: Optional[str] = None) -> dict:
        """
        发送消息给智能助手
        
        Args:
            user_id: 用户 ID
            message: 消息内容
            session_id: 会话 ID（可选）
        
        Returns:
            响应结果
        """
        url = f"{self.base_url}/api/message"
        payload = {
            "user_id": user_id,
            "message": message
        }
        
        if session_id:
            payload["session_id"] = session_id
        elif self.session_id:
            payload["session_id"] = self.session_id
        
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            result = response.json()
            
            # 保存会话 ID
            if 'session_id' in result:
                self.session_id = result['session_id']
            
            return result
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": f"请求失败: {str(e)}"
            }
    
    def get_session_history(self, session_id: Optional[str] = None) -> dict:
        """
        获取会话历史
        
        Args:
            session_id: 会话 ID
        
        Returns:
            会话历史
        """
        sid = session_id or self.session_id
        if not sid:
            return {"success": False, "error": "没有活动会话"}
        
        url = f"{self.base_url}/api/session/{sid}/history"
        try:
            response = requests.get(url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": f"请求失败: {str(e)}"
            }
    
    def reset_session(self, session_id: Optional[str] = None) -> dict:
        """
        重置会话
        
        Args:
            session_id: 会话 ID
        
        Returns:
            操作结果
        """
        sid = session_id or self.session_id
        if not sid:
            return {"success": False, "error": "没有活动会话"}
        
        url = f"{self.base_url}/api/session/{sid}/reset"
        try:
            response = requests.post(url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": f"请求失败: {str(e)}"
            }
    
    def delete_session(self, session_id: Optional[str] = None) -> dict:
        """
        删除会话
        
        Args:
            session_id: 会话 ID
        
        Returns:
            操作结果
        """
        sid = session_id or self.session_id
        if not sid:
            return {"success": False, "error": "没有活动会话"}
        
        url = f"{self.base_url}/api/session/{sid}"
        try:
            response = requests.delete(url)
            response.raise_for_status()
            result = response.json()
            
            # 清除本地会话 ID
            if result.get('success'):
                self.session_id = None
            
            return result
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": f"请求失败: {str(e)}"
            }


class MovieAgentWebSocketClient:
    """电影智能助手 WebSocket 客户端"""
    
    def __init__(self, server_url: str = "http://localhost:5000"):
        """
        初始化 WebSocket 客户端
        
        Args:
            server_url: 服务器地址
        """
        self.server_url = server_url
        self.sio = socketio.Client()
        self.session_id: Optional[str] = None
        self.connected = False
        
        # 注册事件处理器
        self._register_handlers()
    
    def _register_handlers(self):
        """注册事件处理器"""
        
        @self.sio.on('connect')
        def on_connect():
            self.connected = True
            print("[WebSocket] 已连接到服务器")
        
        @self.sio.on('disconnect')
        def on_disconnect():
            self.connected = False
            print("[WebSocket] 已断开连接")
        
        @self.sio.on('connected')
        def on_connected(data):
            self.session_id = data.get('session_id')
            print(f"[WebSocket] 会话已创建: {self.session_id}")
        
        @self.sio.on('processing')
        def on_processing(data):
            print(f"[WebSocket] {data.get('message', '处理中...')}")
        
        @self.sio.on('response')
        def on_response(data):
            if data.get('success'):
                print(f"\n助手: {data.get('response')}\n")
            else:
                print(f"\n错误: {data.get('error')}\n")
        
        @self.sio.on('error')
        def on_error(data):
            print(f"[WebSocket] 错误: {data.get('error')}")
    
    def connect(self):
        """连接到服务器"""
        try:
            self.sio.connect(self.server_url)
            return True
        except Exception as e:
            print(f"连接失败: {str(e)}")
            return False
    
    def disconnect(self):
        """断开连接"""
        if self.connected:
            self.sio.disconnect()
    
    def send_message(self, user_id: str, message: str):
        """
        发送消息
        
        Args:
            user_id: 用户 ID
            message: 消息内容
        """
        if not self.connected:
            print("未连接到服务器")
            return
        
        self.sio.emit('message', {
            'user_id': user_id,
            'message': message,
            'session_id': self.session_id
        })
    
    def reset_session(self):
        """重置会话"""
        if not self.connected:
            print("未连接到服务器")
            return
        
        self.sio.emit('reset_session', {
            'session_id': self.session_id
        })


# ============= 使用示例 =============

def example_rest_client():
    """REST 客户端示例"""
    print("=" * 60)
    print("REST API 客户端示例")
    print("=" * 60)
    
    client = MovieAgentClient("http://localhost:5000")
    
    # 发送消息
    print("\n1. 搜索电影:")
    result = client.send_message(
        user_id="user123",
        message="帮我找一些评分高的科幻电影"
    )
    if result.get('success'):
        print(f"助手: {result['response']}")
    else:
        print(f"错误: {result.get('error')}")
    
    # 继续对话
    print("\n2. 订阅电影:")
    result = client.send_message(
        user_id="user123",
        message="订阅第一部电影"
    )
    if result.get('success'):
        print(f"助手: {result['response']}")
    
    # 查看会话历史
    print("\n3. 查看会话历史:")
    result = client.get_session_history()
    if result.get('success'):
        print(f"历史记录: {len(result['history'])} 条消息")
    
    # 重置会话
    print("\n4. 重置会话:")
    result = client.reset_session()
    print(f"结果: {result.get('message')}")


def example_websocket_client():
    """WebSocket 客户端示例"""
    print("=" * 60)
    print("WebSocket 客户端示例")
    print("=" * 60)
    
    client = MovieAgentWebSocketClient("http://localhost:5000")
    
    # 连接
    if not client.connect():
        print("无法连接到服务器，请确保服务器正在运行")
        return
    
    # 交互式对话
    print("\n提示: 输入 'quit' 退出\n")
    
    try:
        while True:
            message = input("你: ").strip()
            
            if not message:
                continue
            
            if message.lower() in ['quit', 'exit']:
                break
            
            client.send_message("user123", message)
            
            # 等待响应（WebSocket 会自动处理）
            import time
            time.sleep(0.5)
    
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        client.disconnect()


def example_integration():
    """集成示例 - 在应用中使用"""
    print("=" * 60)
    print("应用集成示例")
    print("=" * 60)
    
    # 创建客户端
    client = MovieAgentClient("http://localhost:5000")
    
    # 模拟应用逻辑
    def handle_user_request(user_id: str, user_message: str):
        """处理用户请求"""
        print(f"\n用户 {user_id}: {user_message}")
        
        result = client.send_message(user_id, user_message)
        
        if result.get('success'):
            response = result['response']
            print(f"助手: {response}")
            
            # 在应用中可以进一步处理响应
            # 例如：存储到数据库、发送通知等
            return response
        else:
            error = result.get('error', '未知错误')
            print(f"错误: {error}")
            return None
    
    # 模拟一系列用户请求
    handle_user_request("user_001", "搜索诺兰的电影")
    handle_user_request("user_001", "订阅《盗梦空间》")
    handle_user_request("user_001", "下载4K版本")
    handle_user_request("user_001", "查看下载进度")


def main():
    """主函数"""
    print("\n")
    print("╔" + "═" * 58 + "╗")
    print("║" + " " * 15 + "客户端使用示例" + " " * 27 + "║")
    print("╚" + "═" * 58 + "╝")
    
    examples = [
        ("1", "REST API 客户端", example_rest_client),
        ("2", "WebSocket 客户端", example_websocket_client),
        ("3", "应用集成示例", example_integration),
    ]
    
    print("\n请选择示例：")
    for num, name, _ in examples:
        print(f"  [{num}] {name}")
    
    choice = input("\n请输入选项 (1-3): ").strip()
    
    for num, name, func in examples:
        if num == choice:
            print()
            func()
            break
    else:
        print("无效的选项！")


if __name__ == "__main__":
    main()
