import asyncio
import time
from typing import List, Any, Optional

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.schemas.common import BatchProgressKeyData as _SchemaBatchProgressKeyData
from app.schemas.common import ProgressKeyData as _SchemaProgressKeyData
from app.schemas.history import BatchTransferHistoryRedoRequest as _SchemaBatchTransferHistoryRedoRequest
from app.schemas.history import TransferHistory as _SchemaTransferHistory
from app.schemas.history import TransferHistoryPage as _SchemaTransferHistoryPage
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.history import DownloadHistory as _SchemaDownloadHistory
from app.api.response import ResponseAPIRouter
from app.agent.contracts import ReplyMode
from app.agent.runtime_loader import get_running_agent_manager
from app.agent.prompt.transfer_redo import (
    build_batch_manual_redo_prompt,
    build_manual_redo_prompt,
)
from app.runtime.config import settings, global_vars
from app.application.security.access import verify_token
from app.db import get_async_db, get_db
from app.db.models import User
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.transferhistory import TransferHistory
from app.api.deps import (
    get_current_active_manage_user,
    get_current_active_superuser,
    get_download_history_mutation_command,
    get_transfer_history_mutation_command,
)
from app.runtime.progress import ProgressHelper
from app.application.history import (
    DownloadHistoryMutationCommand,
    TransferHistoryMutationCommand,
)
from app.foundation.text import cut as jieba_cut
from app.runtime.log import logger

router = ResponseAPIRouter()


def normalize_history_ids(history_ids: list[int]) -> list[int]:
    """对输入的历史记录 ID 列表进行规范化处理，去除重复项并保持原有顺序。"""
    normalized_ids: list[int] = []
    for history_id in history_ids:
        if history_id not in normalized_ids:
            normalized_ids.append(history_id)
    return normalized_ids


def _start_ai_redo_task(history_id: int, prompt: str, progress_key: str):
    """在后台线程中启动单条 AI 重新整理任务，并通过 ProgressHelper 实时更新进度。"""
    progress = ProgressHelper(progress_key)
    progress.start()
    progress.update(
        text=f"智能助手正在准备整理记录 #{history_id} ...",
        data={"history_id": history_id, "success": True},
    )

    def update_output(text: str):
        progress.update(text=text, data={"history_id": history_id})

    async def runner():
        try:
            manager = get_running_agent_manager()
            if manager is None:
                logger.warning("智能助手服务未运行，跳过单条整理历史 AI 重做")
                raise RuntimeError("智能助手服务未运行")
            await manager.run_background_prompt(
                message=prompt,
                session_prefix=f"__agent_manual_redo_{history_id}",
                output_callback=update_output,
                reply_mode=ReplyMode.CAPTURE_ONLY,
                allow_message_tools=False,
            )
            progress.update(
                text="智能助手整理完成",
                data={"history_id": history_id, "success": True, "completed": True},
            )
        except Exception as e:
            progress.update(
                text=f"智能助手整理失败：{str(e)}",
                data={
                    "history_id": history_id,
                    "success": False,
                    "completed": True,
                    "error": str(e),
                },
            )
        finally:
            progress.end()

    asyncio.run_coroutine_threadsafe(runner(), global_vars.loop)


def _start_batch_ai_redo_task(
    history_ids: list[int],
    prompt: str,
    progress_key: str,
):
    """在后台线程中启动批量 AI 重新整理任务，并通过 ProgressHelper 实时更新进度。"""
    progress = ProgressHelper(progress_key)
    progress.start()
    progress.update(
        text=f"智能助手正在准备批量整理 {len(history_ids)} 条记录 ...",
        data={"history_ids": history_ids, "success": True},
    )

    def update_output(text: str):
        progress.update(text=text, data={"history_ids": history_ids})

    async def runner():
        try:
            manager = get_running_agent_manager()
            if manager is None:
                logger.warning("智能助手服务未运行，跳过批量整理历史 AI 重做")
                raise RuntimeError("智能助手服务未运行")
            await manager.run_background_prompt(
                message=prompt,
                session_prefix="__agent_manual_redo_batch",
                output_callback=update_output,
                reply_mode=ReplyMode.CAPTURE_ONLY,
                allow_message_tools=False,
            )
            progress.update(
                text="智能助手批量整理完成",
                data={"history_ids": history_ids, "success": True, "completed": True},
            )
        except Exception as e:
            progress.update(
                text=f"智能助手批量整理失败：{str(e)}",
                data={
                    "history_ids": history_ids,
                    "success": False,
                    "completed": True,
                    "error": str(e),
                },
            )
        finally:
            progress.end()

    asyncio.run_coroutine_threadsafe(runner(), global_vars.loop)


@router.get(
    "/download",
    summary="查询下载历史记录",
    response_model=List[_SchemaDownloadHistory],
)
async def download_history(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    db: AsyncSession = Depends(get_async_db),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    按下载时间倒序查询下载历史记录
    """
    return await DownloadHistory.async_list_by_page(db, page, count)


@router.delete(
    "/download",
    summary="删除下载历史记录",
    response_model=_SchemaResponse[None],
)
def delete_download_history(
    history_in: _SchemaDownloadHistory,
    command: DownloadHistoryMutationCommand = Depends(
        get_download_history_mutation_command
    ),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    删除下载历史记录
    """
    result = command.delete(history_in.id)
    return _SchemaResponse(success=result.success, message=result.message)


def _glob_to_like(pattern: str) -> str:
    """
    将 glob 通配符模式转换为 SQL LIKE 模式（使用 \\ 作为转义字符）
    """
    result = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return result.replace("*", "%").replace("?", "_")


@router.get(
    "/transfer",
    summary="查询整理记录",
    response_model=_SchemaResponse[_SchemaTransferHistoryPage],
)
async def transfer_history(
    title: Optional[str] = None,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    status: Optional[bool] = None,
    db: AsyncSession = Depends(get_async_db),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    查询整理记录，title 支持通配符 * 和 ?（如 *.mkv、*2024*）
    """
    if title == "失败":
        title = None
        status = False
    elif title == "成功":
        title = None
        status = True

    if title:
        if "*" in title or "?" in title:
            like_pattern = _glob_to_like(title)
            total = await TransferHistory.async_count_by_title(
                db, title=like_pattern, status=status, wildcard=True
            )
            result = await TransferHistory.async_list_by_title(
                db, title=like_pattern, page=page, count=count, status=status, wildcard=True
            )
        else:
            words = jieba_cut(title, HMM=False)
            like_pattern = "%".join(words)
            total = await TransferHistory.async_count_by_title(
                db, title=like_pattern, status=status
            )
            result = await TransferHistory.async_list_by_title(
                db, title=like_pattern, page=page, count=count, status=status
            )
    else:
        result = await TransferHistory.async_list_by_page(
            db, page=page, count=count, status=status
        )
        total = await TransferHistory.async_count(db, status=status)

    return _SchemaResponse(
        success=True,
        data={
            "list": [item.to_dict() for item in result],
            "total": total,
        },
    )


@router.delete("/transfer", summary="删除整理记录", response_model=_SchemaResponse[None])
def delete_transfer_history(
    history_in: _SchemaTransferHistory,
    deletesrc: Optional[bool] = False,
    deletedest: Optional[bool] = False,
    command: TransferHistoryMutationCommand = Depends(
        get_transfer_history_mutation_command
    ),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    删除整理记录。
    """
    result = command.delete(
        history_in.id,
        delete_source=bool(deletesrc),
        delete_destination=bool(deletedest),
    )
    return _SchemaResponse(success=result.success, message=result.message)


@router.post(
    "/transfer/{history_id}/ai-redo",
    summary="智能助手重新整理",
    response_model=_SchemaResponse[_SchemaProgressKeyData],
)
def ai_redo_transfer_history(
    history_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    手动触发单条历史记录的 AI 重新整理，并返回进度键。
    """
    if not settings.AI_AGENT_ENABLE:
        return _SchemaResponse(success=False, message="MoviePilot智能助手未启用")

    history = TransferHistory.get(db, history_id)
    if not history:
        return _SchemaResponse(success=False, message="整理记录不存在")

    prompt = build_manual_redo_prompt(history)
    progress_key = f"ai_redo_transfer_{history_id}_{int(time.time() * 1000)}"
    _start_ai_redo_task(
        history_id=history_id,
        prompt=prompt,
        progress_key=progress_key,
    )

    return _SchemaResponse(success=True, data={"progress_key": progress_key})


@router.post(
    "/transfer/ai-redo",
    summary="智能助手批量重新整理",
    response_model=_SchemaResponse[_SchemaBatchProgressKeyData],
)
def batch_ai_redo_transfer_history(
    payload: _SchemaBatchTransferHistoryRedoRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    手动触发多条历史记录的 AI 批量重新整理，并返回进度键。
    """
    if not settings.AI_AGENT_ENABLE:
        return _SchemaResponse(success=False, message="MoviePilot智能助手未启用")

    history_ids = normalize_history_ids(payload.history_ids)
    if not history_ids:
        return _SchemaResponse(success=False, message="未提供有效的整理记录")

    histories = []
    missing_ids = []
    for history_id in history_ids:
        history = TransferHistory.get(db, history_id)
        if not history:
            missing_ids.append(history_id)
            continue
        histories.append(history)

    if missing_ids:
        return _SchemaResponse(
            success=False,
            message="整理记录不存在: "
            + ", ".join(str(history_id) for history_id in missing_ids),
        )

    prompt = build_batch_manual_redo_prompt(histories)
    progress_key = f"ai_redo_transfer_batch_{int(time.time() * 1000)}"
    _start_batch_ai_redo_task(
        history_ids=history_ids,
        prompt=prompt,
        progress_key=progress_key,
    )

    return _SchemaResponse(
        success=True,
        data={"progress_key": progress_key, "history_ids": history_ids},
    )


@router.get(
    "/empty/transfer",
    summary="清空整理记录",
    response_model=_SchemaResponse[None],
)
def empty_transfer_history(
    command: TransferHistoryMutationCommand = Depends(
        get_transfer_history_mutation_command
    ),
    _: User = Depends(get_current_active_superuser),
) -> Any:
    """
    清空整理记录
    """
    result = command.truncate()
    return _SchemaResponse(success=result.success, message=result.message)
