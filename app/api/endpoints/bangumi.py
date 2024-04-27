from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.bangumi import BangumiChain
from app.core.context import MediaInfo
from app.core.security import verify_token

router = APIRouter()


@router.get("/calendar", summary="Bangumi每日放送", response_model=List[schemas.MediaInfo])
def calendar(page: int = 1,
             count: int = 30,
             _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览Bangumi每日放送
    """
    infos = BangumiChain().calendar(page=page, count=count)
    if not infos:
        return []
    medias = [MediaInfo(bangumi_info=info) for info in infos]
    return [media.to_dict() for media in medias]


@router.get("/credits/{bangumiid}", summary="查询Bangumi演职员表", response_model=List[schemas.MediaPerson])
def bangumi_credits(bangumiid: int,
                    page: int = 1,
                    count: int = 20,
                    _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询Bangumi演职员表
    """
    persons = BangumiChain().bangumi_credits(bangumiid, page=page, count=count)
    if not persons:
        return []
    return [schemas.MediaPerson(source='bangumi', **person) for person in persons]


@router.get("/recommend/{bangumiid}", summary="查询Bangumi推荐", response_model=List[schemas.MediaInfo])
def bangumi_recommend(bangumiid: int,
                      _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询Bangumi推荐
    """
    infos = BangumiChain().bangumi_recommend(bangumiid)
    if not infos:
        return []
    medias = [MediaInfo(bangumi_info=info) for info in infos]
    return [media.to_dict() for media in medias]


@router.get("/person/{person_id}", summary="人物详情", response_model=schemas.MediaPerson)
def bangumi_person(person_id: int,
                   _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据人物ID查询人物详情
    """
    personinfo = BangumiChain().person_detail(person_id=person_id)
    if not personinfo:
        return schemas.MediaPerson(source='bangumi')
    else:
        return schemas.MediaPerson(source='bangumi', **{
            "id": personinfo.get("id"),
            "name": personinfo.get("name"),
            "images": personinfo.get("images"),
            "biography": personinfo.get("summary"),
            "birthday": personinfo.get("birth_day"),
            "gender": personinfo.get("gender")
        })


@router.get("/person/credits/{person_id}", summary="人物参演作品", response_model=List[schemas.MediaInfo])
def bangumi_person_credits(person_id: int,
                           page: int = 1,
                           _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据人物ID查询人物参演作品
    """
    infos = BangumiChain().person_credits(person_id=person_id, page=page)
    if not infos:
        return []
    else:
        return [MediaInfo(bangumi_info=info).to_dict() for info in infos]


@router.get("/{bangumiid}", summary="查询Bangumi详情", response_model=schemas.MediaInfo)
def bangumi_info(bangumiid: int,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询Bangumi详情
    """
    info = BangumiChain().bangumi_info(bangumiid)
    if info:
        return MediaInfo(bangumi_info=info).to_dict()
    else:
        return schemas.MediaInfo()
