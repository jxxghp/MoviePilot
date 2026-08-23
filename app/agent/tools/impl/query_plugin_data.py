"""查询插件数据工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._plugin_tool_utils import (
    PLUGIN_DATA_KEY_PREVIEW_LIMIT,
    build_preview_payload,
    get_plugin_snapshot,
)
from app.application.agentdata import get_agent_plugin_data_port
from app.runtime.log import logger


class QueryPluginDataInput(BaseModel):
    """查询插件数据工具的输入参数模型"""

    plugin_id: str = Field(
        ...,
        description="The plugin ID to query. Use query_installed_plugins first to discover valid plugin IDs.",
    )
    key: Optional[str] = Field(
        None,
        description="Optional plugin data key. If omitted, returns all plugin data entries for the plugin.",
    )
    max_chars: Optional[int] = Field(
        None,
        description="Maximum number of preview characters to return when plugin data is too large. Default 12000, capped at 50000.",
    )


class QueryPluginDataTool(MoviePilotTool):
    name: str = "query_plugin_data"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Plugin,
        ToolTag.Admin,
    ]
    description: str = (
        "Query persisted data of an installed plugin. "
        "Optionally specify a key to read a single data item; otherwise all plugin data entries are returned. "
        "When the result is too large, the tool automatically truncates it and returns a preview instead."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = QueryPluginDataInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        plugin_id = kwargs.get("plugin_id", "")
        key = kwargs.get("key")
        if key:
            return f"查询插件数据: {plugin_id}.{key}"
        return f"查询插件全部数据: {plugin_id}"

    @staticmethod
    async def _query_plugin_data(
            plugin_id: str, key: Optional[str] = None, max_chars: Optional[int] = None
    ) -> str:
        """
        插件数据改走异步 ORM 查询，避免再套一层线程池。
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

        plugin_data_oper = get_agent_plugin_data_port()
        if key:
            value = await plugin_data_oper.async_get_data(plugin_id, key)
            if value is None:
                return json.dumps(
                    {
                        "success": True,
                        **plugin_info,
                        "key": key,
                        "found": False,
                        "message": f"插件 {plugin_id} 没有数据项 {key}",
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            truncated, total_chars, returned_chars, preview = build_preview_payload(
                value, max_chars
            )
            result = {
                "success": True,
                **plugin_info,
                "key": key,
                "found": True,
                "truncated": truncated,
                "total_chars": total_chars,
                "returned_chars": returned_chars,
            }
            if truncated:
                result["value_preview"] = preview
                result["message"] = "插件数据内容过大，已截断预览"
            else:
                result["value"] = value
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)

        rows = await plugin_data_oper.async_get_data_all(plugin_id) or []
        data_map = {row.key: row.value for row in rows}
        keys = list(data_map.keys())
        key_preview = keys[:PLUGIN_DATA_KEY_PREVIEW_LIMIT]

        result = {
            "success": True,
            **plugin_info,
            "count": len(data_map),
            "keys": key_preview,
            "keys_truncated": len(keys) > PLUGIN_DATA_KEY_PREVIEW_LIMIT,
        }

        if not data_map:
            result["data"] = {}
            result["truncated"] = False
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)

        truncated, total_chars, returned_chars, preview = build_preview_payload(
            data_map, max_chars
        )
        result["truncated"] = truncated
        result["total_chars"] = total_chars
        result["returned_chars"] = returned_chars
        if truncated:
            result["data_preview"] = preview
            result["message"] = "插件数据内容过大，已截断。请传入 key 精确查询单个数据项。"
        else:
            result["data"] = data_map
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)

    async def run(
        self,
        plugin_id: str,
        key: Optional[str] = None,
        max_chars: Optional[int] = None,
        **kwargs,
    ) -> str:
        logger.info(
            f"执行工具: {self.name}, 参数: plugin_id={plugin_id}, key={key}"
        )

        try:
            return await self._query_plugin_data(plugin_id, key, max_chars)
        except Exception as e:
            logger.error(f"查询插件数据失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"查询插件数据时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
