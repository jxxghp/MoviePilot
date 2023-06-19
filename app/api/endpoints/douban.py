from typing import List, Any

from fastapi import APIRouter, Depends
from fastapi import BackgroundTasks

from app import schemas
from app.chain.douban import DoubanChain
from app.core.context import MediaInfo
from app.db.models.user import User
from app.db.userauth import get_current_active_superuser
from app.db.userauth import get_current_active_user
from app.schemas import MediaType

router = APIRouter()


def start_douban_chain():
    """
    启动链式任务
    """
    DoubanChain().sync()


@router.get("/sync", response_model=schemas.Response)
async def sync_douban(
        background_tasks: BackgroundTasks,
        _: User = Depends(get_current_active_superuser)) -> Any:
    """
    同步豆瓣想看
    """
    background_tasks.add_task(start_douban_chain)
    return schemas.Response(success=True, message="任务已启动")


@router.get("/id", response_model=schemas.Context)
async def recognize_doubanid(doubanid: str,
                             _: User = Depends(get_current_active_user)) -> Any:
    """
    根据豆瓣ID识别媒体信息
    """
    # 识别媒体信息
    context = DoubanChain().recognize_by_doubanid(doubanid=doubanid)
    return context.to_dict()


@router.get("/info", response_model=schemas.MediaInfo)
async def douban_info(doubanid: str) -> Any:
    """
    根据豆瓣ID查询豆瓣媒体信息
    """
    doubaninfo = DoubanChain().douban_info(doubanid=doubanid)
    if doubaninfo:
        return MediaInfo(douban_info=doubaninfo).to_dict()
    else:
        return schemas.MediaInfo()


@router.get("/movies", response_model=List[schemas.MediaInfo])
async def douban_movies(sort: str = "R",
                        tags: str = "",
                        start: int = 0,
                        count: int = 30,
                        _: User = Depends(get_current_active_user)) -> Any:
    """
    浏览豆瓣电影信息
    """
    movies = DoubanChain().douban_discover(mtype=MediaType.MOVIE,
                                           sort=sort, tags=tags, start=start, count=count)
    if not movies:
        return []
    return [MediaInfo(douban_info=movie).to_dict() for movie in movies]


@router.get("/tvs", response_model=List[schemas.MediaInfo])
async def douban_tvs(sort: str = "R",
                     tags: str = "",
                     start: int = 0,
                     count: int = 30,
                     _: User = Depends(get_current_active_user)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    tvs = DoubanChain().douban_discover(mtype=MediaType.TV,
                                        sort=sort, tags=tags, start=start, count=count)
    if not tvs:
        return []
    return [MediaInfo(douban_info=tv).to_dict() for tv in tvs]


@router.get("/movie_top250", response_model=List[schemas.MediaInfo])
async def movie_top250(page: int = 1,
                       count: int = 30,
                       _: User = Depends(get_current_active_user)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    movies = DoubanChain().movie_top250(page=page, count=count)
    return [MediaInfo(douban_info=movie).to_dict() for movie in movies]


@router.get("/tv_weekly_chinese", response_model=List[schemas.MediaInfo])
async def tv_weekly_chinese(page: int = 1,
                            count: int = 30,
                            _: User = Depends(get_current_active_user)) -> Any:
    """
    中国每周剧集口碑榜
    """
    tvs = DoubanChain().tv_weekly_chinese(page=page, count=count)
    return [MediaInfo(douban_info=tv).to_dict() for tv in tvs]


@router.get("/tv_weekly_global", response_model=List[schemas.MediaInfo])
async def tv_weekly_global(page: int = 1,
                           count: int = 30,
                           _: User = Depends(get_current_active_user)) -> Any:
    """
    全球每周剧集口碑榜
    """
    tvs = DoubanChain().tv_weekly_global(page=page, count=count)
    return [MediaInfo(douban_info=tv).to_dict() for tv in tvs]
