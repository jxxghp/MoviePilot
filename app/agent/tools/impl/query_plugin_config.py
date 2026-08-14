"""查询插件配置工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._plugin_tool_utils import get_plugin_snapshot
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.log import logger


class QueryPluginConfigInput(BaseModel):
    """查询插件配置工具的输入参数模型"""

    plugin_id: str = Field(
        ...,
        description="The plugin ID to query. Use query_installed_plugins first to discover valid plugin IDs.",
    )


class QueryPluginConfigTool(MoviePilotTool):
    name: str = "query_plugin_config"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Plugin,
        ToolTag.Admin,
    ]
    description: str = (
        "Query the saved configuration of an installed plugin. "
        "Returns the current saved config and, when available, the plugin's default config model. "
        "Use this before update_plugin_config so you only change the intended keys."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = QueryPluginConfigInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        plugin_id = kwargs.get("plugin_id", "")
        return f"查询插件配置: {plugin_id}"

    @staticmethod
    def _query_plugin_config(plugin_id: str) -> str:
        """
        读取插件已保存配置，并尽量补充默认配置模型方便后续精确修改。
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

        plugin_manager = PluginManager()
        saved_config = plugin_manager.get_plugin_config(plugin_id) or {}
        result = {
            "success": True,
            **plugin_info,
            "config": saved_config,
        }

        # get_form 的 model 通常就是插件期望的配置结构，适合作为修改前的键参考。
        plugin_instance = plugin_manager.running_plugins.get(plugin_id)
        if plugin_instance and hasattr(plugin_instance, "get_form"):
            try:
                _form_schema, default_model = plugin_instance.get_form()
                if default_model is not None:
                    result["default_model"] = default_model
            except Exception as err:
                logger.warning(f"读取插件 {plugin_id} 默认配置模型失败: {err}")

        return json.dumps(result, ensure_ascii=False, indent=2, default=str)

    async def run(self, plugin_id: str, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: plugin_id={plugin_id}")

        try:
            # 插件配置来自内存配置缓存和运行态插件实例，直接读取即可。
            return self._query_plugin_config(plugin_id)
        except Exception as e:
            logger.error(f"查询插件配置失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"查询插件配置时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
