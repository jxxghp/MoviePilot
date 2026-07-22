from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from app import schemas
from app.chain.anilist import AniListChain
from app.core.context import MediaInfo
from app.core.security import verify_token

router = APIRouter()

PageParam = Annotated[int, Query(ge=1)]
CountParam = Annotated[int, Query(ge=1, le=50)]


def _serialize_medias(medias: list[MediaInfo]) -> list[schemas.MediaInfo]:
    """
    将内部媒体对象转换为 REST 响应模型。

    :param medias: 统一媒体信息列表
    :return: REST 媒体响应列表
    """
    return [schemas.MediaInfo(**media.to_dict()) for media in medias]


@router.get(
    "/trending",
    summary="查询 AniList 当前趋势榜",
    response_model=list[schemas.MediaInfo],
)
async def anilist_trending(
    page: PageParam = 1,
    count: CountParam = 20,
    _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MediaInfo]:
    """查询 AniList TRENDING NOW 榜单"""
    medias = await AniListChain().async_trending(page=page, count=count)
    return _serialize_medias(medias)


@router.get(
    "/popular-this-season",
    summary="查询 AniList 本季热门榜",
    response_model=list[schemas.MediaInfo],
)
async def anilist_popular_this_season(
    page: PageParam = 1,
    count: CountParam = 20,
    _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MediaInfo]:
    """查询 AniList POPULAR THIS SEASON 榜单"""
    medias = await AniListChain().async_popular_this_season(page=page, count=count)
    return _serialize_medias(medias)


@router.get(
    "/discover",
    summary="探索 AniList 动画",
    response_model=list[schemas.MediaInfo],
)
async def anilist_discover(
    page: PageParam = 1,
    count: CountParam = 20,
    search: Optional[str] = None,
    genre: Optional[str] = None,
    media_format: Optional[str] = Query(None, alias="format"),
    season: Optional[str] = None,
    season_year: Optional[int] = None,
    status: Optional[str] = None,
    country: Optional[str] = None,
    sort: Optional[str] = None,
    _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MediaInfo]:
    """按标题、类型、风格、季度、年份、状态、地区和排序探索 AniList 动画"""
    medias = await AniListChain().async_discover(
        page=page,
        count=count,
        search=search,
        genre=genre,
        media_format=media_format,
        season=season,
        season_year=season_year,
        status=status,
        country=country,
        sort=sort,
    )
    return _serialize_medias(medias)


@router.get(
    "/credits/{anilist_id}",
    summary="查询 AniList 配音演员",
    response_model=list[schemas.MediaPerson],
)
async def anilist_credits(
    anilist_id: int,
    page: PageParam = 1,
    count: CountParam = 20,
    _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MediaPerson]:
    """查询 AniList 动画的日语配音演员"""
    return await AniListChain().async_credits(
        anilist_id=anilist_id, page=page, count=count
    )


@router.get(
    "/recommend/{anilist_id}",
    summary="查询 AniList 相关推荐",
    response_model=list[schemas.MediaInfo],
)
async def anilist_recommendations(
    anilist_id: int,
    page: PageParam = 1,
    count: CountParam = 20,
    _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MediaInfo]:
    """查询 AniList 动画相关推荐"""
    medias = await AniListChain().async_recommendations(
        anilist_id=anilist_id, page=page, count=count
    )
    return _serialize_medias(medias)


@router.get(
    "/person/{person_id}",
    summary="查询 AniList 人物详情",
    response_model=schemas.MediaPerson,
)
async def anilist_person(
    person_id: int,
    _: schemas.TokenPayload = Depends(verify_token),
) -> Optional[schemas.MediaPerson]:
    """根据 AniList 人物 ID 查询详情"""
    return await AniListChain().async_person_detail(person_id=person_id)


@router.get(
    "/person/credits/{person_id}",
    summary="查询 AniList 人物作品",
    response_model=list[schemas.MediaInfo],
)
async def anilist_person_credits(
    person_id: int,
    page: PageParam = 1,
    count: CountParam = 20,
    _: schemas.TokenPayload = Depends(verify_token),
) -> list[schemas.MediaInfo]:
    """查询 AniList 人物参与的动画作品"""
    medias = await AniListChain().async_person_credits(
        person_id=person_id, page=page, count=count
    )
    return _serialize_medias(medias)


@router.get(
    "/{anilist_id}",
    summary="查询 AniList 动画详情",
    response_model=schemas.MediaInfo,
)
async def anilist_info(
    anilist_id: int,
    _: schemas.TokenPayload = Depends(verify_token),
) -> schemas.MediaInfo:
    """根据 AniList 媒体 ID 查询动画详情"""
    info = await AniListChain().async_info(anilist_id)
    if not info:
        return schemas.MediaInfo()
    return schemas.MediaInfo(**MediaInfo(anilist_info=info).to_dict())
