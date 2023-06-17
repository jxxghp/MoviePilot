from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.search import SearchChain
from app.db.models.user import User
from app.db.userauth import get_current_active_user
from app.schemas.types import MediaType

router = APIRouter()


@router.get("/tmdbid", response_model=List[schemas.Context])
async def search_by_tmdbid(tmdbid: int,
                           mtype: str = None,
                           _: User = Depends(get_current_active_user)) -> Any:
    """
    根据TMDBID搜索资源
    """
    if mtype:
        mtype = MediaType.TV if mtype == MediaType.TV.value else MediaType.MOVIE
    torrents = SearchChain().search_by_tmdbid(tmdbid=tmdbid, mtype=mtype)
    return [torrent.to_dict() for torrent in torrents]
