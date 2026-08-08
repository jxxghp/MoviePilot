from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app import schemas
from app.chain.music import MusicChain
from app.core.context import MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.core.security import verify_token
from app.modules.listenbrainz import (
    LISTENBRAINZ_CHART_RANGES,
    LISTENBRAINZ_FRESH_MAX_DAYS,
    LISTENBRAINZ_FRESH_SORTS,
)

router = APIRouter()

CountParam = Annotated[int, Query(ge=1, le=100)]
PageParam = Annotated[int, Query(ge=1)]
MusicSourceParam = Annotated[str, Query(pattern="^musicbrainz$")]
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


@router.get(
    "/search",
    summary="搜索音乐元数据",
    response_model=list[schemas.MusicInfo],
)
async def search_music(
        query: str = Query(min_length=1),
        count: CountParam = 20,
        _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MusicInfo]:
    """按歌曲、专辑或艺术家关键词搜索标准音乐候选。"""
    results = await MusicChain().async_search(query=query, limit=count)
    return [_serialize_music(info) for info in results]


@router.post(
    "/recognize",
    summary="识别音乐元数据详情",
    response_model=schemas.MusicInfo,
)
async def recognize_music(
        request: schemas.MusicRecognizeRequest,
        _: schemas.TokenPayload = Depends(verify_token),
) -> schemas.MusicInfo:
    """根据音乐元数据来源和媒体 ID 获取标准详情。"""
    info = await MusicChain().async_recognize(
        source=request.source,
        media_id=request.media_id,
    )
    if not info:
        raise HTTPException(status_code=404, detail="未识别到音乐信息")
    return _serialize_music(info)


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
