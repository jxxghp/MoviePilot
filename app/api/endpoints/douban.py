from typing import Any, List, Optional

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.douban import DoubanChain
from app.core.context import MediaInfo
from app.core.security import verify_token
from app.schemas import MediaType

router = APIRouter()


@router.get("/person/{person_id}", summary="人物详情", response_model=schemas.MediaPerson)
def douban_person(person_id: int,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据人物ID查询人物详情
    """
    return DoubanChain().person_detail(person_id=person_id)


@router.get("/person/credits/{person_id}", summary="人物参演作品", response_model=List[schemas.MediaInfo])
def douban_person_credits(person_id: int,
                          page: Optional[int] = 1,
                          _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据人物ID查询人物参演作品
    """
    medias = DoubanChain().person_credits(person_id=person_id, page=page)
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get("/credits/{doubanid}/{type_name}", summary="豆瓣演员阵容", response_model=List[schemas.MediaPerson])
def douban_credits(doubanid: str,
                   type_name: str,
                   _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据豆瓣ID查询演员阵容，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        return DoubanChain().movie_credits(doubanid=doubanid)
    elif mediatype == MediaType.TV:
        return DoubanChain().tv_credits(doubanid=doubanid)
    return []


@router.get("/recommend/{doubanid}/{type_name}", summary="豆瓣推荐电影/电视剧", response_model=List[schemas.MediaInfo])
def douban_recommend(doubanid: str,
                     type_name: str,
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据豆瓣ID查询推荐电影/电视剧，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        medias = DoubanChain().movie_recommend(doubanid=doubanid)
    elif mediatype == MediaType.TV:
        medias = DoubanChain().tv_recommend(doubanid=doubanid)
    else:
        return []
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get("/{doubanid}", summary="查询豆瓣详情", response_model=schemas.MediaInfo)
def douban_info(doubanid: str,
                _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据豆瓣ID查询豆瓣媒体信息
    """
    doubaninfo = DoubanChain().douban_info(doubanid=doubanid)
    if doubaninfo:
        return MediaInfo(douban_info=doubaninfo).to_dict()
    else:
        return schemas.MediaInfo()
