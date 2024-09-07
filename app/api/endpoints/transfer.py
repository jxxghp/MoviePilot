import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.media import MediaChain
from app.chain.transfer import TransferChain
from app.core.metainfo import MetaInfoPath
from app.core.security import verify_token, verify_apitoken
from app.db import get_db
from app.db.models.transferhistory import TransferHistory
from app.db.user_oper import get_current_active_superuser
from app.schemas import MediaType, FileItem

router = APIRouter()


@router.get("/name", summary="查询整理后的名称", response_model=schemas.Response)
def query_name(path: str, filetype: str,
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询整理后的名称
    :param path: 文件路径
    :param filetype: 文件类型
    :param _: Token校验
    """
    meta = MetaInfoPath(Path(path))
    mediainfo = MediaChain().recognize_media(meta)
    if not mediainfo:
        return schemas.Response(success=False, message="未识别到媒体信息")
    new_path = TransferChain().recommend_name(meta=meta, mediainfo=mediainfo)
    if not new_path:
        return schemas.Response(success=False, message="未识别到新名称")
    if filetype == "dir":
        parents = Path(new_path).parents
        if len(parents) > 2:
            new_name = parents[1].name
        else:
            new_name = parents[0].name
    else:
        new_name = Path(new_path).name
    return schemas.Response(success=True, data={
        "name": new_name
    })


@router.post("/manual", summary="手动转移", response_model=schemas.Response)
def manual_transfer(fileitem: FileItem = None,
                    logid: int = None,
                    target_storage: str = None,
                    target_path: str = None,
                    tmdbid: int = None,
                    doubanid: str = None,
                    type_name: str = None,
                    season: int = None,
                    transfer_type: str = None,
                    episode_format: str = None,
                    episode_detail: str = None,
                    episode_part: str = None,
                    episode_offset: int = 0,
                    min_filesize: int = 0,
                    scrape: bool = None,
                    from_history: bool = None,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    手动转移，文件或历史记录，支持自定义剧集识别格式
    :param fileitem: 文件信息
    :param logid: 转移历史记录ID
    :param target_storage: 目标存储
    :param target_path: 目标路径
    :param type_name: 媒体类型、电影/电视剧
    :param tmdbid: tmdbid
    :param doubanid: 豆瓣ID
    :param season: 剧集季号
    :param transfer_type: 转移类型，move/copy 等
    :param episode_format: 剧集识别格式
    :param episode_detail: 剧集识别详细信息
    :param episode_part: 剧集识别分集信息
    :param episode_offset: 剧集识别偏移量
    :param min_filesize: 最小文件大小(MB)
    :param scrape: 是否刮削元数据
    :param from_history: 从历史记录中获取tmdbid、season、episode_detail等信息
    :param db: 数据库
    :param _: Token校验
    """
    force = False
    target_path = Path(target_path) if target_path else None
    transfer = TransferChain()
    if logid:
        # 查询历史记录
        history: TransferHistory = TransferHistory.get(db, logid)
        if not history:
            return schemas.Response(success=False, message=f"历史记录不存在，ID：{logid}")
        # 强制转移
        force = True
        if history.status and ("move" in history.mode):
            # 重新整理成功的转移，则使用成功的 dest 做 in_path
            src_fileitem = FileItem(**json.loads(history.dest_fileitem))
        else:
            # 源路径
            src_fileitem = FileItem(**json.loads(history.src_fileitem))
            # 目的路径
            if history.dest_fileitem:
                # 删除旧的已整理文件
                dest_fileitem = FileItem(**json.loads(history.dest_fileitem))
                transfer.delete_files(dest_fileitem)

        # 从历史数据获取信息
        if from_history:
            type_name = history.type if history.type else type_name
            tmdbid = int(history.tmdbid) if history.tmdbid else tmdbid
            doubanid = str(history.doubanid) if history.doubanid else doubanid
            season = int(str(history.seasons).replace("S", "")) if history.seasons else season
            if history.episodes:
                if "-" in str(history.episodes):
                    # E01-E03多集合并
                    episode_start, episode_end = str(history.episodes).split("-")
                    episode_list: list[int] = []
                    for i in range(int(episode_start.replace("E", "")), int(episode_end.replace("E", "")) + 1):
                        episode_list.append(i)
                    episode_detail = ",".join(str(e) for e in episode_list)
                else:
                    # E01单集
                    episode_detail = str(history.episodes).replace("E", "")

    elif fileitem:
        src_fileitem = fileitem
    else:
        return schemas.Response(success=False, message=f"缺少参数")

    # 类型
    mtype = MediaType(type_name) if type_name else None
    # 自定义格式
    epformat = None
    if episode_offset or episode_part or episode_detail or episode_format:
        epformat = schemas.EpisodeFormat(
            format=episode_format,
            detail=episode_detail,
            part=episode_part,
            offset=episode_offset,
        )
    # 开始转移
    state, errormsg = transfer.manual_transfer(
        fileitem=src_fileitem,
        target_storage=target_storage,
        target_path=target_path,
        tmdbid=tmdbid,
        doubanid=doubanid,
        mtype=mtype,
        season=season,
        transfer_type=transfer_type,
        epformat=epformat,
        min_filesize=min_filesize,
        scrape=scrape,
        force=force
    )
    # 失败
    if not state:
        if isinstance(errormsg, list):
            errormsg = f"整理完成，{len(errormsg)} 个文件转移失败！"
        return schemas.Response(success=False, message=errormsg)
    # 成功
    return schemas.Response(success=True)


@router.get("/now", summary="立即执行下载器文件整理", response_model=schemas.Response)
def now(_: str = Depends(verify_apitoken)) -> Any:
    """
    立即执行下载器文件整理 API_TOKEN认证（?token=xxx）
    """
    TransferChain().process()
    return schemas.Response(success=True)
