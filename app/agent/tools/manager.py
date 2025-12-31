"""MoviePilot工具管理器
用于HTTP API调用工具
"""

import json
import uuid
from typing import Any, Dict, List, Optional

from app.agent import ConversationMemoryManager
from app.agent.tools.factory import MoviePilotToolFactory
from app.log import logger


class ToolDefinition:
    """工具定义"""

    def __init__(self, name: str, description: str, input_schema: Dict[str, Any]):
        self.name = name
        self.description = description
        self.input_schema = input_schema


class MoviePilotToolsManager:
    """MoviePilot工具管理器（用于HTTP API）"""

    def __init__(self, user_id: str = "api_user", session_id: str = uuid.uuid4()):
        """
        初始化工具管理器
        
        Args:
            user_id: 用户ID
            session_id: 会话ID
        """
        self.user_id = user_id
        self.session_id = session_id
        self.tools: List[Any] = []
        self.memory_manager = ConversationMemoryManager()
        self._load_tools()

    def _load_tools(self):
        """加载所有MoviePilot工具"""
        try:
            # 创建工具实例
            self.tools = MoviePilotToolFactory.create_tools(
                session_id=self.session_id,
                user_id=self.user_id,
                channel=None,
                source="api",
                username="API Client",
                callback_handler=None,
                memory_mananger=None,
            )
            logger.info(f"成功加载 {len(self.tools)} 个工具")
        except Exception as e:
            logger.error(f"加载工具失败: {e}", exc_info=True)
            self.tools = []

    def list_tools(self) -> List[ToolDefinition]:
        """
        列出所有可用的工具
        
        Returns:
            工具定义列表
        """
        tools_list = []
        for tool in self.tools:
            # 获取工具的输入参数模型
            args_schema = getattr(tool, 'args_schema', None)
            if args_schema:
                # 将Pydantic模型转换为JSON Schema
                input_schema = self._convert_to_json_schema(args_schema)
            else:
                # 如果没有args_schema，使用基本信息
                input_schema = {
                    "type": "object",
                    "properties": {},
                    "required": []
                }

            tools_list.append(ToolDefinition(
                name=tool.name,
                description=tool.description or "",
                input_schema=input_schema
            ))

        return tools_list

    def get_tool(self, tool_name: str) -> Optional[Any]:
        """
        获取指定工具实例
        
        Args:
            tool_name: 工具名称
            
        Returns:
            工具实例，如果未找到返回None
        """
        for tool in self.tools:
            if tool.name == tool_name:
                return tool
        return None

    @staticmethod
    def _normalize_arguments(tool_instance: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据工具的参数schema规范化参数类型
        
        Args:
            tool_instance: 工具实例
            arguments: 原始参数
            
        Returns:
            规范化后的参数
        """
        # 获取工具的参数schema
        args_schema = getattr(tool_instance, 'args_schema', None)
        if not args_schema:
            return arguments
        
        # 获取schema中的字段定义
        try:
            schema = args_schema.model_json_schema()
            properties = schema.get("properties", {})
        except Exception as e:
            logger.warning(f"获取工具schema失败: {e}")
            return arguments
        
        # 规范化参数
        normalized = {}
        for key, value in arguments.items():
            if key not in properties:
                # 参数不在schema中，保持原样
                normalized[key] = value
                continue
            
            field_info = properties[key]
            field_type = field_info.get("type")
            
            # 处理 anyOf 类型（例如 Optional[int] 会生成 anyOf）
            any_of = field_info.get("anyOf")
            if any_of and not field_type:
                # 从 anyOf 中提取实际类型
                for type_option in any_of:
                    if "type" in type_option and type_option["type"] != "null":
                        field_type = type_option["type"]
                        break
            
            # 根据类型进行转换
            if field_type == "integer" and isinstance(value, str):
                try:
                    normalized[key] = int(value)
                except (ValueError, TypeError):
                    logger.warning(f"无法将参数 {key}='{value}' 转换为整数，保持原值")
                    normalized[key] = value
            elif field_type == "number" and isinstance(value, str):
                try:
                    normalized[key] = float(value)
                except (ValueError, TypeError):
                    logger.warning(f"无法将参数 {key}='{value}' 转换为浮点数，保持原值")
                    normalized[key] = value
            elif field_type == "boolean" and isinstance(value, str):
                # 转换字符串为布尔值
                normalized[key] = value.lower() in ("true", "1", "yes", "on")
            else:
                # 其他类型保持原样
                normalized[key] = value
        
        return normalized

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        调用工具
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
            
        Returns:
            工具执行结果（字符串）
        """
        tool_instance = self.get_tool(tool_name)

        if not tool_instance:
            error_msg = json.dumps({
                "error": f"工具 '{tool_name}' 未找到"
            }, ensure_ascii=False)
            return error_msg

        try:
            # 规范化参数类型
            normalized_arguments = self._normalize_arguments(tool_instance, arguments)
            
            # 调用工具的run方法
            result = await tool_instance.run(**normalized_arguments)

            # 确保返回字符串
            if isinstance(result, str):
                formated_result = result
            elif isinstance(result, int, float):
                formated_result = str(result)
            else:
                formated_result = json.dumps(result, ensure_ascii=False, indent=2)

            return formated_result
        except Exception as e:
            logger.error(f"调用工具 {tool_name} 时发生错误: {e}", exc_info=True)
            error_msg = json.dumps({
                "error": f"调用工具 '{tool_name}' 时发生错误: {str(e)}"
            }, ensure_ascii=False)
            return error_msg

    @staticmethod
    def _convert_to_json_schema(args_schema: Any) -> Dict[str, Any]:
        """
        将Pydantic模型转换为JSON Schema
        
        Args:
            args_schema: Pydantic模型类
            
        Returns:
            JSON Schema字典
        """
        # 获取Pydantic模型的字段信息
        schema = args_schema.model_json_schema()

        # 构建JSON Schema
        properties = {}
        required = []

        if "properties" in schema:
            for field_name, field_info in schema["properties"].items():
                # 转换字段类型
                field_type = field_info.get("type", "string")
                field_description = field_info.get("description", "")

                # 处理可选字段
                if field_name not in schema.get("required", []):
                    # 可选字段
                    default_value = field_info.get("default")
                    properties[field_name] = {
                        "type": field_type,
                        "description": field_description
                    }
                    if default_value is not None:
                        properties[field_name]["default"] = default_value
                else:
                    properties[field_name] = {
                        "type": field_type,
                        "description": field_description
                    }
                    required.append(field_name)

                # 处理枚举类型
                if "enum" in field_info:
                    properties[field_name]["enum"] = field_info["enum"]

                # 处理数组类型
                if field_type == "array" and "items" in field_info:
                    properties[field_name]["items"] = field_info["items"]

        return {
            "type": "object",
            "properties": properties,
            "required": required
        }
