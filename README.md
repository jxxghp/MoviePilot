# 🎬 电影智能助手 - Agent 智能体系统

基于 Agent 智能体的电影资源搜索、订阅和下载系统。使用自然语言与智能助手交互，实现智能化的电影资源管理。

## ✨ 核心特性

- 🤖 **Agent 智能体架构** - 基于工具调用的智能体系统
- 🔧 **工具化封装** - 将所有功能封装为可调用的工具
- 💬 **自然语言交互** - 使用自然语言与智能助手对话
- 🔍 **智能电影搜索** - 多维度搜索（关键词、年份、类型、评分）
- 📺 **订阅管理** - 自动监控电影更新和资源
- ⬇️ **下载管理** - 创建、监控、取消下载任务
- 🌐 **多接口支持** - REST API 和 WebSocket 双接口
- 📱 **Web 演示界面** - 开箱即用的聊天界面

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                     用户交互层                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐     │
│  │ REST API │  │ WebSocket│  │  Web 演示界面     │     │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘     │
└───────┼─────────────┼─────────────────┼───────────────┘
        │             │                 │
        └─────────────┼─────────────────┘
                      │
        ┌─────────────▼─────────────┐
        │    消息处理层 (Handler)    │
        │  - HTTPMessageHandler      │
        │  - WebSocketMessageHandler │
        └─────────────┬───────────────┘
                      │
        ┌─────────────▼─────────────┐
        │     智能体核心 (Agent)     │
        │  - 消息历史管理            │
        │  - 工具调用协调            │
        │  - LLM 集成               │
        └─────────────┬───────────────┘
                      │
        ┌─────────────▼─────────────┐
        │     工具层 (Tools)        │
        │  - search_movies          │
        │  - subscribe_movie        │
        │  - download_movie         │
        │  - list_subscriptions     │
        │  - list_downloads         │
        │  - ...                    │
        └─────────────┬───────────────┘
                      │
        ┌─────────────▼─────────────┐
        │     数据层 (Database)     │
        │  - 电影数据               │
        │  - 订阅记录               │
        │  - 下载任务               │
        └───────────────────────────┘
```

## 📁 文件结构

```
.
├── agent_core.py           # 智能体核心模块
├── movie_tools.py          # 电影管理工具集
├── message_handler.py      # 消息处理模块
├── api_server.py          # API 服务器
├── example_usage.py       # 使用示例
├── requirements.txt       # 依赖列表
└── README.md             # 本文档
```

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 运行示例

```bash
# 运行交互式示例
python example_usage.py
```

### 3. 启动 API 服务器

```bash
python api_server.py
```

服务器启动后：
- REST API: http://localhost:5000
- WebSocket: ws://localhost:5000
- Web 演示界面: http://localhost:5000/demo

## 📖 使用指南

### Agent 智能体使用

```python
from agent_core import Agent
from movie_tools import create_movie_tools

# 创建智能体
agent = Agent(
    name="MovieAgent",
    system_prompt="你是一个专业的电影资源管理助手"
)

# 注册工具
tools = create_movie_tools()
agent.register_tools(tools)

# 开始对话
response = agent.chat("帮我搜索一些科幻电影")
print(response)
```

### 消息处理器使用

```python
from message_handler import MessageHandler

# 创建消息处理器
handler = MessageHandler()

# 处理消息
result = handler.handle_message(
    user_id="user123",
    message="搜索评分9分以上的电影",
    session_id="session001"
)

print(result['response'])
```

### REST API 使用

```bash
# 发送消息
curl -X POST http://localhost:5000/api/message \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "message": "帮我找一些诺兰的电影"
  }'

# 获取会话历史
curl http://localhost:5000/api/session/session123/history

# 重置会话
curl -X POST http://localhost:5000/api/session/session123/reset

# 删除会话
curl -X DELETE http://localhost:5000/api/session/session123
```

### WebSocket 使用

```javascript
const socket = io('http://localhost:5000');

// 连接
socket.on('connect', () => {
  console.log('Connected');
});

// 发送消息
socket.emit('message', {
  user_id: 'user123',
  message: '搜索科幻电影'
});

// 接收响应
socket.on('response', (data) => {
  console.log('Response:', data.response);
});
```

## 🔧 工具列表

### 1. search_movies
搜索电影资源

**参数：**
- `keyword` (string): 搜索关键词
- `year` (integer): 上映年份
- `genre` (string): 电影类型
- `min_rating` (number): 最低评分

**示例：**
```
"帮我搜索2010年后的科幻电影"
"找一些评分9分以上的电影"
```

### 2. get_movie_details
获取电影详细信息

**参数：**
- `movie_id` (string, required): 电影 ID

**示例：**
```
"告诉我盗梦空间的详细信息"
```

### 3. subscribe_movie
订阅电影

**参数：**
- `movie_id` (string, required): 电影 ID
- `quality` (string): 画质 (720p/1080p/4K)

**示例：**
```
"订阅《星际穿越》，要4K画质"
```

### 4. download_movie
下载电影

**参数：**
- `movie_id` (string, required): 电影 ID
- `quality` (string): 画质 (720p/1080p/4K)

**示例：**
```
"下载《肖申克的救赎》，1080p画质"
```

### 5. list_subscriptions
列出所有订阅

**示例：**
```
"查看我的订阅列表"
```

### 6. list_downloads
列出下载任务

**参数：**
- `status` (string): 状态筛选

**示例：**
```
"查看所有下载任务"
"查看正在下载的任务"
```

### 7. check_download_status
查看下载进度

**参数：**
- `download_id` (string, required): 下载任务 ID

**示例：**
```
"检查下载进度"
```

### 8. cancel_download
取消下载任务

**参数：**
- `download_id` (string, required): 下载任务 ID

**示例：**
```
"取消下载任务"
```

## 🎯 使用场景

### 场景 1: 搜索和浏览电影

```
用户: "帮我找一些评分高的科幻电影"
助手: [调用 search_movies 工具]
      "为您找到以下高评分科幻电影：
      1. 星际穿越 (9.3分)
      2. 盗梦空间 (9.3分)
      ..."
```

### 场景 2: 订阅感兴趣的电影

```
用户: "订阅《盗梦空间》"
助手: [调用 subscribe_movie 工具]
      "已成功订阅《盗梦空间》，系统将自动监控资源更新。"
```

### 场景 3: 下载电影资源

```
用户: "下载《星际穿越》，要4K的"
助手: [调用 download_movie 工具]
      "已创建下载任务：
      - 电影：星际穿越
      - 画质：4K
      - 大小：10.5GB
      - 状态：下载中 (5%)"
```

### 场景 4: 管理下载任务

```
用户: "查看下载进度"
助手: [调用 list_downloads 工具]
      "当前下载任务：
      1. 星际穿越 4K - 下载中 (35%)
      2. 肖申克的救赎 1080p - 已完成"
```

## 🔌 与现有系统集成

### 集成到 Web 应用

```python
from flask import Flask, request, jsonify
from message_handler import HTTPMessageHandler

app = Flask(__name__)
handler = HTTPMessageHandler()

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    result = handler.handle_http_request(data)
    return jsonify(result)
```

### 集成到微信/钉钉机器人

```python
from message_handler import MessageHandler

handler = MessageHandler()

def handle_wechat_message(user_id, message):
    result = handler.handle_message(user_id, message)
    return result['response']
```

### 集成 LLM (OpenAI/Claude)

```python
from agent_core import Agent
from movie_tools import create_movie_tools
import openai

# 配置 OpenAI 客户端
client = openai.OpenAI(api_key="your-api-key")

# 创建智能体（带 LLM）
agent = Agent(
    name="MovieAgent",
    system_prompt="你是一个专业的电影资源管理助手",
    llm_client=client
)

tools = create_movie_tools()
agent.register_tools(tools)

# 现在智能体可以使用 LLM 进行更智能的对话
response = agent.chat("帮我推荐一些适合周末看的电影")
```

## 🛠️ 自定义扩展

### 添加新工具

```python
from agent_core import Tool

def custom_function(param1: str, param2: int) -> dict:
    # 你的业务逻辑
    return {"result": "success"}

custom_tool = Tool(
    name="custom_tool",
    description="自定义工具的描述",
    parameters={
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "参数1"},
            "param2": {"type": "integer", "description": "参数2"}
        },
        "required": ["param1"]
    },
    function=custom_function
)

# 注册到智能体
agent.register_tool(custom_tool)
```

### 自定义数据源

修改 `movie_tools.py` 中的 `MovieDatabase` 类，连接到你的数据库：

```python
class MovieDatabase:
    def __init__(self):
        # 连接到真实数据库
        self.db = YourDatabaseConnection()
    
    def search_movies(self, **kwargs):
        # 实现真实的数据库查询
        return self.db.query(...)
```

## 📊 性能优化

- **缓存机制**: 对频繁查询的数据进行缓存
- **异步处理**: 使用异步工具调用提升响应速度
- **批量操作**: 支持批量订阅和下载
- **流式响应**: WebSocket 支持流式返回大数据

## 🔒 安全考虑

- **用户认证**: 集成身份验证机制
- **权限控制**: 不同用户不同操作权限
- **速率限制**: 防止 API 滥用
- **输入验证**: 严格验证用户输入

## 🧪 测试

```bash
# 运行示例测试
python example_usage.py

# 启动 API 服务器并访问演示界面
python api_server.py
# 访问 http://localhost:5000/demo
```

## 📝 更新日志

### v1.0.0 (2024)
- ✅ 实现 Agent 智能体核心
- ✅ 封装电影管理工具集
- ✅ 实现消息处理器
- ✅ 提供 REST API 和 WebSocket 接口
- ✅ 创建 Web 演示界面
- ✅ 支持 LLM 集成

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License

## 📮 联系方式

如有问题或建议，请提交 Issue 或联系开发团队。

---

**使用愉快！🎉**
