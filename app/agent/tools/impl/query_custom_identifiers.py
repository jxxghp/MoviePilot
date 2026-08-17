"""查询自定义识别词工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


class QueryCustomIdentifiersInput(BaseModel):
    """查询自定义识别词工具的输入参数模型"""



class QueryCustomIdentifiersTool(MoviePilotTool):
    name: str = "query_custom_identifiers"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.FilterRule,
        ToolTag.Admin,
    ]
    description: str = (
        "Query all currently configured custom identifiers (自定义识别词). "
        "Returns the list of identifier rules used for preprocessing torrent/file names before media recognition. "
        "Use this tool to check existing rules before adding new ones to avoid duplicates."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = QueryCustomIdentifiersInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        return "查询自定义识别词"

    @staticmethod
    def _load_custom_identifiers():
        """从内存配置缓存中读取自定义识别词。"""
        return SystemConfigOper().get(SystemConfigKey.CustomIdentifiers)

    async def run(self, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}")
        try:
            identifiers = self._load_custom_identifiers()
            if identifiers:
                return json.dumps(
                    {
                        "success": True,
                        "count": len(identifiers),
                        "identifiers": identifiers,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            return json.dumps(
                {
                    "success": True,
                    "count": 0,
                    "identifiers": [],
                    "message": "当前没有配置自定义识别词",
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            logger.error(f"查询自定义识别词失败: {e}")
            return json.dumps(
                {"success": False, "message": f"查询自定义识别词时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
