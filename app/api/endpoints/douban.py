from typing import Any, List, Optional

from fastapi import Depends

from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.workflow import MediaInfo as _SchemaMediaInfo
from app.api.response import ResponseAPIRouter
from app.chain.douban import DoubanChain
from app.domain.context import MediaInfo
from app.application.security.access import verify_token
from app.schemas.types import MediaType

router = ResponseAPIRouter()


@router.get(
    "/person/{person_id}", summary="人物详情", response_model=_SchemaMediaPerson
)
async def douban_person(
    person_id: int, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    根据人物ID查询人物详情
    """
    return await DoubanChain().async_person_detail(person_id=person_id)


@router.get(
    "/person/credits/{person_id}",
    summary="人物参演作品",
    response_model=List[_SchemaMediaInfo],
)
async def douban_person_credits(
    person_id: int,
    page: Optional[int] = 1,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据人物ID查询人物参演作品
    """
    medias = await DoubanChain().async_person_credits(person_id=person_id, page=page)
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get(
    "/credits/{doubanid}/{type_name}",
    summary="豆瓣演员阵容",
    response_model=List[_SchemaMediaPerson],
)
async def douban_credits(
    doubanid: str, type_name: str, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    根据豆瓣ID查询演员阵容，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        return await DoubanChain().async_movie_credits(doubanid=doubanid)
    elif mediatype == MediaType.TV:
        return await DoubanChain().async_tv_credits(doubanid=doubanid)
    return []


@router.get(
    "/recommend/{doubanid}/{type_name}",
    summary="豆瓣推荐电影/电视剧",
    response_model=List[_SchemaMediaInfo],
)
async def douban_recommend(
    doubanid: str, type_name: str, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    根据豆瓣ID查询推荐电影/电视剧，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        medias = await DoubanChain().async_movie_recommend(doubanid=doubanid)
    elif mediatype == MediaType.TV:
        medias = await DoubanChain().async_tv_recommend(doubanid=doubanid)
    else:
        return []
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get("/{doubanid}", summary="查询豆瓣详情", response_model=_SchemaMediaInfo)
async def douban_info(
    doubanid: str, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    根据豆瓣ID查询豆瓣媒体信息
    """
    doubaninfo = await DoubanChain().async_douban_info(doubanid=doubanid)
    if doubaninfo:
        return MediaInfo(douban_info=doubaninfo).to_dict()
    else:
        return _SchemaMediaInfo()
