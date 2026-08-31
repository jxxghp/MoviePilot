from typing import Any, Awaitable, List, Optional

from fastapi import Depends, HTTPException, status

from app.adapters.web.security.access import verify_token
from app.api.response import ResponseAPIRouter
from app.application.music.projection import simplify_music_info
from app.chain.listenbrainz import (
    LISTENBRAINZ_CHART_RANGES,
    LISTENBRAINZ_FRESH_MAX_DAYS,
    LISTENBRAINZ_FRESH_SORTS,
)
from app.chain.recommend import RecommendChain
from app.domain.media import normalize_music_type
from app.runtime.events import eventmanager
from app.schemas.event import RecommendMediaSource as _SchemaRecommendMediaSource
from app.schemas.event import RecommendSourceEventData
from app.schemas.exception import TMDbException
from app.schemas.recommend import AgentRecommendationItem as _SchemaAgentRecommendationItem
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.transfer import MusicInfo as _SchemaMusicInfo
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    ChainEventType,
    MediaType,
    media_type_to_agent,
)
from app.schemas.workflow import MediaInfo as _SchemaMediaInfo

router = ResponseAPIRouter()


async def _require_tmdb_result(operation: Awaitable[List[Any]]) -> List[Any]:
    """保留 TMDB 成功空列表，并把上游请求异常转换为明确的网关错误。"""
    try:
        return await operation
    except TMDbException as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="TMDB请求失败",
        ) from error


@router.get(
    "/source",
    summary="获取推荐数据源",
    response_model=List[_SchemaRecommendMediaSource],
)
def source(_: _SchemaTokenPayload = Depends(verify_token)) -> Any:
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


@router.get(
    "/agent",
    summary="统一获取 Agent 推荐结果",
    response_model=_SchemaResponse[list[_SchemaAgentRecommendationItem]],
)
async def agent_recommendations(
    source: str = "tmdb_trending",
    media_type: str = "all",
    page: int = 1,
    music_type: Optional[str] = None,
    range_name: str = "this_month",
    sort_by: str = "listen_count.desc",
    days: int = 14,
    fresh_sort: str = "release_date",
    past: bool = True,
    future: bool = True,
    min_listen_count: int = 0,
    with_cover: bool = False,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaResponse:
    """按稳定来源标识返回有界影视、动画或音乐推荐结果。"""
    page = max(1, page)
    count = 20
    if media_type != "all":
        media_type_enum = MediaType.from_agent(media_type)
        if media_type_enum is None:
            return _SchemaResponse(success=False, message="无效的媒体类型")
        media_type = media_type_enum.to_agent()
    chain = RecommendChain()
    if source in {"listenbrainz_chart", "listenbrainz_fresh"}:
        if media_type not in {"all", "music"}:
            return _SchemaResponse(
                success=False,
                message="ListenBrainz 来源只支持音乐媒体类型",
            )
        normalized_music_type = normalize_music_type(music_type, allow_artist=False) if music_type else None
        if music_type and normalized_music_type is None:
            return _SchemaResponse(success=False, message="无效的音乐实体类型")
        if source == "listenbrainz_chart":
            if range_name not in LISTENBRAINZ_CHART_RANGES:
                return _SchemaResponse(success=False, message="无效的榜单周期")
            if sort_by not in {"listen_count.desc", "listen_count.asc"}:
                return _SchemaResponse(success=False, message="无效的榜单排序")
            music_results = await chain.async_music_chart(
                range_name=range_name,
                page=page,
                count=count,
                sort_by=sort_by,
                min_listen_count=max(0, min_listen_count),
                with_cover=with_cover,
                entity=normalized_music_type or MUSIC_ENTITY_RECORDING,
            )
        else:
            if normalized_music_type not in {None, MUSIC_ENTITY_ALBUM}:
                return _SchemaResponse(
                    success=False,
                    message="新发行结果只支持专辑",
                )
            if fresh_sort not in LISTENBRAINZ_FRESH_SORTS:
                return _SchemaResponse(success=False, message="无效的新发行排序")
            if not past and not future:
                return _SchemaResponse(
                    success=False,
                    message="past 和 future 不能同时为 false",
                )
            music_results = await chain.async_music_fresh_releases(
                days=max(1, min(days, LISTENBRAINZ_FRESH_MAX_DAYS)),
                sort=fresh_sort,
                past=past,
                future=future,
                page=page,
                count=count,
                with_cover=with_cover,
            )
        return _SchemaResponse(
            success=True,
            data=[simplify_music_info(item) for item in music_results or []],
        )
    if media_type == "music":
        return _SchemaResponse(
            success=False,
            message="音乐推荐需使用 ListenBrainz 来源",
        )
    source_calls = {
        "tmdb_trending": lambda: chain.async_tmdb_trending(page=page),
        "tmdb_movies": lambda: chain.async_tmdb_movies(page=page),
        "tmdb_tvs": lambda: chain.async_tmdb_tvs(page=page),
        "douban_movie_hot": lambda: chain.async_douban_movie_hot(page=page, count=count),
        "douban_tv_hot": lambda: chain.async_douban_tv_hot(page=page, count=count),
        "douban_movie_showing": lambda: chain.async_douban_movie_showing(page=page, count=count),
        "douban_movies": lambda: chain.async_douban_movies(page=page, count=count),
        "douban_tvs": lambda: chain.async_douban_tvs(page=page, count=count),
        "douban_movie_top250": lambda: chain.async_douban_movie_top250(page=page, count=count),
        "douban_tv_weekly_chinese": lambda: chain.async_douban_tv_weekly_chinese(page=page, count=count),
        "douban_tv_weekly_global": lambda: chain.async_douban_tv_weekly_global(page=page, count=count),
        "douban_tv_animation": lambda: chain.async_douban_tv_animation(page=page, count=count),
        "bangumi_calendar": lambda: chain.async_bangumi_calendar(page=page, count=count),
    }
    if source == "douban_hot":
        results = []
        if media_type in {"all", "movie"}:
            results.extend(await chain.async_douban_movie_hot(page=page, count=count))
        if media_type in {"all", "tv"}:
            results.extend(await chain.async_douban_tv_hot(page=page, count=count))
    else:
        operation = source_calls.get(source)
        if operation is None:
            return _SchemaResponse(success=False, message=f"不支持的推荐来源: {source}")
        results = await operation()
    projected = []
    for item in (results or [])[:count]:
        if not isinstance(item, dict):
            continue
        projected.append(
            {
                "title": item.get("title"),
                "en_title": item.get("en_title"),
                "year": item.get("year"),
                "type": media_type_to_agent(item.get("type")),
                "season": item.get("season"),
                "tmdb_id": item.get("tmdb_id"),
                "imdb_id": item.get("imdb_id"),
                "douban_id": item.get("douban_id"),
                "bangumi_id": item.get("bangumi_id"),
                "anilist_id": item.get("anilist_id"),
                "media_source": item.get("media_source"),
                "media_id": item.get("media_id"),
                "vote_average": item.get("vote_average"),
                "poster_path": item.get("poster_path"),
                "detail_link": item.get("detail_link"),
            }
        )
    return _SchemaResponse(success=True, data=projected)


@router.get(
    "/bangumi_calendar",
    summary="Bangumi每日放送",
    response_model=List[_SchemaMediaInfo],
)
async def bangumi_calendar(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览Bangumi每日放送
    """
    return await RecommendChain().async_bangumi_calendar(page=page, count=count)


@router.get(
    "/music_weekly",
    summary="ListenBrainz 本周热门音乐",
    response_model=List[_SchemaMusicInfo],
)
async def music_weekly(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """浏览本周全站热门音乐。"""
    return await RecommendChain().async_music_weekly(page=page, count=count)


@router.get(
    "/music_douban",
    summary="豆瓣音乐推荐",
    response_model=List[_SchemaMusicInfo],
)
async def music_douban(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """浏览豆瓣音乐推荐合集。"""
    return await RecommendChain().async_music_douban(page=page, count=count)


@router.get("/douban_showing", summary="豆瓣正在热映", response_model=List[_SchemaMediaInfo])
async def douban_showing(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览豆瓣正在热映
    """
    return await RecommendChain().async_douban_movie_showing(page=page, count=count)


@router.get("/douban_movies", summary="豆瓣电影", response_model=List[_SchemaMediaInfo])
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
    return await RecommendChain().async_douban_movies(sort=sort, tags=tags, page=page, count=count)


@router.get("/douban_tvs", summary="豆瓣剧集", response_model=List[_SchemaMediaInfo])
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
    return await RecommendChain().async_douban_tvs(sort=sort, tags=tags, page=page, count=count)


@router.get(
    "/douban_movie_top250",
    summary="豆瓣电影TOP250",
    response_model=List[_SchemaMediaInfo],
)
async def douban_movie_top250(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    浏览豆瓣剧集信息
    """
    return await RecommendChain().async_douban_movie_top250(page=page, count=count)


@router.get(
    "/douban_tv_weekly_chinese",
    summary="豆瓣国产剧集周榜",
    response_model=List[_SchemaMediaInfo],
)
async def douban_tv_weekly_chinese(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    中国每周剧集口碑榜
    """
    return await RecommendChain().async_douban_tv_weekly_chinese(page=page, count=count)


@router.get(
    "/douban_tv_weekly_global",
    summary="豆瓣全球剧集周榜",
    response_model=List[_SchemaMediaInfo],
)
async def douban_tv_weekly_global(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    全球每周剧集口碑榜
    """
    return await RecommendChain().async_douban_tv_weekly_global(page=page, count=count)


@router.get(
    "/douban_tv_animation",
    summary="豆瓣动画剧集",
    response_model=List[_SchemaMediaInfo],
)
async def douban_tv_animation(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    热门动画剧集
    """
    return await RecommendChain().async_douban_tv_animation(page=page, count=count)


@router.get("/douban_movie_hot", summary="豆瓣热门电影", response_model=List[_SchemaMediaInfo])
async def douban_movie_hot(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    热门电影
    """
    return await RecommendChain().async_douban_movie_hot(page=page, count=count)


@router.get("/douban_tv_hot", summary="豆瓣热门电视剧", response_model=List[_SchemaMediaInfo])
async def douban_tv_hot(
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    热门电视剧
    """
    return await RecommendChain().async_douban_tv_hot(page=page, count=count)


@router.get("/tmdb_movies", summary="TMDB电影", response_model=List[_SchemaMediaInfo])
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
    return await _require_tmdb_result(
        RecommendChain().async_tmdb_movies(
            sort_by=sort_by,
            with_genres=with_genres,
            with_original_language=with_original_language,
            with_keywords=with_keywords,
            with_watch_providers=with_watch_providers,
            vote_average=vote_average,
            vote_count=vote_count,
            release_date=release_date,
            page=page,
            raise_exception=True,
        )
    )


@router.get("/tmdb_tvs", summary="TMDB剧集", response_model=List[_SchemaMediaInfo])
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
    return await _require_tmdb_result(
        RecommendChain().async_tmdb_tvs(
            sort_by=sort_by,
            with_genres=with_genres,
            with_original_language=with_original_language,
            with_keywords=with_keywords,
            with_watch_providers=with_watch_providers,
            vote_average=vote_average,
            vote_count=vote_count,
            release_date=release_date,
            page=page,
            raise_exception=True,
        )
    )


@router.get("/tmdb_trending", summary="TMDB流行趋势", response_model=List[_SchemaMediaInfo])
async def tmdb_trending(page: Optional[int] = 1, _: _SchemaTokenPayload = Depends(verify_token)) -> Any:
    """
    TMDB流行趋势
    """
    return await _require_tmdb_result(RecommendChain().async_tmdb_trending(page=page, raise_exception=True))
