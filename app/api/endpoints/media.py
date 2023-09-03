from typing import List, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import schemas
from app.chain.douban import DoubanChain
from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.core.security import verify_token
from app.db import get_db
from app.db.mediaserver_oper import MediaServerOper
from app.schemas import MediaType

router = APIRouter()


@router.get("/recognize", summary="识别媒体信息（种子）", response_model=schemas.Context)
def recognize(title: str,
              subtitle: str = None,
              db: Session = Depends(get_db),
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据标题、副标题识别媒体信息
    """
    # 识别媒体信息
    context = MediaChain(db).recognize_by_title(title=title, subtitle=subtitle)
    if context:
        return context.to_dict()
    return schemas.Context()


@router.get("/recognize_file", summary="识别媒体信息（文件）", response_model=schemas.Context)
def recognize(path: str,
              db: Session = Depends(get_db),
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据文件路径识别媒体信息
    """
    # 识别媒体信息
    context = MediaChain(db).recognize_by_path(path)
    if context:
        return context.to_dict()
    return schemas.Context()


@router.get("/search", summary="搜索媒体信息", response_model=List[schemas.MediaInfo])
def search_by_title(title: str,
                    page: int = 1,
                    count: int = 8,
                    db: Session = Depends(get_db),
                    _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    模糊搜索媒体信息列表
    """
    _, medias = MediaChain(db).search(title=title)
    if medias:
        return [media.to_dict() for media in medias[(page - 1) * count: page * count]]
    return []


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
    exist = MediaServerOper(db).exists(
        title=meta.name, year=year, mtype=mtype, tmdbid=tmdbid, season=season
    )
    return schemas.Response(success=True if exist else False, data={
        "item": exist or {}
    })


@router.get("/{mediaid}", summary="查询媒体详情", response_model=schemas.MediaInfo)
def tmdb_info(mediaid: str, type_name: str,
              db: Session = Depends(get_db),
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据媒体ID查询themoviedb或豆瓣媒体信息，type_name: 电影/电视剧
    """
    mtype = MediaType(type_name)
    if mediaid.startswith("tmdb:"):
        result = TmdbChain(db).tmdb_info(int(mediaid[5:]), mtype)
        return MediaInfo(tmdb_info=result).to_dict()
    elif mediaid.startswith("douban:"):
        # 查询豆瓣信息
        doubaninfo = DoubanChain(db).douban_info(doubanid=mediaid[7:])
        if not doubaninfo:
            return schemas.MediaInfo()
        result = DoubanChain(db).recognize_by_doubaninfo(doubaninfo)
        if result:
            # TMDB
            return result.media_info.to_dict()
        else:
            # 豆瓣
            return MediaInfo(douban_info=doubaninfo).to_dict()
    else:
        return schemas.MediaInfo()
