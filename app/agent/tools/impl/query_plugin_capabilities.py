"""查询插件能力工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.log import logger


class QueryPluginCapabilitiesInput(BaseModel):
    """查询插件能力工具的输入参数模型"""

    plugin_id: Optional[str] = Field(
        None,
        description="Optional plugin ID to query capabilities for a specific plugin. "
        "If not provided, returns capabilities of all running plugins. "
        "Use query_installed_plugins tool to get the plugin IDs first.",
    )


class QueryPluginCapabilitiesTool(MoviePilotTool):
    name: str = "query_plugin_capabilities"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Plugin,
        ToolTag.Admin,
    ]
    description: str = (
        "Query the capabilities of installed plugins, including supported commands and scheduled services. "
        "Commands are slash-commands (e.g. /xxx) that can be executed via the run_slash_command tool. "
        "Scheduled services are periodic tasks that can be triggered via the run_scheduler tool. "
        "Optionally specify a plugin_id to query a specific plugin, or omit to query all running plugins."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = QueryPluginCapabilitiesInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        plugin_id = kwargs.get("plugin_id")
        if plugin_id:
            return f"查询插件 {plugin_id} 的能力"
        return "查询所有插件的能力"

    @staticmethod
    def _load_plugin_capabilities(plugin_id: Optional[str] = None) -> dict:
        """读取运行中插件实例暴露的内存能力信息。"""
        plugin_manager = PluginManager()
        result = {}

        commands = plugin_manager.get_plugin_commands(pid=plugin_id)
        if commands:
            result["commands"] = [
                {
                    "cmd": cmd.get("cmd"),
                    "desc": cmd.get("desc"),
                    "plugin_id": cmd.get("pid"),
                    **({"data": cmd.get("data")} if cmd.get("data") else {}),
                }
                for cmd in commands
            ]

        actions = plugin_manager.get_plugin_actions(pid=plugin_id)
        if actions:
            actions_list = []
            for action_group in actions:
                actions_list.append(
                    {
                        "plugin_id": action_group.get("plugin_id"),
                        "plugin_name": action_group.get("plugin_name"),
                        "actions": [
                            {
                                "id": action.get("id"),
                                "name": action.get("name"),
                            }
                            for action in action_group.get("actions", [])
                        ],
                    }
                )
            result["actions"] = actions_list

        services = plugin_manager.get_plugin_services(pid=plugin_id)
        if services:
            services_list = []
            for svc in services:
                svc_info = {
                    "id": svc.get("id"),
                    "name": svc.get("name"),
                }
                trigger = svc.get("trigger")
                if trigger:
                    svc_info["trigger"] = str(trigger)
                svc_kwargs = svc.get("kwargs")
                if svc_kwargs:
                    svc_info["trigger_kwargs"] = {
                        k: str(v) for k, v in svc_kwargs.items()
                    }
                services_list.append(svc_info)
            result["services"] = services_list

        return result

    async def run(self, plugin_id: Optional[str] = None, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: plugin_id={plugin_id}")
        try:
            result = self._load_plugin_capabilities(plugin_id)
            if not result:
                if plugin_id:
                    return f"插件 {plugin_id} 没有注册任何命令、动作或定时服务"
                return "当前没有运行中的插件注册了命令、动作或定时服务"

            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"查询插件能力失败: {e}", exc_info=True)
            return f"查询插件能力时发生错误: {str(e)}"
