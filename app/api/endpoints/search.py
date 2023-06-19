from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.search import SearchChain
from app.db.models.user import User
from app.db.userauth import get_current_active_user
from app.schemas.types import MediaType

router = APIRouter()


@router.get("/tmdbid", summary="精确搜索资源", response_model=List[schemas.Context])
async def search_by_tmdbid(tmdbid: int,
                           mtype: str = None,
                           _: User = Depends(get_current_active_user)) -> Any:
    """
    根据TMDBID精确搜索站点资源
    """
    if mtype:
        mtype = MediaType.TV if mtype == MediaType.TV.value else MediaType.MOVIE
    torrents = SearchChain().search_by_tmdbid(tmdbid=tmdbid, mtype=mtype)
    return [torrent.to_dict() for torrent in torrents]


@router.get("/title", summary="模糊搜索资源", response_model=List[schemas.TorrentInfo])
async def search_by_title(title: str,
                          _: User = Depends(get_current_active_user)) -> Any:
    """
    根据名称模糊搜索站点资源
    """
    torrents = SearchChain().search_by_title(title=title)
    return [torrent.to_dict() for torrent in torrents]
