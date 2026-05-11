"""查询插件市场工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl._plugin_tool_utils import (
    DEFAULT_PLUGIN_CANDIDATE_LIMIT,
    MAX_PLUGIN_CANDIDATE_LIMIT,
    load_market_plugins,
    search_plugin_candidates,
    summarize_candidates,
    summarize_plugin,
)
from app.log import logger


class QueryMarketPluginsInput(BaseModel):
    """查询插件市场工具的输入参数模型"""

    explanation: str = Field(
        ...,
        description="Clear explanation of why this tool is being used in the current context",
    )
    query: Optional[str] = Field(
        None,
        description="Optional keyword to filter plugin market results by plugin ID, name, description, or author.",
    )
    max_results: Optional[int] = Field(
        DEFAULT_PLUGIN_CANDIDATE_LIMIT,
        description="Maximum number of plugins to return. Defaults to 50, capped at 200.",
    )
    force_refresh: Optional[bool] = Field(
        False,
        description="Whether to refresh plugin market caches before querying.",
    )


class QueryMarketPluginsTool(MoviePilotTool):
    name: str = "query_market_plugins"
    description: str = (
        "Query available plugins from the plugin market and local plugin repositories. "
        "Can return the full plugin list or filter by keywords before install_plugin is used."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = QueryMarketPluginsInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        query = kwargs.get("query")
        if query:
            return f"查询插件市场: {query}"
        return "查询插件市场全部插件"

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
        force_refresh: bool = False,
        **kwargs,
    ) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: query={query}, force_refresh={force_refresh}"
        )

        try:
            plugins = await load_market_plugins(force_refresh=force_refresh)
            if not plugins:
                return json.dumps(
                    {"success": False, "message": "当前插件市场没有可用插件"},
                    ensure_ascii=False,
                )

            limit = self._clamp_results(max_results)
            if query:
                matches = search_plugin_candidates(query, plugins)
                return json.dumps(
                    {
                        "success": True,
                        "query": query,
                        "total_available": len(plugins),
                        "match_count": len(matches),
                        "truncated": len(matches) > limit,
                        "plugins": summarize_candidates(matches, limit=limit),
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            plugin_summaries = [summarize_plugin(plugin) for plugin in plugins[:limit]]
            return json.dumps(
                {
                    "success": True,
                    "total_available": len(plugins),
                    "returned_count": len(plugin_summaries),
                    "truncated": len(plugins) > limit,
                    "plugins": plugin_summaries,
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            logger.error(f"查询插件市场失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"查询插件市场时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
