from typing import Any, List

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.douban import DoubanChain
from app.chain.recommend import RecommendChain
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
                          page: int = 1,
                          _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据人物ID查询人物参演作品
    """
    medias = DoubanChain().person_credits(person_id=person_id, page=page)
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get("/showing", summary="豆瓣正在热映", response_model=List[schemas.MediaInfo])
def movie_showing(page: int = 1,
                  count: int = 30,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣正在热映
    """
    return RecommendChain().douban_movie_showing(page=page, count=count)


@router.get("/movies", summary="豆瓣电影", response_model=List[schemas.MediaInfo])
def douban_movies(sort: str = "R",
                  tags: str = "",
                  page: int = 1,
                  count: int = 30,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣电影信息
    """
    return RecommendChain().douban_movies(sort=sort, tags=tags, page=page, count=count)


@router.get("/tvs", summary="豆瓣剧集", response_model=List[schemas.MediaInfo])
def douban_tvs(sort: str = "R",
               tags: str = "",
               page: int = 1,
               count: int = 30,
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    return RecommendChain().douban_tvs(sort=sort, tags=tags, page=page, count=count)


@router.get("/movie_top250", summary="豆瓣电影TOP250", response_model=List[schemas.MediaInfo])
def movie_top250(page: int = 1,
                 count: int = 30,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    return RecommendChain().douban_movie_top250(page=page, count=count)


@router.get("/tv_weekly_chinese", summary="豆瓣国产剧集周榜", response_model=List[schemas.MediaInfo])
def tv_weekly_chinese(page: int = 1,
                      count: int = 30,
                      _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    中国每周剧集口碑榜
    """
    return RecommendChain().douban_tv_weekly_chinese(page=page, count=count)


@router.get("/tv_weekly_global", summary="豆瓣全球剧集周榜", response_model=List[schemas.MediaInfo])
def tv_weekly_global(page: int = 1,
                     count: int = 30,
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    全球每周剧集口碑榜
    """
    return RecommendChain().douban_tv_weekly_global(page=page, count=count)


@router.get("/tv_animation", summary="豆瓣动画剧集", response_model=List[schemas.MediaInfo])
def tv_animation(page: int = 1,
                 count: int = 30,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    热门动画剧集
    """
    return RecommendChain().douban_tv_animation(page=page, count=count)


@router.get("/movie_hot", summary="豆瓣热门电影", response_model=List[schemas.MediaInfo])
def movie_hot(page: int = 1,
              count: int = 30,
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    热门电影
    """
    return RecommendChain().douban_movie_hot(page=page, count=count)


@router.get("/tv_hot", summary="豆瓣热门电视剧", response_model=List[schemas.MediaInfo])
def tv_hot(page: int = 1,
           count: int = 30,
           _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    热门电视剧
    """
    return RecommendChain().douban_tv_hot(page=page, count=count)


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
