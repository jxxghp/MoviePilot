from typing import Any, List

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.context import MediaInfo, Context, TorrentInfo
from app.core.metainfo import MetaInfo
from app.core.security import verify_token
from app.db.models.user import User
from app.db.userauth import get_current_active_user

router = APIRouter()


@router.get("/", summary="正在下载", response_model=List[schemas.DownloadingTorrent])
def read(
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询正在下载的任务
    """
    return DownloadChain().downloading()


@router.post("/", summary="添加下载（含媒体信息）", response_model=schemas.Response)
def download(
        media_in: schemas.MediaInfo,
        torrent_in: schemas.TorrentInfo,
        current_user: User = Depends(get_current_active_user)) -> Any:
    """
    添加下载任务（含媒体信息）
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
    if not did:
        return schemas.Response(success=False, message="任务添加失败")
    return schemas.Response(success=True, data={
        "download_id": did
    })


@router.post("/add", summary="添加下载（不含媒体信息）", response_model=schemas.Response)
def add(
        torrent_in: schemas.TorrentInfo,
        current_user: User = Depends(get_current_active_user)) -> Any:
    """
    添加下载任务（不含媒体信息）
    """
    # 元数据
    metainfo = MetaInfo(title=torrent_in.title, subtitle=torrent_in.description)
    # 媒体信息
    mediainfo = MediaChain().recognize_media(meta=metainfo)
    if not mediainfo:
        return schemas.Response(success=False, message="无法识别媒体信息")
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
    if not did:
        return schemas.Response(success=False, message="任务添加失败")
    return schemas.Response(success=True, data={
        "download_id": did
    })


@router.get("/start/{hashString}", summary="开始任务", response_model=schemas.Response)
def start(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    开如下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "start")
    return schemas.Response(success=True if ret else False)


@router.get("/stop/{hashString}", summary="暂停任务", response_model=schemas.Response)
def stop(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    暂停下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "stop")
    return schemas.Response(success=True if ret else False)


@router.delete("/{hashString}", summary="删除下载任务", response_model=schemas.Response)
def info(
        hashString: str,
        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    删除下载任务
    """
    ret = DownloadChain().remove_downloading(hashString)
    return schemas.Response(success=True if ret else False)
