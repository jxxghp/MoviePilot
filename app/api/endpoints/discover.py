from typing import Any, List, Optional

from fastapi import APIRouter, Depends

from app import schemas
from app.core.event import eventmanager
from app.core.security import verify_token
from app.schemas import DiscoverSourceEventData
from app.schemas.types import ChainEventType, MediaType
from app.chain.bangumi import BangumiChain
from app.chain.douban import DoubanChain
from app.chain.tmdb import TmdbChain

router = APIRouter()


@router.get("/source", summary="获取探索数据源", response_model=List[schemas.DiscoverMediaSource])
def source(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取探索数据源
    """
    # 广播事件，请示额外的探索数据源支持
    event_data = DiscoverSourceEventData()
    event = eventmanager.send_event(ChainEventType.DiscoverSource, event_data)
    # 使用事件返回的上下文数据
    if event and event.event_data:
        event_data: DiscoverSourceEventData = event.event_data
        if event_data.extra_sources:
            return event_data.extra_sources
    return []


@router.get("/bangumi", summary="探索Bangumi", response_model=List[schemas.MediaInfo])
def bangumi(type: Optional[int] = 2,
            cat: Optional[int] = None,
            sort: Optional[str] = 'rank',
            year: Optional[str] = None,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
            _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    探索Bangumi
    """
    medias = BangumiChain().discover(type=type, cat=cat, sort=sort, year=year,
                                     limit=count, offset=(page - 1) * count)
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get("/douban_movies", summary="探索豆瓣电影", response_model=List[schemas.MediaInfo])
def douban_movies(sort: Optional[str] = "R",
                  tags: Optional[str] = "",
                  page: Optional[int] = 1,
                  count: Optional[int] = 30,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣电影信息
    """
    movies = DoubanChain().douban_discover(mtype=MediaType.MOVIE,
                                           sort=sort, tags=tags, page=page, count=count)
    return [media.to_dict() for media in movies] if movies else []


@router.get("/douban_tvs", summary="探索豆瓣剧集", response_model=List[schemas.MediaInfo])
def douban_tvs(sort: Optional[str] = "R",
               tags: Optional[str] = "",
               page: Optional[int] = 1,
               count: Optional[int] = 30,
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    tvs = DoubanChain().douban_discover(mtype=MediaType.TV,
                                        sort=sort, tags=tags, page=page, count=count)
    return [media.to_dict() for media in tvs] if tvs else []


@router.get("/tmdb_movies", summary="探索TMDB电影", response_model=List[schemas.MediaInfo])
def tmdb_movies(sort_by: Optional[str] = "popularity.desc",
                with_genres: Optional[str] = "",
                with_original_language: Optional[str] = "",
                with_keywords: Optional[str] = "",
                with_watch_providers: Optional[str] = "",
                vote_average: Optional[float] = 0.0,
                vote_count: Optional[int] = 0,
                release_date: Optional[str] = "",
                page: Optional[int] = 1,
                _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览TMDB电影信息
    """
    movies = TmdbChain().tmdb_discover(mtype=MediaType.MOVIE,
                                       sort_by=sort_by,
                                       with_genres=with_genres,
                                       with_original_language=with_original_language,
                                       with_keywords=with_keywords,
                                       with_watch_providers=with_watch_providers,
                                       vote_average=vote_average,
                                       vote_count=vote_count,
                                       release_date=release_date,
                                       page=page)
    return [movie.to_dict() for movie in movies] if movies else []


@router.get("/tmdb_tvs", summary="探索TMDB剧集", response_model=List[schemas.MediaInfo])
def tmdb_tvs(sort_by: Optional[str] = "popularity.desc",
             with_genres: Optional[str] = "",
             with_original_language: Optional[str] = "",
             with_keywords: Optional[str] = "",
             with_watch_providers: Optional[str] = "",
             vote_average: Optional[float] = 0.0,
             vote_count: Optional[int] = 0,
             release_date: Optional[str] = "",
             page: Optional[int] = 1,
             _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览TMDB剧集信息
    """
    tvs = TmdbChain().tmdb_discover(mtype=MediaType.TV,
                                    sort_by=sort_by,
                                    with_genres=with_genres,
                                    with_original_language=with_original_language,
                                    with_keywords=with_keywords,
                                    with_watch_providers=with_watch_providers,
                                    vote_average=vote_average,
                                    vote_count=vote_count,
                                    release_date=release_date,
                                    page=page)
    return [tv.to_dict() for tv in tvs] if tvs else []
