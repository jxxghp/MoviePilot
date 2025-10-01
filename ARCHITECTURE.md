# 🏗️ 架构设计文档

## 概述

本项目实现了一个基于 Agent 智能体的电影资源管理系统，将传统的消息交互功能升级为智能化的对话式交互，通过工具化封装实现了电影搜索、订阅和下载等核心功能。

## 架构层次

### 1. 核心层 (Core Layer)

#### Agent 智能体 (`agent_core.py`)

**职责:**
- 管理对话历史和上下文
- 协调工具调用
- 集成 LLM（可选）
- 解析用户意图

**核心类:**

```python
class Agent:
    - name: 智能体名称
    - system_prompt: 系统提示词
    - llm_client: LLM 客户端
    - tools: 已注册的工具字典
    - conversation_history: 对话历史
    
    方法:
    - register_tool(): 注册单个工具
    - register_tools(): 批量注册工具
    - chat(): 处理用户消息，返回响应
    - reset_conversation(): 重置对话历史
```

**工作流程:**
```
用户消息 → Agent.chat()
    ↓
解析意图（LLM 或规则）
    ↓
选择合适的工具
    ↓
执行工具调用
    ↓
格式化返回结果
    ↓
返回用户
```

### 2. 工具层 (Tool Layer)

#### 电影管理工具 (`movie_tools.py`)

**设计原则:**
- 每个工具都是独立的函数
- 统一的输入输出格式（JSON）
- 清晰的参数定义和描述
- 完善的错误处理

**工具列表:**

| 工具名称 | 功能 | 参数 | 返回值 |
|---------|------|------|--------|
| `search_movies` | 搜索电影 | keyword, year, genre, min_rating | 电影列表 |
| `get_movie_details` | 获取详情 | movie_id | 电影详细信息 |
| `subscribe_movie` | 订阅电影 | movie_id, quality | 订阅结果 |
| `unsubscribe_movie` | 取消订阅 | movie_id | 取消结果 |
| `list_subscriptions` | 订阅列表 | - | 订阅列表 |
| `download_movie` | 下载电影 | movie_id, quality | 下载任务 |
| `list_downloads` | 下载列表 | status | 下载列表 |
| `check_download_status` | 下载进度 | download_id | 下载状态 |
| `cancel_download` | 取消下载 | download_id | 取消结果 |

**工具定义结构:**
```python
Tool(
    name="tool_name",
    description="工具描述",
    parameters={
        "type": "object",
        "properties": {
            "param_name": {
                "type": "string",
                "description": "参数描述"
            }
        },
        "required": ["param_name"]
    },
    function=tool_function
)
```

### 3. 消息处理层 (Message Handler Layer)

#### 消息处理器 (`message_handler.py`)

**职责:**
- 封装 Agent，提供统一的消息处理接口
- 管理多用户会话
- 支持不同的通信协议（HTTP、WebSocket）

**核心类:**

```python
class MessageHandler:
    - agent: 智能体实例
    - sessions: 会话字典
    
    方法:
    - handle_message(): 处理单条消息
    - reset_session(): 重置会话
    - delete_session(): 删除会话
    - get_session_history(): 获取会话历史

class HTTPMessageHandler(MessageHandler):
    - handle_http_request(): 处理 HTTP 请求

class WebSocketMessageHandler(MessageHandler):
    - handle_websocket_message(): 处理 WebSocket 消息
    - register_connection(): 注册连接
    - unregister_connection(): 注销连接
```

### 4. 接口层 (API Layer)

#### API 服务器 (`api_server.py`)

**功能:**
- 提供 REST API 接口
- 提供 WebSocket 实时通信
- 提供 Web 演示界面

**REST API 端点:**

```
GET  /health                            # 健康检查
POST /api/message                       # 发送消息
POST /api/session/{session_id}/reset   # 重置会话
GET  /api/session/{session_id}/history # 获取历史
DELETE /api/session/{session_id}       # 删除会话
GET  /                                 # API 文档
GET  /demo                             # 演示界面
```

**WebSocket 事件:**

```
connect              # 连接
disconnect           # 断开
message              # 发送消息
join                 # 加入会话
leave                # 离开会话
reset_session        # 重置会话
```

### 5. 数据层 (Data Layer)

#### 电影数据库 (`movie_tools.py`)

**当前实现:**
- 内存存储（模拟数据）
- 适合演示和测试

**扩展方向:**
```python
class MovieDatabase:
    def __init__(self):
        # 可以替换为真实数据库连接
        self.db = DatabaseConnection()
        
    def search_movies(self, **kwargs):
        # 实现真实的数据库查询
        return self.db.query(...)
```

**支持的数据库:**
- SQLite（轻量级）
- PostgreSQL（生产环境）
- MongoDB（文档存储）
- Redis（缓存）

## 数据流

### 用户消息处理流程

```
┌─────────────┐
│   用户输入   │
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│   API 接口      │
│ (REST/WebSocket)│
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│ MessageHandler  │
│ - 验证输入      │
│ - 路由到 Agent  │
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│     Agent       │
│ - 理解意图      │
│ - 选择工具      │
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│   Tool 调用     │
│ - 执行业务逻辑  │
│ - 访问数据层    │
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│  格式化响应     │
└──────┬──────────┘
       │
       ▼
┌─────────────────┐
│  返回给用户     │
└─────────────────┘
```

### 工具调用流程

```
Agent.chat(message)
    │
    ├─> 有 LLM？
    │   ├─ 是 ─> 调用 LLM API
    │   │         ↓
    │   │      解析 tool_calls
    │   │
    │   └─ 否 ─> 规则匹配
    │             ↓
    │          提取工具名和参数
    │
    ├─> 执行工具
    │   │
    │   ├─ 验证工具存在
    │   ├─ 验证参数
    │   ├─ 调用工具函数
    │   └─ 捕获异常
    │
    └─> 返回结果
        │
        ├─ 格式化为 JSON
        └─ 添加到对话历史
```

## 扩展点

### 1. 添加新工具

```python
def new_tool_function(param1: str, param2: int) -> dict:
    """工具函数实现"""
    try:
        # 业务逻辑
        result = do_something(param1, param2)
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}

# 创建工具定义
new_tool = Tool(
    name="new_tool",
    description="工具描述",
    parameters={
        "type": "object",
        "properties": {
            "param1": {"type": "string"},
            "param2": {"type": "integer"}
        }
    },
    function=new_tool_function
)

# 注册到 Agent
agent.register_tool(new_tool)
```

### 2. 集成 LLM

```python
# OpenAI
import openai
client = openai.OpenAI(api_key="...")
agent = Agent(llm_client=client)

# Azure OpenAI
client = openai.AzureOpenAI(...)
agent = Agent(llm_client=client)

# 其他 LLM
# 只需实现兼容 OpenAI 格式的接口即可
```

### 3. 自定义消息处理器

```python
class CustomMessageHandler(MessageHandler):
    def handle_message(self, user_id, message, session_id):
        # 自定义预处理
        message = self.preprocess(message)
        
        # 调用父类方法
        result = super().handle_message(user_id, message, session_id)
        
        # 自定义后处理
        result = self.postprocess(result)
        
        return result
```

### 4. 添加新的接口类型

```python
# gRPC
class GRPCMessageHandler(MessageHandler):
    def handle_grpc_request(self, request):
        # 实现 gRPC 处理逻辑
        pass

# GraphQL
class GraphQLMessageHandler(MessageHandler):
    def resolve_chat(self, info, message):
        # 实现 GraphQL resolver
        pass
```

## 性能优化

### 1. 缓存策略

```python
from functools import lru_cache

@lru_cache(maxsize=100)
def search_movies_cached(keyword, genre, min_rating):
    return search_movies(keyword, genre, min_rating)
```

### 2. 异步处理

```python
import asyncio

async def async_tool_call(tool, args):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, tool.function, **args)
    return result
```

### 3. 连接池

```python
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

engine = create_engine(
    database_url,
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20
)
```

## 安全考虑

### 1. 输入验证

```python
def validate_input(data: dict) -> bool:
    # 验证必需字段
    required = ['user_id', 'message']
    if not all(k in data for k in required):
        return False
    
    # 验证数据类型
    if not isinstance(data['message'], str):
        return False
    
    # 验证长度限制
    if len(data['message']) > 1000:
        return False
    
    return True
```

### 2. 认证授权

```python
from functools import wraps
from flask import request, jsonify

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token or not verify_token(token):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/api/message', methods=['POST'])
@require_auth
def handle_message():
    # 处理逻辑
    pass
```

### 3. 速率限制

```python
from flask_limiter import Limiter

limiter = Limiter(
    app,
    key_func=lambda: request.headers.get('X-User-ID'),
    default_limits=["100 per hour"]
)

@app.route('/api/message')
@limiter.limit("10 per minute")
def handle_message():
    pass
```

## 监控和日志

### 1. 日志记录

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('agent.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

def chat(self, message):
    logger.info(f"Received message: {message}")
    try:
        response = self.process(message)
        logger.info(f"Response: {response}")
        return response
    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        raise
```

### 2. 性能监控

```python
import time
from functools import wraps

def timing_decorator(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = f(*args, **kwargs)
        duration = time.time() - start
        logger.info(f"{f.__name__} took {duration:.2f}s")
        return result
    return wrapper
```

### 3. 指标收集

```python
from prometheus_client import Counter, Histogram

message_counter = Counter('messages_total', 'Total messages')
response_time = Histogram('response_time_seconds', 'Response time')

@response_time.time()
def handle_message(message):
    message_counter.inc()
    return process(message)
```

## 测试策略

### 1. 单元测试

```python
import unittest

class TestAgent(unittest.TestCase):
    def setUp(self):
        self.agent = Agent()
        
    def test_tool_registration(self):
        tool = Tool(name="test", ...)
        self.agent.register_tool(tool)
        self.assertIn("test", self.agent.tools)
```

### 2. 集成测试

```python
def test_end_to_end():
    client = MovieAgentClient()
    result = client.send_message("user1", "搜索电影")
    assert result['success'] == True
```

### 3. 负载测试

```python
from locust import HttpUser, task

class MovieAgentUser(HttpUser):
    @task
    def send_message(self):
        self.client.post("/api/message", json={
            "user_id": "test",
            "message": "搜索电影"
        })
```

## 部署建议

### 1. Docker 部署

```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "api_server.py"]
```

### 2. Kubernetes 部署

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: movie-agent
spec:
  replicas: 3
  selector:
    matchLabels:
      app: movie-agent
  template:
    metadata:
      labels:
        app: movie-agent
    spec:
      containers:
      - name: movie-agent
        image: movie-agent:latest
        ports:
        - containerPort: 5000
```

### 3. 负载均衡

```nginx
upstream movie_agent {
    server backend1:5000;
    server backend2:5000;
    server backend3:5000;
}

server {
    listen 80;
    location / {
        proxy_pass http://movie_agent;
    }
}
```

## 总结

本架构设计实现了：

✅ **模块化设计** - 清晰的层次结构，易于维护和扩展
✅ **工具化封装** - 所有功能都封装为可复用的工具
✅ **灵活集成** - 支持多种 LLM 和数据源
✅ **多协议支持** - REST API 和 WebSocket
✅ **可扩展性** - 易于添加新工具和功能
✅ **安全性** - 提供认证、授权、验证机制
✅ **可观测性** - 日志、监控、指标收集
✅ **生产就绪** - 完善的错误处理和性能优化
