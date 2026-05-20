from typing import List, Callable

from app.agent.tools.impl.add_download import AddDownloadTool
from app.agent.tools.impl.add_subscribe import AddSubscribeTool
from app.agent.tools.impl.update_subscribe import UpdateSubscribeTool
from app.agent.tools.impl.search_subscribe import SearchSubscribeTool
from app.agent.tools.impl.get_recommendations import GetRecommendationsTool
from app.agent.tools.impl.query_downloaders import QueryDownloadersTool
from app.agent.tools.impl.query_download_tasks import QueryDownloadTasksTool
from app.agent.tools.impl.query_library_exists import QueryLibraryExistsTool
from app.agent.tools.impl.query_library_latest import QueryLibraryLatestTool
from app.agent.tools.impl.query_sites import QuerySitesTool
from app.agent.tools.impl.update_site import UpdateSiteTool
from app.agent.tools.impl.query_site_userdata import QuerySiteUserdataTool
from app.agent.tools.impl.test_site import TestSiteTool
from app.agent.tools.impl.query_subscribes import QuerySubscribesTool
from app.agent.tools.impl.query_subscribe_shares import QuerySubscribeSharesTool
from app.agent.tools.impl.query_rule_groups import QueryRuleGroupsTool
from app.agent.tools.impl.query_builtin_filter_rules import QueryBuiltinFilterRulesTool
from app.agent.tools.impl.query_custom_filter_rules import QueryCustomFilterRulesTool
from app.agent.tools.impl.add_custom_filter_rule import AddCustomFilterRuleTool
from app.agent.tools.impl.update_custom_filter_rule import UpdateCustomFilterRuleTool
from app.agent.tools.impl.delete_custom_filter_rule import DeleteCustomFilterRuleTool
from app.agent.tools.impl.add_rule_group import AddRuleGroupTool
from app.agent.tools.impl.update_rule_group import UpdateRuleGroupTool
from app.agent.tools.impl.delete_rule_group import DeleteRuleGroupTool
from app.agent.tools.impl.query_popular_subscribes import QueryPopularSubscribesTool
from app.agent.tools.impl.query_subscribe_history import QuerySubscribeHistoryTool
from app.agent.tools.impl.delete_subscribe import DeleteSubscribeTool
from app.agent.tools.impl.search_media import SearchMediaTool
from app.agent.tools.impl.search_person import SearchPersonTool
from app.agent.tools.impl.search_person_credits import SearchPersonCreditsTool
from app.agent.tools.impl.recognize_media import RecognizeMediaTool
from app.agent.tools.impl.scrape_metadata import ScrapeMetadataTool
from app.agent.tools.impl.query_episode_schedule import QueryEpisodeScheduleTool
from app.agent.tools.impl.query_media_detail import QueryMediaDetailTool
from app.agent.tools.impl.search_torrents import SearchTorrentsTool
from app.agent.tools.impl.get_search_results import GetSearchResultsTool
from app.agent.tools.impl.search_web import SearchWebTool
from app.agent.tools.impl.send_message import SendMessageTool
from app.agent.tools.impl.ask_user_choice import AskUserChoiceTool
from app.agent.tools.impl.send_local_file import SendLocalFileTool
from app.agent.tools.impl.send_voice_message import SendVoiceMessageTool
from app.agent.tools.impl.query_schedulers import QuerySchedulersTool
from app.agent.tools.impl.run_scheduler import RunSchedulerTool
from app.agent.tools.impl.query_workflows import QueryWorkflowsTool
from app.agent.tools.impl.run_workflow import RunWorkflowTool
from app.agent.tools.impl.query_personas import QueryPersonasTool
from app.agent.tools.impl.switch_persona import SwitchPersonaTool
from app.agent.tools.impl.update_persona_definition import UpdatePersonaDefinitionTool
from app.agent.tools.impl.update_site_cookie import UpdateSiteCookieTool
from app.agent.tools.impl.delete_download import DeleteDownloadTool
from app.agent.tools.impl.delete_download_history import DeleteDownloadHistoryTool
from app.agent.tools.impl.delete_transfer_history import DeleteTransferHistoryTool
from app.agent.tools.impl.modify_download import ModifyDownloadTool
from app.agent.tools.impl.query_directory_settings import QueryDirectorySettingsTool
from app.agent.tools.impl.list_directory import ListDirectoryTool
from app.agent.tools.impl.query_transfer_history import QueryTransferHistoryTool
from app.agent.tools.impl.transfer_file import TransferFileTool
from app.agent.tools.impl.execute_command import ExecuteCommandTool
from app.agent.tools.impl.edit_file import EditFileTool
from app.agent.tools.impl.write_file import WriteFileTool
from app.agent.tools.impl.read_file import ReadFileTool
from app.agent.tools.impl.browse_webpage import BrowseWebpageTool
from app.agent.tools.impl.query_installed_plugins import QueryInstalledPluginsTool
from app.agent.tools.impl.query_market_plugins import QueryMarketPluginsTool
from app.agent.tools.impl.query_plugin_capabilities import QueryPluginCapabilitiesTool
from app.agent.tools.impl.query_plugin_config import QueryPluginConfigTool
from app.agent.tools.impl.update_plugin_config import UpdatePluginConfigTool
from app.agent.tools.impl.reload_plugin import ReloadPluginTool
from app.agent.tools.impl.query_plugin_data import QueryPluginDataTool
from app.agent.tools.impl.install_plugin import InstallPluginTool
from app.agent.tools.impl.uninstall_plugin import UninstallPluginTool
from app.agent.tools.impl.run_slash_command import RunSlashCommandTool
from app.agent.tools.impl.list_slash_commands import ListSlashCommandsTool
from app.agent.tools.impl.query_custom_identifiers import QueryCustomIdentifiersTool
from app.agent.tools.impl.update_custom_identifiers import UpdateCustomIdentifiersTool
from app.agent.tools.impl.query_system_settings import QuerySystemSettingsTool
from app.agent.tools.impl.update_system_settings import UpdateSystemSettingsTool
from app.agent.tools.impl.submit_feedback_issue import SubmitFeedbackIssueTool
from app.agent.llm.capability import AgentCapabilityManager
from app.core.plugin import PluginManager
from app.log import logger
from app.schemas.message import ChannelCapabilityManager
from app.schemas.types import MessageChannel
from .base import MoviePilotTool


class MoviePilotToolFactory:
    """
    MoviePilot工具工厂
    """

    # 这些通用工具需要始终保留，避免大工具集裁剪后让 Agent 丢失基础的
    # 文件系统、命令执行或交互确认能力。AskUserChoiceTool 仅在支持按钮
    # 的渠道中才会实际注入，因此后续会再按已加载工具做一次求交集。
    TOOL_SELECTOR_ALWAYS_INCLUDE_NAMES = (
        "list_directory",
        "write_file",
        "read_file",
        "edit_file",
        "execute_command",
        "ask_user_choice",
    )

    @staticmethod
    def _should_enable_choice_tool(channel: str = None) -> bool:
        if not channel:
            return False
        try:
            message_channel = MessageChannel(channel)
        except ValueError:
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

    @staticmethod
    def create_tools(
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
        tool_definitions = [
            SearchMediaTool,
            SearchPersonTool,
            SearchPersonCreditsTool,
            RecognizeMediaTool,
            ScrapeMetadataTool,
            QueryEpisodeScheduleTool,
            QueryMediaDetailTool,
            AddSubscribeTool,
            UpdateSubscribeTool,
            SearchSubscribeTool,
            SearchTorrentsTool,
            GetSearchResultsTool,
            SearchWebTool,
            AddDownloadTool,
            QuerySubscribesTool,
            QuerySubscribeSharesTool,
            QueryPopularSubscribesTool,
            QueryBuiltinFilterRulesTool,
            QueryCustomFilterRulesTool,
            QueryRuleGroupsTool,
            AddCustomFilterRuleTool,
            UpdateCustomFilterRuleTool,
            DeleteCustomFilterRuleTool,
            AddRuleGroupTool,
            UpdateRuleGroupTool,
            DeleteRuleGroupTool,
            QuerySubscribeHistoryTool,
            DeleteSubscribeTool,
            QueryDownloadTasksTool,
            DeleteDownloadTool,
            DeleteDownloadHistoryTool,
            DeleteTransferHistoryTool,
            ModifyDownloadTool,
            QueryDownloadersTool,
            QuerySitesTool,
            UpdateSiteTool,
            QuerySiteUserdataTool,
            TestSiteTool,
            UpdateSiteCookieTool,
            GetRecommendationsTool,
            QueryLibraryExistsTool,
            QueryLibraryLatestTool,
            QueryDirectorySettingsTool,
            ListDirectoryTool,
            QueryTransferHistoryTool,
            TransferFileTool,
            SendMessageTool,
            QuerySchedulersTool,
            RunSchedulerTool,
            QueryWorkflowsTool,
            RunWorkflowTool,
            QueryPersonasTool,
            SwitchPersonaTool,
            UpdatePersonaDefinitionTool,
            ExecuteCommandTool,
            EditFileTool,
            WriteFileTool,
            ReadFileTool,
            BrowseWebpageTool,
            QueryInstalledPluginsTool,
            QueryMarketPluginsTool,
            QueryPluginCapabilitiesTool,
            QueryPluginConfigTool,
            UpdatePluginConfigTool,
            ReloadPluginTool,
            QueryPluginDataTool,
            InstallPluginTool,
            UninstallPluginTool,
            RunSlashCommandTool,
            ListSlashCommandsTool,
            QueryCustomIdentifiersTool,
            UpdateCustomIdentifiersTool,
            QuerySystemSettingsTool,
            UpdateSystemSettingsTool,
            SubmitFeedbackIssueTool,
        ]
        if MoviePilotToolFactory._should_enable_choice_tool(channel):
            tool_definitions.append(AskUserChoiceTool)
        tool_definitions.append(SendLocalFileTool)
        if AgentCapabilityManager.supports_audio_output():
            tool_definitions.append(SendVoiceMessageTool)
        # 创建内置工具
        for ToolClass in tool_definitions:
            tool = ToolClass(session_id=session_id, user_id=user_id)
            if not allow_message_tools and getattr(tool, "sends_message", False):
                continue
            tool.set_message_attr(channel=channel, source=source, username=username)
            tool.set_stream_handler(stream_handler=stream_handler)
            tool.set_agent_context(agent_context=agent_context)
            tools.append(tool)

        # 加载插件提供的工具
        plugin_tools_count = 0
        plugin_tools_info = PluginManager().get_plugin_agent_tools()
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
            logger.info(
                f"成功创建 {len(tools)} 个MoviePilot工具（内置工具: {builtin_tools_count} 个，插件工具: {plugin_tools_count} 个）"
            )
        else:
            logger.info(f"成功创建 {len(tools)} 个MoviePilot工具")
        return tools
