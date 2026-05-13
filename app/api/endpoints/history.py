import asyncio
import time
from pathlib import Path
from typing import List, Any, Optional

import jieba
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app import schemas
from app.agent import ReplyMode, prompt_manager, agent_manager
from app.chain.storage import StorageChain
from app.core.config import settings, global_vars
from app.core.event import eventmanager
from app.core.security import verify_token
from app.db import get_async_db, get_db
from app.db.models import User
from app.db.models.downloadhistory import DownloadHistory, DownloadFiles
from app.db.models.transferhistory import TransferHistory
from app.db.user_oper import (
    get_current_active_superuser_async,
    get_current_active_superuser,
)
from app.helper.progress import ProgressHelper
from app.schemas.types import EventType

router = APIRouter()


def normalize_history_ids(history_ids: list[int]) -> list[int]:
    """对输入的历史记录 ID 列表进行规范化处理，去除重复项并保持原有顺序。"""
    normalized_ids: list[int] = []
    for history_id in history_ids:
        if history_id not in normalized_ids:
            normalized_ids.append(history_id)
    return normalized_ids


def build_manual_redo_template_context(
    history: TransferHistory,
) -> dict[str, int | str]:
    """仅负责把整理历史对象映射成 System Tasks 需要的模板变量。"""
    src_fileitem = history.src_fileitem or {}
    source_path = src_fileitem.get("path") if isinstance(src_fileitem, dict) else ""
    source_path = source_path or history.src or ""
    season_episode = f"{history.seasons or ''}{history.episodes or ''}".strip()
    return {
        "history_id": history.id,
        "current_status": "success" if history.status else "failed",
        "recognized_title": history.title or "unknown",
        "media_type": history.type or "unknown",
        "category": history.category or "unknown",
        "year": history.year or "unknown",
        "season_episode": season_episode or "unknown",
        "source_path": source_path or "unknown",
        "source_storage": history.src_storage or "local",
        "destination_path": history.dest or "unknown",
        "destination_storage": history.dest_storage or "unknown",
        "transfer_mode": history.mode or "unknown",
        "tmdbid": history.tmdbid or "none",
        "doubanid": history.doubanid or "none",
        "error_message": history.errmsg or "none",
    }


def format_manual_redo_record_context(history: Any) -> str:
    """把单条整理记录格式化为批量任务可直接消费的上下文块。"""
    context = build_manual_redo_template_context(history)
    return "\n".join(
        [
            f"Record #{context['history_id']}:",
            f"- Current status: {context['current_status']}",
            f"- Current recognized title: {context['recognized_title']}",
            f"- Media type: {context['media_type']}",
            f"- Category: {context['category']}",
            f"- Year: {context['year']}",
            f"- Season/Episode: {context['season_episode']}",
            f"- Source path: {context['source_path']}",
            f"- Source storage: {context['source_storage']}",
            f"- Destination path: {context['destination_path']}",
            f"- Destination storage: {context['destination_storage']}",
            f"- Transfer mode: {context['transfer_mode']}",
            f"- Current TMDB ID: {context['tmdbid']}",
            f"- Current Douban ID: {context['doubanid']}",
            f"- Error message: {context['error_message']}",
        ]
    )


def build_manual_redo_prompt(history: Any) -> str:
    """构建手动 AI 整理提示词。"""
    return prompt_manager.render_system_task_message(
        "manual_transfer_redo",
        template_context=build_manual_redo_template_context(history),
    )


def build_batch_manual_redo_template_context(
    histories: list[Any],
) -> dict[str, int | str]:
    """仅负责把多条整理历史对象映射成批量 System Tasks 需要的模板变量。"""
    return {
        "history_ids_csv": ", ".join(str(history.id) for history in histories),
        "history_count": len(histories),
        "records_context": "\n\n".join(
            format_manual_redo_record_context(history) for history in histories
        ),
    }


def build_batch_manual_redo_prompt(histories: list[Any]) -> str:
    """构建批量手动 AI 整理提示词。"""
    return prompt_manager.render_system_task_message(
        "batch_manual_transfer_redo",
        template_context=build_batch_manual_redo_template_context(histories),
    )


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
            await agent_manager.run_background_prompt(
                message=prompt,
                session_prefix=f"__agent_manual_redo_{history_id}",
                output_callback=update_output,
                reply_mode=ReplyMode.CAPTURE_ONLY,
                persist_output_message=False,
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
            await agent_manager.run_background_prompt(
                message=prompt,
                session_prefix="__agent_manual_redo_batch",
                output_callback=update_output,
                reply_mode=ReplyMode.CAPTURE_ONLY,
                persist_output_message=False,
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
    response_model=List[schemas.DownloadHistory],
)
async def download_history(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    db: AsyncSession = Depends(get_async_db),
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    查询下载历史记录
    """
    return await DownloadHistory.async_list_by_page(db, page, count)


@router.delete("/download", summary="删除下载历史记录", response_model=schemas.Response)
async def delete_download_history(
    history_in: schemas.DownloadHistory,
    db: AsyncSession = Depends(get_async_db),
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    删除下载历史记录
    """
    await DownloadHistory.async_delete(db, history_in.id)
    return schemas.Response(success=True)


def _glob_to_like(pattern: str) -> str:
    """
    将 glob 通配符模式转换为 SQL LIKE 模式（使用 \\ 作为转义字符）
    """
    result = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return result.replace("*", "%").replace("?", "_")


@router.get("/transfer", summary="查询整理记录", response_model=schemas.Response)
async def transfer_history(
    title: Optional[str] = None,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    status: Optional[bool] = None,
    db: AsyncSession = Depends(get_async_db),
    _: schemas.TokenPayload = Depends(verify_token),
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
            words = jieba.cut(title, HMM=False)
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

    return schemas.Response(
        success=True,
        data={
            "list": [item.to_dict() for item in result],
            "total": total,
        },
    )


@router.delete("/transfer", summary="删除整理记录", response_model=schemas.Response)
def delete_transfer_history(
    history_in: schemas.TransferHistory,
    deletesrc: Optional[bool] = False,
    deletedest: Optional[bool] = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_superuser),
) -> Any:
    """
    删除整理记录
    """
    history: TransferHistory = TransferHistory.get(db, history_in.id)
    if not history:
        return schemas.Response(success=False, message="记录不存在")
    # 册除媒体库文件
    if deletedest and history.dest_fileitem:
        dest_fileitem = schemas.FileItem(**history.dest_fileitem)
        StorageChain().delete_media_file(dest_fileitem)

    # 删除源文件
    if deletesrc and history.src_fileitem:
        src_fileitem = schemas.FileItem(**history.src_fileitem)
        state = StorageChain().delete_media_file(src_fileitem)
        if not state:
            return schemas.Response(
                success=False, message=f"{src_fileitem.path} 删除失败"
            )
        # 删除下载记录中关联的文件
        DownloadFiles.delete_by_fullpath(db, Path(src_fileitem.path).as_posix())
        # 发送事件
        eventmanager.send_event(
            EventType.DownloadFileDeleted,
            {"src": history.src, "hash": history.download_hash},
        )
    # 删除记录
    TransferHistory.delete(db, history_in.id)
    return schemas.Response(success=True)


@router.post(
    "/transfer/{history_id}/ai-redo",
    summary="智能助手重新整理",
    response_model=schemas.Response,
)
def ai_redo_transfer_history(
    history_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_superuser),
) -> Any:
    """
    手动触发单条历史记录的 AI 重新整理，并返回进度键。
    """
    if not settings.AI_AGENT_ENABLE:
        return schemas.Response(success=False, message="MoviePilot智能助手未启用")

    history = TransferHistory.get(db, history_id)
    if not history:
        return schemas.Response(success=False, message="整理记录不存在")

    prompt = build_manual_redo_prompt(history)
    progress_key = f"ai_redo_transfer_{history_id}_{int(time.time() * 1000)}"
    _start_ai_redo_task(
        history_id=history_id,
        prompt=prompt,
        progress_key=progress_key,
    )

    return schemas.Response(success=True, data={"progress_key": progress_key})


@router.post(
    "/transfer/ai-redo", summary="智能助手批量重新整理", response_model=schemas.Response
)
def batch_ai_redo_transfer_history(
    payload: schemas.BatchTransferHistoryRedoRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_superuser),
) -> Any:
    """
    手动触发多条历史记录的 AI 批量重新整理，并返回进度键。
    """
    if not settings.AI_AGENT_ENABLE:
        return schemas.Response(success=False, message="MoviePilot智能助手未启用")

    history_ids = normalize_history_ids(payload.history_ids)
    if not history_ids:
        return schemas.Response(success=False, message="未提供有效的整理记录")

    histories = []
    missing_ids = []
    for history_id in history_ids:
        history = TransferHistory.get(db, history_id)
        if not history:
            missing_ids.append(history_id)
            continue
        histories.append(history)

    if missing_ids:
        return schemas.Response(
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

    return schemas.Response(
        success=True,
        data={"progress_key": progress_key, "history_ids": history_ids},
    )


@router.get("/empty/transfer", summary="清空整理记录", response_model=schemas.Response)
async def empty_transfer_history(
    db: AsyncSession = Depends(get_async_db),
    _: User = Depends(get_current_active_superuser_async),
) -> Any:
    """
    清空整理记录
    """
    await TransferHistory.async_truncate(db)
    return schemas.Response(success=True)
