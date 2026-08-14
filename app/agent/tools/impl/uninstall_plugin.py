"""卸载插件工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._plugin_tool_utils import (
    list_installed_plugins,
    summarize_plugin,
    uninstall_plugin_runtime,
)
from app.runtime.log import logger


class UninstallPluginInput(BaseModel):
    """卸载插件工具的输入参数模型"""

    plugin_id: str = Field(
        ...,
        description="Exact plugin ID to uninstall. Use query_installed_plugins first to find the correct plugin_id.",
    )


class UninstallPluginTool(MoviePilotTool):
    name: str = "uninstall_plugin"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Plugin,
        ToolTag.Admin,
    ]
    description: str = (
        "Uninstall an installed plugin by exact plugin_id. "
        "Use query_installed_plugins first when you need filtering or discovery."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = UninstallPluginInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        plugin_id = kwargs.get("plugin_id")
        return f"卸载插件: {plugin_id or '未知插件'}"

    async def run(
        self,
        plugin_id: str,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}, 参数: plugin_id={plugin_id}")

        try:
            plugins = list_installed_plugins()
            if not plugins:
                return json.dumps(
                    {"success": False, "message": "当前没有已安装的插件"},
                    ensure_ascii=False,
                )

            candidate = next((plugin for plugin in plugins if plugin.id == plugin_id), None)
            if not candidate:
                return json.dumps(
                    {
                        "success": False,
                        "message": f"未找到已安装插件: {plugin_id}。请先调用 query_installed_plugins 确认 plugin_id。",
                    },
                    ensure_ascii=False,
                )

            cleanup_result = await uninstall_plugin_runtime(candidate.id)
            return json.dumps(
                {
                    "success": True,
                    "message": f"插件 {candidate.id} 已卸载",
                    "plugin": summarize_plugin(candidate),
                    **cleanup_result,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            logger.error(f"卸载插件失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"卸载插件时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
