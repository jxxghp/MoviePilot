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


@router.get("/search", response_model=List[schemas.MediaInfo])
async def search_by_title(title: str,
                          _: User = Depends(get_current_active_user)) -> Any:
    """
    模糊搜索媒体信息列表
    """
    _, medias = MediaChain().search(title=title)
    return [media.to_dict() for media in medias]


@router.get("/doubanid", response_model=schemas.Context)
async def recognize_doubanid(doubanid: str,
                             _: User = Depends(get_current_active_user)) -> Any:
    """
    根据豆瓣ID识别媒体信息
    """
    # 识别媒体信息
    context = MediaChain().recognize_by_doubanid(doubanid=doubanid)
    return context.to_dict()


@router.get("/tmdbinfo", response_model=schemas.MediaInfo)
async def tmdb_info(tmdbid: int, type_name: str) -> Any:
    """
    根据TMDBID查询themoviedb媒体信息
    """
    mtype = MediaType.MOVIE if type_name == MediaType.MOVIE.value else MediaType.TV
    media = MediaChain().recognize_media(tmdbid=tmdbid, mtype=mtype)
    if media:
        return media.to_dict()
    else:
        return schemas.MediaInfo()


@router.get("/doubaninfo", response_model=schemas.MediaInfo)
async def douban_info(doubanid: str) -> Any:
    """
    根据豆瓣ID查询豆瓣媒体信息
    """
    doubaninfo = MediaChain().douban_info(doubanid=doubanid)
    if doubaninfo:
        return MediaInfo(douban_info=doubaninfo).to_dict()
    else:
        return schemas.MediaInfo()


@router.get("/tmdbmovies", response_model=List[schemas.MediaInfo])
async def tmdb_movies(sort_by: str = "popularity.desc",
                      with_genres: str = "",
                      with_original_language: str = "",
                      page: int = 1,
                      _: User = Depends(get_current_active_user)) -> Any:
    """
    浏览TMDB电影信息
    """
    movies = MediaChain().tmdb_movies(sort_by=sort_by,
                                      with_genres=with_genres,
                                      with_original_language=with_original_language,
                                      page=page)
    return [movie.to_dict() for movie in movies]


@router.get("/tmdbtvs", response_model=List[schemas.MediaInfo])
async def tmdb_tvs(sort_by: str = "popularity.desc",
                   with_genres: str = "",
                   with_original_language: str = "",
                   page: int = 1,
                   _: User = Depends(get_current_active_user)) -> Any:
    """
    浏览TMDB剧集信息
    """
    tvs = MediaChain().tmdb_tvs(sort_by=sort_by,
                                with_genres=with_genres,
                                with_original_language=with_original_language,
                                page=page)
    return [tv.to_dict() for tv in tvs]


@router.get("/doubanmovies", response_model=List[schemas.MediaInfo])
async def douban_movies(sort: str = "R",
                        tags: str = "",
                        start: int = 0,
                        count: int = 30,
                        _: User = Depends(get_current_active_user)) -> Any:
    """
    浏览豆瓣电影信息
    """
    movies = MediaChain().douban_movies(sort=sort, tags=tags, start=start, count=count)
    return [movie.to_dict() for movie in movies]


@router.get("/doubantvs", response_model=List[schemas.MediaInfo])
async def douban_tvs(sort: str = "R",
                     tags: str = "",
                     start: int = 0,
                     count: int = 30,
                     _: User = Depends(get_current_active_user)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    tvs = MediaChain().douban_tvs(sort=sort, tags=tags, start=start, count=count)
    return [tv.to_dict() for tv in tvs]
