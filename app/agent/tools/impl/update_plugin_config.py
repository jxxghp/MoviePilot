"""修改插件配置工具"""

import json
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._plugin_tool_utils import get_plugin_snapshot
from app.application.plugin.runtime import get_plugin_manager
from app.runtime.log import logger


class UpdatePluginConfigInput(BaseModel):
    """修改插件配置工具的输入参数模型"""

    plugin_id: str = Field(
        ...,
        description="The plugin ID to update. Use query_plugin_config first to inspect the current config.",
    )
    updates: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Config items to save. By default this tool merges these keys into the existing config "
            "instead of replacing the whole config."
        ),
    )
    remove_keys: Optional[List[str]] = Field(
        None,
        description="Optional config keys to remove from the saved plugin config.",
    )
    replace: Optional[bool] = Field(
        False,
        description=(
            "Whether to replace the entire saved config with 'updates'. "
            "Default false, which performs a partial merge update."
        ),
    )


class UpdatePluginConfigTool(MoviePilotTool):
    name: str = "update_plugin_config"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Plugin,
        ToolTag.Admin,
    ]
    description: str = (
        "Update the saved configuration of an installed plugin. "
        "By default this performs a partial merge update and does NOT reload the plugin automatically. "
        "Call reload_plugin afterwards to apply the latest saved config to the running plugin."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = UpdatePluginConfigInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        plugin_id = kwargs.get("plugin_id", "")
        replace = kwargs.get("replace", False)
        action = "覆盖插件配置" if replace else "修改插件配置"
        return f"{action}: {plugin_id}"

    @staticmethod
    async def _update_plugin_config(
        plugin_id: str,
        updates: Optional[Dict[str, Any]] = None,
        remove_keys: Optional[List[str]] = None,
        replace: bool = False,
    ) -> str:
        """
        仅异步保存插件配置，不主动生效，让 Agent 可以先批量改完再显式重载插件。
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

        remove_keys = remove_keys or []
        if not replace and not updates and not remove_keys:
            return json.dumps(
                {"success": False, "message": "没有提供任何需要修改的配置项"},
                ensure_ascii=False,
            )

        plugin_manager = get_plugin_manager()
        with plugin_manager.mutation(f"更新插件 {plugin_id} 配置"):
            current_config = dict(plugin_manager.get_plugin_config(plugin_id) or {})

            # merge 模式以当前保存值为基准，replace 模式则从空配置开始重建。
            next_config = {} if replace else dict(current_config)
            if updates:
                next_config.update(updates)
            for key in remove_keys:
                next_config.pop(key, None)

            changed_keys = sorted(
                key
                for key in set(current_config.keys()) | set(next_config.keys())
                if current_config.get(key) != next_config.get(key)
                or (key in current_config) != (key in next_config)
            )

            if not await plugin_manager.async_save_plugin_config(
                plugin_id,
                next_config,
            ):
                return json.dumps(
                    {
                        "success": False,
                        "message": f"保存插件 {plugin_id} 配置失败",
                    },
                    ensure_ascii=False,
                )

            return json.dumps(
                {
                    "success": True,
                    **plugin_info,
                    "message": "插件配置已保存，请调用 reload_plugin 使最新配置生效",
                    "replace": replace,
                    "changed_keys": changed_keys,
                    "removed_keys": remove_keys,
                    "config_requires_reload": True,
                    "previous_config": current_config,
                    "saved_config": next_config,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )

    async def run(
        self,
        plugin_id: str,
        updates: Optional[Dict[str, Any]] = None,
        remove_keys: Optional[List[str]] = None,
        replace: bool = False,
        **kwargs,
    ) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: plugin_id={plugin_id}, replace={replace}"
        )

        try:
            return await self._update_plugin_config(
                plugin_id, updates, remove_keys, replace
            )
        except Exception as e:
            logger.error(f"修改插件配置失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"修改插件配置时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
