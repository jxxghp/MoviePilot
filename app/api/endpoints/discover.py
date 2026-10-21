"""
提供跨媒体来源的发现和筛选选项接口。
"""

from typing import Any, List, Optional

from fastapi import Depends

from app.adapters.web.security.access import verify_token
from app.api.response import (
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.chain.bangumi import BangumiChain
from app.chain.douban import DoubanChain
from app.chain.tmdb import TmdbChain
from app.runtime.events import eventmanager
from app.schemas.context import MediaInfo as _SchemaMediaInfo
from app.schemas.event import DiscoverMediaSource as _SchemaDiscoverMediaSource
from app.schemas.event import DiscoverSourceEventData
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import ChainEventType, MediaType

router = ResponseAPIRouter()


@router.get(
    "/source",
    summary="获取探索数据源",
    response_model=List[_SchemaDiscoverMediaSource],
)
def source(_: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    获取探索数据源
    """
    # 广播事件，请示额外的探索数据源支持
    event_data = DiscoverSourceEventData()
    event = eventmanager.send_event(ChainEventType.DiscoverSource, event_data)
    # 使用事件返回的上下文数据
    if event and isinstance(event.event_data, DiscoverSourceEventData):
        event_data = event.event_data
        if event_data.extra_sources:
            return event_data.extra_sources
    return []


@router.get("/bangumi", summary="探索Bangumi", response_model=List[_SchemaMediaInfo])
async def bangumi(
    type: Optional[int] = 2,
    cat: Optional[int] = None,
    sort: Optional[str] = "rank",
    year: Optional[str] = None,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    探索Bangumi
    """
    medias = await BangumiChain().async_discover(
        type=type, cat=cat, sort=sort, year=year, limit=count, offset=((page or 1) - 1) * (count if count is not None else 30)
    )
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get(
    "/douban_movies", summary="探索豆瓣电影", response_model=List[_SchemaMediaInfo]
)
async def douban_movies(
    sort: Optional[str] = "R",
    tags: Optional[str] = "",
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览豆瓣电影信息
    """
    movies = await DoubanChain().async_douban_discover(
        mtype=MediaType.MOVIE, sort=sort or "R", tags=tags or "", page=page, count=count
    )
    return [media.to_dict() for media in movies] if movies else []


@router.get(
    "/douban_tvs", summary="探索豆瓣剧集", response_model=List[_SchemaMediaInfo]
)
async def douban_tvs(
    sort: Optional[str] = "R",
    tags: Optional[str] = "",
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览豆瓣剧集信息
    """
    tvs = await DoubanChain().async_douban_discover(
        mtype=MediaType.TV, sort=sort or "R", tags=tags or "", page=page, count=count
    )
    return [media.to_dict() for media in tvs] if tvs else []


@router.get(
    "/tmdb_movies", summary="探索TMDB电影", response_model=List[_SchemaMediaInfo]
)
async def tmdb_movies(
    sort_by: Optional[str] = "popularity.desc",
    with_genres: Optional[str] = "",
    with_original_language: Optional[str] = "",
    with_keywords: Optional[str] = "",
    with_watch_providers: Optional[str] = "",
    vote_average: Optional[float] = 0.0,
    vote_count: Optional[int] = 0,
    release_date: Optional[str] = "",
    page: Optional[int] = 1,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览TMDB电影信息
    """
    movies = await TmdbChain().async_tmdb_discover(
        mtype=MediaType.MOVIE,
        sort_by=sort_by or "popularity.desc",
        with_genres=with_genres or "",
        with_original_language=with_original_language or "",
        with_keywords=with_keywords or "",
        with_watch_providers=with_watch_providers or "",
        vote_average=vote_average or 0.0,
        vote_count=vote_count or 0,
        release_date=release_date or "",
        page=page,
    )
    return [movie.to_dict() for movie in movies] if movies else []


@router.get("/tmdb_tvs", summary="探索TMDB剧集", response_model=List[_SchemaMediaInfo])
async def tmdb_tvs(
    sort_by: Optional[str] = "popularity.desc",
    with_genres: Optional[str] = "",
    with_original_language: Optional[str] = "",
    with_keywords: Optional[str] = "",
    with_watch_providers: Optional[str] = "",
    vote_average: Optional[float] = 0.0,
    vote_count: Optional[int] = 0,
    release_date: Optional[str] = "",
    page: Optional[int] = 1,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览TMDB剧集信息
    """
    tvs = await TmdbChain().async_tmdb_discover(
        mtype=MediaType.TV,
        sort_by=sort_by or "popularity.desc",
        with_genres=with_genres or "",
        with_original_language=with_original_language or "",
        with_keywords=with_keywords or "",
        with_watch_providers=with_watch_providers or "",
        vote_average=vote_average or 0.0,
        vote_count=vote_count or 0,
        release_date=release_date or "",
        page=page,
    )
    return [tv.to_dict() for tv in tvs] if tvs else []
