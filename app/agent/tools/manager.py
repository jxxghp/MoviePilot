import json
import uuid
from typing import Any, Dict, List, Optional

from app.agent.tools.base import format_tool_result_for_agent
from app.agent.tools.factory import MoviePilotToolFactory
from app.log import logger


class ToolDefinition:
    """
    工具定义
    """

    def __init__(self, name: str, description: str, input_schema: Dict[str, Any]):
        self.name = name
        self.description = description
        self.input_schema = input_schema


class MoviePilotToolsManager:
    """
    MoviePilot工具管理器（用于HTTP API）
    """

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
        self._load_tools()

    def _load_tools(self):
        """
        加载所有MoviePilot工具
        """
        try:
            # 创建工具实例
            self.tools = MoviePilotToolFactory.create_tools(
                session_id=self.session_id,
                user_id=self.user_id,
                channel=None,
                source="api",
                username="API Client",
                stream_handler=None,
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
            args_schema = getattr(tool, "args_schema", None)
            if args_schema:
                # 将Pydantic模型转换为JSON Schema
                input_schema = self._convert_to_json_schema(args_schema)
            else:
                # 如果没有args_schema，使用基本信息
                input_schema = {"type": "object", "properties": {}, "required": []}

            tools_list.append(
                ToolDefinition(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=input_schema,
                )
            )

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
    def _resolve_field_schema(field_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析字段schema，兼容 Optional[T] 生成的 anyOf 结构
        """
        if field_info.get("type"):
            return field_info

        any_of = field_info.get("anyOf")
        if not any_of:
            return field_info

        for type_option in any_of:
            if type_option.get("type") and type_option["type"] != "null":
                merged = dict(type_option)
                if "description" not in merged and field_info.get("description"):
                    merged["description"] = field_info["description"]
                if "default" not in merged and "default" in field_info:
                    merged["default"] = field_info["default"]
                return merged

        return field_info

    @staticmethod
    def _normalize_scalar_value(field_type: Optional[str], value: Any, key: str) -> Any:
        """
        根据字段类型规范化单个值
        """
        if field_type == "integer" and isinstance(value, str):
            try:
                return int(value)
            except (ValueError, TypeError):
                logger.warning(f"无法将参数 {key}='{value}' 转换为整数，返回 None")
                return None
        if field_type == "number" and isinstance(value, str):
            try:
                return float(value)
            except (ValueError, TypeError):
                logger.warning(f"无法将参数 {key}='{value}' 转换为浮点数，返回 None")
                return None
        if field_type == "boolean":
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes", "on")
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, bool):
                return value
            return True
        return value

    @staticmethod
    def _parse_array_string(value: str, key: str, item_type: str = "string") -> list:
        """
        将逗号分隔的字符串解析为列表，并根据 item_type 转换元素类型
        """
        trimmed = value.strip()
        if not trimmed:
            return []
        return [
            MoviePilotToolsManager._normalize_scalar_value(item_type, item.strip(), key)
            for item in trimmed.split(",")
            if item.strip()
        ]

    @staticmethod
    def _normalize_arguments(
        tool_instance: Any, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        根据工具的参数schema规范化参数类型

        Args:
            tool_instance: 工具实例
            arguments: 原始参数

        Returns:
            规范化后的参数
        """
        # 获取工具的参数schema
        args_schema = getattr(tool_instance, "args_schema", None)
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

            field_info = MoviePilotToolsManager._resolve_field_schema(properties[key])
            field_type = field_info.get("type")

            # 数组类型：将字符串解析为列表
            if field_type == "array" and isinstance(value, str):
                item_type = field_info.get("items", {}).get("type", "string")
                normalized[key] = MoviePilotToolsManager._parse_array_string(
                    value, key, item_type
                )
                continue

            # 根据类型进行转换
            normalized[key] = MoviePilotToolsManager._normalize_scalar_value(
                field_type, value, key
            )

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
            error_msg = json.dumps(
                {"error": f"工具 '{tool_name}' 未找到"}, ensure_ascii=False
            )
            return error_msg

        try:
            # 规范化参数类型
            normalized_arguments = self._normalize_arguments(tool_instance, arguments)

            # 调用工具的run方法。HTTP/MCP 工具调用不会经过 BaseTool._arun，
            # 因此这里也必须复用同一套返回值格式化和兜底截断逻辑。
            result = await tool_instance.run(**normalized_arguments)
            return format_tool_result_for_agent(
                result,
                tool_name=tool_name,
                max_chars=getattr(tool_instance, "result_max_chars", None),
            )
        except Exception as e:
            logger.error(f"调用工具 {tool_name} 时发生错误: {e}", exc_info=True)
            error_msg = json.dumps(
                {"error": f"调用工具 '{tool_name}' 时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
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
                resolved_field_info = MoviePilotToolsManager._resolve_field_schema(
                    field_info
                )
                # 转换字段类型
                field_type = resolved_field_info.get("type", "string")
                field_description = resolved_field_info.get("description", "")

                # 处理可选字段
                if field_name not in schema.get("required", []):
                    # 可选字段
                    default_value = resolved_field_info.get("default")
                    properties[field_name] = {
                        "type": field_type,
                        "description": field_description,
                    }
                    if default_value is not None:
                        properties[field_name]["default"] = default_value
                else:
                    properties[field_name] = {
                        "type": field_type,
                        "description": field_description,
                    }
                    required.append(field_name)

                # 处理枚举类型
                if "enum" in resolved_field_info:
                    properties[field_name]["enum"] = resolved_field_info["enum"]

                # 处理数组类型
                if field_type == "array" and "items" in resolved_field_info:
                    properties[field_name]["items"] = resolved_field_info["items"]

        return {"type": "object", "properties": properties, "required": required}


moviepilot_tool_manager = MoviePilotToolsManager()
