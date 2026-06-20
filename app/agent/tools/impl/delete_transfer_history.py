"""删除整理历史记录工具"""

from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.storage import StorageChain
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.schemas import FileItem


class DeleteTransferHistoryInput(BaseModel):
    """删除整理历史记录工具的输入参数模型"""

    explanation: Optional[str] = Field(None,
        description="Clear explanation of why this tool is being used in the current context",)
    history_id: int = Field(
        ..., description="The ID of the transfer history record to delete"
    )


class DeleteTransferHistoryTool(MoviePilotTool):
    name: str = "delete_transfer_history"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Transfer,
        ToolTag.Admin,
    ]
    description: str = (
        "Delete a specific transfer history record by its ID. For non-successful-move records with an old "
        "destination file, the tool removes that media-library file before deleting the history record. This is "
        "useful before retrying or re-organizing because the system skips files that already have transfer history."
    )
    args_schema: Type[BaseModel] = DeleteTransferHistoryInput
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据参数生成友好的提示消息"""
        history_id = kwargs.get("history_id")
        return f"删除整理历史记录: ID={history_id}"

    async def run(self, history_id: int, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: history_id={history_id}")

        try:
            transferhis = TransferHistoryOper()
            history = await transferhis.async_get(history_id)
            if not history:
                return f"错误：整理历史记录不存在，ID={history_id}"

            title = history.title or "未知"
            src = history.src or "未知"
            status = "成功" if history.status else "失败"
            deleted_dest = False
            if history.dest_fileitem and not (history.status and history.mode == "move"):
                dest_fileitem = FileItem(**history.dest_fileitem)
                storage_chain = StorageChain()
                if storage_chain.exists(dest_fileitem):
                    if not storage_chain.delete_media_file(dest_fileitem):
                        return f"错误：旧媒体库文件删除失败，路径={dest_fileitem.path}"
                    deleted_dest = True
            await transferhis.async_delete(history_id)
            message = (
                f"已删除整理历史记录：ID={history_id}，标题={title}，源路径={src}，状态={status}"
            )
            if deleted_dest:
                message += "，已删除旧媒体库文件"
            return message
        except Exception as e:
            logger.error(f"删除整理历史记录失败: {e}", exc_info=True)
            return f"删除整理历史记录时发生错误: {str(e)}"
