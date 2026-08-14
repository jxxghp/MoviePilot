"""安装插件工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._plugin_tool_utils import (
    get_plugin_snapshot,
    install_plugin_runtime,
    load_market_plugins,
    summarize_plugin,
)
from app.runtime.log import logger


class InstallPluginInput(BaseModel):
    """安装插件工具的输入参数模型"""

    plugin_id: str = Field(
        ...,
        description="Exact plugin ID to install. Use query_market_plugins first to find the correct plugin_id.",
    )
    force: bool = Field(
        False,
        description="Whether to force reinstall or upgrade the specified plugin.",
    )
    force_refresh_market: bool = Field(
        False,
        description="Whether to refresh plugin market caches before reading the market list.",
    )


class InstallPluginTool(MoviePilotTool):
    name: str = "install_plugin"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Plugin,
        ToolTag.Admin,
    ]
    description: str = (
        "Install a plugin by exact plugin_id from the plugin market or local plugin repositories. "
        "Use query_market_plugins first when you need filtering or discovery."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = InstallPluginInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        plugin_id = kwargs.get("plugin_id")
        return f"安装插件: {plugin_id or '未知插件'}"

    async def run(
        self,
        plugin_id: str,
        force: bool = False,
        force_refresh_market: bool = False,
        **kwargs,
    ) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: plugin_id={plugin_id}, force={force}"
        )

        try:
            plugins = await load_market_plugins(force_refresh=force_refresh_market)
            if not plugins:
                return json.dumps(
                    {"success": False, "message": "当前插件市场没有可用插件"},
                    ensure_ascii=False,
                )

            candidate = next((plugin for plugin in plugins if plugin.id == plugin_id), None)
            if not candidate:
                return json.dumps(
                    {
                        "success": False,
                        "message": f"未在插件市场中找到插件: {plugin_id}。请先调用 query_market_plugins 确认 plugin_id。",
                    },
                    ensure_ascii=False,
                )

            success, message, refreshed_only = await install_plugin_runtime(
                candidate.id,
                getattr(candidate, "repo_url", None),
                force=force,
            )
            if not success:
                return json.dumps(
                    {
                        "success": False,
                        "plugin": summarize_plugin(candidate),
                        "message": message,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            plugin_snapshot = get_plugin_snapshot(candidate.id)
            if refreshed_only and getattr(candidate, "has_update", False) and not force:
                message = "插件已安装，当前仅刷新加载；如需升级到市场新版本，请设置 force=true"

            return json.dumps(
                {
                    "success": True,
                    "message": message,
                    "force": force,
                    "refreshed_only": refreshed_only,
                    "plugin": summarize_plugin(candidate),
                    "runtime": plugin_snapshot,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            logger.error(f"安装插件失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"安装插件时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
