# Agent 集成指南

本指南详细说明如何将 Agent 智能体系统集成到现有项目中，替换原有的 message 交互功能。

## 目录

- [概述](#概述)
- [快速开始](#快速开始)
- [集成步骤](#集成步骤)
- [实际案例](#实际案例)
- [工具扩展](#工具扩展)
- [常见问题](#常见问题)

## 概述

Agent 系统采用模块化设计，参考 CodePilot 项目的架构，主要包含：

1. **Agent 核心** (`agent.py`): 负责与 LLM 交互和工具调度
2. **工具系统** (`tools.py`): 封装各种功能为工具
3. **消息处理器** (`message_handler.py`): 统一的消息处理接口
4. **基础组件** (`base.py`): 工具基类和数据模型

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，填入你的 API Key
```

### 3. 基础使用

```python
from agent import MovieAgent

async def main():
    agent = MovieAgent(
        api_key="your-api-key",
        model="gpt-4-turbo-preview"
    )
    
    response = await agent.chat("帮我搜索电影")
    print(response)
```

## 集成步骤

### 步骤 1: 分析现有系统

首先，识别现有系统中的消息处理逻辑：

```python
# 原有的消息处理（示例）
class MessageHandler:
    def handle_message(self, user_id: str, message: str):
        # 原有的业务逻辑
        if "搜索" in message:
            return self.search_movie(message)
        elif "订阅" in message:
            return self.subscribe_movie(message)
        # ... 更多 if-else
```

### 步骤 2: 将功能封装为工具

将现有功能封装为 Agent 工具：

```python
from agent.base import BaseTool, ToolParameter
from typing import List, Dict, Any

class YourSearchTool(BaseTool):
    def __init__(self, your_service):
        super().__init__()
        self.service = your_service  # 注入现有服务
    
    @property
    def name(self) -> str:
        return "search_movie"
    
    @property
    def description(self) -> str:
        return "搜索电影资源"
    
    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="keyword",
                type="string",
                description="搜索关键词",
                required=True
            )
        ]
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        keyword = kwargs.get("keyword")
        # 调用现有的搜索服务
        results = await self.service.search(keyword)
        return {
            "success": True,
            "data": results
        }
```

### 步骤 3: 初始化 Agent 系统

```python
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig
from agent import MovieAgent

class YourApplication:
    def __init__(self):
        # 初始化 Agent
        config = AgentConfig(
            openai_api_key="your-api-key",
            openai_model="gpt-4-turbo-preview"
        )
        self.agent_handler = AgentMessageHandler(config)
        
        # 注册自定义工具
        self._register_custom_tools()
    
    def _register_custom_tools(self):
        # 注册工具时注入现有服务
        search_tool = YourSearchTool(self.search_service)
        self.agent_handler.agent.add_tool(search_tool)
```

### 步骤 4: 替换消息处理逻辑

```python
# 新的消息处理
class NewMessageHandler:
    def __init__(self, agent_handler):
        self.agent_handler = agent_handler
    
    async def handle_message(self, user_id: str, message: str):
        session_id = f"user_{user_id}"
        response = await self.agent_handler.handle_message(
            message=message,
            session_id=session_id,
            user_id=user_id
        )
        return response
```

## 实际案例

### 案例 1: FastAPI 项目集成

```python
from fastapi import FastAPI, Depends
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig

app = FastAPI()

# 依赖注入
def get_agent_handler():
    config = AgentConfig(
        openai_api_key="your-api-key"
    )
    return AgentMessageHandler(config)

# 替换原有的消息路由
@app.post("/api/message")
async def handle_message(
    message: str,
    user_id: str,
    handler: AgentMessageHandler = Depends(get_agent_handler)
):
    session_id = f"user_{user_id}"
    response = await handler.handle_message(
        message=message,
        session_id=session_id,
        user_id=user_id
    )
    return {"response": response}
```

### 案例 2: Flask 项目集成

```python
from flask import Flask, request, jsonify
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig
import asyncio

app = Flask(__name__)

# 初始化 Agent
config = AgentConfig(openai_api_key="your-api-key")
agent_handler = AgentMessageHandler(config)

@app.route("/api/message", methods=["POST"])
def handle_message():
    data = request.get_json()
    message = data.get("message")
    user_id = data.get("user_id")
    
    session_id = f"user_{user_id}"
    
    # 在同步环境中运行异步代码
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    response = loop.run_until_complete(
        agent_handler.handle_message(message, session_id, user_id)
    )
    loop.close()
    
    return jsonify({"response": response})
```

### 案例 3: WebSocket 集成

```python
from fastapi import FastAPI, WebSocket
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig

app = FastAPI()
agent_handler = AgentMessageHandler(config)

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    session_id = f"user_{user_id}"
    
    try:
        while True:
            message = await websocket.receive_text()
            
            # 流式返回
            async for chunk in agent_handler.handle_message_stream(
                message, session_id, user_id
            ):
                await websocket.send_text(chunk)
    except:
        await websocket.close()
```

### 案例 4: Django 项目集成

```python
from django.http import JsonResponse
from django.views import View
from agent.message_handler import AgentMessageHandler
from agent.config import AgentConfig
import asyncio
import json

class MessageView(View):
    def __init__(self):
        super().__init__()
        config = AgentConfig(openai_api_key="your-api-key")
        self.agent_handler = AgentMessageHandler(config)
    
    def post(self, request):
        data = json.loads(request.body)
        message = data.get("message")
        user_id = data.get("user_id")
        
        session_id = f"user_{user_id}"
        
        # 使用 asyncio 运行异步函数
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        response = loop.run_until_complete(
            self.agent_handler.handle_message(message, session_id, user_id)
        )
        loop.close()
        
        return JsonResponse({"response": response})
```

## 工具扩展

### 扩展现有工具

如果需要接入实际的业务服务，可以继承并扩展现有工具：

```python
from agent.tools import SearchMovieTool

class RealSearchMovieTool(SearchMovieTool):
    def __init__(self, tmdb_api_key: str):
        super().__init__()
        self.tmdb_api_key = tmdb_api_key
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        keyword = kwargs.get("keyword")
        
        # 调用 TMDB API
        results = await self._search_tmdb(keyword)
        
        return {
            "success": True,
            "data": results,
            "message": f"找到 {len(results)} 部电影"
        }
    
    async def _search_tmdb(self, keyword: str):
        # 实际的 TMDB API 调用
        import aiohttp
        async with aiohttp.ClientSession() as session:
            url = f"https://api.themoviedb.org/3/search/movie"
            params = {
                "api_key": self.tmdb_api_key,
                "query": keyword,
                "language": "zh-CN"
            }
            async with session.get(url, params=params) as response:
                data = await response.json()
                return data.get("results", [])
```

### 创建新工具

```python
from agent.base import BaseTool, ToolParameter

class NotifyTool(BaseTool):
    """发送通知工具"""
    
    @property
    def name(self) -> str:
        return "send_notification"
    
    @property
    def description(self) -> str:
        return "发送通知给用户"
    
    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="message",
                type="string",
                description="通知内容",
                required=True
            ),
            ToolParameter(
                name="type",
                type="string",
                description="通知类型",
                required=False,
                enum=["info", "warning", "error"]
            )
        ]
    
    async def execute(self, **kwargs) -> Dict[str, Any]:
        message = kwargs.get("message")
        notify_type = kwargs.get("type", "info")
        
        # 调用通知服务
        await self.notification_service.send(message, notify_type)
        
        return {
            "success": True,
            "message": "通知已发送"
        }
```

## 与现有服务集成

### 方法 1: 依赖注入

```python
class MovieSearchService:
    """现有的搜索服务"""
    def search(self, keyword: str):
        # 现有逻辑
        pass

class SearchTool(BaseTool):
    def __init__(self, search_service: MovieSearchService):
        super().__init__()
        self.search_service = search_service
    
    async def execute(self, **kwargs):
        # 使用注入的服务
        results = self.search_service.search(kwargs["keyword"])
        return {"success": True, "data": results}

# 使用
search_service = MovieSearchService()
search_tool = SearchTool(search_service)
agent.add_tool(search_tool)
```

### 方法 2: 适配器模式

```python
class LegacyMovieSystem:
    """现有的电影系统"""
    def find_movies(self, title, year=None):
        # 旧的接口
        pass

class MovieSystemAdapter(BaseTool):
    """适配器，将旧接口适配为工具"""
    def __init__(self, legacy_system: LegacyMovieSystem):
        super().__init__()
        self.legacy = legacy_system
    
    @property
    def name(self) -> str:
        return "search_movie"
    
    async def execute(self, **kwargs):
        # 适配参数
        keyword = kwargs.get("keyword")
        year = kwargs.get("year")
        
        # 调用旧接口
        results = self.legacy.find_movies(keyword, year)
        
        # 适配返回值
        return {
            "success": True,
            "data": [{"title": r.name, "year": r.year} for r in results]
        }
```

## 常见问题

### Q1: 如何保持向后兼容？

可以同时保留原有接口和新接口：

```python
class MessageHandler:
    def __init__(self):
        self.agent_handler = AgentMessageHandler(config)
        self.legacy_handler = LegacyMessageHandler()
    
    async def handle_message(self, user_id: str, message: str, use_agent: bool = True):
        if use_agent:
            return await self.agent_handler.handle_message(message, f"user_{user_id}", user_id)
        else:
            return await self.legacy_handler.handle_message(message)
```

### Q2: 如何处理工具执行错误？

工具应该捕获异常并返回友好的错误信息：

```python
async def execute(self, **kwargs):
    try:
        result = await self.do_something(**kwargs)
        return {
            "success": True,
            "data": result
        }
    except Exception as e:
        logger.error(f"工具执行失败: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "操作失败，请稍后重试"
        }
```

### Q3: 如何优化性能？

1. **缓存**: 对常见查询结果进行缓存
2. **异步**: 使用异步I/O操作
3. **批处理**: 合并多个相似请求
4. **限流**: 控制请求频率

```python
from functools import lru_cache
import asyncio

class CachedSearchTool(BaseTool):
    def __init__(self):
        super().__init__()
        self._cache = {}
    
    async def execute(self, **kwargs):
        keyword = kwargs.get("keyword")
        
        # 检查缓存
        if keyword in self._cache:
            return self._cache[keyword]
        
        # 执行搜索
        result = await self._search(keyword)
        
        # 缓存结果
        self._cache[keyword] = result
        return result
```

### Q4: 如何测试 Agent 系统？

```python
import pytest
from agent import MovieAgent

@pytest.mark.asyncio
async def test_agent_search():
    agent = MovieAgent(api_key="test-key")
    
    # Mock 工具
    class MockSearchTool(BaseTool):
        async def execute(self, **kwargs):
            return {
                "success": True,
                "data": [{"title": "Test Movie"}]
            }
    
    agent.add_tool(MockSearchTool())
    
    response = await agent.chat("搜索电影")
    assert "Test Movie" in response
```

### Q5: 如何监控和日志？

```python
import logging
from agent.base import BaseTool

logger = logging.getLogger(__name__)

class MonitoredTool(BaseTool):
    async def execute(self, **kwargs):
        logger.info(f"工具执行开始: {self.name}, 参数: {kwargs}")
        
        start_time = time.time()
        try:
            result = await self._do_execute(**kwargs)
            duration = time.time() - start_time
            
            logger.info(f"工具执行成功: {self.name}, 耗时: {duration:.2f}s")
            return result
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"工具执行失败: {self.name}, 耗时: {duration:.2f}s, 错误: {e}")
            raise
```

## 迁移检查清单

- [ ] 分析现有消息处理逻辑
- [ ] 识别需要封装的功能
- [ ] 创建工具类
- [ ] 配置 Agent
- [ ] 实现消息处理接口
- [ ] 编写单元测试
- [ ] 进行集成测试
- [ ] 灰度发布
- [ ] 监控和优化

## 总结

通过本指南，您应该能够：

1. 理解 Agent 系统的架构
2. 将现有功能封装为工具
3. 替换原有的消息处理逻辑
4. 扩展和定制 Agent 系统
5. 解决集成过程中的常见问题

如有疑问，请参考示例代码或提交 Issue。
