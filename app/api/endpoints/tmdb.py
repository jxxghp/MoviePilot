from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.core.context import MediaInfo
from app.db.models.user import User
from app.db.userauth import get_current_active_user
from app.schemas.types import MediaType

router = APIRouter()


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
