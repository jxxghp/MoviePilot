from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.security import verify_token
from app.schemas.types import MediaType

router = APIRouter()


@router.get("/last", summary="查询搜索结果", response_model=List[schemas.Context])
async def search_latest(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询搜索结果
    """
    torrents = SearchChain().last_search_results()
    return [torrent.to_dict() for torrent in torrents]


@router.get("/media/{mediaid}", summary="精确搜索资源", response_model=List[schemas.Context])
def search_by_id(mediaid: str,
                 mtype: str = None,
                 area: str = "title",
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID/豆瓣ID精确搜索站点资源 tmdb:/douban:/
    """
    torrents = []
    if mtype:
        mtype = MediaType(mtype)
    if mediaid.startswith("tmdb:"):
        tmdbid = int(mediaid.replace("tmdb:", ""))
        if settings.RECOGNIZE_SOURCE == "douban":
            # 通过TMDBID识别豆瓣ID
            doubaninfo = MediaChain().get_doubaninfo_by_tmdbid(tmdbid=tmdbid, mtype=mtype)
            if doubaninfo:
                torrents = SearchChain().search_by_id(doubanid=doubaninfo.get("id"),
                                                      mtype=mtype, area=area)
        else:
            torrents = SearchChain().search_by_id(tmdbid=tmdbid, mtype=mtype, area=area)
    elif mediaid.startswith("douban:"):
        doubanid = mediaid.replace("douban:", "")
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # 通过豆瓣ID识别TMDBID
            tmdbinfo = MediaChain().get_tmdbinfo_by_doubanid(doubanid=doubanid, mtype=mtype)
            if tmdbinfo:
                torrents = SearchChain().search_by_id(tmdbid=tmdbinfo.get("id"),
                                                      mtype=mtype, area=area)
        else:
            torrents = SearchChain().search_by_id(doubanid=doubanid, mtype=mtype, area=area)
    else:
        return []
    return [torrent.to_dict() for torrent in torrents]


@router.get("/title", summary="模糊搜索资源", response_model=List[schemas.TorrentInfo])
async def search_by_title(keyword: str = None,
                          page: int = 0,
                          site: int = None,
                          _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据名称模糊搜索站点资源，支持分页，关键词为空是返回首页资源
    """
    torrents = SearchChain().search_by_title(title=keyword, page=page, site=site)
    return [torrent.to_dict() for torrent in torrents]
