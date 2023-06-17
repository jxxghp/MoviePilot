from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.core.context import MediaInfo
from app.db.models.user import User
from app.db.userauth import get_current_active_user
from app.schemas.types import MediaType

router = APIRouter()


@router.get("/recognize", response_model=schemas.Context)
async def recognize(title: str,
                    subtitle: str = None,
                    _: User = Depends(get_current_active_user)) -> Any:
    """
    识别媒体信息
    """
    # 识别媒体信息
    context = MediaChain().recognize_by_title(title=title, subtitle=subtitle)
    return context.to_dict()


@router.get("/tmdb", response_model=schemas.MediaInfo)
async def tmdb_info(tmdbid: int, type_name: str) -> Any:
    """
    根据TMDBID查询媒体信息
    """
    mtype = MediaType.MOVIE if type_name == MediaType.MOVIE.value else MediaType.TV
    media = MediaChain().recognize_media(tmdbid=tmdbid, mtype=mtype)
    if media:
        return media.to_dict()
    else:
        return schemas.MediaInfo()


@router.get("/douban", response_model=schemas.MediaInfo)
async def douban_info(doubanid: str) -> Any:
    """
    根据豆瓣ID查询豆瓣媒体信息
    """
    doubaninfo = MediaChain().douban_info(doubanid=doubanid)
    if doubaninfo:
        return MediaInfo(douban_info=doubaninfo).to_dict()
    else:
        return schemas.MediaInfo()


@router.get("/search", response_model=List[schemas.MediaInfo])
async def search_by_title(title: str,
                          _: User = Depends(get_current_active_user)) -> Any:
    """
    搜索媒体信息
    """
    _, medias = MediaChain().search(title=title)
    return [media.to_dict() for media in medias]
