from typing import List, Any

from fastapi import APIRouter, Depends
from fastapi import BackgroundTasks

from app import schemas
from app.chain.douban import DoubanChain
from app.chain.media import MediaChain
from app.core.context import MediaInfo
from app.db.models.user import User
from app.db.userauth import get_current_active_superuser
from app.db.userauth import get_current_active_user

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


@router.get("/doubanid", response_model=schemas.Context)
async def recognize_doubanid(doubanid: str,
                             _: User = Depends(get_current_active_user)) -> Any:
    """
    根据豆瓣ID识别媒体信息
    """
    # 识别媒体信息
    context = MediaChain().recognize_by_doubanid(doubanid=doubanid)
    return context.to_dict()


@router.get("/doubaninfo", response_model=schemas.MediaInfo)
async def douban_info(doubanid: str) -> Any:
    """
    根据豆瓣ID查询豆瓣媒体信息
    """
    doubaninfo = MediaChain().douban_info(doubanid=doubanid)
    if doubaninfo:
        return MediaInfo(douban_info=doubaninfo).to_dict()
    else:
        return schemas.MediaInfo()


@router.get("/doubanmovies", response_model=List[schemas.MediaInfo])
async def douban_movies(sort: str = "R",
                        tags: str = "",
                        start: int = 0,
                        count: int = 30,
                        _: User = Depends(get_current_active_user)) -> Any:
    """
    浏览豆瓣电影信息
    """
    movies = MediaChain().douban_movies(sort=sort, tags=tags, start=start, count=count)
    return [movie.to_dict() for movie in movies]


@router.get("/doubantvs", response_model=List[schemas.MediaInfo])
async def douban_tvs(sort: str = "R",
                     tags: str = "",
                     start: int = 0,
                     count: int = 30,
                     _: User = Depends(get_current_active_user)) -> Any:
    """
    浏览豆瓣剧集信息
    """
    tvs = MediaChain().douban_tvs(sort=sort, tags=tags, start=start, count=count)
    return [tv.to_dict() for tv in tvs]
