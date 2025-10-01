# 电影智能体 (Movie Agent)

基于 CodePilot 项目的 Agent 架构设计，实现的智能电影资源搜索、订阅和下载系统。

## 功能特性

- 🤖 **智能对话**: 基于 LLM 的自然语言交互
- 🔧 **工具系统**: 模块化的工具封装，易于扩展
- 🎬 **电影管理**: 搜索、订阅、下载电影资源
- 💬 **上下文感知**: 支持多轮对话，记住上下文
- 📡 **流式响应**: 支持流式输出，实时反馈

## 架构设计

参考 CodePilot 的 Agent 实现，本项目采用以下架构：

```
agent/
├── __init__.py           # 模块入口
├── base.py               # 基础类定义（工具基类、Schema等）
├── agent.py              # 智能体核心实现
├── tools.py              # 工具集实现
├── config.py             # 配置管理
├── message_handler.py    # 消息处理器（替换原有message交互）
└── examples/             # 使用示例
    ├── basic_usage.py        # 基础使用示例
    └── integration_example.py # 集成示例
```

### 核心组件

1. **BaseTool**: 工具基类，定义工具的标准接口
2. **ToolRegistry**: 工具注册表，管理所有可用工具
3. **MovieAgent**: 智能体核心，负责与 LLM 交互和工具调用
4. **AgentMessageHandler**: 消息处理器，封装智能体交互逻辑

## 可用工具

### 1. search_movie - 搜索电影
搜索电影资源，支持按名称、年份、类型等条件搜索。

**参数**:
- `keyword` (必需): 搜索关键词
- `year` (可选): 电影年份
- `genre` (可选): 电影类型
- `page` (可选): 页码

### 2. subscribe_movie - 订阅电影
订阅电影，当有新资源时自动通知。

**参数**:
- `movie_id` (必需): 电影 ID
- `movie_name` (必需): 电影名称
- `quality` (可选): 期望的视频质量（4K/1080P/720P/任意）
- `notify` (可选): 是否开启通知

### 3. download_movie - 下载电影
添加电影到下载队列并开始下载。

**参数**:
- `resource_id` (必需): 资源 ID
- `movie_name` (必需): 电影名称
- `save_path` (可选): 保存路径
- `priority` (可选): 下载优先级（高/中/低）

### 4. list_subscriptions - 查看订阅列表
查看当前的电影订阅列表。

**参数**:
- `status` (可选): 订阅状态筛选
- `page` (可选): 页码

### 5. cancel_subscription - 取消订阅
取消电影订阅。

**参数**:
- `subscription_id` (必需): 订阅 ID

### 6. check_download_status - 查看下载状态
查看下载任务的状态和进度。

**参数**:
- `task_id` (可选): 下载任务 ID

### 7. get_movie_info - 获取电影详情
获取电影的详细信息。

**参数**:
- `movie_id` (必需): 电影 ID

## 安装

```bash
# 安装依赖
pip install openai pydantic
```

## 配置

创建 `.env` 文件：

```env
OPENAI_API_KEY=your_api_key_here
OPENAI_BASE_URL=https://api.openai.com/v1  # 可选，使用自定义 API 端点
```

## 使用方法

### 基础使用

```python
import asyncio
from agent import MovieAgent
from agent.config import AgentConfig

async def main():
    # 创建智能体
    agent = MovieAgent(
        api_key="your-api-key",
        base_url="https://api.openai.com/v1",  # 可选
        model="gpt-4-turbo-preview"
    )
    
    # 对话
    response = await agent.chat("帮我搜索2023年的科幻电影")
    print(response)
    
    # 流式对话
    async for chunk in agent.chat_stream("订阅《沙丘2》"):
        print(chunk, end="", flush=True)

asyncio.run(main())
```

### 集成到现有系统

```python
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig

# 初始化
config = AgentConfig(
    openai_api_key="your-api-key",
    openai_model="gpt-4-turbo-preview"
)
handler = AgentMessageHandler(config)

# 处理用户消息
async def handle_user_message(user_id: str, message: str):
    session_id = f"user_{user_id}"
    response = await handler.handle_message(
        message=message,
        session_id=session_id,
        user_id=user_id
    )
    return response

# 流式处理
async def handle_user_message_stream(user_id: str, message: str):
    session_id = f"user_{user_id}"
    async for chunk in handler.handle_message_stream(
        message=message,
        session_id=session_id,
        user_id=user_id
    ):
        yield chunk
```

### FastAPI 集成

```python
from fastapi import FastAPI
from pydantic import BaseModel
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig

app = FastAPI()
agent_handler = AgentMessageHandler(config)

class ChatRequest(BaseModel):
    message: str
    user_id: str

@app.post("/chat")
async def chat(request: ChatRequest):
    session_id = f"user_{request.user_id}"
    response = await agent_handler.handle_message(
        message=request.message,
        session_id=session_id,
        user_id=request.user_id
    )
    return {"response": response}
```

## 扩展工具

可以轻松添加自定义工具：

```python
from agent.base import BaseTool, ToolParameter
from typing import List, Dict, Any

class CustomTool(BaseTool):
    @property
    def name(self) -> str:
        return "custom_tool"
    
    @property
    def description(self) -> str:
        return "自定义工具描述"
    
    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="param1",
                type="string",
                description="参数1描述",
                required=True
            )
        ]
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        # 实现工具逻辑
        return {
            "success": True,
            "data": {}
        }

# 注册工具
agent = MovieAgent(...)
agent.add_tool(CustomTool())
```

## 对话示例

```
用户: 帮我找一部好看的科幻电影
智能体: 我来帮您搜索科幻电影。[调用 search_movie 工具]
        我为您找到了几部不错的科幻电影：
        1. 《沙丘2》(2024) - 评分9.2
        2. 《星际穿越》(2014) - 评分9.3
        3. 《银翼杀手2049》(2017) - 评分8.4
        您对哪一部感兴趣？

用户: 订阅《沙丘2》
智能体: [调用 subscribe_movie 工具]
        已成功订阅《沙丘2》！当有新的资源或更新时，我会及时通知您。

用户: 查看我的订阅
智能体: [调用 list_subscriptions 工具]
        您当前有以下订阅：
        1. 《沙丘2》- 活跃中
        2. 《星际穿越》- 已完成
```

## TODO

- [ ] 接入实际的电影数据库 API (TMDB/豆瓣)
- [ ] 实现真实的下载管理功能
- [ ] 添加用户认证和权限管理
- [ ] 实现订阅通知系统
- [ ] 添加更多工具（评分、评论等）
- [ ] 优化对话历史管理
- [ ] 添加缓存机制提高性能

## 参考

本项目参考了 [CodePilot](https://github.com/jxxghp/CodePilot) 的 Agent 架构设计。

## License

MIT
