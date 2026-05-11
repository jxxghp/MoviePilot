"""查询已安装插件工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl._plugin_tool_utils import (
    DEFAULT_PLUGIN_CANDIDATE_LIMIT,
    MAX_PLUGIN_CANDIDATE_LIMIT,
    list_installed_plugins,
    search_plugin_candidates,
    summarize_candidates,
    summarize_plugin,
)
from app.log import logger


class QueryInstalledPluginsInput(BaseModel):
    """查询已安装插件工具的输入参数模型"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why this tool is being used in the current context",
    )
    query: Optional[str] = Field(
        None,
        description="Optional keyword to filter installed plugins by plugin ID, name, description, or author.",
    )
    max_results: Optional[int] = Field(
        DEFAULT_PLUGIN_CANDIDATE_LIMIT,
        description="Maximum number of plugins to return. Defaults to 50, capped at 200.",
    )


class QueryInstalledPluginsTool(MoviePilotTool):
    name: str = "query_installed_plugins"
    description: str = (
        "Query installed plugins in MoviePilot. Returns all installed plugins or filters them by keywords. "
        "Use this tool to find the exact plugin_id before uninstall_plugin or other plugin management tools are used."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = QueryInstalledPluginsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        query = kwargs.get("query")
        if query:
            return f"查询已安装插件: {query}"
        return "查询已安装插件"

    @staticmethod
    def _clamp_results(max_results: Optional[int]) -> int:
        if max_results is None:
            return DEFAULT_PLUGIN_CANDIDATE_LIMIT
        try:
            return max(1, min(int(max_results), MAX_PLUGIN_CANDIDATE_LIMIT))
        except (TypeError, ValueError):
            return DEFAULT_PLUGIN_CANDIDATE_LIMIT

    async def run(
        self,
        query: Optional[str] = None,
        max_results: Optional[int] = DEFAULT_PLUGIN_CANDIDATE_LIMIT,
        **kwargs,
    ) -> str:
        logger.info(f"执行工具: {self.name}, 参数: query={query}")
        try:
            installed_plugins = list_installed_plugins()
            if not installed_plugins:
                return json.dumps(
                    {"success": False, "message": "当前没有已安装的插件"},
                    ensure_ascii=False,
                )

            limit = self._clamp_results(max_results)
            if query:
                matches = search_plugin_candidates(query, installed_plugins)
                return json.dumps(
                    {
                        "success": True,
                        "query": query,
                        "total_installed": len(installed_plugins),
                        "match_count": len(matches),
                        "truncated": len(matches) > limit,
                        "plugins": summarize_candidates(matches, limit=limit),
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            plugin_summaries = [
                summarize_plugin(plugin) for plugin in installed_plugins[:limit]
            ]
            return json.dumps(
                {
                    "success": True,
                    "total_installed": len(installed_plugins),
                    "returned_count": len(plugin_summaries),
                    "truncated": len(installed_plugins) > limit,
                    "plugins": plugin_summaries,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            logger.error(f"查询已安装插件失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"查询已安装插件时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
