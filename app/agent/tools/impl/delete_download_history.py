"""删除下载历史记录工具"""

from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.agentdata import get_agent_download_history_port
from app.runtime.log import logger


class DeleteDownloadHistoryInput(BaseModel):
    """删除下载历史记录工具的输入参数模型"""

    history_id: int = Field(
        ..., description="The ID of the download history record to delete"
    )


class DeleteDownloadHistoryTool(MoviePilotTool):
    name: str = "delete_download_history"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Download,
        ToolTag.Admin,
    ]
    description: str = "Delete a download history record by ID. This only removes the record from the database, does not delete any actual files."
    args_schema: Type[BaseModel] = DeleteDownloadHistoryInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        history_id = kwargs.get("history_id")
        return f"删除下载历史记录 ID: {history_id}"

    async def run(self, history_id: int, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: history_id={history_id}")

        try:
            await get_agent_download_history_port().async_delete_history(history_id)
            return f"下载历史记录 ID: {history_id} 已成功删除"
        except Exception as e:
            logger.error(f"删除下载历史记录失败: {e}", exc_info=True)
            return f"删除下载历史记录时发生错误: {str(e)}"
