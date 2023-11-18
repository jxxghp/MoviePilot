from typing import List, Any

from fastapi import APIRouter, Depends, Response

from app import schemas
from app.chain.douban import DoubanChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.security import verify_token
from app.schemas import MediaType
from app.utils.http import RequestUtils

router = APIRouter()


@router.get("/img/{imgurl:path}", summary="豆瓣图片代理")
def douban_img(imgurl: str) -> Any:
    """
    豆瓣图片代理
    """
    if not imgurl:
        return None
    response = RequestUtils(headers={
        'Referer': "https://movie.douban.com/"
    }, ua=settings.USER_AGENT).get_res(url=imgurl)
    if response:
        return Response(content=response.content, media_type="image/jpeg")
    return None


@router.get("/showing", summary="豆瓣正在热映", response_model=List[schemas.MediaInfo])
def movie_showing(page: int = 1,
                  count: int = 30,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣正在热映
    """
    movies = DoubanChain().movie_showing(page=page, count=count)
    if not movies:
        return []
    medias = [MediaInfo(douban_info=movie) for movie in movies]
    return [media.to_dict() for media in medias]


@router.get("/movies", summary="豆瓣电影", response_model=List[schemas.MediaInfo])
def douban_movies(sort: str = "R",
                  tags: str = "",
                  page: int = 1,
                  count: int = 30,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣电影信息
    """
    movies = DoubanChain().douban_discover(mtype=MediaType.MOVIE,
                                           sort=sort, tags=tags, page=page, count=count)
    if not movies:
        return []
    medias = [MediaInfo(douban_info=movie) for movie in movies]
    return [media.to_dict() for media in medias
            if media.poster_path
            and "movie_large.jpg" not in media.poster_path
            and "tv_normal.png" not in media.poster_path]


@router.get("/tvs", summary="豆瓣剧集", response_model=List[schemas.MediaInfo])
def douban_tvs(sort: str = "R",
               tags: str = "",
               page: int = 1,
               count: int = 30,
               _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    tvs = DoubanChain().douban_discover(mtype=MediaType.TV,
                                        sort=sort, tags=tags, page=page, count=count)
    if not tvs:
        return []
    medias = [MediaInfo(douban_info=tv) for tv in tvs]
    return [media.to_dict() for media in medias
            if media.poster_path
            and "movie_large.jpg" not in media.poster_path
            and "tv_normal.jpg" not in media.poster_path
            and "tv_large.jpg" not in media.poster_path]


@router.get("/movie_top250", summary="豆瓣电影TOP250", response_model=List[schemas.MediaInfo])
def movie_top250(page: int = 1,
                 count: int = 30,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    movies = DoubanChain().movie_top250(page=page, count=count)
    return [MediaInfo(douban_info=movie).to_dict() for movie in movies]


@router.get("/tv_weekly_chinese", summary="豆瓣国产剧集周榜", response_model=List[schemas.MediaInfo])
def tv_weekly_chinese(page: int = 1,
                      count: int = 30,
                      _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    中国每周剧集口碑榜
    """
    tvs = DoubanChain().tv_weekly_chinese(page=page, count=count)
    return [MediaInfo(douban_info=tv).to_dict() for tv in tvs]


@router.get("/tv_weekly_global", summary="豆瓣全球剧集周榜", response_model=List[schemas.MediaInfo])
def tv_weekly_global(page: int = 1,
                     count: int = 30,
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    全球每周剧集口碑榜
    """
    tvs = DoubanChain().tv_weekly_global(page=page, count=count)
    return [MediaInfo(douban_info=tv).to_dict() for tv in tvs]


@router.get("/tv_animation", summary="豆瓣动画剧集", response_model=List[schemas.MediaInfo])
def tv_animation(page: int = 1,
                 count: int = 30,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    热门动画剧集
    """
    tvs = DoubanChain().tv_animation(page=page, count=count)
    return [MediaInfo(douban_info=tv).to_dict() for tv in tvs]


@router.get("/movie_hot", summary="豆瓣热门电影", response_model=List[schemas.MediaInfo])
def movie_hot(page: int = 1,
              count: int = 30,
              _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    热门电影
    """
    movies = DoubanChain().movie_hot(page=page, count=count)
    return [MediaInfo(douban_info=movie).to_dict() for movie in movies]


@router.get("/tv_hot", summary="豆瓣热门电视剧", response_model=List[schemas.MediaInfo])
def tv_hot(page: int = 1,
           count: int = 30,
           _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    热门电视剧
    """
    tvs = DoubanChain().tv_hot(page=page, count=count)
    return [MediaInfo(douban_info=tv).to_dict() for tv in tvs]


@router.get("/credits/{doubanid}/{type_name}", summary="豆瓣演员阵容", response_model=List[schemas.DoubanPerson])
def douban_credits(doubanid: str,
                   type_name: str,
                   page: int = 1,
                   _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID查询演员阵容，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        doubaninfos = DoubanChain().movie_credits(doubanid=doubanid, page=page)
    elif mediatype == MediaType.TV:
        doubaninfos = DoubanChain().tv_credits(doubanid=doubanid, page=page)
    else:
        return []
    if not doubaninfos:
        return []
    else:
        return [schemas.DoubanPerson(**doubaninfo) for doubaninfo in doubaninfos]


@router.get("/recommend/{doubanid}/{type_name}", summary="豆瓣推荐电影/电视剧", response_model=List[schemas.MediaInfo])
def douban_recommend(doubanid: str,
                     type_name: str,
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据豆瓣ID查询推荐电影/电视剧，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        doubaninfos = DoubanChain().movie_recommend(doubanid=doubanid)
    elif mediatype == MediaType.TV:
        doubaninfos = DoubanChain().tv_recommend(doubanid=doubanid)
    else:
        return []
    if not doubaninfos:
        return []
    else:
        return [MediaInfo(douban_info=doubaninfo).to_dict() for doubaninfo in doubaninfos]


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
