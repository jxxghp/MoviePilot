"""重载插件工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._plugin_tool_utils import (
    get_plugin_snapshot,
    reload_plugin_runtime,
)
from app.runtime.log import logger


class ReloadPluginInput(BaseModel):
    """重载插件工具的输入参数模型"""

    plugin_id: str = Field(
        ...,
        description="The plugin ID to reload so the latest saved config takes effect.",
    )


class ReloadPluginTool(MoviePilotTool):
    name: str = "reload_plugin"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Plugin,
        ToolTag.Admin,
    ]
    description: str = (
        "Reload an installed plugin so its latest saved configuration takes effect. "
        "This also refreshes the plugin's registered commands, scheduled services, and API routes."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = ReloadPluginInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        plugin_id = kwargs.get("plugin_id", "")
        return f"重载插件: {plugin_id}"

    @staticmethod
    def _reload_plugin_sync(plugin_id: str) -> str:
        """
        按后台接口同样的流程重载插件，确保最新配置和注册信息一起刷新。
        """
        plugin_info = get_plugin_snapshot(plugin_id)
        if not plugin_info:
            return json.dumps(
                {
                    "success": False,
                    "message": f"插件 {plugin_id} 不存在，请先使用 query_installed_plugins 查询有效插件 ID",
                },
                ensure_ascii=False,
            )

        reload_plugin_runtime(plugin_id)
        refreshed_plugin = get_plugin_snapshot(plugin_id) or plugin_info

        return json.dumps(
            {
                "success": True,
                **refreshed_plugin,
                "message": "插件已重载，最新配置已生效",
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    async def run(self, plugin_id: str, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: plugin_id={plugin_id}")

        try:
            return await self.run_blocking(
                "plugin", self._reload_plugin_sync, plugin_id
            )
        except Exception as e:
            logger.error(f"重载插件失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"重载插件时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
