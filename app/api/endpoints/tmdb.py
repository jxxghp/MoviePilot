from typing import List, Any, Optional

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.tmdb import TmdbChain
from app.core.security import verify_token
from app.db.models.user import User
from app.db.user_oper import get_current_active_superuser_async
from app.modules.themoviedb.tmdb_cache import TmdbCache
from app.schemas.types import MediaType

router = APIRouter()


@router.get(
    "/cache", summary="查询 TheMovieDb 识别缓存", response_model=schemas.Response
)
async def tmdb_recognition_cache(
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """查询可管理的 TheMovieDb 识别缓存。"""
    cache_items = TmdbCache().list_items()
    recognized_count = sum(1 for item in cache_items if item["tmdb_id"])
    return schemas.Response(
        success=True,
        data={
            "count": len(cache_items),
            "recognized": recognized_count,
            "unrecognized": len(cache_items) - recognized_count,
            "data": cache_items,
        },
    )


@router.delete(
    "/cache/{cache_key:path}",
    summary="删除指定 TheMovieDb 识别缓存",
    response_model=schemas.Response,
)
async def delete_tmdb_recognition_cache(
    cache_key: str,
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """按缓存键删除单条 TheMovieDb 识别缓存。"""
    deleted_item = TmdbCache().delete(cache_key)
    if not deleted_item:
        return schemas.Response(success=False, message="TheMovieDb 识别缓存不存在")
    return schemas.Response(success=True, message="TheMovieDb 识别缓存删除成功")


@router.delete(
    "/cache", summary="清空 TheMovieDb 识别缓存", response_model=schemas.Response
)
async def clear_tmdb_recognition_cache(
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """清空全部 TheMovieDb 识别缓存。"""
    TmdbCache().clear()
    return schemas.Response(success=True, message="TheMovieDb 识别缓存清理完成")


@router.get(
    "/seasons/{tmdbid}", summary="TMDB所有季", response_model=List[schemas.TmdbSeason]
)
async def tmdb_seasons(
    tmdbid: int, _: schemas.TokenPayload = Depends(verify_token)
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
    response_model=List[schemas.MediaInfo],
)
async def tmdb_similar(
    tmdbid: int, type_name: str, _: schemas.TokenPayload = Depends(verify_token)
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
    response_model=List[schemas.MediaInfo],
)
async def tmdb_recommend(
    tmdbid: int, type_name: str, _: schemas.TokenPayload = Depends(verify_token)
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
    response_model=List[schemas.MediaInfo],
)
async def tmdb_collection(
    collection_id: int,
    page: Optional[int] = 1,
    count: Optional[int] = 20,
    _: schemas.TokenPayload = Depends(verify_token),
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
    response_model=List[schemas.MediaPerson],
)
async def tmdb_credits(
    tmdbid: int,
    type_name: str,
    page: Optional[int] = 1,
    _: schemas.TokenPayload = Depends(verify_token),
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
    "/person/{person_id}", summary="人物详情", response_model=schemas.MediaPerson
)
async def tmdb_person(
    person_id: int, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    根据人物ID查询人物详情
    """
    return await TmdbChain().async_person_detail(person_id=person_id)


@router.get(
    "/person/credits/{person_id}",
    summary="人物参演作品",
    response_model=List[schemas.MediaInfo],
)
async def tmdb_person_credits(
    person_id: int,
    page: Optional[int] = 1,
    _: schemas.TokenPayload = Depends(verify_token),
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
    response_model=List[schemas.TmdbEpisode],
)
async def tmdb_season_episodes(
    tmdbid: int,
    season: int,
    episode_group: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Any:
    """
    根据TMDBID查询某季的所有信信息
    """
    return await TmdbChain().async_tmdb_episodes(
        tmdbid=tmdbid, season=season, episode_group=episode_group
    )
