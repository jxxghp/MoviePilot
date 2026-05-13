from pathlib import Path
from typing import Any, List, Annotated, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.config import settings, global_vars
from app.core.security import verify_token, verify_apitoken
from app.db import get_db
from app.db.models import User
from app.db.models.transferhistory import TransferHistory
from app.db.user_oper import get_current_active_superuser
from app.helper.directory import DirectoryHelper
from app.schemas import MediaType, FileItem, ManualTransferItem

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


@router.post("/manual", summary="手动转移", response_model=schemas.Response)
def manual_transfer(
    transer_item: ManualTransferItem,
    background: Optional[bool] = False,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_active_superuser),
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
            src_fileitem = FileItem(**history.dest_fileitem)
        else:
            # 源路径
            src_fileitem = FileItem(**history.src_fileitem)
            # 目的路径
            if history.dest_fileitem and not transer_item.preview:
                # 删除旧的已整理文件
                dest_fileitem = FileItem(**history.dest_fileitem)
                state = StorageChain().delete_media_file(dest_fileitem)
                if not state:
                    return schemas.Response(
                        success=False, message=f"{dest_fileitem.path} 删除失败"
                    )

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

    elif transer_item.fileitem:
        src_fileitem = transer_item.fileitem
    else:
        return schemas.Response(success=False, message=f"缺少参数")

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


@router.get("/now", summary="立即执行下载器文件整理", response_model=schemas.Response)
def now(_: Annotated[str, Depends(verify_apitoken)]) -> Any:
    """
    立即执行下载器文件整理 API_TOKEN认证（?token=xxx）
    """
    TransferChain().process()
    return schemas.Response(success=True)
