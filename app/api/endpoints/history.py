import asyncio
import time
from collections.abc import Coroutine
from typing import Any, Callable, List, Optional

from fastapi import Depends, Response

from app.adapters.web.security.access import verify_token
from app.agent.contracts import ReplyMode
from app.agent.prompt.transfer import (
    build_batch_manual_redo_prompt,
    build_manual_redo_prompt,
)
from app.api.context import (
    get_api_runtime_config,
    get_background_task_registry,
    resolve_api_runtime_config,
    resolve_background_task_registry,
)
from app.api.dependencies.auth import (
    get_current_active_manage_user,
    get_current_active_superuser,
)
from app.api.dependencies.history import (
    get_download_history_mutation_command,
    get_history_query_service,
    get_transfer_execution_repository,
    get_transfer_history_mutation_command,
)
from app.api.response import (
    COLLECTION_TOTAL_HEADER,
    COLLECTION_TOTAL_OPENAPI_KEY,
    ResponseAPIRouter,
)
from app.application.agent import get_running_agent_manager
from app.application.configuration import ApiRuntimeConfig
from app.application.history import (
    DownloadHistoryMutationCommand,
    HistoryQueryService,
    TransferHistoryMutationCommand,
)
from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionRepository,
    TransferRetryRequestResult,
)
from app.runtime.errors import public_error_message
from app.runtime.log import logger
from app.runtime.loop import main_loop_registry
from app.runtime.progress import AsyncProgressHelper
from app.runtime.tasks import TaskRegistry
from app.schemas.common import BatchProgressKeyData as _SchemaBatchProgressKeyData
from app.schemas.common import ProgressKeyData as _SchemaProgressKeyData
from app.schemas.history import BatchTransferHistoryRedoRequest as _SchemaBatchTransferHistoryRedoRequest
from app.schemas.history import DownloadHistory as _SchemaDownloadHistory
from app.schemas.history import TransferHistory as _SchemaTransferHistory
from app.schemas.history import TransferHistoryDeleteResult as _SchemaTransferHistoryDeleteResult
from app.schemas.history import TransferHistoryPage as _SchemaTransferHistoryPage
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload

router = ResponseAPIRouter()


def normalize_history_ids(history_ids: list[int]) -> list[int]:
    """对输入的历史记录 ID 列表进行规范化处理，去除重复项并保持原有顺序。"""
    normalized_ids: list[int] = []
    for history_id in history_ids:
        if history_id not in normalized_ids:
            normalized_ids.append(history_id)
    return normalized_ids


def _request_durable_transfer_retry(
    *,
    history_id: int,
    task_id: str,
    requested_by: str,
    repository: TransferExecutionRepository,
) -> TransferRetryRequestResult:
    """把 durable 历史重试交给唯一持久调度器，不在请求线程执行整理。"""
    return TransferExecutionCommand(repository).request_retry(
        task_id=task_id,
        reason=f"AI REST 请求重试整理历史 #{history_id}",
        requested_by=requested_by,
    )


def _format_retry_rejections(
    rejections: list[tuple[int, TransferRetryRequestResult]],
) -> str:
    """把批量整理重试拒绝原因格式化为前端可直接理解的提示。"""
    return "；".join(
        f"第 {history_id} 条：{public_error_message(result.message, context='transfer')}"
        for history_id, result in rejections
    )


def _partition_durable_histories(
    histories: list[_SchemaTransferHistory],
) -> tuple[list[_SchemaTransferHistory], list[_SchemaTransferHistory]]:
    """按是否绑定持久任务回执分离 durable 与旧整理历史。"""
    return (
        [history for history in histories if history.transfer_task_id],
        [history for history in histories if not history.transfer_task_id],
    )


async def _request_batch_durable_retries(
    histories: list[_SchemaTransferHistory],
    repository: TransferExecutionRepository,
) -> tuple[int, list[tuple[int, TransferRetryRequestResult]]]:
    """逐任务登记 durable 重试并保留每条拒绝的稳定状态。"""
    accepted_count = 0
    rejections: list[tuple[int, TransferRetryRequestResult]] = []
    for history in histories:
        retry = await asyncio.to_thread(
            _request_durable_transfer_retry,
            history_id=history.id,
            task_id=history.transfer_task_id or "",
            requested_by="history_ai_redo_batch",
            repository=repository,
        )
        if retry.accepted:
            accepted_count += 1
        else:
            rejections.append((history.id, retry))
    return accepted_count, rejections


def _durable_retry_messages(
    *,
    accepted_count: int,
    rejections: list[tuple[int, TransferRetryRequestResult]],
) -> list[str]:
    """构造 durable 批量登记结果消息，供纯 durable 和混合请求复用。"""
    messages: list[str] = []
    if accepted_count:
        messages.append(f"已提交 {accepted_count} 个整理任务，后台将自动处理")
    if rejections:
        messages.append("以下整理记录未能提交重试：" + _format_retry_rejections(rejections))
    return messages


async def _complete_durable_retry_batch(
    *,
    histories: list[_SchemaTransferHistory],
    messages: list[str],
    rejections: list[tuple[int, TransferRetryRequestResult]],
) -> Any:
    """完成纯 durable 批量响应；存在拒绝时不伪造成功进度。"""
    message = "；".join(messages)
    if rejections:
        return _SchemaResponse(success=False, message=message)
    progress_key = f"transfer_retry_batch_{int(time.time() * 1000)}"
    history_ids = [history.id for history in histories]
    await _complete_durable_retry_progress(
        progress_key=progress_key,
        text=message,
        history_ids=history_ids,
    )
    return _SchemaResponse(
        success=True,
        message=message,
        data={"progress_key": progress_key, "history_ids": history_ids},
    )


async def _complete_durable_retry_progress(
    *,
    progress_key: str,
    text: str,
    history_ids: list[int],
) -> None:
    """写入可被现有 SSE 客户端立即消费的 durable 重试完成进度。"""
    progress = AsyncProgressHelper(progress_key)
    await progress.start()
    await progress.end(
        text=text,
        data={
            "history_ids": history_ids,
            "success": True,
            "completed": True,
            "message": text,
        },
    )


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
            loop=main_loop_registry.require(),
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
            logger.error(f"智能助手后台整理失败：{e}", exc_info=True)
            await progress.update(
                text="智能助手整理失败，请稍后重试",
                data={
                    "history_id": history_id,
                    "success": False,
                    "completed": True,
                    "error": "智能助手整理失败，请稍后重试",
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
            loop=main_loop_registry.require(),
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
            logger.error(f"智能助手后台批量整理失败：{e}", exc_info=True)
            await progress.update(
                text="智能助手批量整理失败，请稍后重试",
                data={
                    "history_ids": history_ids,
                    "success": False,
                    "completed": True,
                    "error": "智能助手批量整理失败，请稍后重试",
                },
            )
        finally:
            await progress.end()

    registry.create(runner(), owner="api.history.ai_redo_batch")


def _submit_legacy_batch_ai_redo(
    *,
    histories: list[_SchemaTransferHistory],
    all_history_ids: list[int],
    messages: list[str],
    task_registry: TaskRegistry,
) -> _SchemaResponse[_SchemaBatchProgressKeyData]:
    """提交旧整理历史给 Agent，并构造批量进度兼容响应。"""
    legacy_history_ids = [history.id for history in histories]
    progress_key = f"ai_redo_transfer_batch_{int(time.time() * 1000)}"
    _start_batch_ai_redo_task(
        history_ids=legacy_history_ids,
        prompt=build_batch_manual_redo_prompt(histories),
        progress_key=progress_key,
        task_registry=task_registry,
    )
    message = "；".join(
        [*messages, f"已提交 {len(histories)} 条旧历史给智能助手处理"]
    )
    return _SchemaResponse(
        success=True,
        message=message,
        data={
            "progress_key": progress_key,
            "history_ids": all_history_ids,
        },
    )


@router.get(
    "/download",
    summary="查询下载历史记录",
    response_model=List[_SchemaDownloadHistory],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def download_history(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    query: HistoryQueryService = Depends(get_history_query_service),
    _: _SchemaTokenPayload = Depends(verify_token),
    response: Response = None,
) -> Any:
    """
    按下载时间倒序查询下载历史记录
    """
    results = await query.list_download(page=page, count=count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_download()
        )
    return results


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


@router.delete(
    "/transfer",
    summary="删除整理记录",
    response_model=_SchemaResponse[_SchemaTransferHistoryDeleteResult],
)
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
    return _SchemaResponse(
        success=result.success,
        message=result.message,
        data=result,
    )


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
    execution_repository: TransferExecutionRepository = Depends(
        get_transfer_execution_repository
    ),
) -> Any:
    """
    手动触发单条历史记录的 AI 重新整理，并返回进度键。
    """
    runtime_config = resolve_api_runtime_config(runtime_config)
    history = await query.get_transfer(history_id)
    if not history:
        return _SchemaResponse(success=False, message="整理记录不存在")

    if history.transfer_task_id:
        retry = await asyncio.to_thread(
            _request_durable_transfer_retry,
            history_id=history.id,
            task_id=history.transfer_task_id,
            requested_by="history_ai_redo",
            repository=execution_repository,
        )
        if not retry.accepted:
            retry_message = public_error_message(retry.message, context="transfer")
            return _SchemaResponse(success=False, message=retry_message)
        retry_message = public_error_message(retry.message, context="transfer")
        progress_key = f"transfer_retry_{history_id}_{int(time.time() * 1000)}"
        await _complete_durable_retry_progress(
            progress_key=progress_key,
            text=retry_message,
            history_ids=[history.id],
        )
        return _SchemaResponse(
            success=True,
            message=retry_message,
            data={"progress_key": progress_key},
        )

    if not runtime_config.ai_agent_enable:
        return _SchemaResponse(success=False, message="MoviePilot智能助手未启用")

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
    execution_repository: TransferExecutionRepository = Depends(
        get_transfer_execution_repository
    ),
) -> Any:
    """
    手动触发多条历史记录的 AI 批量重新整理，并返回进度键。
    """
    runtime_config = resolve_api_runtime_config(runtime_config)
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

    durable_histories, legacy_histories = _partition_durable_histories(histories)
    accepted_count, rejections = await _request_batch_durable_retries(
        durable_histories,
        execution_repository,
    )
    response_message_parts = _durable_retry_messages(
        accepted_count=accepted_count,
        rejections=rejections,
    )

    if not legacy_histories:
        return await _complete_durable_retry_batch(
            histories=durable_histories,
            messages=response_message_parts,
            rejections=rejections,
        )

    if rejections:
        response_message_parts.append(
            f"{len(legacy_histories)} 条旧历史未提交：批量请求包含被拒绝的持久任务"
        )
        return _SchemaResponse(
            success=False,
            message="；".join(response_message_parts),
        )

    if not runtime_config.ai_agent_enable:
        response_message_parts.append(
            f"{len(legacy_histories)} 条旧历史未处理：MoviePilot智能助手未启用"
        )
        return _SchemaResponse(
            success=False,
            message="；".join(response_message_parts),
        )

    return _submit_legacy_batch_ai_redo(
        histories=legacy_histories,
        all_history_ids=[history.id for history in histories],
        messages=response_message_parts,
        task_registry=task_registry,
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
