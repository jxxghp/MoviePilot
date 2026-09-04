from pathlib import Path
from typing import Annotated, Any, List, Literal, Optional, cast

from fastapi import Depends, HTTPException, Query, status

from app.adapters.web.security.access import verify_apitoken, verify_token
from app.api.dependencies.auth import get_current_active_manage_user
from app.api.dependencies.history import get_transfer_execution_repository, get_transfer_history_lookup_service
from app.api.response import (
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application.configuration import get_api_runtime_config_snapshot
from app.application.directory import DirectoryHelper
from app.application.history import TransferHistoryLookupService
from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionConflictError,
    TransferExecutionRepository,
    TransferExecutionState,
    TransferManualReviewDecision,
    TransferManualReviewQuery,
    TransferManualReviewTaskView,
    TransferStepResult,
)
from app.chain.media import MediaChain
from app.chain.transfer.facade import TransferChain
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.common import NameData as _SchemaNameData
from app.schemas.response import Response as _SchemaResponse
from app.schemas.system import TransferDirectoryConf as _SchemaTransferDirectoryConf
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.transfer import EpisodeFormat as _SchemaEpisodeFormat
from app.schemas.transfer import EpisodeFormatRecommendData as _SchemaEpisodeFormatRecommendData
from app.schemas.transfer import EpisodeFormatRecommendItem, ManualTransferItem
from app.schemas.transfer import ManualTransferHistoryInfo as _SchemaManualTransferHistoryInfo
from app.schemas.transfer import ManualTransferResultData as _SchemaManualTransferResultData
from app.schemas.transfer import ManualTransferTargetPath as _SchemaManualTransferTargetPath
from app.schemas.transfer import TransferJob as _SchemaTransferJob
from app.schemas.transfer import TransferManualReviewData as _SchemaTransferManualReviewData
from app.schemas.transfer import TransferManualReviewPageData as _SchemaTransferManualReviewPageData
from app.schemas.transfer import TransferManualReviewRequest as _SchemaTransferManualReviewRequest
from app.schemas.transfer import TransferManualReviewTaskData as _SchemaTransferManualReviewTaskData
from app.schemas.types import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING, MediaType
from app.schemas.workflow import FileItem
from app.schemas.workflow import FileItem as _SchemaFileItem

router = ResponseAPIRouter()


def _public_transfer_message(message: Optional[object]) -> Optional[str]:
    """把整理链返回的错误转换为前端可直接展示的文案。"""
    if message is None or not str(message).strip():
        return None
    from app.runtime.errors import public_error_message

    return public_error_message(message, context="transfer")


def _public_transfer_result(data: dict[str, Any]) -> dict[str, Any]:
    """裁剪整理结果中的错误字段，保留预览数据的原有结构。"""
    result = dict(data)
    if result.get("message"):
        result["message"] = _public_transfer_message(result["message"])
    items = result.get("items")
    if isinstance(items, list):
        result["items"] = [
            {
                **item,
                "message": _public_transfer_message(item.get("message")),
            }
            if isinstance(item, dict)
            else item
            for item in items
        ]
    return result


def _build_failure_preview_item(file_item: FileItem, message: Optional[str]) -> dict:
    """构造手动整理预览失败项。"""
    return {
        "source": file_item.path if file_item else None,
        "target": None,
        "target_dir": None,
        "success": False,
        "message": _public_transfer_message(message),
        "type": None,
        "title": None,
        "season": None,
        "episode": None,
        "episode_end": None,
        "part": None,
        "org_string": None,
        "apply_words": [],
        "resource_team": None,
        "customization": None,
    }


def _merge_transfer_messages(messages: List[str]) -> str:
    """合并手动整理批量预览提示信息，并统一转换错误文案。"""
    valid_messages = [
        public_message
        for msg in messages
        if msg
        for public_message in [_public_transfer_message(msg)]
        if public_message
    ]
    if not valid_messages:
        return ""
    return "、".join(valid_messages[:2]) + (
        f"，等{len(valid_messages)}条消息" if len(valid_messages) > 2 else ""
    )


def _manual_review_actor(current_user: object) -> str:
    """按名称、用户名和用户 ID 的稳定顺序提取人工复核操作者。"""
    for attribute in ("name", "username", "id"):
        value = getattr(current_user, attribute, None)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="当前管理用户缺少可审计身份",
    )


def _manual_review_task_data(
    task: TransferManualReviewTaskView,
) -> _SchemaTransferManualReviewTaskData:
    """把 Application 人工复核投影映射为严格公开响应。"""
    return cast(
        _SchemaTransferManualReviewTaskData,
        _SchemaTransferManualReviewTaskData.model_validate({
            "task_id": task.task_id,
            "source": {
                "storage": task.source.storage,
                "path": task.source.path,
            },
            "state": task.state.value,
            "step": {
                "operation_id": task.step.operation_id,
                "kind": task.step.kind,
                "intent": task.step.intent,
                "evidence": task.step.evidence,
                "error": _public_transfer_message(task.step.error),
            },
            "review_revision": task.review_revision,
        }),
    )


@router.get(  # type: ignore[misc]
    "/tasks/manual-reviews",
    summary="分页查询 durable 整理人工复核任务",
    response_model=_SchemaResponse[_SchemaTransferManualReviewPageData],
)
def list_transfer_manual_reviews(
    state_filter: Literal["manual_review", "retry_wait"] = Query(
        default="manual_review",
        alias="state",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    current_user: object = Depends(get_current_active_manage_user),
    repository: TransferExecutionRepository = Depends(
        get_transfer_execution_repository
    ),
) -> Any:
    """分页返回待复核或已判定等待 durable 恢复的任务。"""
    del current_user
    result = TransferManualReviewQuery(repository).list(
        state=TransferExecutionState(state_filter),
        page=page,
        page_size=page_size,
    )
    return _SchemaResponse(
        success=True,
        data=_SchemaTransferManualReviewPageData(
            items=[_manual_review_task_data(item) for item in result.items],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
        ),
    )


@router.get(  # type: ignore[misc]
    "/tasks/{task_id}/manual-review",
    summary="查询 durable 整理人工复核详情",
    response_model=_SchemaResponse[_SchemaTransferManualReviewTaskData],
)
def get_transfer_manual_review(
    task_id: str,
    current_user: object = Depends(get_current_active_manage_user),
    repository: TransferExecutionRepository = Depends(
        get_transfer_execution_repository
    ),
) -> Any:
    """按任务标识返回严格裁剪的人工复核详情。"""
    del current_user
    task = TransferManualReviewQuery(repository).get(task_id=task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="人工复核任务不存在",
        )
    return _SchemaResponse(success=True, data=_manual_review_task_data(task))


@router.post(  # type: ignore[misc]
    "/tasks/{task_id}/manual-review",
    summary="人工判定整理步骤的外部执行结果",
    response_model=_SchemaResponse[_SchemaTransferManualReviewData],
)
def resolve_transfer_manual_review(
    task_id: str,
    review: _SchemaTransferManualReviewRequest,
    current_user: object = Depends(get_current_active_manage_user),
    repository: TransferExecutionRepository = Depends(
        get_transfer_execution_repository
    ),
) -> Any:
    """提交无租约人工判定，并返回不含 attempt 与 lease 的公开状态。"""
    result = (
        TransferStepResult(payload=dict(review.result_payload))
        if review.result_payload is not None
        else None
    )
    try:
        resolved = TransferExecutionCommand(repository).resolve_manual_review(
            task_id=task_id,
            operation_id=review.operation_id,
            decision=TransferManualReviewDecision(review.decision),
            actor=_manual_review_actor(current_user),
            reason=review.reason,
            result=result,
        )
    except TransferExecutionConflictError as error:
        logger.warning(f"整理人工复核请求冲突：{error}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="整理任务状态已变化，请刷新后重试",
        ) from error
    return _SchemaResponse(
        success=True,
        data=_SchemaTransferManualReviewData(
            task_id=resolved.task_id,
            operation_id=resolved.operation_id,
            decision=resolved.decision.value,
            state=resolved.state.value,
            review_revision=resolved.review_revision,
        ),
    )


@router.get(
    "/name",
    summary="查询整理后的名称",
    response_model=_SchemaResponse[_SchemaNameData],
)
def query_name(
    path: str, filetype: str, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    查询整理后的名称
    :param path: 文件路径
    :param filetype: 文件类型
    :param _: Token校验
    """
    context = MediaChain().recognize_by_path(
        path,
        obtain_images=False,
    )
    if not context or not context.media_info:
        return _SchemaResponse(success=False, message="未识别到媒体信息")
    new_path = TransferChain().recommend_name(
        meta=context.meta_info, mediainfo=context.media_info
    )
    if not new_path:
        return _SchemaResponse(success=False, message="未识别到新名称")
    if filetype == "dir":
        media_path = DirectoryHelper.get_media_root_path(
            rename_format=get_api_runtime_config_snapshot().rename_format(
                context.media_info.type
            ),
            rename_path=Path(new_path),
            media_type=context.media_info.type,
        )
        if media_path:
            new_name = media_path.name
        else:
            # fallback
            parents = Path(new_path).parents
            if len(parents) > 2:
                new_name = parents[1].name
            else:
                new_name = parents[0].name
    else:
        new_name = Path(new_path).name
    return _SchemaResponse(success=True, data={"name": new_name})


@router.get("/queue", summary="查询整理队列", response_model=List[_SchemaTransferJob])
async def query_queue(_: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    查询整理队列
    :param _: Token校验
    """
    return TransferChain().get_queue_tasks()


@router.delete(
    "/queue", summary="从整理队列中删除任务", response_model=_SchemaResponse[None]
)
async def remove_queue(
    fileitem: _SchemaFileItem, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    查询整理队列
    :param fileitem: 文件项
    :param _: Token校验
    """
    TransferChain().remove_from_queue(fileitem)
    # 取消整理
    runtime_stop_state.stop_transfer(fileitem.path)
    return _SchemaResponse(success=True)


def _resolve_manual_transfer_source_fileitems(
    transer_item: ManualTransferItem,
    history_query: TransferHistoryLookupService,
) -> tuple[List[FileItem], Optional[str]]:
    """
    从手动整理请求中解析源文件项。
    """
    if transer_item.logids:
        fileitems: List[FileItem] = []
        for logid in transer_item.logids:
            history = history_query.get(logid)
            if not history:
                return [], f"整理记录不存在，ID：{logid}"
            if history.status and ("move" in history.mode):
                fileitems.append(FileItem(**history.dest_fileitem))
            else:
                fileitems.append(FileItem(**history.src_fileitem))
        return fileitems, None

    if transer_item.logid:
        history = history_query.get(transer_item.logid)
        if not history:
            return [], f"整理记录不存在，ID：{transer_item.logid}"
        if history.status and ("move" in history.mode):
            return [FileItem(**history.dest_fileitem)], None
        return [FileItem(**history.src_fileitem)], None

    if transer_item.fileitems:
        return [fileitem for fileitem in transer_item.fileitems if fileitem], None
    if transer_item.fileitem:
        return [transer_item.fileitem], None
    return [], None


def _deduplicate_fileitems(fileitems: List[FileItem]) -> List[FileItem]:
    """
    按存储和路径去重文件项。
    """
    dedup_fileitems: List[FileItem] = []
    seen_paths = set()
    for current_fileitem in fileitems:
        storage = current_fileitem.storage or "local"
        path = current_fileitem.path
        if not path:
            continue
        key = (storage, path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        dedup_fileitems.append(current_fileitem)
    return dedup_fileitems


def _build_manual_transfer_target_path(
    directory: Optional[_SchemaTransferDirectoryConf] = None,
) -> _SchemaManualTransferTargetPath:
    """
    根据目录配置生成手动整理目的路径响应。
    """
    if not directory or not directory.library_path:
        return _SchemaManualTransferTargetPath()

    return _SchemaManualTransferTargetPath(
        target_storage=directory.library_storage or "local",
        target_path=directory.library_path,
        transfer_type=directory.transfer_type,
        scrape=directory.scraping or False,
        library_type_folder=directory.library_type_folder or False,
        library_category_folder=directory.library_category_folder or False,
    )


def _get_manual_transfer_target_key(
    directory: _SchemaTransferDirectoryConf,
) -> tuple[Optional[str], Optional[str]]:
    """
    生成目的目录唯一键。
    """
    return (
        directory.library_storage or "local",
        Path(directory.library_path).as_posix() if directory.library_path else None,
    )


@router.post(
    "/manual/target-path",
    summary="匹配手动转移目的路径",
    response_model=_SchemaResponse[_SchemaManualTransferTargetPath],
)
def match_manual_transfer_target_path(
    transer_item: ManualTransferItem,
    history_query: TransferHistoryLookupService = Depends(
        get_transfer_history_lookup_service
    ),
    _: object = Depends(get_current_active_manage_user),
) -> Any:
    """
    根据源文件匹配手动整理目的路径。

    :param transer_item: 手工整理项
    :param history_query: 整理历史投影服务
    :param _: Token校验
    """
    src_fileitems, error_message = _resolve_manual_transfer_source_fileitems(
        transer_item=transer_item,
        history_query=history_query,
    )
    if error_message:
        return _SchemaResponse(success=False, message=error_message)

    matched_directories: List[_SchemaTransferDirectoryConf] = []
    target_storage = transer_item.target_storage or None
    for src_fileitem in _deduplicate_fileitems(src_fileitems):
        directory = DirectoryHelper().get_dir(
            media=None,
            storage=src_fileitem.storage or "local",
            src_path=Path(src_fileitem.path),
            target_storage=target_storage,
        )
        if not directory or not directory.library_path:
            return _SchemaResponse(
                success=True,
                data=_SchemaManualTransferTargetPath().model_dump(),
            )
        matched_directories.append(directory)

    if not matched_directories:
        return _SchemaResponse(
            success=True,
            data=_SchemaManualTransferTargetPath().model_dump(),
        )

    first_directory = matched_directories[0]
    first_key = _get_manual_transfer_target_key(first_directory)
    if any(
        _get_manual_transfer_target_key(directory) != first_key
        for directory in matched_directories[1:]
    ):
        return _SchemaResponse(
            success=True,
            data=_SchemaManualTransferTargetPath().model_dump(),
        )

    return _SchemaResponse(
        success=True,
        data=_build_manual_transfer_target_path(first_directory).model_dump(),
    )


@router.post(
    "/manual/history",
    summary="查询手动转移成功历史",
    response_model=_SchemaResponse[_SchemaManualTransferHistoryInfo],
)
def query_manual_transfer_history(
    transer_item: ManualTransferItem,
    history_query: TransferHistoryLookupService = Depends(
        get_transfer_history_lookup_service
    ),
    _: object = Depends(get_current_active_manage_user),
) -> Any:
    """
    查询文件或目录命中的成功整理记录。

    :param transer_item: 手工整理项
    :param history_query: 整理历史投影服务
    :param _: Token校验
    """
    src_fileitems, error_message = _resolve_manual_transfer_source_fileitems(
        transer_item=transer_item,
        history_query=history_query,
    )
    if error_message:
        return _SchemaResponse(success=False, message=error_message)

    histories = TransferChain().get_manual_transfer_histories(
        _deduplicate_fileitems(src_fileitems)
    )
    history_info = _SchemaManualTransferHistoryInfo(
        reorganize=bool(histories),
        history_count=len(histories),
    )
    return _SchemaResponse(success=True, data=history_info.model_dump())


@router.post(
    "/manual",
    summary="手动转移",
    response_model=_SchemaResponse[_SchemaManualTransferResultData],
)
def manual_transfer(
    transer_item: ManualTransferItem,
    background: Optional[bool] = False,
    history_query: TransferHistoryLookupService = Depends(
        get_transfer_history_lookup_service
    ),
    _: object = Depends(get_current_active_manage_user),
) -> Any:
    """
    解析手动整理 HTTP 请求并委托兼容用例处理器。

    :param transer_item: 手工整理项
    :param background: 后台运行
    :param history_query: 整理历史投影服务
    :param _: Token校验
    """
    return _execute_manual_transfer(
        transer_item=transer_item,
        background=background,
        history_query=history_query,
    )


def _execute_manual_transfer(
    transer_item: ManualTransferItem,
    background: Optional[bool],
    history_query: TransferHistoryLookupService,
) -> Any:
    """执行历史恢复、批量预览与 TransferChain 兼容编排。"""
    force = False
    downloader = None
    download_hash = None
    src_fileitems: List[FileItem] = []
    cleanup_dest_fileitem: Optional[FileItem] = None
    target_path = Path(transer_item.target_path) if transer_item.target_path else None
    if transer_item.logid:
        # 查询历史记录
        history = history_query.get(transer_item.logid)
        if not history:
            return _SchemaResponse(
                success=False, message=f"整理记录不存在，ID：{transer_item.logid}"
            )
        # 强制转移
        force = True
        # 下载器与 Hash 是同一组下载上下文，重新识别时由当前文件路径重新匹配。
        downloader = history.downloader if transer_item.from_history else None
        download_hash = history.download_hash if transer_item.from_history else None
        if history.status and ("move" in history.mode):
            # 重新整理成功的转移，则使用成功的 dest 做 in_path
            src_fileitems = [FileItem(**history.dest_fileitem)]
        else:
            # 源路径
            src_fileitems = [FileItem(**history.src_fileitem)]
            if (
                history.dest_fileitem
                and not transer_item.preview
                and not transer_item.reorganize
            ):
                cleanup_dest_fileitem = FileItem(**history.dest_fileitem)

        # 从历史数据获取信息
        if transer_item.from_history:
            transer_item.type_name = (
                history.type if history.type else transer_item.type_name
            )
            transer_item.media_source = (
                history.media_source or transer_item.media_source
            )
            transer_item.media_id = (
                history.media_id or transer_item.media_id
            )
            transer_item.music_type = (
                getattr(history, "music_type", None) or transer_item.music_type
            )
            transer_item.season = (
                int(str(history.seasons).replace("S", ""))
                if history.seasons
                else transer_item.season
            )
            transer_item.episode_group = (
                history.episode_group or transer_item.episode_group
            )
            if history.episodes:
                if "-" in str(history.episodes):
                    # E01-E03多集合并
                    episode_start, episode_end = str(history.episodes).split("-")
                    episode_list: list[int] = []
                    for i in range(
                        int(episode_start.replace("E", "")),
                        int(episode_end.replace("E", "")) + 1,
                    ):
                        episode_list.append(i)
                    transer_item.episode_detail = ",".join(str(e) for e in episode_list)
                else:
                    # E01单集
                    transer_item.episode_detail = str(history.episodes).replace("E", "")

    elif transer_item.fileitems:
        src_fileitems = [fileitem for fileitem in transer_item.fileitems if fileitem]
    elif transer_item.fileitem:
        src_fileitems = [transer_item.fileitem]
    else:
        return _SchemaResponse(success=False, message="缺少参数")

    dedup_fileitems: List[FileItem] = []
    seen_paths = set()
    for current_fileitem in src_fileitems:
        storage = current_fileitem.storage or "local"
        path = current_fileitem.path
        if not path:
            continue
        key = (storage, path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        dedup_fileitems.append(current_fileitem)
    src_fileitems = dedup_fileitems
    if not src_fileitems:
        return _SchemaResponse(success=False, message="缺少参数")

    # 类型（“自动/auto/none”按未指定处理）
    mtype = None
    type_name = str(transer_item.type_name).strip() if transer_item.type_name else ""
    if type_name and type_name.lower() not in {"自动", "auto", "none"}:
        try:
            mtype = MediaType(type_name)
        except ValueError:
            return _SchemaResponse(
                success=False, message=f"不支持的媒体类型：{type_name}"
            )

    def _resolve_music_type(file_item: FileItem) -> Optional[str]:
        """为未显式指定实体的旧客户端按源项类型补全音乐命名空间。"""
        if mtype != MediaType.MUSIC or transer_item.music_type:
            return transer_item.music_type
        return (
            MUSIC_ENTITY_ALBUM
            if file_item.type == "dir"
            else MUSIC_ENTITY_RECORDING
        )
    # 自定义格式
    epformat = None
    if (
        transer_item.episode_offset
        or transer_item.episode_part
        or transer_item.episode_detail
        or transer_item.episode_format
    ):
        epformat = _SchemaEpisodeFormat(
            format=transer_item.episode_format,
            detail=transer_item.episode_detail,
            part=transer_item.episode_part,
            offset=transer_item.episode_offset,
        )
    explicit_selected_files = bool(transer_item.fileitems)

    # 前端显式传入文件列表时，按选中的文件逐个处理，避免将目录整体展开。
    if explicit_selected_files:
        preview_items: List[dict] = []
        error_messages: List[str] = []
        all_success = True
        for src_fileitem in src_fileitems:
            state, errormsg = TransferChain().manual_transfer(
                fileitem=src_fileitem,
                target_storage=transer_item.target_storage,
                target_path=target_path,
                media_source=transer_item.media_source,
                media_id=transer_item.media_id,
                music_type=_resolve_music_type(src_fileitem),
                mtype=mtype,
                season=transer_item.season,
                episode_group=transer_item.episode_group,
                transfer_type=transer_item.transfer_type,
                epformat=epformat,
                min_filesize=transer_item.min_filesize,
                scrape=transer_item.scrape,
                library_type_folder=transer_item.library_type_folder,
                library_category_folder=transer_item.library_category_folder,
                force=force,
                background=background,
                downloader=downloader,
                download_hash=download_hash,
                preview=transer_item.preview,
                reorganize=transer_item.reorganize,
                sync_extra_files=False,
                cleanup_dest_fileitem=cleanup_dest_fileitem,
            )
            if transer_item.preview:
                if isinstance(errormsg, dict):
                    preview_items.extend(
                        _public_transfer_result(errormsg).get("items") or []
                    )
                    if errormsg.get("message"):
                        error_messages.append(errormsg.get("message"))
                    if not state:
                        all_success = False
                else:
                    if errormsg:
                        public_message = _public_transfer_message(errormsg)
                        if public_message:
                            error_messages.append(public_message)
                    preview_items.append(
                        _build_failure_preview_item(
                            src_fileitem,
                            _public_transfer_message(errormsg),
                        )
                    )
                    all_success = False
            elif not state:
                all_success = False
                if isinstance(errormsg, list):
                    error_messages.extend([str(msg) for msg in errormsg if msg])
                elif errormsg:
                    error_messages.append(str(errormsg))

        if transer_item.preview:
            merged_preview_items: List[dict] = []
            seen_sources = set()
            for preview_item in preview_items:
                source = preview_item.get("source")
                if source in seen_sources:
                    continue
                seen_sources.add(source)
                merged_preview_items.append(
                    _public_transfer_result(preview_item)
                    if isinstance(preview_item, dict)
                    else preview_item
                )
            merged_message = _merge_transfer_messages(error_messages)
            preview_data = {
                "summary": {
                    "total": len(merged_preview_items),
                    "success": len(
                        [item for item in merged_preview_items if item.get("success")]
                    ),
                    "failed": len(
                        [item for item in merged_preview_items if not item.get("success")]
                    ),
                },
                "items": merged_preview_items,
                "message": merged_message,
            }
            return _SchemaResponse(
                success=True,
                message=merged_message or None,
                data=preview_data,
            )

        if not all_success:
            return _SchemaResponse(
                success=False,
                message=_merge_transfer_messages(error_messages),
            )
        return _SchemaResponse(success=True)

    src_fileitem = src_fileitems[0]
    # 开始转移
    state, errormsg = TransferChain().manual_transfer(
        fileitem=src_fileitem,
        target_storage=transer_item.target_storage,
        target_path=target_path,
        media_source=transer_item.media_source,
        media_id=transer_item.media_id,
        music_type=_resolve_music_type(src_fileitem),
        mtype=mtype,
        season=transer_item.season,
        episode_group=transer_item.episode_group,
        transfer_type=transer_item.transfer_type,
        epformat=epformat,
        min_filesize=transer_item.min_filesize,
        scrape=transer_item.scrape,
        library_type_folder=transer_item.library_type_folder,
        library_category_folder=transer_item.library_category_folder,
        force=force,
        background=background,
        downloader=downloader,
        download_hash=download_hash,
        preview=transer_item.preview,
        reorganize=transer_item.reorganize,
        sync_extra_files=True,
        cleanup_dest_fileitem=cleanup_dest_fileitem,
    )
    # 失败
    if not state:
        if isinstance(errormsg, list):
            errormsg = f"整理完成，{len(errormsg)} 个文件转移失败！"
        if isinstance(errormsg, dict):
            public_result = _public_transfer_result(errormsg)
            return _SchemaResponse(
                success=True,
                message=public_result.get("message"),
                data=public_result,
            )
        return _SchemaResponse(
            success=False,
            message=_public_transfer_message(errormsg),
        )
    # 成功
    if transer_item.preview:
        return _SchemaResponse(success=True, data=errormsg or {})
    return _SchemaResponse(success=True)


@router.post(
    "/episode-format/recommend",
    summary="推荐集数定位模板",
    response_model=_SchemaResponse[_SchemaEpisodeFormatRecommendData],
)
def recommend_episode_format(
    recommend_item: EpisodeFormatRecommendItem,
    _: object = Depends(get_current_active_manage_user),
) -> Any:
    """
    根据目录样本推荐集数定位模板
    :param recommend_item: 推荐请求
    :param _: Token校验
    """
    target_path = recommend_item.fileitem.path if recommend_item.fileitem else None
    logger.info(f"开始推荐集数定位模板：{target_path}")
    state, errmsg, data = TransferChain().recommend_episode_format(
        fileitem=recommend_item.fileitem,
        fileitems=recommend_item.fileitems,
    )
    if not state:
        logger.warn(f"推荐集数定位模板失败：{target_path} - {errmsg}")
        return _SchemaResponse(
            success=False,
            message=_public_transfer_message(errmsg),
        )
    logger.info(
        f"推荐集数定位模板成功：{target_path} - 规则 {data.get('rule_name') if data else None}"
    )
    return _SchemaResponse(success=True, data=data)


@router.get("/now", summary="立即执行下载器文件整理", response_model=_SchemaResponse[None])
def now(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    立即执行下载器文件整理 API_TOKEN认证（?token=xxx）
    """
    TransferChain().process()
    return _SchemaResponse(success=True)
