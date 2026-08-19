from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Query

from app.schemas.music import MusicAlbumInfo as _SchemaMusicAlbumInfo
from app.schemas.music import MusicArtistInfo as _SchemaMusicArtistInfo
from app.schemas.music import MusicRecognitionCacheData as _SchemaMusicRecognitionCacheData
from app.schemas.music import MusicRecognizeRequest as _SchemaMusicRecognizeRequest
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.transfer import MusicInfo as _SchemaMusicInfo
from app.api.response import ResponseAPIRouter
from app.application.orchestration.media import MediaChain
from app.application.orchestration.recommend import RecommendChain
from app.schemas.types import MediaSource, MediaType
from app.domain.context import MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.adapters.web.security.access import verify_token
from app.api.deps import get_current_active_superuser_async
from app.application.orchestration.listenbrainz import (
    LISTENBRAINZ_CHART_RANGES,
    LISTENBRAINZ_FRESH_MAX_DAYS,
    LISTENBRAINZ_FRESH_SORTS,
)
from app.application.orchestration.musicbrainz import MusicBrainzChain

router = ResponseAPIRouter()

CountParam = Annotated[int, Query(ge=1, le=100)]
PageParam = Annotated[int, Query(ge=1)]
MusicSourceParam = Annotated[
    MediaSource,
    Query(),
]
MusicExploreSourceParam = Annotated[
    MediaSource,
    Query(),
]
MusicModeParam = Annotated[str, Query(pattern="^(chart|fresh)$")]
MusicEntityParam = Annotated[str, Query(pattern="^(recording|album)$")]
MusicRangeParam = Annotated[str, Query(pattern=f"^({'|'.join(LISTENBRAINZ_CHART_RANGES)})$")]
MusicSortParam = Annotated[str, Query(pattern="^listen_count\\.(desc|asc)$")]
MusicFreshSortParam = Annotated[str, Query(pattern=f"^({'|'.join(LISTENBRAINZ_FRESH_SORTS)})$")]
DoubanMusicSortParam = Annotated[str, Query(pattern="^(U|S|R|O)$")]
MusicDaysParam = Annotated[int, Query(ge=1, le=LISTENBRAINZ_FRESH_MAX_DAYS)]
# MusicBrainz 浏览接口支持的专辑类型筛选
MusicAlbumTypeParam = Annotated[
    Optional[str],
    Query(pattern="^(album|single|ep|broadcast|other|compilation|soundtrack|live|remix)$"),
]
_MUSIC_EXPLORE_SOURCES = frozenset({
    MediaSource.MusicBrainz,
    MediaSource.DoubanMusic,
})


def _validate_music_source(
        media_source: MediaSource,
        allowed_sources: Optional[frozenset[MediaSource]] = None,
) -> MediaSource:
    """规范音乐来源；仅来源专属端点额外限制内置来源集合。"""
    try:
        normalized_source = MediaSource(media_source)
    except (TypeError, ValueError) as err:
        raise HTTPException(status_code=422, detail="无效的媒体来源") from err
    if allowed_sources is not None and normalized_source not in allowed_sources:
        raise HTTPException(status_code=422, detail="该媒体来源不支持此音乐接口")
    return normalized_source


def _serialize_music(info: MusicInfo) -> _SchemaMusicInfo:
    """将内部音乐信息转换为 REST 响应模型。"""
    return _SchemaMusicInfo(**info.to_dict())


def _serialize_album(info: MusicAlbumInfo) -> _SchemaMusicAlbumInfo:
    """将内部专辑信息转换为 REST 响应模型。"""
    return _SchemaMusicAlbumInfo(**info.to_dict())


def _serialize_artist(info: MusicArtistInfo) -> _SchemaMusicArtistInfo:
    """将内部艺术家信息转换为 REST 响应模型。"""
    return _SchemaMusicArtistInfo(**info.to_dict())


@router.post(
    "/recognize",
    summary="识别音乐元数据详情",
    response_model=_SchemaMusicInfo,
)
async def recognize_music(
        request: _SchemaMusicRecognizeRequest,
        _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaMusicInfo:
    """根据音乐元数据来源和媒体 ID 获取标准详情，与影视识别共用统一入口。"""
    recognize_kwargs = {
        "media_source": request.media_source,
        "media_id": request.media_id,
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
    "/cache",
    summary="查询音乐识别缓存",
    response_model=_SchemaResponse[_SchemaMusicRecognitionCacheData],
)
async def music_recognition_cache(
    _: object = Depends(get_current_active_superuser_async),
) -> _SchemaResponse:
    """查询可管理的 MusicBrainz 识别缓存。"""
    cache_items = MusicBrainzChain().cache_items()
    recognized_count = sum(1 for item in cache_items if item["media_id"])
    return _SchemaResponse(
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
    response_model=_SchemaResponse[None],
)
async def delete_music_recognition_cache(
    cache_key: str,
    _: object = Depends(get_current_active_superuser_async),
) -> _SchemaResponse:
    """按缓存键删除单条 MusicBrainz 识别缓存。"""
    deleted_item = MusicBrainzChain().delete_cache(cache_key)
    if not deleted_item:
        return _SchemaResponse(success=False, message="音乐识别缓存不存在")
    return _SchemaResponse(success=True, message="音乐识别缓存删除成功")


@router.delete(
    "/cache", summary="清空音乐识别缓存", response_model=_SchemaResponse[None]
)
async def clear_music_recognition_cache(
    _: object = Depends(get_current_active_superuser_async),
) -> _SchemaResponse:
    """清空全部 MusicBrainz 识别缓存。"""
    MusicBrainzChain().clear_cache()
    return _SchemaResponse(success=True, message="音乐识别缓存清理完成")


@router.get(
    "/explore",
    summary="探索音乐",
    response_model=list[_SchemaMusicInfo],
)
async def explore_music(
        page: PageParam = 1,
        count: CountParam = 30,
        media_source: MusicExploreSourceParam = MediaSource.MusicBrainz,
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
        tags: str = "",
        douban_sort: DoubanMusicSortParam = "U",
        _: _SchemaTokenPayload = Depends(verify_token),
) -> list[_SchemaMusicInfo]:
    """MusicBrainz 返回榜单或新发行，豆瓣音乐固定按官方标签分类浏览。"""
    media_source = _validate_music_source(media_source, _MUSIC_EXPLORE_SOURCES)
    chain = RecommendChain()
    if media_source != MediaSource.MusicBrainz:
        results = await chain.async_music_discover(
            media_source=media_source,
            page=page,
            count=count,
            entity=entity,
            mode="tag",
            tags=tags,
            sort=douban_sort,
        )
    elif mode == "fresh":
        results = await chain.async_music_fresh_releases(
            days=days,
            sort=sort,
            past=past,
            future=future,
            page=page,
            count=count,
            with_cover=with_cover,
        )
    else:
        results = await chain.async_music_chart(
            range_name=range_name,
            page=page,
            count=count,
            sort_by=sort_by,
            min_listen_count=min_listen_count,
            with_cover=with_cover,
            entity=entity,
        )
    if media_source != MediaSource.MusicBrainz and with_cover:
        results = [info for info in results if info.cover_url or info.poster_path]
    return [_serialize_music(info) for info in results]


@router.get(
    "/album/{album_id}",
    summary="查询音乐专辑详情",
    response_model=_SchemaMusicAlbumInfo,
)
async def music_album(
        album_id: str,
        media_source: MusicSourceParam = MediaSource.MusicBrainz,
        _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaMusicAlbumInfo:
    """按专辑标准 ID 返回专辑详情、曲目列表和发行版本。"""
    media_source = _validate_music_source(media_source)
    info = await MediaChain().async_get_music_album(
        media_source=media_source, media_id=album_id
    )
    if not info:
        raise HTTPException(status_code=404, detail="未识别到专辑信息")
    return _serialize_album(info)


@router.get(
    "/album/{album_id}/related",
    summary="查询关联音乐专辑",
    response_model=list[_SchemaMusicInfo],
)
async def music_album_related(
        album_id: str,
        count: CountParam = 24,
        media_source: MusicSourceParam = MediaSource.MusicBrainz,
        _: _SchemaTokenPayload = Depends(verify_token),
) -> list[_SchemaMusicInfo]:
    """按来源和专辑 ID 返回可继续浏览的关联专辑。"""
    media_source = _validate_music_source(media_source)
    results = await MediaChain().async_get_music_album_related(
        media_source=media_source,
        media_id=album_id,
        count=count,
    )
    return [_serialize_music(info) for info in results]


@router.get(
    "/artist/{artist_id}/albums",
    summary="查询艺术家的专辑列表",
    response_model=list[_SchemaMusicInfo],
)
async def music_artist_albums(
        artist_id: str,
        page: PageParam = 1,
        count: CountParam = 30,
        album_type: MusicAlbumTypeParam = None,
        media_source: MusicSourceParam = MediaSource.MusicBrainz,
        _: _SchemaTokenPayload = Depends(verify_token),
) -> list[_SchemaMusicInfo]:
    """按艺术家标准 ID 分页返回其专辑、EP 和单曲。"""
    media_source = _validate_music_source(media_source)
    results = await MediaChain().async_get_music_artist_albums(
        media_source=media_source,
        media_id=artist_id,
        page=page,
        count=count,
        album_type=album_type,
    )
    return [_serialize_music(info) for info in results]


@router.get(
    "/artist/{artist_id}/related",
    summary="查询关联艺术家",
    response_model=list[_SchemaMusicArtistInfo],
)
async def music_artist_related(
        artist_id: str,
        count: CountParam = 24,
        media_source: MusicSourceParam = MediaSource.MusicBrainz,
        _: _SchemaTokenPayload = Depends(verify_token),
) -> list[_SchemaMusicArtistInfo]:
    """按艺术家关系返回可继续浏览的关联艺术家。"""
    media_source = _validate_music_source(media_source)
    results = await MediaChain().async_get_music_artist_related(
        media_source=media_source,
        media_id=artist_id,
        count=count,
    )
    return [_serialize_artist(info) for info in results]


@router.get(
    "/artist/{artist_id}",
    summary="查询音乐艺术家详情",
    response_model=_SchemaMusicArtistInfo,
)
async def music_artist(
        artist_id: str,
        media_source: MusicSourceParam = MediaSource.MusicBrainz,
        _: _SchemaTokenPayload = Depends(verify_token),
) -> _SchemaMusicArtistInfo:
    """按艺术家标准 ID 返回艺术家详情。"""
    media_source = _validate_music_source(media_source)
    info = await MediaChain().async_get_music_artist(
        media_source=media_source, media_id=artist_id
    )
    if not info:
        raise HTTPException(status_code=404, detail="未识别到艺术家信息")
    return _serialize_artist(info)
