from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.chain.douban import DoubanChain
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.context import MediaInfo, Context, TorrentInfo
from app.core.metainfo import MetaInfo
from app.core.security import verify_token
from app.db import get_db
from app.db.models.user import User
from app.db.userauth import get_current_active_superuser
from app.schemas import NotExistMediaInfo, MediaType

router = APIRouter()


@router.get("/", summary="正在下载", response_model=List[schemas.DownloadingTorrent])
def read_downloading(
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询正在下载的任务
    """
    return DownloadChain(db).downloading()


@router.post("/", summary="添加下载", response_model=schemas.Response)
def add_downloading(
        media_in: schemas.MediaInfo,
        torrent_in: schemas.TorrentInfo,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    添加下载任务
    """
    # 元数据
    metainfo = MetaInfo(title=torrent_in.title, subtitle=torrent_in.description)
    # 媒体信息
    mediainfo = MediaInfo()
    mediainfo.from_dict(media_in.dict())
    # 种子信息
    torrentinfo = TorrentInfo()
    torrentinfo.from_dict(torrent_in.dict())
    # 上下文
    context = Context(
        meta_info=metainfo,
        media_info=mediainfo,
        torrent_info=torrentinfo
    )
    did = DownloadChain(db).download_single(context=context)
    return schemas.Response(success=True if did else False, data={
        "download_id": did
    })


@router.post("/notexists", summary="查询缺失媒体信息", response_model=List[NotExistMediaInfo])
def exists(media_in: schemas.MediaInfo,
           db: Session = Depends(get_db),
           _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询缺失媒体信息
    """
    # 媒体信息
    mediainfo = MediaInfo()
    meta = MetaInfo(title=media_in.title)
    if media_in.tmdb_id:
        mediainfo.from_dict(media_in.dict())
    elif media_in.douban_id:
        context = DoubanChain(db).recognize_by_doubanid(doubanid=media_in.douban_id)
        if context:
            mediainfo = context.media_info
            meta = context.meta_info
    else:
        context = MediaChain(db).recognize_by_title(title=f"{media_in.title} {media_in.year}")
        if context:
            mediainfo = context.media_info
            meta = context.meta_info
    # 查询缺失信息
    if not mediainfo or not mediainfo.tmdb_id:
        raise HTTPException(status_code=404, detail="媒体信息不存在")
    exist_flag, no_exists = DownloadChain(db).get_no_exists_info(meta=meta, mediainfo=mediainfo)
    if mediainfo.type == MediaType.MOVIE:
        # 电影已存在时返回空列表，存在时返回空对像列表
        return [] if exist_flag else [NotExistMediaInfo()]
    elif no_exists and no_exists.get(mediainfo.tmdb_id):
        # 电视剧返回缺失的剧集
        return list(no_exists.get(mediainfo.tmdb_id).values())
    return []


@router.get("/start/{hashString}", summary="开始任务", response_model=schemas.Response)
def start_downloading(
        hashString: str,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    开如下载任务
    """
    ret = DownloadChain(db).set_downloading(hashString, "start")
    return schemas.Response(success=True if ret else False)


@router.get("/stop/{hashString}", summary="暂停任务", response_model=schemas.Response)
def stop_downloading(
        hashString: str,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    控制下载任务
    """
    ret = DownloadChain(db).set_downloading(hashString, "stop")
    return schemas.Response(success=True if ret else False)


@router.delete("/{hashString}", summary="删除下载任务", response_model=schemas.Response)
def remove_downloading(
        hashString: str,
        db: Session = Depends(get_db),
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    控制下载任务
    """
    ret = DownloadChain(db).remove_downloading(hashString)
    return schemas.Response(success=True if ret else False)
