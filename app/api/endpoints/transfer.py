from pathlib import Path
from typing import Any, List, Annotated, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.media import MediaChain
from app.chain.transfer import TransferChain
from app.core.config import settings, global_vars
from app.core.security import verify_token, verify_apitoken
from app.db import get_db
from app.db.models import User
from app.db.models.transferhistory import TransferHistory
from app.db.user_oper import (
    get_current_active_manage_user,
    get_current_active_superuser,
)
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.schemas import (
    MediaType,
    FileItem,
    ManualTransferItem,
    EpisodeFormatRecommendItem,
)

router = APIRouter()


@router.get("/name", summary="查询整理后的名称", response_model=schemas.Response)
def query_name(
    path: str, filetype: str, _: schemas.TokenPayload = Depends(verify_token)
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
        return schemas.Response(success=False, message="未识别到媒体信息")
    new_path = TransferChain().recommend_name(
        meta=context.meta_info, mediainfo=context.media_info
    )
    if not new_path:
        return schemas.Response(success=False, message="未识别到新名称")
    if filetype == "dir":
        media_path = DirectoryHelper.get_media_root_path(
            rename_format=settings.RENAME_FORMAT(context.media_info.type),
            rename_path=Path(new_path),
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
    return schemas.Response(success=True, data={"name": new_name})


@router.get("/queue", summary="查询整理队列", response_model=List[schemas.TransferJob])
async def query_queue(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询整理队列
    :param _: Token校验
    """
    return TransferChain().get_queue_tasks()


@router.delete(
    "/queue", summary="从整理队列中删除任务", response_model=schemas.Response
)
async def remove_queue(
    fileitem: schemas.FileItem, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    查询整理队列
    :param fileitem: 文件项
    :param _: Token校验
    """
    TransferChain().remove_from_queue(fileitem)
    # 取消整理
    global_vars.stop_transfer(fileitem.path)
    return schemas.Response(success=True)


def _resolve_manual_transfer_source_fileitems(
    transer_item: ManualTransferItem, db: Session
) -> tuple[List[FileItem], Optional[str]]:
    """
    从手动整理请求中解析源文件项。
    """
    if transer_item.logids:
        fileitems: List[FileItem] = []
        for logid in transer_item.logids:
            history: TransferHistory = TransferHistory.get(db, logid)
            if not history:
                return [], f"整理记录不存在，ID：{logid}"
            if history.status and ("move" in history.mode):
                fileitems.append(FileItem(**history.dest_fileitem))
            else:
                fileitems.append(FileItem(**history.src_fileitem))
        return fileitems, None

    if transer_item.logid:
        history: TransferHistory = TransferHistory.get(db, transer_item.logid)
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
    directory: Optional[schemas.TransferDirectoryConf] = None,
) -> schemas.ManualTransferTargetPath:
    """
    根据目录配置生成手动整理目的路径响应。
    """
    if not directory or not directory.library_path:
        return schemas.ManualTransferTargetPath()

    return schemas.ManualTransferTargetPath(
        target_storage=directory.library_storage or "local",
        target_path=directory.library_path,
        transfer_type=directory.transfer_type,
        scrape=directory.scraping or False,
        library_type_folder=directory.library_type_folder or False,
        library_category_folder=directory.library_category_folder or False,
    )


def _get_manual_transfer_target_key(
    directory: schemas.TransferDirectoryConf,
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
    response_model=schemas.Response,
)
def match_manual_transfer_target_path(
    transer_item: ManualTransferItem,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    根据源文件匹配手动整理目的路径。

    :param transer_item: 手工整理项
    :param db: 数据库
    :param _: Token校验
    """
    src_fileitems, error_message = _resolve_manual_transfer_source_fileitems(
        transer_item=transer_item,
        db=db,
    )
    if error_message:
        return schemas.Response(success=False, message=error_message)

    matched_directories: List[schemas.TransferDirectoryConf] = []
    target_storage = transer_item.target_storage or None
    for src_fileitem in _deduplicate_fileitems(src_fileitems):
        directory = DirectoryHelper().get_dir(
            media=None,
            storage=src_fileitem.storage or "local",
            src_path=Path(src_fileitem.path),
            target_storage=target_storage,
        )
        if not directory or not directory.library_path:
            return schemas.Response(
                success=True,
                data=schemas.ManualTransferTargetPath().model_dump(),
            )
        matched_directories.append(directory)

    if not matched_directories:
        return schemas.Response(
            success=True,
            data=schemas.ManualTransferTargetPath().model_dump(),
        )

    first_directory = matched_directories[0]
    first_key = _get_manual_transfer_target_key(first_directory)
    if any(
        _get_manual_transfer_target_key(directory) != first_key
        for directory in matched_directories[1:]
    ):
        return schemas.Response(
            success=True,
            data=schemas.ManualTransferTargetPath().model_dump(),
        )

    return schemas.Response(
        success=True,
        data=_build_manual_transfer_target_path(first_directory).model_dump(),
    )


@router.post("/manual", summary="手动转移", response_model=schemas.Response)
def manual_transfer(
    transer_item: ManualTransferItem,
    background: Optional[bool] = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_manage_user),
) -> Any:
    """
    手动转移，文件或历史记录，支持自定义剧集识别格式
    :param transer_item: 手工整理项
    :param background: 后台运行
    :param db: 数据库
    :param _: Token校验
    """
    force = False
    downloader = None
    download_hash = None
    src_fileitems: List[FileItem] = []
    cleanup_dest_fileitem: Optional[FileItem] = None
    target_path = Path(transer_item.target_path) if transer_item.target_path else None
    if transer_item.logid:
        # 查询历史记录
        history: TransferHistory = TransferHistory.get(db, transer_item.logid)
        if not history:
            return schemas.Response(
                success=False, message=f"整理记录不存在，ID：{transer_item.logid}"
            )
        # 强制转移
        force = True
        downloader = history.downloader
        download_hash = history.download_hash
        if history.status and ("move" in history.mode):
            # 重新整理成功的转移，则使用成功的 dest 做 in_path
            src_fileitems = [FileItem(**history.dest_fileitem)]
        else:
            # 源路径
            src_fileitems = [FileItem(**history.src_fileitem)]
            if history.dest_fileitem and not transer_item.preview:
                cleanup_dest_fileitem = FileItem(**history.dest_fileitem)

        # 从历史数据获取信息
        if transer_item.from_history:
            transer_item.type_name = (
                history.type if history.type else transer_item.type_name
            )
            transer_item.tmdbid = (
                int(history.tmdbid) if history.tmdbid else transer_item.tmdbid
            )
            transer_item.doubanid = (
                str(history.doubanid) if history.doubanid else transer_item.doubanid
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
        return schemas.Response(success=False, message=f"缺少参数")

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
        return schemas.Response(success=False, message="缺少参数")

    # 类型（“自动/auto/none”按未指定处理）
    mtype = None
    type_name = str(transer_item.type_name).strip() if transer_item.type_name else ""
    if type_name and type_name.lower() not in {"自动", "auto", "none"}:
        try:
            mtype = MediaType(type_name)
        except ValueError:
            return schemas.Response(
                success=False, message=f"不支持的媒体类型：{type_name}"
            )
    # 自定义格式
    epformat = None
    if (
        transer_item.episode_offset
        or transer_item.episode_part
        or transer_item.episode_detail
        or transer_item.episode_format
    ):
        epformat = schemas.EpisodeFormat(
            format=transer_item.episode_format,
            detail=transer_item.episode_detail,
            part=transer_item.episode_part,
            offset=transer_item.episode_offset,
        )
    explicit_selected_files = bool(transer_item.fileitems)

    def _build_failure_preview_item(file_item: FileItem, message: str) -> dict:
        """
        构造手动整理预览失败项。
        """
        return {
            "source": file_item.path if file_item else None,
            "target": None,
            "target_dir": None,
            "success": False,
            "message": message,
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

    def _merge_messages(messages: List[str]) -> str:
        """
        合并手动整理批量预览提示信息。
        """
        valid_messages = [msg for msg in messages if msg]
        if not valid_messages:
            return ""
        return "、".join(valid_messages[:2]) + (
            f"，等{len(valid_messages)}条消息" if len(valid_messages) > 2 else ""
        )

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
                tmdbid=transer_item.tmdbid,
                doubanid=transer_item.doubanid,
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
                sync_extra_files=False,
                cleanup_dest_fileitem=cleanup_dest_fileitem,
            )
            if transer_item.preview:
                if isinstance(errormsg, dict):
                    preview_items.extend(errormsg.get("items") or [])
                    if errormsg.get("message"):
                        error_messages.append(errormsg.get("message"))
                    if not state:
                        all_success = False
                else:
                    if errormsg:
                        error_messages.append(str(errormsg))
                    preview_items.append(
                        _build_failure_preview_item(src_fileitem, str(errormsg))
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
                merged_preview_items.append(preview_item)
            merged_message = _merge_messages(error_messages)
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
            return schemas.Response(
                success=True,
                message=merged_message or None,
                data=preview_data,
            )

        if not all_success:
            return schemas.Response(
                success=False,
                message=_merge_messages(error_messages),
            )
        return schemas.Response(success=True)

    src_fileitem = src_fileitems[0]
    # 开始转移
    state, errormsg = TransferChain().manual_transfer(
        fileitem=src_fileitem,
        target_storage=transer_item.target_storage,
        target_path=target_path,
        tmdbid=transer_item.tmdbid,
        doubanid=transer_item.doubanid,
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
        sync_extra_files=True,
        cleanup_dest_fileitem=cleanup_dest_fileitem,
    )
    # 失败
    if not state:
        if isinstance(errormsg, list):
            errormsg = f"整理完成，{len(errormsg)} 个文件转移失败！"
        if isinstance(errormsg, dict):
            return schemas.Response(
                success=True,
                message=errormsg.get("message"),
                data=errormsg,
            )
        return schemas.Response(success=False, message=errormsg)
    # 成功
    if transer_item.preview:
        return schemas.Response(success=True, data=errormsg or {})
    return schemas.Response(success=True)


@router.post(
    "/episode-format/recommend",
    summary="推荐集数定位模板",
    response_model=schemas.Response,
)
def recommend_episode_format(
    recommend_item: EpisodeFormatRecommendItem,
    _: User = Depends(get_current_active_manage_user),
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
        return schemas.Response(success=False, message=errmsg)
    logger.info(
        f"推荐集数定位模板成功：{target_path} - 规则 {data.get('rule_name') if data else None}"
    )
    return schemas.Response(success=True, data=data)


@router.get("/now", summary="立即执行下载器文件整理", response_model=schemas.Response)
def now(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    立即执行下载器文件整理 API_TOKEN认证（?token=xxx）
    """
    TransferChain().process()
    return schemas.Response(success=True)
