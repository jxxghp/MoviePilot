"""删除整理历史记录工具"""

import asyncio
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.application.agentdata import get_agent_transfer_history_port
from app.application.chain.data import get_chain_transfer_execution_port
from app.application.transfer_execution import (
    TransferExecutionCommand,
    TransferRetryRequestResult,
)
from app.chain.storage import StorageChain
from app.runtime.log import logger
from app.schemas.workflow import FileItem


class DeleteTransferHistoryInput(BaseModel):
    """删除整理历史记录工具的输入参数模型"""

    history_id: int = Field(
        ..., description="The ID of the transfer history record to delete"
    )


def _delete_history_destination_file(fileitem: FileItem) -> tuple[bool, bool]:
    """在存储 worker 内完成旧目标检查和删除，保持历史删除前的顺序。"""
    storage_chain = StorageChain()
    if not storage_chain.exists(fileitem):
        return False, False
    return True, bool(storage_chain.delete_media_file(fileitem))


def _request_transfer_retry(
    *,
    history_id: int,
    task_id: str,
    user_id: str,
) -> TransferRetryRequestResult:
    """在线程池中登记 durable 重试，避免 Agent 事件循环执行同步数据库 I/O。"""
    return TransferExecutionCommand(
        get_chain_transfer_execution_port()
    ).request_retry(
        task_id=task_id,
        reason=f"Agent 请求重试整理历史 #{history_id}",
        requested_by=f"agent:{user_id or 'unknown'}",
    )


class DeleteTransferHistoryTool(MoviePilotTool):
    name: str = "delete_transfer_history"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Transfer,
        ToolTag.Admin,
    ]
    description: str = (
        "Request a safe retry for durable transfer history, or delete a legacy transfer history record by its ID. "
        "Durable records keep their files and history and are retried only by the persistent scheduler. For legacy "
        "non-successful-move records, the tool removes the old destination before deleting the history. If a durable "
        "retry is accepted or rejected, stop and report that result; do not call transfer_file for the same record."
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
            transferhis = get_agent_transfer_history_port()
            history = await transferhis.async_get(history_id)
            if not history:
                return f"错误：整理历史记录不存在，ID={history_id}"

            task_id = getattr(history, "transfer_task_id", None)
            if task_id:
                retry = await self.run_blocking(
                    "db",
                    _request_transfer_retry,
                    history_id=history_id,
                    task_id=task_id,
                    user_id=self._user_id,
                )
                outcome = "已登记" if retry.accepted else "未登记"
                return (
                    f"durable 整理任务{outcome}重试：ID={history_id}，"
                    f"task_id={task_id}，state={retry.state.value}，{retry.message}。"
                    "已保留目标文件、历史记录和失败计数；不要调用 transfer_file。"
                )

            title = history.title or "未知"
            src = history.src or "未知"
            status = "成功" if history.status else "失败"
            deleted_dest = False
            if history.dest_fileitem and not (history.status and history.mode == "move"):
                dest_fileitem = FileItem(**history.dest_fileitem)
                try:
                    destination_exists, destination_deleted = await self.run_blocking(
                        "storage",
                        _delete_history_destination_file,
                        dest_fileitem,
                    )
                except asyncio.CancelledError:
                    logger.warning(
                        "删除整理历史的旧媒体文件等待已取消，底层文件操作可能仍在继续，"
                        "请确认实际状态后再重试，历史记录尚未删除，path=%s",
                        dest_fileitem.path,
                    )
                    raise
                if destination_exists:
                    if not destination_deleted:
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
