from typing import Any, List, Annotated, Optional

from fastapi import APIRouter, Depends, Body

from app import schemas
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.context import MediaInfo, Context, TorrentInfo
from app.core.metainfo import MetaInfo
from app.core.security import verify_token
from app.db.models.user import User
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_active_user
from app.helper.directory import DirectoryHelper
from app.schemas.types import SystemConfigKey

router = APIRouter()


@router.get("/", summary="正在下载", response_model=List[schemas.DownloadingTorrent])
def current(
    name: Optional[str] = None, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    查询正在下载的任务
    """
    return DownloadChain().downloading(name)


@router.post("/", summary="添加下载（含媒体信息）", response_model=schemas.Response)
def download(
    media_in: schemas.MediaInfo,
    torrent_in: schemas.TorrentInfo,
    downloader: Annotated[str | None, Body()] = None,
    save_path: Annotated[str | None, Body()] = None,
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    添加下载任务（含媒体信息）
    """
    # 元数据
    metainfo = MetaInfo(title=torrent_in.title, subtitle=torrent_in.description)
    # 媒体信息
    mediainfo = MediaInfo()
    mediainfo.from_dict(media_in.model_dump())
    # 种子信息
    torrentinfo = TorrentInfo()
    torrentinfo.from_dict(torrent_in.model_dump())
    # 手动下载始终使用选择的下载器
    torrentinfo.site_downloader = downloader
    # 上下文
    context = Context(
        meta_info=metainfo, media_info=mediainfo, torrent_info=torrentinfo
    )
    did = DownloadChain().download_single(
        context=context,
        username=current_user.name,
        save_path=save_path,
        source="Manual",
    )
    if not did:
        return schemas.Response(success=False, message="任务添加失败")
    return schemas.Response(success=True, data={"download_id": did})


@router.post(
    "/add", summary="添加下载（不含媒体信息）", response_model=schemas.Response
)
def add(
    torrent_in: schemas.TorrentInfo,
    tmdbid: Annotated[int | None, Body()] = None,
    doubanid: Annotated[str | None, Body()] = None,
    downloader: Annotated[str | None, Body()] = None,
    # 保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等
    save_path: Annotated[str | None, Body()] = None,
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    添加下载任务（不含媒体信息）
    """
    # 元数据
    metainfo = MetaInfo(title=torrent_in.title, subtitle=torrent_in.description)
    # 媒体信息
    if tmdbid or doubanid:
        mediainfo = MediaChain().recognize_media(
            meta=metainfo,
            tmdbid=tmdbid,
            doubanid=doubanid,
        )
    else:
        mediainfo = MediaChain().recognize_by_meta(
            metainfo,
            obtain_images=False,
        )
    if not mediainfo:
        return schemas.Response(success=False, message="无法识别媒体信息")
    # 种子信息
    torrentinfo = TorrentInfo()
    torrentinfo.from_dict(torrent_in.model_dump())
    # 上下文
    context = Context(
        meta_info=metainfo, media_info=mediainfo, torrent_info=torrentinfo
    )

    did = DownloadChain().download_single(
        context=context,
        username=current_user.name,
        downloader=downloader,
        save_path=save_path,
        source="Manual",
    )
    if not did:
        return schemas.Response(success=False, message="任务添加失败")
    return schemas.Response(success=True, data={"download_id": did})


@router.get("/start/{hashString}", summary="开始任务", response_model=schemas.Response)
def start(
    hashString: str,
    name: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    开如下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "start", name=name)
    return schemas.Response(success=True if ret else False)


@router.get("/stop/{hashString}", summary="暂停任务", response_model=schemas.Response)
def stop(
    hashString: str,
    name: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    暂停下载任务
    """
    ret = DownloadChain().set_downloading(hashString, "stop", name=name)
    return schemas.Response(success=True if ret else False)


@router.get("/clients", summary="查询可用下载器", response_model=List[dict])
async def clients(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询可用下载器
    """
    downloaders: List[dict] = SystemConfigOper().get(SystemConfigKey.Downloaders)
    if downloaders:
        return [
            {"name": d.get("name"), "type": d.get("type")}
            for d in downloaders
            if d.get("enabled")
        ]
    return []


@router.get(
    "/paths", summary="查询可用下载路径", response_model=List[schemas.DownloadDirectory]
)
def paths(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询可直接用于下载接口 save_path 参数的下载路径
    """
    return [
        schemas.DownloadDirectory(
            name=dir_info.name,
            storage=dir_info.storage or "local",
            download_path=dir_info.download_path,
            save_path=schemas.FileURI(
                storage=dir_info.storage or "local",
                path=dir_info.download_path,
            ).uri,
            priority=dir_info.priority,
            media_type=dir_info.media_type,
            media_category=dir_info.media_category,
        )
        for dir_info in DirectoryHelper().get_download_dirs()
        if dir_info.download_path
    ]


@router.delete("/{hashString}", summary="删除下载任务", response_model=schemas.Response)
def delete(
    hashString: str,
    name: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    删除下载任务
    """
    ret = DownloadChain().remove_downloading(hashString, name=name)
    return schemas.Response(success=True if ret else False)
