from typing import Any, List, Optional

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.douban import DoubanChain
from app.core.context import MediaInfo
from app.core.security import verify_token
from app.db.models.user import User
from app.db.user_oper import get_current_active_superuser_async
from app.modules.douban.douban_cache import DoubanCache
from app.schemas import MediaType

router = APIRouter()


@router.get(
    "/cache", summary="查询豆瓣识别缓存", response_model=schemas.Response
)
async def douban_recognition_cache(
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """查询可管理的豆瓣识别缓存。"""
    cache_items = DoubanCache().list_items()
    recognized_count = sum(1 for item in cache_items if item["douban_id"])
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
    summary="删除指定豆瓣识别缓存",
    response_model=schemas.Response,
)
async def delete_douban_recognition_cache(
    cache_key: str,
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """按缓存键删除单条豆瓣识别缓存。"""
    deleted_item = DoubanCache().delete(cache_key)
    if not deleted_item:
        return schemas.Response(success=False, message="豆瓣识别缓存不存在")
    return schemas.Response(success=True, message="豆瓣识别缓存删除成功")


@router.delete(
    "/cache", summary="清空豆瓣识别缓存", response_model=schemas.Response
)
async def clear_douban_recognition_cache(
    _: User = Depends(get_current_active_superuser_async),
) -> schemas.Response:
    """清空全部豆瓣识别缓存。"""
    DoubanCache().clear()
    return schemas.Response(success=True, message="豆瓣识别缓存清理完成")


@router.get(
    "/person/{person_id}", summary="人物详情", response_model=schemas.MediaPerson
)
async def douban_person(
    person_id: int, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    根据人物ID查询人物详情
    """
    return await DoubanChain().async_person_detail(person_id=person_id)


@router.get(
    "/person/credits/{person_id}",
    summary="人物参演作品",
    response_model=List[schemas.MediaInfo],
)
async def douban_person_credits(
    person_id: int,
    page: Optional[int] = 1,
    _: schemas.TokenPayload = Depends(verify_token),
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
    response_model=List[schemas.MediaPerson],
)
async def douban_credits(
    doubanid: str, type_name: str, _: schemas.TokenPayload = Depends(verify_token)
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
    response_model=List[schemas.MediaInfo],
)
async def douban_recommend(
    doubanid: str, type_name: str, _: schemas.TokenPayload = Depends(verify_token)
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


@router.get("/{doubanid}", summary="查询豆瓣详情", response_model=schemas.MediaInfo)
async def douban_info(
    doubanid: str, _: schemas.TokenPayload = Depends(verify_token)
) -> Any:
    """
    根据豆瓣ID查询豆瓣媒体信息
    """
    doubaninfo = await DoubanChain().async_douban_info(doubanid=doubanid)
    if doubaninfo:
        return MediaInfo(douban_info=doubaninfo).to_dict()
    else:
        return schemas.MediaInfo()
