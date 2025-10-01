"""
API 服务器
提供 REST API 和 WebSocket 接口
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
from message_handler import HTTPMessageHandler, WebSocketMessageHandler
import json
from typing import Dict, Any


# 创建 Flask 应用
app = Flask(__name__)
app.config['SECRET_KEY'] = 'movie-agent-secret-key'
CORS(app)

# 创建 SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# 创建消息处理器
http_handler = HTTPMessageHandler()
ws_handler = WebSocketMessageHandler()


# ============= REST API 路由 =============

@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({
        "status": "ok",
        "service": "Movie Agent API"
    })


@app.route('/api/message', methods=['POST'])
def handle_message():
    """
    处理消息请求
    
    请求格式：
    {
        "user_id": "用户ID",
        "message": "用户消息",
        "session_id": "会话ID（可选）"
    }
    """
    try:
        data = request.get_json()
        result = http_handler.handle_http_request(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/api/session/<session_id>/reset', methods=['POST'])
def reset_session(session_id: str):
    """重置会话"""
    result = http_handler.handle_session_reset(session_id)
    return jsonify(result)


@app.route('/api/session/<session_id>/history', methods=['GET'])
def get_session_history(session_id: str):
    """获取会话历史"""
    result = http_handler.handle_session_history(session_id)
    return jsonify(result)


@app.route('/api/session/<session_id>', methods=['DELETE'])
def delete_session(session_id: str):
    """删除会话"""
    success = http_handler.delete_session(session_id)
    return jsonify({
        "success": success,
        "message": "会话已删除" if success else "会话不存在"
    })


# ============= WebSocket 事件处理 =============

@socketio.on('connect')
def handle_connect():
    """客户端连接"""
    print(f"[WebSocket] 客户端已连接: {request.sid}")
    emit('connected', {
        "message": "已连接到电影智能助手",
        "session_id": request.sid
    })


@socketio.on('disconnect')
def handle_disconnect():
    """客户端断开连接"""
    print(f"[WebSocket] 客户端已断开: {request.sid}")


@socketio.on('message')
def handle_ws_message(data: Dict[str, Any]):
    """
    处理 WebSocket 消息
    
    消息格式：
    {
        "user_id": "用户ID",
        "message": "用户消息",
        "session_id": "会话ID（可选）"
    }
    """
    try:
        user_id = data.get('user_id', request.sid)
        message = data.get('message', '')
        session_id = data.get('session_id', request.sid)
        
        if not message:
            emit('error', {"error": "消息不能为空"})
            return
        
        # 发送处理中状态
        emit('processing', {"message": "正在处理您的请求..."})
        
        # 处理消息
        result = http_handler.handle_message(user_id, message, session_id)
        
        # 发送响应
        emit('response', result)
        
    except Exception as e:
        emit('error', {"error": str(e)})


@socketio.on('join')
def handle_join(data: Dict[str, Any]):
    """加入房间（会话）"""
    room = data.get('session_id', request.sid)
    join_room(room)
    emit('joined', {
        "session_id": room,
        "message": f"已加入会话 {room}"
    })


@socketio.on('leave')
def handle_leave(data: Dict[str, Any]):
    """离开房间（会话）"""
    room = data.get('session_id', request.sid)
    leave_room(room)
    emit('left', {
        "session_id": room,
        "message": f"已离开会话 {room}"
    })


@socketio.on('reset_session')
def handle_ws_reset_session(data: Dict[str, Any]):
    """重置会话"""
    session_id = data.get('session_id')
    if session_id:
        success = http_handler.reset_session(session_id)
        emit('session_reset', {
            "success": success,
            "session_id": session_id
        })


# ============= 演示和测试页面 =============

@app.route('/')
def index():
    """首页 - API 文档"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>电影智能助手 API</title>
        <meta charset="utf-8">
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                max-width: 1000px;
                margin: 50px auto;
                padding: 20px;
                background: #f5f5f5;
            }
            .container {
                background: white;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            h1 {
                color: #333;
                border-bottom: 3px solid #4CAF50;
                padding-bottom: 10px;
            }
            h2 {
                color: #555;
                margin-top: 30px;
            }
            .endpoint {
                background: #f9f9f9;
                padding: 15px;
                margin: 10px 0;
                border-left: 4px solid #4CAF50;
                border-radius: 4px;
            }
            .method {
                display: inline-block;
                padding: 5px 10px;
                border-radius: 3px;
                font-weight: bold;
                margin-right: 10px;
            }
            .get { background: #61affe; color: white; }
            .post { background: #49cc90; color: white; }
            .delete { background: #f93e3e; color: white; }
            code {
                background: #272822;
                color: #f8f8f2;
                padding: 15px;
                display: block;
                border-radius: 5px;
                overflow-x: auto;
                margin: 10px 0;
            }
            .feature {
                background: #e3f2fd;
                padding: 10px;
                margin: 5px 0;
                border-radius: 4px;
            }
            a {
                color: #4CAF50;
                text-decoration: none;
            }
            a:hover {
                text-decoration: underline;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎬 电影智能助手 API</h1>
            <p>基于 Agent 智能体的电影资源搜索、订阅和下载系统</p>
            
            <h2>✨ 核心功能</h2>
            <div class="feature">🔍 智能电影搜索 - 支持关键词、年份、类型、评分等多维度搜索</div>
            <div class="feature">📺 电影订阅管理 - 自动监控电影更新和资源</div>
            <div class="feature">⬇️ 下载任务管理 - 创建、监控、取消下载任务</div>
            <div class="feature">💬 自然语言交互 - 使用自然语言与智能助手对话</div>
            <div class="feature">🔧 工具化封装 - 所有功能封装为 Agent 工具</div>
            
            <h2>🌐 REST API 接口</h2>
            
            <div class="endpoint">
                <span class="method get">GET</span>
                <strong>/health</strong>
                <p>健康检查接口</p>
            </div>
            
            <div class="endpoint">
                <span class="method post">POST</span>
                <strong>/api/message</strong>
                <p>发送消息给智能助手</p>
                <code>{
    "user_id": "user123",
    "message": "帮我搜索科幻电影",
    "session_id": "session123"  // 可选
}</code>
            </div>
            
            <div class="endpoint">
                <span class="method post">POST</span>
                <strong>/api/session/{session_id}/reset</strong>
                <p>重置会话</p>
            </div>
            
            <div class="endpoint">
                <span class="method get">GET</span>
                <strong>/api/session/{session_id}/history</strong>
                <p>获取会话历史</p>
            </div>
            
            <div class="endpoint">
                <span class="method delete">DELETE</span>
                <strong>/api/session/{session_id}</strong>
                <p>删除会话</p>
            </div>
            
            <h2>🔌 WebSocket 接口</h2>
            <p>连接地址: <code>ws://localhost:5000</code></p>
            
            <h3>事件</h3>
            <ul>
                <li><strong>connect</strong> - 连接到服务器</li>
                <li><strong>message</strong> - 发送消息</li>
                <li><strong>join</strong> - 加入会话</li>
                <li><strong>leave</strong> - 离开会话</li>
                <li><strong>reset_session</strong> - 重置会话</li>
                <li><strong>disconnect</strong> - 断开连接</li>
            </ul>
            
            <h2>💡 使用示例</h2>
            
            <h3>搜索电影</h3>
            <code>curl -X POST http://localhost:5000/api/message \\
  -H "Content-Type: application/json" \\
  -d '{
    "user_id": "user123",
    "message": "帮我找一些评分9分以上的科幻电影"
  }'</code>
            
            <h3>订阅电影</h3>
            <code>curl -X POST http://localhost:5000/api/message \\
  -H "Content-Type: application/json" \\
  -d '{
    "user_id": "user123",
    "message": "我想订阅盗梦空间"
  }'</code>
            
            <h3>下载电影</h3>
            <code>curl -X POST http://localhost:5000/api/message \\
  -H "Content-Type: application/json" \\
  -d '{
    "user_id": "user123",
    "message": "下载肖申克的救赎，4K画质"
  }'</code>
            
            <h2>📚 更多信息</h2>
            <p><a href="/demo">📱 Web 演示界面</a></p>
            <p><a href="https://github.com" target="_blank">📖 GitHub 仓库</a></p>
        </div>
    </body>
    </html>
    """


@app.route('/demo')
def demo():
    """演示页面"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>电影智能助手 - 演示</title>
        <meta charset="utf-8">
        <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                margin: 0;
                padding: 0;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
            }
            .chat-container {
                width: 600px;
                height: 700px;
                background: white;
                border-radius: 15px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.3);
                display: flex;
                flex-direction: column;
                overflow: hidden;
            }
            .chat-header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 20px;
                text-align: center;
            }
            .chat-header h2 {
                margin: 0;
                font-size: 24px;
            }
            .chat-header p {
                margin: 5px 0 0 0;
                font-size: 14px;
                opacity: 0.9;
            }
            .chat-messages {
                flex: 1;
                overflow-y: auto;
                padding: 20px;
                background: #f8f9fa;
            }
            .message {
                margin-bottom: 15px;
                display: flex;
                animation: fadeIn 0.3s;
            }
            @keyframes fadeIn {
                from { opacity: 0; transform: translateY(10px); }
                to { opacity: 1; transform: translateY(0); }
            }
            .message.user {
                justify-content: flex-end;
            }
            .message-content {
                max-width: 70%;
                padding: 12px 16px;
                border-radius: 18px;
                word-wrap: break-word;
            }
            .message.user .message-content {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }
            .message.assistant .message-content {
                background: white;
                border: 1px solid #e0e0e0;
                color: #333;
            }
            .message.system .message-content {
                background: #fff3cd;
                border: 1px solid #ffc107;
                color: #856404;
                font-size: 13px;
                max-width: 90%;
            }
            .chat-input-container {
                padding: 20px;
                background: white;
                border-top: 1px solid #e0e0e0;
            }
            .chat-input-form {
                display: flex;
                gap: 10px;
            }
            #messageInput {
                flex: 1;
                padding: 12px 16px;
                border: 2px solid #e0e0e0;
                border-radius: 25px;
                font-size: 14px;
                outline: none;
                transition: border-color 0.3s;
            }
            #messageInput:focus {
                border-color: #667eea;
            }
            #sendButton {
                padding: 12px 24px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 25px;
                font-size: 14px;
                font-weight: bold;
                cursor: pointer;
                transition: transform 0.2s, box-shadow 0.2s;
            }
            #sendButton:hover {
                transform: translateY(-2px);
                box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
            }
            #sendButton:active {
                transform: translateY(0);
            }
            #sendButton:disabled {
                background: #ccc;
                cursor: not-allowed;
                transform: none;
            }
            .status {
                padding: 8px 12px;
                margin: 10px 0;
                border-radius: 8px;
                font-size: 12px;
                text-align: center;
            }
            .status.connected {
                background: #d4edda;
                color: #155724;
            }
            .status.disconnected {
                background: #f8d7da;
                color: #721c24;
            }
            .quick-actions {
                display: flex;
                gap: 8px;
                margin-bottom: 10px;
                flex-wrap: wrap;
            }
            .quick-action-btn {
                padding: 6px 12px;
                background: #e3f2fd;
                border: 1px solid #2196F3;
                border-radius: 15px;
                font-size: 12px;
                cursor: pointer;
                transition: all 0.2s;
            }
            .quick-action-btn:hover {
                background: #2196F3;
                color: white;
            }
        </style>
    </head>
    <body>
        <div class="chat-container">
            <div class="chat-header">
                <h2>🎬 电影智能助手</h2>
                <p>搜索 • 订阅 • 下载</p>
                <div id="status" class="status disconnected">连接中...</div>
            </div>
            <div class="chat-messages" id="messages"></div>
            <div class="chat-input-container">
                <div class="quick-actions">
                    <button class="quick-action-btn" onclick="sendQuickMessage('搜索科幻电影')">搜索科幻电影</button>
                    <button class="quick-action-btn" onclick="sendQuickMessage('评分最高的电影')">评分最高</button>
                    <button class="quick-action-btn" onclick="sendQuickMessage('查看我的订阅')">我的订阅</button>
                    <button class="quick-action-btn" onclick="sendQuickMessage('查看下载任务')">下载任务</button>
                </div>
                <form class="chat-input-form" id="messageForm">
                    <input type="text" id="messageInput" placeholder="输入消息... 例如：帮我找一些诺兰的电影" autocomplete="off">
                    <button type="submit" id="sendButton">发送</button>
                </form>
            </div>
        </div>

        <script>
            const socket = io();
            const messagesDiv = document.getElementById('messages');
            const messageForm = document.getElementById('messageForm');
            const messageInput = document.getElementById('messageInput');
            const sendButton = document.getElementById('sendButton');
            const statusDiv = document.getElementById('status');

            // 连接事件
            socket.on('connect', () => {
                console.log('Connected to server');
                statusDiv.textContent = '✓ 已连接';
                statusDiv.className = 'status connected';
                addSystemMessage('已连接到服务器，开始对话吧！');
            });

            socket.on('disconnect', () => {
                console.log('Disconnected from server');
                statusDiv.textContent = '✗ 已断开';
                statusDiv.className = 'status disconnected';
                addSystemMessage('与服务器断开连接');
            });

            socket.on('connected', (data) => {
                console.log('Session ID:', data.session_id);
            });

            // 处理中状态
            socket.on('processing', (data) => {
                addSystemMessage(data.message);
            });

            // 接收响应
            socket.on('response', (data) => {
                if (data.success) {
                    addMessage('assistant', data.response);
                } else {
                    addMessage('assistant', '抱歉，处理出错: ' + data.error);
                }
            });

            // 错误处理
            socket.on('error', (data) => {
                addSystemMessage('错误: ' + data.error);
            });

            // 发送消息
            messageForm.addEventListener('submit', (e) => {
                e.preventDefault();
                const message = messageInput.value.trim();
                if (message) {
                    sendMessage(message);
                    messageInput.value = '';
                }
            });

            function sendMessage(message) {
                addMessage('user', message);
                socket.emit('message', {
                    user_id: 'demo_user',
                    message: message
                });
            }

            function sendQuickMessage(message) {
                sendMessage(message);
            }

            function addMessage(role, content) {
                const messageDiv = document.createElement('div');
                messageDiv.className = `message ${role}`;
                
                const contentDiv = document.createElement('div');
                contentDiv.className = 'message-content';
                contentDiv.textContent = content;
                
                messageDiv.appendChild(contentDiv);
                messagesDiv.appendChild(messageDiv);
                messagesDiv.scrollTop = messagesDiv.scrollHeight;
            }

            function addSystemMessage(content) {
                const messageDiv = document.createElement('div');
                messageDiv.className = 'message system';
                
                const contentDiv = document.createElement('div');
                contentDiv.className = 'message-content';
                contentDiv.textContent = content;
                
                messageDiv.appendChild(contentDiv);
                messagesDiv.appendChild(messageDiv);
                messagesDiv.scrollTop = messagesDiv.scrollHeight;
            }

            // 欢迎消息
            setTimeout(() => {
                addMessage('assistant', '你好！我是电影智能助手 🎬\\n\\n我可以帮你：\\n• 搜索电影资源\\n• 订阅感兴趣的电影\\n• 下载电影到本地\\n• 管理订阅和下载任务\\n\\n请告诉我你需要什么帮助？');
            }, 500);
        </script>
    </body>
    </html>
    """


if __name__ == '__main__':
    print("=" * 60)
    print("🎬 电影智能助手 API 服务器")
    print("=" * 60)
    print("📡 REST API: http://localhost:5000")
    print("🔌 WebSocket: ws://localhost:5000")
    print("📱 演示界面: http://localhost:5000/demo")
    print("=" * 60)
    
    # 启动服务器
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
