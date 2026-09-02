from __future__ import annotations

import asyncio
import json
import threading
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.runtime.log import logger

if TYPE_CHECKING:
    from app.agent.policy.contracts import ToolPolicyContext
    from app.agent.policy.orchestrator import AgentToolPolicyOrchestrator
    from app.agent.tools.catalog import ToolCatalogSnapshot
    from app.application.agent import AgentDataContext


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

    def __init__(
        self,
        user_id: str = "api_user",
        session_id: str = uuid.uuid4(),
        is_admin: bool = True,
        policy_orchestrator: Optional[AgentToolPolicyOrchestrator] = None,
        data: Optional[AgentDataContext] = None,
    ):
        """
        初始化工具管理器

        Args:
            user_id: 用户ID
            session_id: 会话ID
        """
        self.user_id = user_id
        self.session_id = session_id
        self.is_admin = is_admin
        self.policy_orchestrator = policy_orchestrator
        self._data = data
        self._policy_context: Optional[ToolPolicyContext] = None
        self.tools: List[Any] = []
        self.catalog: Optional[ToolCatalogSnapshot] = None
        self._tools_lock = threading.Lock()
        self._plugin_agent_tools_revision = -1
        self._catalog_materialized = False
        self._catalog_managed_by_factory = False

    def _invalidate_catalog_locked(self) -> None:
        """在持有工具锁时清除由旧数据上下文构造的目录快照。"""
        self.tools = []
        self.catalog = None
        self._plugin_agent_tools_revision = -1
        self._catalog_materialized = False
        self._catalog_managed_by_factory = False

    def set_data_context(self, data: AgentDataContext) -> None:
        """绑定组合根上下文，并使工厂拥有的旧工具快照原子失效。"""
        with self._tools_lock:
            if self._data is data:
                return
            if self._catalog_materialized and not self._catalog_managed_by_factory:
                raise RuntimeError("调用方工具目录已物化，不能替换数据上下文")
            self._data = data
            self._invalidate_catalog_locked()

    def reset_data_context(self) -> None:
        """撤销当前 lifespan 数据上下文，并清除引用旧仓储的工具快照。"""
        with self._tools_lock:
            self._data = None
            self._invalidate_catalog_locked()

    @staticmethod
    def _summarize_error(error: Exception) -> str:
        """仅在错误路径加载策略脱敏器，保持默认导入轻量。"""
        from app.agent.policy.sanitizer import summarize_error

        return summarize_error(error)

    def _load_tools_locked(self) -> None:
        """
        在 manager 锁内加载所有 MoviePilot 工具。

        工厂负责插件 revision 前后稳定窗口；manager 只发布完整快照，避免
        并发调用观察到一半刷新后的工具列表。
        """
        from app.agent.loader import get_tool_factory

        try:
            catalog = get_tool_factory().create_catalog(
                session_id=self.session_id,
                user_id=self.user_id,
                channel=None,
                source="api",
                username="API Client",
                stream_handler=None,
                agent_context={"is_admin": self.is_admin},
                include_external_service_tools=True,
                data=self._data,
            )
            self.catalog = catalog
            self.tools = catalog.tools
            self._plugin_agent_tools_revision = catalog.plugin_revision
            self._catalog_materialized = True
            self._catalog_managed_by_factory = True
            logger.info(f"成功加载 {len(self.tools)} 个工具")
        except Exception as e:
            logger.error(f"加载工具失败: {self._summarize_error(e)}")
            self.tools = []
            self.catalog = None
            self._plugin_agent_tools_revision = -1
            self._catalog_materialized = False
            self._catalog_managed_by_factory = False
            from app.agent.tools.catalog import ToolCatalogError

            if isinstance(e, ToolCatalogError):
                raise

    def _load_tools(self) -> None:
        """显式刷新工具目录，并保证外部调用原子发布完整快照。"""
        with self._tools_lock:
            self._load_tools_locked()

    def _ensure_tools_current(self) -> None:
        """
        首次使用时加载目录，并在插件注册表变化后惰性刷新工具实例。
        """
        # 调用方可能显式注入工具实例；这些实例仍由调用方拥有，manager 不应
        # 在第一次查询时用全量目录覆盖它们。
        if not self._catalog_materialized and self.tools:
            self._catalog_materialized = True
            return

        if self._catalog_materialized and not self._catalog_managed_by_factory:
            return

        if not self._catalog_materialized:
            with self._tools_lock:
                if not self._catalog_materialized:
                    self._load_tools_locked()
            return

        from app.application.plugin.runtime import get_plugin_manager

        plugin_manager = get_plugin_manager()
        if self._plugin_agent_tools_revision == plugin_manager.get_plugin_agent_tools_revision():
            return
        with self._tools_lock:
            if self._plugin_agent_tools_revision == plugin_manager.get_plugin_agent_tools_revision():
                return
            self._load_tools_locked()

    def _ensure_policy_runtime(
        self,
    ) -> tuple[AgentToolPolicyOrchestrator, ToolPolicyContext]:
        """返回 direct 入口的策略对象，仅在真实工具调用前完成构造。"""
        policy_orchestrator = self.policy_orchestrator
        policy_context = self._policy_context
        if policy_orchestrator is not None and policy_context is not None:
            return policy_orchestrator, policy_context

        from app.agent.policy.contracts import (
            AuthSource,
            PrincipalType,
            ToolOrigin,
            ToolPolicyContext,
        )
        from app.agent.policy.orchestrator import DEFAULT_TOOL_POLICY_ORCHESTRATOR

        if policy_orchestrator is None:
            policy_orchestrator = DEFAULT_TOOL_POLICY_ORCHESTRATOR
        if policy_context is None:
            policy_context = ToolPolicyContext(
                session_id=self.session_id,
                user_id=self.user_id,
                origin=ToolOrigin.OPERATOR_DIRECT,
                principal_type=PrincipalType.SYSTEM_ADMIN_INTEGRATION,
                auth_source=AuthSource.API_TOKEN,
                channel=None,
                source="api",
                agent_context={"is_admin": self.is_admin},
            )
        self.policy_orchestrator = policy_orchestrator
        self._policy_context = policy_context
        return policy_orchestrator, policy_context

    def list_tools(self) -> List[ToolDefinition]:
        """
        列出所有可用的工具

        Returns:
            工具定义列表
        """
        self._ensure_tools_current()
        with self._tools_lock:
            catalog = self._get_strict_catalog_locked()
            tools = catalog.tools if catalog is not None else []
        tools_list = []
        for tool in tools:
            if getattr(tool, "_require_admin", False) and not self.is_admin:
                continue
            # MCP-only 复杂工具可以保留 oneOf、嵌套对象和跨字段约束；普通工具
            # 继续沿用兼容投影，避免一次性改变已有外部合同。
            schema_factory = getattr(tool, "get_mcp_input_schema", None)
            if callable(schema_factory):
                input_schema = schema_factory()
            else:
                args_schema = getattr(tool, "args_schema", None)
                if args_schema:
                    input_schema = self._convert_to_json_schema(args_schema)
                else:
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
        """按唯一身份获取工具；同名冲突时稳定失败。"""
        return self.get_strict_tool(tool_name)

    def get_strict_tool(self, tool_name: str) -> Optional[Any]:
        """按当前目录唯一身份解析严格调用，重名时稳定失败。"""
        self._ensure_tools_current()
        with self._tools_lock:
            catalog = self._get_strict_catalog_locked()
            if catalog is None:
                return None
            entry = catalog.resolve_unique(tool_name)
            return entry.tool if entry else None

    def _get_strict_catalog_locked(self) -> Optional[ToolCatalogSnapshot]:
        """在 manager 锁内建立并校验唯一、可执行的当前工具目录。"""
        if self.catalog is None or [id(tool) for tool in self.catalog.tools] != [id(tool) for tool in self.tools]:
            from app.agent.loader import get_tool_factory
            from app.agent.tools.catalog import ToolCatalogSnapshot

            self.catalog = ToolCatalogSnapshot.from_tools(
                self.tools,
                plugin_revision=self._plugin_agent_tools_revision,
                factory_revision=get_tool_factory().catalog_factory_revision(),
            )
        return self.catalog.require_unique() if self.catalog is not None else None

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
        args_schema = getattr(tool_instance, "args_schema", None)
        if not args_schema:
            return arguments

        # 获取schema中的字段定义
        try:
            schema = args_schema.model_json_schema()
            properties = schema.get("properties", {})
        except Exception as e:
            logger.warning(f"获取工具schema失败: {MoviePilotToolsManager._summarize_error(e)}")
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
                normalized[key] = MoviePilotToolsManager._parse_array_string(value, key, item_type)
                continue

            # 根据类型进行转换
            normalized[key] = MoviePilotToolsManager._normalize_scalar_value(field_type, value, key)

        return normalized

    def _check_tool_permission(self, tool_instance: Any) -> Optional[str]:
        """为 HTTP/MCP/CLI 入口补齐 require_admin 门禁。"""

        if getattr(tool_instance, "_require_admin", False) and not self.is_admin:
            return "抱歉，您没有执行此工具的权限。只有系统管理员才能执行工具操作。"
        return None

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        调用工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            工具执行结果（字符串）
        """
        tool_instance = self.get_strict_tool(tool_name)

        if not tool_instance:
            error_msg = json.dumps({"error": f"工具 '{tool_name}' 未找到"}, ensure_ascii=False)
            return error_msg

        from app.agent.policy.orchestrator import call_policy_hook
        from app.agent.tools.base import (
            ToolExecutionTimeoutError,
            format_tool_result_for_agent,
        )

        observation = None
        policy_orchestrator = None
        try:
            permission_error = self._check_tool_permission(tool_instance)
            if permission_error:
                return json.dumps({"error": permission_error}, ensure_ascii=False)

            # 规范化参数类型
            normalized_arguments = self._normalize_arguments(tool_instance, arguments)
            policy_orchestrator, policy_context = self._ensure_policy_runtime()
            policy_context.agent_context["is_admin"] = self.is_admin
            observation = call_policy_hook(
                "start",
                policy_orchestrator.start,
                context=policy_context,
                tool=tool_instance,
                arguments=normalized_arguments,
            )

            # 调用工具的run方法。HTTP/MCP 工具调用不会经过 BaseTool._arun，
            # 因此这里也必须复用同一套返回值格式化和兜底截断逻辑。
            result = await tool_instance.run_with_timeout(**normalized_arguments)
            str_result = format_tool_result_for_agent(
                result,
                tool_name=tool_name,
                max_chars=getattr(tool_instance, "result_max_chars", None),
            )
        except asyncio.CancelledError as e:
            if observation is not None and policy_orchestrator is not None:
                call_policy_hook("cancel", policy_orchestrator.fail, observation, e)
            raise
        except ToolExecutionTimeoutError as e:
            if observation is not None and policy_orchestrator is not None:
                call_policy_hook("fail", policy_orchestrator.fail, observation, e)
            error_summary = self._summarize_error(e)
            logger.warning(error_summary)
            return format_tool_result_for_agent(
                error_summary,
                tool_name=tool_name,
                max_chars=getattr(tool_instance, "result_max_chars", None),
            )
        except Exception as e:
            if observation is not None and policy_orchestrator is not None:
                call_policy_hook("fail", policy_orchestrator.fail, observation, e)
            error_summary = self._summarize_error(e)
            logger.error(f"调用工具 {tool_name} 时发生错误: {error_summary}")
            error_msg = json.dumps(
                {"error": f"调用工具 '{tool_name}' 时发生错误: {error_summary}"},
                ensure_ascii=False,
            )
            return error_msg
        if observation is not None and policy_orchestrator is not None:
            call_policy_hook(
                "finish",
                policy_orchestrator.finish,
                observation,
                str_result,
            )
        return str_result

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
                resolved_field_info = MoviePilotToolsManager._resolve_field_schema(field_info)
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
