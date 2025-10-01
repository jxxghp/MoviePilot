"""
电影智能体实现
"""
import json
import logging
from typing import Any, Dict, List, Optional, AsyncGenerator
from openai import AsyncOpenAI

from .base import BaseTool
from .tools import (
    ToolRegistry,
    SearchMovieTool,
    SubscribeMovieTool,
    DownloadMovieTool,
    ListSubscriptionsTool,
    CancelSubscriptionTool,
    CheckDownloadStatusTool,
    GetMovieInfoTool
)

logger = logging.getLogger(__name__)


class MovieAgent:
    """电影智能体"""
    
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "gpt-4-turbo-preview",
        system_prompt: Optional[str] = None
    ):
        """
        初始化电影智能体
        
        Args:
            api_key: OpenAI API Key
            base_url: API Base URL
            model: 模型名称
            system_prompt: 系统提示词
        """
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )
        self.model = model
        self.system_prompt = system_prompt or self._get_default_system_prompt()
        
        # 初始化工具注册表
        self.tool_registry = ToolRegistry()
        self._register_tools()
    
    def _get_default_system_prompt(self) -> str:
        """获取默认系统提示词"""
        return """你是一个专业的电影资源管理助手，能够帮助用户搜索、订阅和下载电影资源。

你的主要职责：
1. 理解用户的需求，判断用户想要搜索、订阅还是下载电影
2. 使用提供的工具来完成用户的请求
3. 以友好、专业的方式与用户交互
4. 如果用户的需求不明确，主动询问必要的信息

工具使用规则：
- 搜索电影前，确保了解用户想要搜索的关键词
- 订阅电影时，需要先搜索找到电影ID
- 下载电影时，需要资源ID和电影名称
- 查看状态时，返回清晰的信息给用户

请始终保持礼貌、高效，并提供准确的信息。"""
    
    def _register_tools(self):
        """注册所有工具"""
        tools = [
            SearchMovieTool(),
            SubscribeMovieTool(),
            DownloadMovieTool(),
            ListSubscriptionsTool(),
            CancelSubscriptionTool(),
            CheckDownloadStatusTool(),
            GetMovieInfoTool()
        ]
        
        for tool in tools:
            self.tool_registry.register(tool)
        
        logger.info(f"已注册 {len(tools)} 个工具")
    
    async def _execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具"""
        tool = self.tool_registry.get_tool(tool_name)
        if not tool:
            return {
                "success": False,
                "error": f"未找到工具: {tool_name}"
            }
        
        try:
            logger.info(f"执行工具: {tool_name}, 参数: {arguments}")
            result = await tool.execute(**arguments)
            logger.info(f"工具执行结果: {result}")
            return result
        except Exception as e:
            logger.error(f"工具执行失败: {tool_name}, 错误: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def chat(
        self,
        user_message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        stream: bool = False
    ) -> AsyncGenerator[str, None] if stream else str:
        """
        与智能体对话
        
        Args:
            user_message: 用户消息
            history: 历史对话记录
            stream: 是否流式返回
            
        Returns:
            智能体回复
        """
        # 构建消息列表
        messages = [
            {"role": "system", "content": self.system_prompt}
        ]
        
        if history:
            messages.extend(history)
        
        messages.append({"role": "user", "content": user_message})
        
        # 获取工具schemas
        tools = self.tool_registry.get_tool_schemas()
        
        # 调用LLM
        max_iterations = 5
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools if tools else None,
                    tool_choice="auto" if tools else None
                )
                
                assistant_message = response.choices[0].message
                
                # 如果没有工具调用，直接返回回复
                if not assistant_message.tool_calls:
                    content = assistant_message.content or ""
                    if stream:
                        yield content
                    else:
                        return content
                
                # 添加助手消息到历史
                messages.append({
                    "role": "assistant",
                    "content": assistant_message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in assistant_message.tool_calls
                    ]
                })
                
                # 执行工具调用
                for tool_call in assistant_message.tool_calls:
                    function_name = tool_call.function.name
                    try:
                        function_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        function_args = {}
                    
                    # 执行工具
                    tool_result = await self._execute_tool(function_name, function_args)
                    
                    # 添加工具结果到消息
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": json.dumps(tool_result, ensure_ascii=False)
                    })
                
                # 继续对话循环，让模型基于工具结果生成回复
                
            except Exception as e:
                logger.error(f"对话失败: {str(e)}")
                error_msg = f"抱歉，处理您的请求时出现错误: {str(e)}"
                if stream:
                    yield error_msg
                else:
                    return error_msg
        
        # 达到最大迭代次数
        final_msg = "抱歉，处理您的请求超时了，请稍后再试。"
        if stream:
            yield final_msg
        else:
            return final_msg
    
    async def chat_stream(
        self,
        user_message: str,
        history: Optional[List[Dict[str, Any]]] = None
    ) -> AsyncGenerator[str, None]:
        """
        流式对话
        
        Args:
            user_message: 用户消息
            history: 历史对话记录
            
        Yields:
            智能体回复片段
        """
        # 构建消息列表
        messages = [
            {"role": "system", "content": self.system_prompt}
        ]
        
        if history:
            messages.extend(history)
        
        messages.append({"role": "user", "content": user_message})
        
        # 获取工具schemas
        tools = self.tool_registry.get_tool_schemas()
        
        # 调用LLM
        max_iterations = 5
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            
            try:
                stream_response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools if tools else None,
                    tool_choice="auto" if tools else None,
                    stream=True
                )
                
                # 收集流式响应
                tool_calls_buffer = []
                content_buffer = []
                current_tool_call = None
                
                async for chunk in stream_response:
                    delta = chunk.choices[0].delta
                    
                    # 处理内容
                    if delta.content:
                        content_buffer.append(delta.content)
                        yield delta.content
                    
                    # 处理工具调用
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            if tc_delta.index >= len(tool_calls_buffer):
                                tool_calls_buffer.append({
                                    "id": tc_delta.id or "",
                                    "type": tc_delta.type or "function",
                                    "function": {
                                        "name": tc_delta.function.name or "",
                                        "arguments": ""
                                    }
                                })
                            
                            if tc_delta.function.arguments:
                                tool_calls_buffer[tc_delta.index]["function"]["arguments"] += tc_delta.function.arguments
                
                # 如果没有工具调用，结束
                if not tool_calls_buffer:
                    return
                
                # 添加助手消息到历史
                messages.append({
                    "role": "assistant",
                    "content": "".join(content_buffer) if content_buffer else None,
                    "tool_calls": tool_calls_buffer
                })
                
                # 执行工具调用
                for tool_call in tool_calls_buffer:
                    function_name = tool_call["function"]["name"]
                    try:
                        function_args = json.loads(tool_call["function"]["arguments"])
                    except json.JSONDecodeError:
                        function_args = {}
                    
                    # 执行工具
                    tool_result = await self._execute_tool(function_name, function_args)
                    
                    # 添加工具结果到消息
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": function_name,
                        "content": json.dumps(tool_result, ensure_ascii=False)
                    })
                
                # 继续对话循环
                
            except Exception as e:
                logger.error(f"流式对话失败: {str(e)}")
                yield f"\n\n抱歉，处理您的请求时出现错误: {str(e)}"
                return
        
        yield "\n\n抱歉，处理您的请求超时了，请稍后再试。"
    
    def add_tool(self, tool: BaseTool):
        """添加自定义工具"""
        self.tool_registry.register(tool)
        logger.info(f"已添加工具: {tool.name}")
