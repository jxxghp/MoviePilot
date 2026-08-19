from typing import List, Any, Optional

from fastapi import Depends

from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.workflow import MediaInfo as _SchemaMediaInfo
from app.api.response import ResponseAPIRouter
from app.application.orchestration.bangumi import BangumiChain
from app.domain.context import MediaInfo
from app.adapters.web.security.access import verify_token

router = ResponseAPIRouter()


@router.get(
    "/credits/{bangumiid}",
    summary="查询Bangumi演职员表",
    response_model=List[_SchemaMediaPerson],
)
async def bangumi_credits(
    bangumiid: int,
    page: Optional[int] = 1,
    count: Optional[int] = 20,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    查询Bangumi演职员表
    """
    persons = await BangumiChain().async_bangumi_credits(bangumiid)
    if persons:
        return persons[(page - 1) * count : page * count]
    return []


@router.get(
    "/recommend/{bangumiid}",
    summary="查询Bangumi推荐",
    response_model=List[_SchemaMediaInfo],
)
async def bangumi_recommend(
    bangumiid: int,
    page: Optional[int] = 1,
    count: Optional[int] = 20,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    查询Bangumi推荐
    """
    medias = await BangumiChain().async_bangumi_recommend(bangumiid)
    if medias:
        return [media.to_dict() for media in medias[(page - 1) * count : page * count]]
    return []


@router.get(
    "/person/{person_id}", summary="人物详情", response_model=_SchemaMediaPerson
)
async def bangumi_person(
    person_id: int, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    根据人物ID查询人物详情
    """
    return await BangumiChain().async_person_detail(person_id=person_id)


@router.get(
    "/person/credits/{person_id}",
    summary="人物参演作品",
    response_model=List[_SchemaMediaInfo],
)
async def bangumi_person_credits(
    person_id: int,
    page: Optional[int] = 1,
    count: Optional[int] = 20,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    根据人物ID查询人物参演作品
    """
    medias = await BangumiChain().async_person_credits(person_id=person_id)
    if medias:
        return [media.to_dict() for media in medias[(page - 1) * count : page * count]]
    return []


@router.get("/{bangumiid}", summary="查询Bangumi详情", response_model=_SchemaMediaInfo)
async def bangumi_info(
    bangumiid: int, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    查询Bangumi详情
    """
    info = await BangumiChain().async_bangumi_info(bangumiid)
    if info:
        return MediaInfo(bangumi_info=info).to_dict()
    else:
        return _SchemaMediaInfo()
