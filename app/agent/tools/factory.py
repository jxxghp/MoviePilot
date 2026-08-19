import hashlib
import inspect

from typing import Any, Callable, List, Optional, Type

from app.agent.llm.capability import AgentCapabilityManager
from app.application.plugin.runtime import get_plugin_manager
from app.foundation.reflection import ModuleHelper
from app.runtime.log import logger
from app.schemas.notification import ChannelCapabilityManager, resolve_channel
from .base import MoviePilotTool
from .catalog import ToolCatalogError, ToolCatalogSnapshot

# 内置工具实现目录：每个一级模块提供一个具体 MoviePilotTool 子类，新增工具只需
# 在此目录下新建模块，无需改动本文件。
_BUILTIN_TOOL_IMPL_PACKAGE = "app.agent.tools.impl"

# 目录扫描默认排除的一级模块：mcp.py 是外部 MCP 工具的运行时适配器，
# 其工具实例按已配置的 MCP 服务器动态构造，不属于固定内置工具集。
_EXCLUDED_BUILTIN_IMPL_MODULE_NAMES = frozenset({"mcp"})

# 按渠道能力或系统配置条件性加入的内置工具的限定名（模块路径 + 类名）。
# 这些工具由 `_get_builtin_tool_classes` 按条件追加，不出现在固定工具集合中。
_ASK_USER_CHOICE_TOOL_QUALNAME = f"{_BUILTIN_TOOL_IMPL_PACKAGE}.ask_user_choice.AskUserChoiceTool"
_SEND_LOCAL_FILE_TOOL_QUALNAME = f"{_BUILTIN_TOOL_IMPL_PACKAGE}.send_local_file.SendLocalFileTool"
_SEND_VOICE_MESSAGE_TOOL_QUALNAME = f"{_BUILTIN_TOOL_IMPL_PACKAGE}.send_voice_message.SendVoiceMessageTool"
_CONDITIONAL_BUILTIN_TOOL_QUALNAMES = frozenset({
    _ASK_USER_CHOICE_TOOL_QUALNAME,
    _SEND_LOCAL_FILE_TOOL_QUALNAME,
    _SEND_VOICE_MESSAGE_TOOL_QUALNAME,
})


def _tool_class_qualname(tool_class: Type[MoviePilotTool]) -> str:
    """返回工具类不依赖对象地址的限定名（模块路径 + 类名）。"""
    return f"{tool_class.__module__}.{tool_class.__qualname__}"


def _is_builtin_tool_class(name: str, obj: Any) -> bool:
    """判断扫描到的类对象是否为内置工具实现目录直接定义的具体工具类。

    :param name: 类在所属模块命名空间中的属性名
    :param obj: 待判定的类对象
    :return: 是 MoviePilotTool 具体子类（非基类、非抽象类），且定义在内置工具
        实现目录的非排除模块中时返回 True
    """
    if not (isinstance(obj, type) and issubclass(obj, MoviePilotTool)):
        return False
    if obj is MoviePilotTool or inspect.isabstract(obj):
        return False
    module_name = str(obj.__module__)
    if not module_name.startswith(f"{_BUILTIN_TOOL_IMPL_PACKAGE}."):
        return False
    impl_module_name = module_name.rsplit(".", 1)[-1]
    return impl_module_name not in _EXCLUDED_BUILTIN_IMPL_MODULE_NAMES


def _discover_builtin_tool_classes() -> tuple[Type[MoviePilotTool], ...]:
    """扫描内置工具实现目录，返回按类名稳定排序的全部具体工具类。

    :return: 内置工具实现目录中发现的工具类元组，按 `__qualname__` 升序排列，
        避免顺序随文件系统遍历结果抖动
    """
    discovered = ModuleHelper.load(
        _BUILTIN_TOOL_IMPL_PACKAGE,
        filter_func=_is_builtin_tool_class,
    )
    return tuple(
        sorted(discovered, key=lambda tool_class: tool_class.__qualname__)
    )


def _split_fixed_and_conditional_tool_classes(
    tool_classes: tuple[Type[MoviePilotTool], ...],
) -> tuple[tuple[Type[MoviePilotTool], ...], dict[str, Type[MoviePilotTool]]]:
    """将扫描结果拆分为固定内置工具集合与按条件追加的工具集合。

    :param tool_classes: `_discover_builtin_tool_classes` 的扫描结果
    :return: (固定工具类元组, 条件工具限定名到工具类的映射)；二者共享同一次扫描
        得到的类对象，避免与后续独立 import 之间出现对象身份不一致
    """
    fixed_tool_classes = []
    conditional_tool_classes_by_qualname: dict[str, Type[MoviePilotTool]] = {}
    for tool_class in tool_classes:
        qualname = _tool_class_qualname(tool_class)
        if qualname in _CONDITIONAL_BUILTIN_TOOL_QUALNAMES:
            conditional_tool_classes_by_qualname[qualname] = tool_class
        else:
            fixed_tool_classes.append(tool_class)
    return tuple(fixed_tool_classes), conditional_tool_classes_by_qualname


_DISCOVERED_BUILTIN_TOOL_CLASSES = _discover_builtin_tool_classes()
_FIXED_BUILTIN_TOOL_CLASSES, _CONDITIONAL_BUILTIN_TOOL_CLASSES_BY_QUALNAME = (
    _split_fixed_and_conditional_tool_classes(_DISCOVERED_BUILTIN_TOOL_CLASSES)
)
AskUserChoiceTool = _CONDITIONAL_BUILTIN_TOOL_CLASSES_BY_QUALNAME[
    _ASK_USER_CHOICE_TOOL_QUALNAME
]
SendLocalFileTool = _CONDITIONAL_BUILTIN_TOOL_CLASSES_BY_QUALNAME[
    _SEND_LOCAL_FILE_TOOL_QUALNAME
]
SendVoiceMessageTool = _CONDITIONAL_BUILTIN_TOOL_CLASSES_BY_QUALNAME[
    _SEND_VOICE_MESSAGE_TOOL_QUALNAME
]


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

    BUILTIN_TOOL_CLASSES: tuple[Type[MoviePilotTool], ...] = _FIXED_BUILTIN_TOOL_CLASSES

    # 这些通用工具需要始终保留，避免大工具集裁剪后让 Agent 丢失基础的
    # 文件系统、命令执行、历史检索或交互确认能力。AskUserChoiceTool 仅在支持按钮
    # 的渠道中才会实际注入，因此后续会再按已加载工具做一次求交集。
    TOOL_SELECTOR_ALWAYS_INCLUDE_NAMES = (
        "list_directory",
        "write_file",
        "read_file",
        "edit_file",
        "apply_patch",
        "execute_command",
        "ask_user_choice",
        "create_agent_task",
        "query_agent_tasks",
    )

    CATALOG_BUILD_MAX_ATTEMPTS = 3

    @classmethod
    def catalog_factory_revision(cls) -> str:
        """返回当前内置工具工厂定义的稳定摘要。"""
        identities = (
            f"{tool_class.__module__}.{tool_class.__qualname__}"
            for tool_class in cls.BUILTIN_TOOL_CLASSES
        )
        return hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()

    @staticmethod
    def _should_enable_choice_tool(channel: Optional[str] = None) -> bool:
        message_channel = resolve_channel(channel)
        if not message_channel:
            return False
        return ChannelCapabilityManager.supports_buttons(
            message_channel
        ) and ChannelCapabilityManager.supports_callbacks(message_channel)

    @classmethod
    def get_tool_selector_always_include_names(
        cls, tools: List[MoviePilotTool]
    ) -> List[str]:
        """
        返回当前实际已加载且需要绕过工具筛选的工具名。

        `LLMToolSelectorMiddleware` 会校验 `always_include` 中的工具名是否
        存在于当前请求里，因此这里必须根据运行时工具列表做交集过滤。
        """
        available_tool_names = {
            tool.name for tool in tools if getattr(tool, "name", None)
        }
        return [
            tool_name
            for tool_name in cls.TOOL_SELECTOR_ALWAYS_INCLUDE_NAMES
            if tool_name in available_tool_names
        ]

    @classmethod
    def _get_builtin_tool_classes(
        cls, channel: Optional[str] = None
    ) -> list[Type[MoviePilotTool]]:
        """
        返回当前渠道可用的内置工具类清单。
        """
        tool_definitions = list(cls.BUILTIN_TOOL_CLASSES)
        if cls._should_enable_choice_tool(channel):
            tool_definitions.append(AskUserChoiceTool)
        tool_definitions.append(SendLocalFileTool)
        if AgentCapabilityManager.supports_audio_output():
            tool_definitions.append(SendVoiceMessageTool)
        return tool_definitions

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
    ) -> List[MoviePilotTool]:
        """
        创建MoviePilot工具列表
        """
        tools = []
        tool_definitions = cls._get_builtin_tool_classes(channel)
        # 创建内置工具
        for ToolClass in tool_definitions:
            tool = ToolClass(session_id=session_id, user_id=user_id)
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
                    tool.set_message_attr(
                        channel=channel, source=source, username=username
                    )
                    tool.set_stream_handler(stream_handler=stream_handler)
                    tool.set_agent_context(agent_context=agent_context)
                    object.__setattr__(
                        tool,
                        "_agent_tool_source",
                        f"plugin:{plugin_id or 'unknown'}",
                    )
                    tools.append(tool)
                    plugin_tools_count += 1
                    logger.debug(
                        f"成功加载插件 {plugin_name}({plugin_id}) 的工具: {ToolClass.__name__}"
                    )
                except Exception as e:
                    logger.error(
                        f"加载插件 {plugin_name}({plugin_id}) 的工具 {ToolClass.__name__} 失败: {str(e)}"
                    )

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
            )
        for _attempt in range(cls.CATALOG_BUILD_MAX_ATTEMPTS):
            before_revision = plugin_manager.get_plugin_agent_tools_revision()
            tools = cls.create_tools(**tool_kwargs)
            after_revision = plugin_manager.get_plugin_agent_tools_revision()
            if before_revision == after_revision:
                return ToolCatalogSnapshot.from_tools(
                    tools,
                    plugin_revision=after_revision,
                    factory_revision=cls.catalog_factory_revision(),
                )
        raise ToolCatalogError("插件工具目录持续变化，无法建立当前快照")
