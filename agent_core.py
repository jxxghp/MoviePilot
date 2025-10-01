"""
智能体核心模块
实现基于工具调用的 Agent 系统
"""
import json
import re
from typing import List, Dict, Any, Callable, Optional
from dataclasses import dataclass, field
from enum import Enum


class MessageRole(Enum):
    """消息角色枚举"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class Message:
    """消息类"""
    role: MessageRole
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


@dataclass
class Tool:
    """工具定义类"""
    name: str
    description: str
    parameters: Dict[str, Any]
    function: Callable
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式供 LLM 使用"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }


@dataclass
class ToolCall:
    """工具调用类"""
    id: str
    name: str
    arguments: Dict[str, Any]


class Agent:
    """智能体核心类"""
    
    def __init__(self, 
                 name: str = "MovieAgent",
                 system_prompt: str = "",
                 llm_client: Any = None):
        """
        初始化智能体
        
        Args:
            name: 智能体名称
            system_prompt: 系统提示词
            llm_client: LLM 客户端 (支持 OpenAI API 格式)
        """
        self.name = name
        self.system_prompt = system_prompt
        self.llm_client = llm_client
        self.tools: Dict[str, Tool] = {}
        self.conversation_history: List[Message] = []
        
        if system_prompt:
            self.conversation_history.append(
                Message(role=MessageRole.SYSTEM, content=system_prompt)
            )
    
    def register_tool(self, tool: Tool) -> None:
        """注册工具"""
        self.tools[tool.name] = tool
        print(f"[Agent] 已注册工具: {tool.name}")
    
    def register_tools(self, tools: List[Tool]) -> None:
        """批量注册工具"""
        for tool in tools:
            self.register_tool(tool)
    
    def _format_messages_for_llm(self) -> List[Dict[str, Any]]:
        """格式化消息历史为 LLM API 格式"""
        messages = []
        for msg in self.conversation_history:
            msg_dict = {
                "role": msg.role.value,
                "content": msg.content
            }
            if msg.tool_calls:
                msg_dict["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id
            if msg.name:
                msg_dict["name"] = msg.name
            messages.append(msg_dict)
        return messages
    
    def _execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """执行工具调用"""
        if tool_name not in self.tools:
            return json.dumps({"error": f"工具 '{tool_name}' 不存在"})
        
        tool = self.tools[tool_name]
        try:
            print(f"[Agent] 执行工具: {tool_name}，参数: {arguments}")
            result = tool.function(**arguments)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            error_msg = f"工具执行错误: {str(e)}"
            print(f"[Agent] {error_msg}")
            return json.dumps({"error": error_msg})
    
    def _parse_tool_calls(self, response_content: str) -> List[ToolCall]:
        """
        从 LLM 响应中解析工具调用
        支持多种格式的工具调用解析
        """
        tool_calls = []
        
        # 尝试解析 JSON 格式的工具调用
        # 格式: ```json\n{"tool": "tool_name", "arguments": {...}}\n```
        json_pattern = r'```(?:json)?\s*\n?(\{[^`]+\})\s*\n?```'
        matches = re.finditer(json_pattern, response_content, re.DOTALL)
        
        for idx, match in enumerate(matches):
            try:
                data = json.loads(match.group(1))
                if "tool" in data and "arguments" in data:
                    tool_calls.append(ToolCall(
                        id=f"call_{idx}",
                        name=data["tool"],
                        arguments=data["arguments"]
                    ))
            except json.JSONDecodeError:
                continue
        
        # 如果没有找到工具调用，尝试解析函数调用格式
        # 格式: function_name(arg1=value1, arg2=value2)
        if not tool_calls:
            func_pattern = r'(\w+)\((.*?)\)'
            matches = re.finditer(func_pattern, response_content)
            
            for idx, match in enumerate(matches):
                func_name = match.group(1)
                if func_name in self.tools:
                    args_str = match.group(2)
                    try:
                        # 简单解析参数
                        arguments = {}
                        if args_str.strip():
                            for arg in args_str.split(','):
                                if '=' in arg:
                                    key, value = arg.split('=', 1)
                                    key = key.strip()
                                    value = value.strip().strip('"\'')
                                    arguments[key] = value
                        
                        tool_calls.append(ToolCall(
                            id=f"call_{idx}",
                            name=func_name,
                            arguments=arguments
                        ))
                    except Exception:
                        continue
        
        return tool_calls
    
    def chat(self, user_message: str, max_iterations: int = 5) -> str:
        """
        与智能体对话
        
        Args:
            user_message: 用户消息
            max_iterations: 最大迭代次数（工具调用轮次）
            
        Returns:
            智能体的最终回复
        """
        # 添加用户消息到历史
        self.conversation_history.append(
            Message(role=MessageRole.USER, content=user_message)
        )
        
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            
            # 如果有 LLM 客户端，使用它
            if self.llm_client:
                try:
                    response = self._call_llm()
                    assistant_message = response.get("content", "")
                    tool_calls = response.get("tool_calls", [])
                except Exception as e:
                    print(f"[Agent] LLM 调用失败: {str(e)}")
                    return f"抱歉，处理您的请求时出现错误: {str(e)}"
            else:
                # 使用简单的规则匹配作为后备
                assistant_message = self._simple_response(user_message)
                tool_calls = []
            
            # 添加助手消息到历史
            self.conversation_history.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=assistant_message,
                    tool_calls=tool_calls if tool_calls else None
                )
            )
            
            # 如果没有工具调用，返回响应
            if not tool_calls:
                # 尝试从文本中解析工具调用
                parsed_calls = self._parse_tool_calls(assistant_message)
                if not parsed_calls:
                    return assistant_message
                tool_calls = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments)
                        }
                    }
                    for call in parsed_calls
                ]
            
            # 执行工具调用
            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                try:
                    arguments = json.loads(tool_call["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}
                
                result = self._execute_tool(tool_name, arguments)
                
                # 添加工具调用结果到历史
                self.conversation_history.append(
                    Message(
                        role=MessageRole.TOOL,
                        content=result,
                        tool_call_id=tool_call["id"],
                        name=tool_name
                    )
                )
        
        return "已达到最大迭代次数，请重新开始对话。"
    
    def _call_llm(self) -> Dict[str, Any]:
        """调用 LLM API"""
        messages = self._format_messages_for_llm()
        tools = [tool.to_dict() for tool in self.tools.values()]
        
        # 调用 OpenAI 格式的 API
        response = self.llm_client.chat.completions.create(
            model="gpt-4",  # 或其他模型
            messages=messages,
            tools=tools if tools else None,
            tool_choice="auto" if tools else None
        )
        
        message = response.choices[0].message
        return {
            "content": message.content or "",
            "tool_calls": message.tool_calls if hasattr(message, "tool_calls") else []
        }
    
    def _simple_response(self, user_message: str) -> str:
        """简单的规则响应（无 LLM 时使用）"""
        user_message_lower = user_message.lower()
        
        # 搜索关键词
        if any(keyword in user_message_lower for keyword in ["搜索", "查找", "找", "search"]):
            # 提取电影名称
            for tool_name in self.tools:
                if "search" in tool_name:
                    return f"我将为您搜索电影。使用工具: {tool_name}"
        
        # 订阅关键词
        if any(keyword in user_message_lower for keyword in ["订阅", "subscribe", "关注"]):
            for tool_name in self.tools:
                if "subscribe" in tool_name:
                    return f"我将为您订阅电影。使用工具: {tool_name}"
        
        # 下载关键词
        if any(keyword in user_message_lower for keyword in ["下载", "download"]):
            for tool_name in self.tools:
                if "download" in tool_name:
                    return f"我将为您下载电影。使用工具: {tool_name}"
        
        # 列表关键词
        if any(keyword in user_message_lower for keyword in ["列表", "列出", "查看", "list", "show"]):
            return "我将为您列出相关信息。"
        
        return "您好！我是电影资源智能助手，可以帮您搜索、订阅和下载电影资源。请告诉我您需要什么帮助？"
    
    def reset_conversation(self) -> None:
        """重置对话历史"""
        self.conversation_history = []
        if self.system_prompt:
            self.conversation_history.append(
                Message(role=MessageRole.SYSTEM, content=self.system_prompt)
            )
        print(f"[Agent] 对话历史已重置")
    
    def get_conversation_history(self) -> List[Message]:
        """获取对话历史"""
        return self.conversation_history
