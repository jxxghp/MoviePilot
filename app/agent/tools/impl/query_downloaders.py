"""查询下载器工具"""

import json
from typing import Optional, Type

from pydantic import BaseModel

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.db.oper.systemconfig import SystemConfigOper
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


class QueryDownloadersInput(BaseModel):
    """查询下载器工具的输入参数模型"""
class QueryDownloadersTool(MoviePilotTool):
    name: str = "query_downloaders"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Download,
    ]
    description: str = (
        "Query downloader configuration and list available downloaders. Non-admin users receive "
        "a safe view with only the fields needed to choose a downloader, without host, account, "
        "password, token or API key values."
    )
    args_schema: Type[BaseModel] = QueryDownloadersInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """生成友好的提示消息"""
        return "查询下载器配置"

    @staticmethod
    def _load_downloaders_config():
        """从内存配置缓存中读取下载器配置。"""
        return SystemConfigOper().get(SystemConfigKey.Downloaders)

    @staticmethod
    def _sanitize_downloaders_config(downloaders_config: list) -> list:
        """
        生成普通用户可见的下载器配置视图。

        :param downloaders_config: 系统下载器完整配置列表
        :return: 仅包含名称、类型和启用状态的安全配置列表
        """
        safe_fields = ("name", "type", "enabled", "default", "priority")
        safe_downloaders = []
        for downloader in downloaders_config:
            if not isinstance(downloader, dict):
                continue
            safe_downloaders.append({
                key: downloader.get(key)
                for key in safe_fields
                if key in downloader
            })
        return safe_downloaders

    async def run(self, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}")
        try:
            downloaders_config = self._load_downloaders_config()
            if downloaders_config:
                if not await self.is_admin_user():
                    downloaders_config = self._sanitize_downloaders_config(
                        downloaders_config
                    )
                return json.dumps(downloaders_config, ensure_ascii=False, indent=2)
            return "未配置下载器。"
        except Exception as e:
            logger.error(f"查询下载器失败: {e}")
            return f"查询下载器时发生错误: {str(e)}"
