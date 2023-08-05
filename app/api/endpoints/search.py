from typing import List, Any

from fastapi import APIRouter, Depends, HTTPException

from app import schemas
from app.chain.douban import DoubanChain
from app.chain.search import SearchChain
from app.core.context import Context
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
                     _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID/豆瓣ID精确搜索站点资源 tmdb:/douban:/
    """
    if mediaid.startswith("tmdb:"):
        tmdbid = int(mediaid.replace("tmdb:", ""))
        if mtype:
            mtype = MediaType(mtype)
        torrents = SearchChain().search_by_tmdbid(tmdbid=tmdbid, mtype=mtype)
    elif mediaid.startswith("douban:"):
        doubanid = mediaid.replace("douban:", "")
        # 识别豆瓣信息
        context = DoubanChain().recognize_by_doubanid(doubanid)
        if not context or not context.media_info or not context.media_info.tmdb_id:
            raise HTTPException(status_code=404, detail="无法识别TMDB媒体信息！")
        torrents = SearchChain().search_by_tmdbid(tmdbid=context.media_info.tmdb_id,
                                                  mtype=context.media_info.type)
    else:
        return []
    return [torrent.to_dict() for torrent in torrents]


@router.get("/title/{keyword}", summary="模糊搜索资源", response_model=List[schemas.Context])
async def search_by_title(keyword: str,
                          page: int = 1,
                          site: int = None,
                          _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据名称模糊搜索站点资源
    """
    torrents = SearchChain().search_by_title(title=keyword, page=page, site=site)
    return [Context(torrent_info=torrent).to_dict() for torrent in torrents]
