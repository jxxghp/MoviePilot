from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.mediaserver import MediaServerChain
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.core.security import verify_token
from app.db import get_db
from app.db.mediaserver_oper import MediaServerOper
from app.db.models import MediaServerItem
from app.schemas import MediaType, NotExistMediaInfo

router = APIRouter()


@router.get("/play/{itemid}", summary="在线播放")
def play_item(itemid: str) -> schemas.Response:
    """
    获取媒体服务器播放页面地址
    """
    if not itemid:
        return schemas.Response(success=False, msg="参数错误")
    if not settings.MEDIASERVER:
        return schemas.Response(success=False, msg="未配置媒体服务器")
    mediaserver = settings.MEDIASERVER.split(",")[0]
    play_url = MediaServerChain().get_play_url(server=mediaserver, item_id=itemid)
    # 重定向到play_url
    if not play_url:
        return schemas.Response(success=False, msg="未找到播放地址")
    return schemas.Response(success=True, data={
        "url": play_url
    })


@router.get("/exists", summary="本地是否存在", response_model=schemas.Response)
def exists(title: str = None,
           year: int = None,
           mtype: str = None,
           tmdbid: int = None,
           season: int = None,
           db: Session = Depends(get_db),
           _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    判断本地是否存在
    """
    meta = MetaInfo(title)
    if not season:
        season = meta.begin_season
    # 返回对象
    ret_info = {}
    # 本地数据库是否存在
    exist: MediaServerItem = MediaServerOper(db).exists(
        title=meta.name, year=year, mtype=mtype, tmdbid=tmdbid, season=season
    )
    if exist:
        ret_info = {
            "id": exist.item_id
        }
    """
    else:
        # 服务器是否存在
        mediainfo = MediaInfo()
        mediainfo.from_dict({
            "title": meta.name,
            "year": year or meta.year,
            "type": mtype or meta.type,
            "tmdb_id": tmdbid,
            "season": season
        })
        exist: schemas.ExistMediaInfo = MediaServerChain().media_exists(
            mediainfo=mediainfo
        )
        if exist:
            ret_info = {
                "id": exist.itemid
            }
    """
    return schemas.Response(success=True if exist else False, data={
        "item": ret_info
    })


@router.post("/notexists", summary="查询缺失媒体信息", response_model=List[schemas.NotExistMediaInfo])
def not_exists(media_in: schemas.MediaInfo,
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


@router.get("/latest", summary="最新入库条目", response_model=List[schemas.MediaServerPlayItem])
def latest(count: int = 18,
           userinfo: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取媒体服务器最新入库条目
    """
    return MediaServerChain().latest(count=count, username=userinfo.username) or []


@router.get("/playing", summary="正在播放条目", response_model=List[schemas.MediaServerPlayItem])
def playing(count: int = 12,
            userinfo: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取媒体服务器正在播放条目
    """
    return MediaServerChain().playing(count=count, username=userinfo.username) or []


@router.get("/library", summary="媒体库列表", response_model=List[schemas.MediaServerLibrary])
def library(userinfo: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取媒体服务器媒体库列表
    """
    return MediaServerChain().librarys(username=userinfo.username) or []
