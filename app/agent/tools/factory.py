import hashlib
from typing import Callable, List, Optional, Type

from app.agent.llm.capability import AgentCapabilityManager
from app.agent.tools.impl.agent_task import AgentTaskTool
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.apply_patch import ApplyPatchTool
from app.agent.tools.impl.ask_user_choice import AskUserChoiceTool
from app.agent.tools.impl.browse_webpage import BrowseWebpageTool
from app.agent.tools.impl.edit_file import EditFileTool
from app.agent.tools.impl.execute_command import ExecuteCommandTool
from app.agent.tools.impl.persona import PersonaTool
from app.agent.tools.impl.query_doctor_report import QueryDoctorReportTool
from app.agent.tools.impl.read_file import ReadFileTool
from app.agent.tools.impl.recognize_captcha import RecognizeCaptchaTool
from app.agent.tools.impl.search_web import SearchWebTool
from app.agent.tools.impl.send_local_file import SendLocalFileTool
from app.agent.tools.impl.send_message import SendMessageTool
from app.agent.tools.impl.send_voice_message import SendVoiceMessageTool
from app.agent.tools.impl.service import (
    DatabaseOperationTool,
    DownloaderOperationTool,
    MediaServerOperationTool,
)
from app.agent.tools.impl.write_file import WriteFileTool
from app.application.agent import AgentDataContext
from app.application.plugin.runtime import get_plugin_manager
from app.runtime.log import logger
from app.schemas.notification import ChannelCapabilityManager
from app.schemas.types import NotificationChannel

from .base import MoviePilotTool
from .catalog import ToolCatalogError, ToolCatalogSnapshot


def _get_plugin_agent_tools() -> list[dict]:
    """读取当前插件工具投影，隔离 Agent 工具工厂与 Runtime 管理器。"""
    try:
        return get_plugin_manager().get_plugin_agent_tools()
    except RuntimeError as error:
        # 纯工具目录探针可以在启动组合根之前运行；此时只跳过可选插件工具，
        # 不隐式创建 PluginManager，避免冷导入重新引入 Runtime 定位器。
        if "尚未由启动组合根装配" not in str(error):
            raise
        return []


class MoviePilotToolFactory:
    """
    MoviePilot工具工厂
    """

    BUILTIN_TOOL_CLASSES: tuple[Type[MoviePilotTool], ...] = (
        SearchWebTool,
        RecognizeCaptchaTool,
        SendMessageTool,
        AgentTaskTool,
        PersonaTool,
        ExecuteCommandTool,
        EditFileTool,
        ApplyPatchTool,
        WriteFileTool,
        ReadFileTool,
        BrowseWebpageTool,
        QueryDoctorReportTool,
    )
    # 下载器与媒体服务器原生操作通过 Skill 按需提供给内置 Agent；只有外部
    # HTTP/MCP 工具管理器需要常驻、自描述的结构化入口。
    EXTERNAL_SERVICE_TOOL_CLASSES: tuple[Type[MoviePilotTool], ...] = (
        DownloaderOperationTool,
        MediaServerOperationTool,
        DatabaseOperationTool,
    )

    # 这些通用工具需要始终保留，避免大工具集裁剪后让 Agent 丢失基础的
    # 文件系统、命令执行、历史检索或交互确认能力。AskUserChoiceTool 仅在支持按钮
    # 的渠道中才会实际注入，因此后续会再按已加载工具做一次求交集。
    TOOL_SELECTOR_ALWAYS_INCLUDE_NAMES = (
        "moviepilot_api",
        "write_file",
        "read_file",
        "edit_file",
        "apply_patch",
        "execute_command",
        "ask_user_choice",
        "agent_task",
    )

    CATALOG_BUILD_MAX_ATTEMPTS = 3

    @classmethod
    def catalog_factory_revision(cls) -> str:
        """返回当前内置工具工厂定义的稳定摘要。"""
        identities = (
            f"{tool_class.__module__}.{tool_class.__qualname__}"
            for tool_class in (*cls.BUILTIN_TOOL_CLASSES, *cls.EXTERNAL_SERVICE_TOOL_CLASSES)
        )
        return hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()

    @staticmethod
    def _should_enable_choice_tool(channel: Optional[str] = None) -> bool:
        if not channel:
            return False
        try:
            message_channel = NotificationChannel(channel)
        except ValueError:
            return False
        return ChannelCapabilityManager.supports_buttons(
            message_channel
        ) and ChannelCapabilityManager.supports_callbacks(message_channel)

    @classmethod
    def get_tool_selector_always_include_names(cls, tools: List[MoviePilotTool]) -> List[str]:
        """
        返回当前实际已加载且需要绕过工具筛选的工具名。

        `LLMToolSelectorMiddleware` 会校验 `always_include` 中的工具名是否
        存在于当前请求里，因此这里必须根据运行时工具列表做交集过滤。
        """
        available_tool_names = {tool.name for tool in tools if getattr(tool, "name", None)}
        return [tool_name for tool_name in cls.TOOL_SELECTOR_ALWAYS_INCLUDE_NAMES if tool_name in available_tool_names]

    @classmethod
    def _get_builtin_tool_classes(cls, channel: Optional[str] = None) -> list[Type[MoviePilotTool]]:
        """
        返回当前渠道可用的内置工具类清单。
        """
        tool_definitions = list(cls.BUILTIN_TOOL_CLASSES)
        if cls._should_enable_choice_tool(channel):
            tool_definitions.append(AskUserChoiceTool)
        tool_definitions.append(SendLocalFileTool)
        if AgentCapabilityManager.supports_audio_output():
            tool_definitions.append(SendVoiceMessageTool)
        tool_definitions.append(MoviePilotApiTool)
        return tool_definitions

    @staticmethod
    def _tool_class_name(tool_class: Type[MoviePilotTool]) -> str:
        """读取工具类的稳定名称，兼容 Pydantic 字段未直接暴露在类对象上的情况。"""
        name = getattr(tool_class, "name", None)
        if isinstance(name, str) and name:
            return name
        fields = getattr(tool_class, "model_fields", {})
        field = fields.get("name") if isinstance(fields, dict) else None
        default = getattr(field, "default", None)
        return default if isinstance(default, str) else ""

    @classmethod
    def create_tools(
        cls,
        session_id: str,
        user_id: str,
        channel: str = None,
        source: str = None,
        username: str = None,
        stream_handler: Callable = None,
        agent_context: dict = None,
        allow_message_tools: bool = True,
        include_external_service_tools: bool = False,
        data: Optional[AgentDataContext] = None,
    ) -> List[MoviePilotTool]:
        """
        创建统一 MoviePilot 工具列表。

        主 Agent、子 Agent、HTTP/MCP 管理器均使用同一新模式：业务能力由
        `moviepilot_api` 结构化网关承载，只有宿主交互与 Agent 编排能力保留为
        原生工具。
        """
        tools = []
        tool_definitions = cls._get_builtin_tool_classes(channel)
        if include_external_service_tools:
            tool_definitions.extend(cls.EXTERNAL_SERVICE_TOOL_CLASSES)
        # 创建内置工具
        for ToolClass in tool_definitions:
            tool = ToolClass(session_id=session_id, user_id=user_id, data=data)
            if not allow_message_tools and getattr(tool, "sends_message", False):
                continue
            tool.set_message_attr(channel=channel, source=source, username=username)
            tool.set_stream_handler(stream_handler=stream_handler)
            tool.set_agent_context(agent_context=agent_context)
            object.__setattr__(tool, "_agent_tool_source", "builtin")
            tools.append(tool)

        # 加载插件提供的工具
        plugin_tools_count = 0
        plugin_tools_info = _get_plugin_agent_tools()
        for plugin_info in plugin_tools_info:
            plugin_id = plugin_info.get("plugin_id")
            plugin_name = plugin_info.get("plugin_name")
            tool_classes = plugin_info.get("tools", [])
            for ToolClass in tool_classes:
                try:
                    # 验证工具类是否继承自 MoviePilotTool
                    if not issubclass(ToolClass, MoviePilotTool):
                        logger.warning(
                            f"插件 {plugin_name}({plugin_id}) 提供的工具类 {ToolClass.__name__} 未继承自 MoviePilotTool，已跳过"
                        )
                        continue
                    # 创建工具实例
                    tool = ToolClass(session_id=session_id, user_id=user_id)
                    if not allow_message_tools and getattr(tool, "sends_message", False):
                        continue
                    tool.set_message_attr(channel=channel, source=source, username=username)
                    tool.set_stream_handler(stream_handler=stream_handler)
                    tool.set_agent_context(agent_context=agent_context)
                    object.__setattr__(
                        tool,
                        "_agent_tool_source",
                        f"plugin:{plugin_id or 'unknown'}",
                    )
                    tools.append(tool)
                    plugin_tools_count += 1
                    logger.debug(f"成功加载插件 {plugin_name}({plugin_id}) 的工具: {ToolClass.__name__}")
                except Exception as e:
                    logger.error(f"加载插件 {plugin_name}({plugin_id}) 的工具 {ToolClass.__name__} 失败: {str(e)}")

        builtin_tools_count = len(tool_definitions)
        if plugin_tools_count > 0:
            logger.debug(
                f"成功创建 {len(tools)} 个MoviePilot工具（内置工具: {builtin_tools_count} 个，插件工具: {plugin_tools_count} 个）"
            )
        else:
            logger.debug(f"成功创建 {len(tools)} 个MoviePilot工具")
        return tools

    @classmethod
    def create_catalog(cls, **tool_kwargs) -> ToolCatalogSnapshot:
        """在插件目录稳定窗口内构造一份完整本地工具快照。"""
        try:
            plugin_manager = get_plugin_manager()
        except RuntimeError as error:
            # 没有启动上下文时仍允许构造内置工具目录；插件工具会在正式启动
            # 后由组合根提供的 Runtime 中重新物化。
            if "尚未由启动组合根装配" not in str(error):
                raise
            return ToolCatalogSnapshot.from_tools(
                cls.create_tools(**tool_kwargs),
                plugin_revision=0,
                factory_revision=cls.catalog_factory_revision(),
            ).require_unique()
        for _attempt in range(cls.CATALOG_BUILD_MAX_ATTEMPTS):
            before_revision = plugin_manager.get_plugin_agent_tools_revision()
            tools = cls.create_tools(**tool_kwargs)
            after_revision = plugin_manager.get_plugin_agent_tools_revision()
            if before_revision == after_revision:
                return ToolCatalogSnapshot.from_tools(
                    tools,
                    plugin_revision=after_revision,
                    factory_revision=cls.catalog_factory_revision(),
                ).require_unique()
        raise ToolCatalogError("插件工具目录持续变化，无法建立当前快照")
