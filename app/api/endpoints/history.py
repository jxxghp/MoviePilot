import time
from collections.abc import Coroutine
from typing import List, Any, Callable, Optional

from fastapi import Depends

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
from app.runtime.config import global_vars
from app.api.context import (
    get_api_runtime_config,
    get_background_task_registry,
    resolve_api_runtime_config,
    resolve_background_task_registry,
)
from app.application.configuration import ApiRuntimeConfig
from app.adapters.web.security.access import verify_token
from app.api.dependencies.auth import (
    get_current_active_manage_user,
    get_current_active_superuser,
)
from app.api.dependencies.history import (
    get_download_history_mutation_command,
    get_history_query_service,
    get_transfer_history_mutation_command,
)
from app.runtime.progress import AsyncProgressHelper
from app.application.history import (
    DownloadHistoryMutationCommand,
    HistoryQueryService,
    TransferHistoryMutationCommand,
)
from app.runtime.log import logger
from app.runtime.tasks import TaskRegistry

router = ResponseAPIRouter()


def normalize_history_ids(history_ids: list[int]) -> list[int]:
    """对输入的历史记录 ID 列表进行规范化处理，去除重复项并保持原有顺序。"""
    normalized_ids: list[int] = []
    for history_id in history_ids:
        if history_id not in normalized_ids:
            normalized_ids.append(history_id)
    return normalized_ids


def _build_progress_output_callback(
    progress: AsyncProgressHelper,
    data: dict[str, Any],
    *,
    submit: Callable[[Coroutine[Any, Any, Any]], object],
) -> Callable[[str], None]:
    """构造同步 Agent 输出回调，并把异步进度更新登记到宿主任务生命周期。"""

    def update_output(text: str) -> None:
        """非阻塞提交一条进度更新，避免同步回调等待缓存 I/O。"""
        submit(progress.update(text=text, data=data))

    return update_output


def _start_ai_redo_task(
    history_id: int,
    prompt: str,
    progress_key: str,
    task_registry: TaskRegistry | None = None,
) -> None:
    """在后台任务中启动单条 AI 重新整理任务，并通过异步进度辅助类实时更新进度。"""
    registry = resolve_background_task_registry(task_registry)
    progress = AsyncProgressHelper(progress_key)
    update_output = _build_progress_output_callback(
        progress,
        {"history_id": history_id},
        submit=lambda coroutine: registry.submit_threadsafe(
            coroutine,
            loop=global_vars.loop,
            owner="api.history.ai_redo.progress",
        ),
    )

    async def runner():
        try:
            await progress.start()
            await progress.update(
                text=f"智能助手正在准备整理记录 #{history_id} ...",
                data={"history_id": history_id, "success": True},
            )
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
            await progress.update(
                text="智能助手整理完成",
                data={"history_id": history_id, "success": True, "completed": True},
            )
        except Exception as e:
            await progress.update(
                text=f"智能助手整理失败：{str(e)}",
                data={
                    "history_id": history_id,
                    "success": False,
                    "completed": True,
                    "error": str(e),
                },
            )
        finally:
            await progress.end()

    registry.create(runner(), owner="api.history.ai_redo")


def _start_batch_ai_redo_task(
    history_ids: list[int],
    prompt: str,
    progress_key: str,
    task_registry: TaskRegistry | None = None,
) -> None:
    """在后台任务中启动批量 AI 重新整理任务，并通过异步进度辅助类实时更新进度。"""
    registry = resolve_background_task_registry(task_registry)
    progress = AsyncProgressHelper(progress_key)
    update_output = _build_progress_output_callback(
        progress,
        {"history_ids": history_ids},
        submit=lambda coroutine: registry.submit_threadsafe(
            coroutine,
            loop=global_vars.loop,
            owner="api.history.ai_redo_batch.progress",
        ),
    )

    async def runner():
        try:
            await progress.start()
            await progress.update(
                text=f"智能助手正在准备批量整理 {len(history_ids)} 条记录 ...",
                data={"history_ids": history_ids, "success": True},
            )
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
            await progress.update(
                text="智能助手批量整理完成",
                data={"history_ids": history_ids, "success": True, "completed": True},
            )
        except Exception as e:
            await progress.update(
                text=f"智能助手批量整理失败：{str(e)}",
                data={
                    "history_ids": history_ids,
                    "success": False,
                    "completed": True,
                    "error": str(e),
                },
            )
        finally:
            await progress.end()

    registry.create(runner(), owner="api.history.ai_redo_batch")


@router.get(
    "/download",
    summary="查询下载历史记录",
    response_model=List[_SchemaDownloadHistory],
)
async def download_history(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    query: HistoryQueryService = Depends(get_history_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    按下载时间倒序查询下载历史记录
    """
    return await query.list_download(page=page, count=count)


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
    query: HistoryQueryService = Depends(get_history_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    查询整理记录，title 支持通配符 * 和 ?（如 *.mkv、*2024*）
    """
    result = await query.list_transfer(
        title=title,
        page=page,
        count=count,
        status=status,
    )
    return _SchemaResponse(success=True, data=result)


@router.delete("/transfer", summary="删除整理记录", response_model=_SchemaResponse[None])
def delete_transfer_history(
    history_in: _SchemaTransferHistory,
    deletesrc: Optional[bool] = False,
    deletedest: Optional[bool] = False,
    command: TransferHistoryMutationCommand = Depends(
        get_transfer_history_mutation_command
    ),
    _: object = Depends(get_current_active_manage_user),
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
async def ai_redo_transfer_history(
    history_id: int,
    query: HistoryQueryService = Depends(get_history_query_service),
    runtime_config: ApiRuntimeConfig = Depends(get_api_runtime_config),
    _: object = Depends(get_current_active_manage_user),
    task_registry: TaskRegistry = Depends(get_background_task_registry),
) -> Any:
    """
    手动触发单条历史记录的 AI 重新整理，并返回进度键。
    """
    runtime_config = resolve_api_runtime_config(runtime_config)
    if not runtime_config.ai_agent_enable:
        return _SchemaResponse(success=False, message="MoviePilot智能助手未启用")

    history = await query.get_transfer(history_id)
    if not history:
        return _SchemaResponse(success=False, message="整理记录不存在")

    prompt = build_manual_redo_prompt(history)
    progress_key = f"ai_redo_transfer_{history_id}_{int(time.time() * 1000)}"
    _start_ai_redo_task(
        history_id=history_id,
        prompt=prompt,
        progress_key=progress_key,
        task_registry=task_registry,
    )

    return _SchemaResponse(success=True, data={"progress_key": progress_key})


@router.post(
    "/transfer/ai-redo",
    summary="智能助手批量重新整理",
    response_model=_SchemaResponse[_SchemaBatchProgressKeyData],
)
async def batch_ai_redo_transfer_history(
    payload: _SchemaBatchTransferHistoryRedoRequest,
    query: HistoryQueryService = Depends(get_history_query_service),
    runtime_config: ApiRuntimeConfig = Depends(get_api_runtime_config),
    _: object = Depends(get_current_active_manage_user),
    task_registry: TaskRegistry = Depends(get_background_task_registry),
) -> Any:
    """
    手动触发多条历史记录的 AI 批量重新整理，并返回进度键。
    """
    runtime_config = resolve_api_runtime_config(runtime_config)
    if not runtime_config.ai_agent_enable:
        return _SchemaResponse(success=False, message="MoviePilot智能助手未启用")

    history_ids = normalize_history_ids(payload.history_ids)
    if not history_ids:
        return _SchemaResponse(success=False, message="未提供有效的整理记录")

    histories, missing_ids = await query.get_transfers(history_ids)

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
        task_registry=task_registry,
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
    _: object = Depends(get_current_active_superuser),
) -> Any:
    """
    清空整理记录
    """
    result = command.truncate()
    return _SchemaResponse(success=result.success, message=result.message)
