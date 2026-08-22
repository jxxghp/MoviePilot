from typing import List, Any, Optional

from fastapi import Depends

from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.response import Response as _SchemaResponse
from app.schemas.tmdb import TmdbRecognitionCacheData as _SchemaTmdbRecognitionCacheData
from app.schemas.tmdb import TmdbSeason as _SchemaTmdbSeason
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.tmdb import TmdbEpisode as _SchemaTmdbEpisode
from app.schemas.workflow import MediaInfo as _SchemaMediaInfo
from app.api.response import ResponseAPIRouter
from app.application.orchestration.tmdb import TmdbChain
from app.application.configuration import get_api_runtime_config_snapshot
from app.adapters.web.security.access import verify_token
from app.application.configuration import get_configured_system_config
from app.api.deps import get_current_active_superuser_async
from app.schemas.types import MediaType, SystemConfigKey

router = ResponseAPIRouter()


@router.get(
    "/cache",
    summary="查询 TheMovieDb 识别缓存",
    response_model=_SchemaResponse[_SchemaTmdbRecognitionCacheData],
)
async def tmdb_recognition_cache(
    _: object = Depends(get_current_active_superuser_async),
) -> _SchemaResponse:
    """查询可管理的 TheMovieDb 识别缓存。"""
    cache_items = TmdbChain().cache_items()
    recognized_count = sum(1 for item in cache_items if item["tmdb_id"])
    return _SchemaResponse(
        success=True,
        data={
            "count": len(cache_items),
            "recognized": recognized_count,
            "unrecognized": len(cache_items) - recognized_count,
            "shared_recognized": get_configured_system_config().get(
                SystemConfigKey.MediaRecognizeShareCount
            ) or 0,
            "shared_recognize_enabled": get_api_runtime_config_snapshot().media_recognize_share,
            "data": cache_items,
        },
    )


@router.delete(
    "/cache/{cache_key:path}",
    summary="删除指定 TheMovieDb 识别缓存",
    response_model=_SchemaResponse[None],
)
async def delete_tmdb_recognition_cache(
    cache_key: str,
    _: object = Depends(get_current_active_superuser_async),
) -> _SchemaResponse:
    """按缓存键删除单条 TheMovieDb 识别缓存。"""
    deleted_item = TmdbChain().delete_cache(cache_key)
    if not deleted_item:
        return _SchemaResponse(success=False, message="TheMovieDb 识别缓存不存在")
    return _SchemaResponse(success=True, message="TheMovieDb 识别缓存删除成功")


@router.delete(
    "/cache", summary="清空 TheMovieDb 识别缓存", response_model=_SchemaResponse[None]
)
async def clear_tmdb_recognition_cache(
    _: object = Depends(get_current_active_superuser_async),
) -> _SchemaResponse:
    """清空全部 TheMovieDb 识别缓存。"""
    TmdbChain().clear_cache()
    return _SchemaResponse(success=True, message="TheMovieDb 识别缓存清理完成")


@router.get(
    "/seasons/{tmdbid}", summary="TMDB所有季", response_model=List[_SchemaTmdbSeason]
)
async def tmdb_seasons(
    tmdbid: int, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    根据TMDBID查询themoviedb所有季信息
    """
    seasons_info = await TmdbChain().async_tmdb_seasons(tmdbid=tmdbid)
    if seasons_info:
        return seasons_info
    return []


@router.get(
    "/similar/{tmdbid}/{type_name}",
    summary="类似电影/电视剧",
    response_model=List[_SchemaMediaInfo],
)
async def tmdb_similar(
    tmdbid: int, type_name: str, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    根据TMDBID查询类似电影/电视剧，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        medias = await TmdbChain().async_movie_similar(tmdbid=tmdbid)
    elif mediatype == MediaType.TV:
        medias = await TmdbChain().async_tv_similar(tmdbid=tmdbid)
    else:
        return []
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get(
    "/recommend/{tmdbid}/{type_name}",
    summary="推荐电影/电视剧",
    response_model=List[_SchemaMediaInfo],
)
async def tmdb_recommend(
    tmdbid: int, type_name: str, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    根据TMDBID查询推荐电影/电视剧，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        medias = await TmdbChain().async_movie_recommend(tmdbid=tmdbid)
    elif mediatype == MediaType.TV:
        medias = await TmdbChain().async_tv_recommend(tmdbid=tmdbid)
    else:
        return []
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get(
    "/collection/{collection_id}",
    summary="系列合集详情",
    response_model=List[_SchemaMediaInfo],
)
async def tmdb_collection(
    collection_id: int,
    page: Optional[int] = 1,
    count: Optional[int] = 20,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据合集ID查询合集详情
    """
    medias = await TmdbChain().async_tmdb_collection(collection_id=collection_id)
    if medias:
        return [media.to_dict() for media in medias][(page - 1) * count : page * count]
    return []


@router.get(
    "/credits/{tmdbid}/{type_name}",
    summary="演员阵容",
    response_model=List[_SchemaMediaPerson],
)
async def tmdb_credits(
    tmdbid: int,
    type_name: str,
    page: Optional[int] = 1,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据TMDBID查询演员阵容，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        persons = await TmdbChain().async_movie_credits(tmdbid=tmdbid, page=page)
    elif mediatype == MediaType.TV:
        persons = await TmdbChain().async_tv_credits(tmdbid=tmdbid, page=page)
    else:
        return []
    return persons or []


@router.get(
    "/person/{person_id}", summary="人物详情", response_model=_SchemaMediaPerson
)
async def tmdb_person(
    person_id: int, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    根据人物ID查询人物详情
    """
    return await TmdbChain().async_person_detail(person_id=person_id)


@router.get(
    "/person/credits/{person_id}",
    summary="人物参演作品",
    response_model=List[_SchemaMediaInfo],
)
async def tmdb_person_credits(
    person_id: int,
    page: Optional[int] = 1,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据人物ID查询人物参演作品
    """
    medias = await TmdbChain().async_person_credits(person_id=person_id, page=page)
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get(
    "/{tmdbid}/{season}",
    summary="TMDB季所有集",
    response_model=List[_SchemaTmdbEpisode],
)
async def tmdb_season_episodes(
    tmdbid: int,
    season: int,
    episode_group: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据TMDBID查询某季的所有信信息
    """
    return await TmdbChain().async_tmdb_episodes(
        tmdbid=tmdbid, season=season, episode_group=episode_group
    )
