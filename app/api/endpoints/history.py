"""
提供下载与整理历史查询、清理确认和整理恢复接口。
"""

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
)
from app.application.transfer.recovery import TransferRecoveryCommand, ai_takeover_rejection
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
from app.schemas.history import TransferHistoryDiscardResult as _SchemaTransferHistoryDiscardResult
from app.schemas.history import TransferHistoryPage as _SchemaTransferHistoryPage
from app.schemas.history import TransferHistoryReplanRequest as _SchemaTransferHistoryReplanRequest
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload

router = ResponseAPIRouter()


def normalize_history_ids(history_ids: list[int]) -> list[int]:
    """
    对输入的历史记录 ID 列表进行规范化处理，去除重复项并保持原有顺序。
    """
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
    """
    构造同步 Agent 输出回调，并把异步进度更新登记到宿主任务生命周期。
    """

    def update_output(text: str) -> None:
        """
        非阻塞提交一条进度更新，避免同步回调等待缓存 I/O。
        """
        submit(progress.update(text=text, data=data))

    return update_output


def _start_ai_redo_task(
    history_id: int,
    prompt: str,
    progress_key: str,
    task_registry: TaskRegistry | None = None,
) -> None:
    """
    在后台任务中启动单条 AI 重新整理任务，并通过异步进度辅助类实时更新进度。
    """
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

    async def runner() -> None:
        """
        执行已登记的后台整理重试并更新进度，保证异常也能收口。
        """
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
                text="智能助手分析完成，请查看整理历史确认文件处理结果",
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
    """
    在后台任务中启动批量 AI 重新整理任务，并通过异步进度辅助类实时更新进度。
    """
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

    async def runner() -> None:
        """
        执行已登记的后台整理重试并更新进度，保证异常也能收口。
        """
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
                text="智能助手批量分析完成，请查看整理历史确认文件处理结果",
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


def _submit_batch_ai_redo(
    *,
    histories: list[_SchemaTransferHistory],
    all_history_ids: list[int],
    messages: list[str],
    task_registry: TaskRegistry,
) -> _SchemaResponse[dict[str, Any]]:
    """
    提交全部已通过状态校验的历史给 Agent，并构造批量进度响应。
    """
    history_ids = [history.id for history in histories]
    progress_key = f"ai_redo_transfer_batch_{int(time.time() * 1000)}"
    _start_batch_ai_redo_task(
        history_ids=history_ids,
        prompt=build_batch_manual_redo_prompt(histories),
        progress_key=progress_key,
        task_registry=task_registry,
    )
    message = "；".join([*messages, f"已提交 {len(histories)} 条历史给智能助手处理"])
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
    response: Response = None,  # type: ignore[assignment]
) -> Any:
    """
    按下载时间倒序查询下载历史记录
    """
    page = page if page is not None else 1
    count = count if count is not None else 30
    results = await query.list_download(page=page, count=count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(await query.count_download())
    return results


@router.delete(
    "/download",
    summary="删除下载历史记录",
    response_model=_SchemaResponse[None],
)
def delete_download_history(
    history_in: _SchemaDownloadHistory,
    command: DownloadHistoryMutationCommand = Depends(get_download_history_mutation_command),
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
    page = page if page is not None else 1
    count = count if count is not None else 30
    result = await query.list_transfer(
        title=title,
        page=page,
        count=count,
        status=status,
    )
    return _SchemaResponse(success=True, data=result)


def _clear_transfer_history(
    command: TransferHistoryMutationCommand,
) -> _SchemaResponse[None]:
    """
    执行旧整理历史清理，并统一构造兼容响应。
    """
    result = command.truncate()
    return _SchemaResponse(success=result.success, message=result.message)


@router.delete(
    "/transfer/all",
    summary="清空旧整理记录",
    response_model=_SchemaResponse[None],
)
def clear_transfer_history(
    command: TransferHistoryMutationCommand = Depends(get_transfer_history_mutation_command),
    _: object = Depends(get_current_active_superuser),
) -> Any:
    """
    清空没有持久任务回执的旧整理记录，不删除任何文件。
    """
    return _clear_transfer_history(command)


@router.delete(
    "/transfer",
    summary="删除整理记录",
    response_model=_SchemaResponse[_SchemaTransferHistoryDeleteResult],
)
def delete_transfer_history(
    history_in: _SchemaTransferHistory,
    deletesrc: Optional[bool] = False,
    deletedest: Optional[bool] = False,
    command: TransferHistoryMutationCommand = Depends(get_transfer_history_mutation_command),
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


# FastAPI 装饰器在当前 mypy 配置下无类型；数据仍由显式响应模型和命令契约约束。
@router.post(
    "/transfer/{history_id}/cleanup-resolved",
    summary="确认下载器清理已完成",
    response_model=_SchemaResponse[None],
)
def resolve_transfer_cleanup(
    history_id: int,
    command: TransferHistoryMutationCommand = Depends(get_transfer_history_mutation_command),
    _: object = Depends(get_current_active_manage_user),
) -> Any:
    """
    确认用户已在下载器中人工完成清理，并关闭该历史的清理失败提示。
    """
    result = command.resolve_cleanup(history_id)
    return _SchemaResponse(success=result.success, message=result.message)


async def _get_discard_transfer_history(
    history_id: int,
    query: HistoryQueryService = Depends(get_history_query_service),
) -> Optional[_SchemaTransferHistory]:
    """
    异步加载历史 DTO，随后由 FastAPI 工作线程执行同步原子清理。
    """
    return await query.get_transfer(history_id)


# FastAPI 装饰器在当前 mypy 配置下视为无类型，响应仍由具体 Pydantic 模型约束。
@router.post(
    "/transfer/{history_id}/discard-corrupt",
    summary="放弃损坏的整理任务",
    response_model=_SchemaResponse[_SchemaTransferHistoryDiscardResult],
)
def discard_corrupt_transfer_history(
    history_id: int,
    history: Optional[_SchemaTransferHistory] = Depends(_get_discard_transfer_history),
    execution_repository: TransferExecutionRepository = Depends(get_transfer_execution_repository),
    _: object = Depends(get_current_active_manage_user),
) -> Any:
    """
    清理无活动租约的损坏 durable 任务，保留历史供重新生成计划。
    """
    if not history:
        return _SchemaResponse(success=False, message="整理记录不存在")
    if not history.transfer_task_id:
        return _SchemaResponse(
            success=True, message="整理任务已清理",
            data=_SchemaTransferHistoryDiscardResult(history_id=history_id),
        )
    result = TransferRecoveryCommand(execution_repository).discard_corrupt_by_history(
        task_id=history.transfer_task_id,
        history_id=history_id,
    )
    return _SchemaResponse(
        success=result.discarded, message=result.message,
        data=_SchemaTransferHistoryDiscardResult(history_id=history_id),
    )


@router.post(
    "/transfer/{history_id}/replan",
    summary="修正识别失败的整理计划",
    response_model=_SchemaResponse[None],
)
def replan_transfer_history(
    history_id: int,
    payload: _SchemaTransferHistoryReplanRequest,
    execution_repository: TransferExecutionRepository = Depends(get_transfer_execution_repository),
    _: object = Depends(get_current_active_manage_user),
) -> Any:
    """
    对已核实身份且没有文件副作用的识别拒绝重新规划，保留同一任务、原始证据及重试预算。

    :param history_id: 当前失败回执的历史标识
    :param payload: 经核实的媒体身份和可选季集修正
    :param execution_repository: 拥有短事务和恢复状态校验的执行仓储
    :param _: 已验证的管理用户
    :return: 受理状态；排队不代表文件已整理成功
    """
    from app.application.transfer.execution import TransferExecutionConflictError

    try:
        result = TransferExecutionCommand(execution_repository).request_replan(
            history_id=history_id, task_id=payload.task_id, settlement_revision=payload.settlement_revision,
            media_source=payload.media_source.value,
            media_id=payload.media_id, media_type=payload.media_type,
            season=payload.season, episode=payload.episode, requested_by="history_ai_replan",
        )
    except (TransferExecutionConflictError, ValueError) as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=result.accepted, message=result.message)


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
    execution_repository: TransferExecutionRepository = Depends(get_transfer_execution_repository),
) -> Any:
    """
    手动触发单条历史记录的 AI 重新整理，并返回进度键。
    """
    runtime_config = resolve_api_runtime_config(runtime_config)
    history = await query.get_transfer(history_id)
    if not history:
        return _SchemaResponse(success=False, message="整理记录不存在")

    if not runtime_config.ai_agent_enable:
        return _SchemaResponse(success=False, message="MoviePilot智能助手未启用")
    rejection = await asyncio.to_thread(
        ai_takeover_rejection, history_id=history.id, task_id=history.transfer_task_id,
        repository=execution_repository,
    )
    if rejection:
        return _SchemaResponse(success=False, message=rejection)

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
    execution_repository: TransferExecutionRepository = Depends(get_transfer_execution_repository),
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
            message="整理记录不存在: " + ", ".join(str(history_id) for history_id in missing_ids),
        )

    if not runtime_config.ai_agent_enable:
        return _SchemaResponse(success=False, message="MoviePilot智能助手未启用")
    for history in histories:
        rejection = await asyncio.to_thread(
            ai_takeover_rejection, history_id=history.id, task_id=history.transfer_task_id,
            repository=execution_repository,
        )
        if rejection:
            return _SchemaResponse(success=False, message=f"第 {history.id} 条：{rejection}")

    return _submit_batch_ai_redo(
        histories=histories,
        all_history_ids=[history.id for history in histories],
        messages=[],
        task_registry=task_registry,
    )


@router.get(
    "/empty/transfer",
    summary="清空整理记录",
    response_model=_SchemaResponse[None],
    include_in_schema=False,
    deprecated=True,
)
def empty_transfer_history(
    command: TransferHistoryMutationCommand = Depends(get_transfer_history_mutation_command),
    _: object = Depends(get_current_active_superuser),
) -> Any:
    """
    兼容旧客户端的清空入口；新调用应使用 DELETE /transfer/all。
    """
    return _clear_transfer_history(command)
