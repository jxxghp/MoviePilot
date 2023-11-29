from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException

from app import schemas
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.context import MediaInfo, Context, TorrentInfo
from app.core.metainfo import MetaInfo
from app.core.security import verify_token
from app.db.models.user import User
from app.db.userauth import get_current_active_user
from app.schemas import NotExistMediaInfo, MediaType

router = APIRouter()


@router.get("/", summary="正在下载", response_model=List[schemas.DownloadingTorrent])
def read_downloading(
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询正在下载的任务
    """
    return DownloadChain().downloading()


@router.post("/", summary="添加下载", response_model=schemas.Response)
def add_downloading(
        media_in: schemas.MediaInfo,
        torrent_in: schemas.TorrentInfo,
        current_user: User = Depends(get_current_active_user),
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
    did = DownloadChain().download_single(context=context, username=current_user.name)
    return schemas.Response(success=True if did else False, data={
        "download_id": did
    })


@router.post("/notexists", summary="查询缺失媒体信息", response_model=List[NotExistMediaInfo])
def exists(media_in: schemas.MediaInfo,
           _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询缺失媒体信息
    """
    # 媒体信息
    meta = MetaInfo(title=media_in.title)
    mtype = MediaType(media_in.type) if media_in.type else None
    if mtype:
        meta.type = mtype
    if media_in.season:
        meta.begin_season = media_in.season
        meta.type = MediaType.TV
    if media_in.year:
        meta.year = media_in.year
    if media_in.tmdb_id or media_in.douban_id:
        mediainfo = MediaChain().recognize_media(meta=meta, mtype=mtype,
                                                 tmdbid=media_in.tmdb_id, doubanid=media_in.douban_id)
    else:
        mediainfo = MediaChain().recognize_by_meta(metainfo=meta)
    # 查询缺失信息
    if not mediainfo:
        raise HTTPException(status_code=404, detail="媒体信息不存在")
    mediakey = mediainfo.tmdb_id or mediainfo.douban_id
    exist_flag, no_exists = DownloadChain().get_no_exists_info(meta=meta, mediainfo=mediainfo)
    if mediainfo.type == MediaType.MOVIE:
        # 电影已存在时返回空列表，存在时返回空对像列表
        return [] if exist_flag else [NotExistMediaInfo()]
    elif no_exists and no_exists.get(mediakey):
        # 电视剧返回缺失的剧集
        return list(no_exists.get(mediakey).values())
    return []


@router.get("/start/{hashString}", summary="开始任务", response_model=schemas.Response)
def start_downloading(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    开如下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "start")
    return schemas.Response(success=True if ret else False)


@router.get("/stop/{hashString}", summary="暂停任务", response_model=schemas.Response)
def stop_downloading(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    暂停下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "stop")
    return schemas.Response(success=True if ret else False)


@router.delete("/{hashString}", summary="删除下载任务", response_model=schemas.Response)
def remove_downloading(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除下载任务
    """
    ret = DownloadChain().remove_downloading(hashString)
    return schemas.Response(success=True if ret else False)
