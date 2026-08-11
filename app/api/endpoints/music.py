from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app import schemas
from app.chain.media import MediaChain
from app.chain.music import MusicChain
from app.schemas.types import MediaType
from app.core.context import MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.core.security import verify_token
from app.db.models.user import User
from app.db.user_oper import get_current_active_superuser_async
from app.modules.listenbrainz import (
    LISTENBRAINZ_CHART_RANGES,
    LISTENBRAINZ_FRESH_MAX_DAYS,
    LISTENBRAINZ_FRESH_SORTS,
)
from app.modules.musicbrainz.music_cache import MusicBrainzCache

router = APIRouter()

CountParam = Annotated[int, Query(ge=1, le=100)]
PageParam = Annotated[int, Query(ge=1)]
MusicSourceParam = Annotated[
    str,
    Query(pattern="^(musicbrainz|theaudiodb|doubanmusic)$"),
]
MusicModeParam = Annotated[str, Query(pattern="^(chart|fresh)$")]
MusicEntityParam = Annotated[str, Query(pattern="^(recording|album)$")]
MusicRangeParam = Annotated[str, Query(pattern=f"^({'|'.join(LISTENBRAINZ_CHART_RANGES)})$")]
MusicSortParam = Annotated[str, Query(pattern="^listen_count\\.(desc|asc)$")]
MusicFreshSortParam = Annotated[str, Query(pattern=f"^({'|'.join(LISTENBRAINZ_FRESH_SORTS)})$")]
MusicDaysParam = Annotated[int, Query(ge=1, le=LISTENBRAINZ_FRESH_MAX_DAYS)]
# MusicBrainz 浏览接口支持的专辑类型筛选
MusicAlbumTypeParam = Annotated[
    Optional[str],
    Query(pattern="^(album|single|ep|broadcast|other|compilation|soundtrack|live|remix)$"),
]


def _serialize_music(info: MusicInfo) -> schemas.MusicInfo:
    """将内部音乐信息转换为 REST 响应模型。"""
    return schemas.MusicInfo(**info.to_dict())


def _serialize_album(info: MusicAlbumInfo) -> schemas.MusicAlbumInfo:
    """将内部专辑信息转换为 REST 响应模型。"""
    return schemas.MusicAlbumInfo(**info.to_dict())


def _serialize_artist(info: MusicArtistInfo) -> schemas.MusicArtistInfo:
    """将内部艺术家信息转换为 REST 响应模型。"""
    return schemas.MusicArtistInfo(**info.to_dict())


@router.post(
    "/recognize",
    summary="识别音乐元数据详情",
    response_model=schemas.MusicInfo,
)
async def recognize_music(
        request: schemas.MusicRecognizeRequest,
        _: schemas.TokenPayload = Depends(verify_token),
) -> schemas.MusicInfo:
    """根据音乐元数据来源和媒体 ID 获取标准详情，与影视识别共用统一入口。"""
    recognize_kwargs = {
        "source": request.source,
        "mediaid": request.media_id,
        "mtype": MediaType.MUSIC,
    }
    if request.music_type is not None:
        recognize_kwargs["music_type"] = request.music_type
    info = await MediaChain().async_recognize_media(
        **recognize_kwargs,
    )
    if not info:
        raise HTTPException(status_code=404, detail="未识别到音乐信息")
    return _serialize_music(info)


@router.get(
    "/cache", summary="查询音乐识别缓存", response_model=schemas.Response
)
async def music_recognition_cache(
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """查询可管理的 MusicBrainz 识别缓存。"""
    cache_items = MusicBrainzCache().list_items()
    recognized_count = sum(1 for item in cache_items if item["media_id"])
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
    summary="删除指定音乐识别缓存",
    response_model=schemas.Response,
)
async def delete_music_recognition_cache(
    cache_key: str,
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """按缓存键删除单条 MusicBrainz 识别缓存。"""
    deleted_item = MusicBrainzCache().delete(cache_key)
    if not deleted_item:
        return schemas.Response(success=False, message="音乐识别缓存不存在")
    return schemas.Response(success=True, message="音乐识别缓存删除成功")


@router.delete(
    "/cache", summary="清空音乐识别缓存", response_model=schemas.Response
)
async def clear_music_recognition_cache(
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """清空全部 MusicBrainz 识别缓存。"""
    MusicBrainzCache().clear()
    return schemas.Response(success=True, message="音乐识别缓存清理完成")


@router.get(
    "/explore",
    summary="探索音乐",
    response_model=list[schemas.MusicInfo],
)
async def explore_music(
        page: PageParam = 1,
        count: CountParam = 30,
        mode: MusicModeParam = "chart",
        entity: MusicEntityParam = "recording",
        range_name: MusicRangeParam = "this_month",
        sort_by: MusicSortParam = "listen_count.desc",
        sort: MusicFreshSortParam = "release_date",
        days: MusicDaysParam = 14,
        past: bool = True,
        future: bool = True,
        min_listen_count: Annotated[int, Query(ge=0)] = 0,
        with_cover: bool = False,
        _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MusicInfo]:
    """按 ListenBrainz 官方热门榜单或新发行两种模式返回可订阅的音乐候选。"""
    chain = MusicChain()
    if mode == "fresh":
        results = await chain.async_fresh_releases(
            days=days,
            sort=sort,
            past=past,
            future=future,
            page=page,
            count=count,
            with_cover=with_cover,
        )
    else:
        results = await chain.async_chart(
            range_name=range_name,
            page=page,
            count=count,
            sort_by=sort_by,
            min_listen_count=min_listen_count,
            with_cover=with_cover,
            entity=entity,
        )
    return [_serialize_music(info) for info in results]


@router.get(
    "/album/{album_id}",
    summary="查询音乐专辑详情",
    response_model=schemas.MusicAlbumInfo,
)
async def music_album(
        album_id: str,
        source: MusicSourceParam = "musicbrainz",
        _: schemas.TokenPayload = Depends(verify_token),
) -> schemas.MusicAlbumInfo:
    """按专辑标准 ID 返回专辑详情、曲目列表和发行版本。"""
    info = await MusicChain().async_album(source=source, media_id=album_id)
    if not info:
        raise HTTPException(status_code=404, detail="未识别到专辑信息")
    return _serialize_album(info)


@router.get(
    "/artist/{artist_id}/albums",
    summary="查询艺术家的专辑列表",
    response_model=list[schemas.MusicInfo],
)
async def music_artist_albums(
        artist_id: str,
        page: PageParam = 1,
        count: CountParam = 30,
        album_type: MusicAlbumTypeParam = None,
        source: MusicSourceParam = "musicbrainz",
        _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MusicInfo]:
    """按艺术家标准 ID 分页返回其专辑、EP 和单曲。"""
    results = await MusicChain().async_artist_albums(
        source=source,
        media_id=artist_id,
        page=page,
        count=count,
        album_type=album_type,
    )
    return [_serialize_music(info) for info in results]


@router.get(
    "/artist/{artist_id}/related",
    summary="查询关联艺术家",
    response_model=list[schemas.MusicArtistInfo],
)
async def music_artist_related(
        artist_id: str,
        count: CountParam = 24,
        source: MusicSourceParam = "musicbrainz",
        _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MusicArtistInfo]:
    """按艺术家关系返回可继续浏览的关联艺术家。"""
    results = await MusicChain().async_artist_related(
        source=source,
        media_id=artist_id,
        count=count,
    )
    return [_serialize_artist(info) for info in results]


@router.get(
    "/artist/{artist_id}",
    summary="查询音乐艺术家详情",
    response_model=schemas.MusicArtistInfo,
)
async def music_artist(
        artist_id: str,
        source: MusicSourceParam = "musicbrainz",
        _: schemas.TokenPayload = Depends(verify_token),
) -> schemas.MusicArtistInfo:
    """按艺术家标准 ID 返回艺术家详情。"""
    info = await MusicChain().async_artist(source=source, media_id=artist_id)
    if not info:
        raise HTTPException(status_code=404, detail="未识别到艺术家信息")
    return _serialize_artist(info)
