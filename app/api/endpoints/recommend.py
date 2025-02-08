from typing import Any, List

from fastapi import APIRouter, Depends

from app import schemas
from app.core.event import eventmanager
from app.core.security import verify_token
from app.schemas.types import ChainEventType
from chain.recommend import RecommendChain
from schemas import RecommendSourceEventData

router = APIRouter()


@router.get("/source", summary="获取推荐数据源", response_model=List[schemas.RecommendMediaSource])
def source(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    获取推荐数据源
    """
    # 广播事件，请示额外的推荐数据源支持
    event_data = RecommendSourceEventData()
    event = eventmanager.send_event(ChainEventType.RecommendSource, event_data)
    # 使用事件返回的上下文数据
    if event and event.event_data:
        event_data: RecommendSourceEventData = event.event_data
        if event_data.extra_sources:
            return event_data.extra_sources
    return []


@router.get("/bangumi_calendar", summary="Bangumi每日放送", response_model=List[schemas.MediaInfo])
def bangumi_calendar(page: int = 1,
                     count: int = 30,
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览Bangumi每日放送
    """
    return RecommendChain().bangumi_calendar(page=page, count=count)


@router.get("/douban_showing", summary="豆瓣正在热映", response_model=List[schemas.MediaInfo])
def douban_showing(page: int = 1,
                   count: int = 30,
                   _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣正在热映
    """
    return RecommendChain().douban_movie_showing(page=page, count=count)


@router.get("/douban_movies", summary="豆瓣电影", response_model=List[schemas.MediaInfo])
def douban_movies(sort: str = "R",
                  tags: str = "",
                  page: int = 1,
                  count: int = 30,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣电影信息
    """
    return RecommendChain().douban_movies(sort=sort, tags=tags, page=page, count=count)


@router.get("/douban_tvs", summary="豆瓣剧集", response_model=List[schemas.MediaInfo])
def douban_tvs(sort: str = "R",
               tags: str = "",
               page: int = 1,
               count: int = 30,
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    return RecommendChain().douban_tvs(sort=sort, tags=tags, page=page, count=count)


@router.get("/douban_movie_top250", summary="豆瓣电影TOP250", response_model=List[schemas.MediaInfo])
def douban_movie_top250(page: int = 1,
                        count: int = 30,
                        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    return RecommendChain().douban_movie_top250(page=page, count=count)


@router.get("/douban_tv_weekly_chinese", summary="豆瓣国产剧集周榜", response_model=List[schemas.MediaInfo])
def douban_tv_weekly_chinese(page: int = 1,
                             count: int = 30,
                             _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    中国每周剧集口碑榜
    """
    return RecommendChain().douban_tv_weekly_chinese(page=page, count=count)


@router.get("/douban_tv_weekly_global", summary="豆瓣全球剧集周榜", response_model=List[schemas.MediaInfo])
def douban_tv_weekly_global(page: int = 1,
                            count: int = 30,
                            _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    全球每周剧集口碑榜
    """
    return RecommendChain().douban_tv_weekly_global(page=page, count=count)


@router.get("/douban_tv_animation", summary="豆瓣动画剧集", response_model=List[schemas.MediaInfo])
def douban_tv_animation(page: int = 1,
                        count: int = 30,
                        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    热门动画剧集
    """
    return RecommendChain().douban_tv_animation(page=page, count=count)


@router.get("/douban_movie_hot", summary="豆瓣热门电影", response_model=List[schemas.MediaInfo])
def douban_movie_hot(page: int = 1,
                     count: int = 30,
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    热门电影
    """
    return RecommendChain().douban_movie_hot(page=page, count=count)


@router.get("/douban_tv_hot", summary="豆瓣热门电视剧", response_model=List[schemas.MediaInfo])
def douban_tv_hot(page: int = 1,
                  count: int = 30,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    热门电视剧
    """
    return RecommendChain().douban_tv_hot(page=page, count=count)


@router.get("/tmdb_movies", summary="TMDB电影", response_model=List[schemas.MediaInfo])
def tmdb_movies(sort_by: str = "popularity.desc",
                with_genres: str = "",
                with_original_language: str = "",
                with_keywords: str = "",
                with_watch_providers: str = "",
                vote_average: float = 0,
                vote_count: int = 0,
                release_date: str = "",
                page: int = 1,
                _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览TMDB电影信息
    """
    return RecommendChain().tmdb_movies(sort_by=sort_by,
                                        with_genres=with_genres,
                                        with_original_language=with_original_language,
                                        with_keywords=with_keywords,
                                        with_watch_providers=with_watch_providers,
                                        vote_average=vote_average,
                                        vote_count=vote_count,
                                        release_date=release_date,
                                        page=page)


@router.get("/tmdb_tvs", summary="TMDB剧集", response_model=List[schemas.MediaInfo])
def tmdb_tvs(sort_by: str = "popularity.desc",
             with_genres: str = "",
             with_original_language: str = "",
             with_keywords: str = "",
             with_watch_providers: str = "",
             vote_average: float = 0,
             vote_count: int = 0,
             release_date: str = "",
             page: int = 1,
             _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览TMDB剧集信息
    """
    return RecommendChain().tmdb_tvs(sort_by=sort_by,
                                     with_genres=with_genres,
                                     with_original_language=with_original_language,
                                     with_keywords=with_keywords,
                                     with_watch_providers=with_watch_providers,
                                     vote_average=vote_average,
                                     vote_count=vote_count,
                                     release_date=release_date,
                                     page=page)


@router.get("/tmdb_trending", summary="TMDB流行趋势", response_model=List[schemas.MediaInfo])
def tmdb_trending(page: int = 1,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    TMDB流行趋势
    """
    return RecommendChain().tmdb_trending(page=page)
