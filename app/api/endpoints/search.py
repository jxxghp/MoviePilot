from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.douban import DoubanChain
from app.chain.search import SearchChain
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
def search_by_tmdbid(mediaid: str,
                     mtype: str = None,
                     area: str = "title",
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID/豆瓣ID精确搜索站点资源 tmdb:/douban:/
    """
    if mediaid.startswith("tmdb:"):
        tmdbid = int(mediaid.replace("tmdb:", ""))
        if mtype:
            mtype = MediaType(mtype)
        torrents = SearchChain().search_by_tmdbid(tmdbid=tmdbid, mtype=mtype, area=area)
    elif mediaid.startswith("douban:"):
        doubanid = mediaid.replace("douban:", "")
        # 识别豆瓣信息
        context = DoubanChain().recognize_by_doubanid(doubanid)
        if not context or not context.media_info or not context.media_info.tmdb_id:
            return []
        torrents = SearchChain().search_by_tmdbid(tmdbid=context.media_info.tmdb_id,
                                                  mtype=context.media_info.type,
                                                  area=area)
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
