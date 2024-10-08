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
def search_latest(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    查询搜索结果
    """
    torrents = SearchChain().last_search_results()
    return [torrent.to_dict() for torrent in torrents]


@router.get("/media/{mediaid}", summary="精确搜索资源", response_model=schemas.Response)
def search_by_id(mediaid: str,
                 mtype: str = None,
                 area: str = "title",
                 season: str = None,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID/豆瓣ID精确搜索站点资源 tmdb:/douban:/bangumi:
    """
    if mtype:
        mtype = MediaType(mtype)
    if season:
        season = int(season)
    if mediaid.startswith("tmdb:"):
        tmdbid = int(mediaid.replace("tmdb:", ""))
        if settings.RECOGNIZE_SOURCE == "douban":
            # 通过TMDBID识别豆瓣ID
            doubaninfo = MediaChain().get_doubaninfo_by_tmdbid(tmdbid=tmdbid, mtype=mtype)
            if doubaninfo:
                torrents = SearchChain().search_by_id(doubanid=doubaninfo.get("id"),
                                                      mtype=mtype, area=area, season=season)
            else:
                return schemas.Response(success=False, message="未识别到豆瓣媒体信息")
        else:
            torrents = SearchChain().search_by_id(tmdbid=tmdbid, mtype=mtype, area=area, season=season)
    elif mediaid.startswith("douban:"):
        doubanid = mediaid.replace("douban:", "")
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # 通过豆瓣ID识别TMDBID
            tmdbinfo = MediaChain().get_tmdbinfo_by_doubanid(doubanid=doubanid, mtype=mtype)
            if tmdbinfo:
                if tmdbinfo.get('season') and not season:
                    season = tmdbinfo.get('season')
                torrents = SearchChain().search_by_id(tmdbid=tmdbinfo.get("id"),
                                                      mtype=mtype, area=area, season=season)
            else:
                return schemas.Response(success=False, message="未识别到TMDB媒体信息")
        else:
            torrents = SearchChain().search_by_id(doubanid=doubanid, mtype=mtype, area=area, season=season)
    elif mediaid.startswith("bangumi:"):
        bangumiid = int(mediaid.replace("bangumi:", ""))
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # 通过BangumiID识别TMDBID
            tmdbinfo = MediaChain().get_tmdbinfo_by_bangumiid(bangumiid=bangumiid)
            if tmdbinfo:
                torrents = SearchChain().search_by_id(tmdbid=tmdbinfo.get("id"),
                                                      mtype=mtype, area=area, season=season)
            else:
                return schemas.Response(success=False, message="未识别到TMDB媒体信息")
        else:
            # 通过BangumiID识别豆瓣ID
            doubaninfo = MediaChain().get_doubaninfo_by_bangumiid(bangumiid=bangumiid)
            if doubaninfo:
                torrents = SearchChain().search_by_id(doubanid=doubaninfo.get("id"),
                                                      mtype=mtype, area=area, season=season)
            else:
                return schemas.Response(success=False, message="未识别到豆瓣媒体信息")
    else:
        return schemas.Response(success=False, message="未知的媒体ID")

    if not torrents:
        return schemas.Response(success=False, message="未搜索到任何资源")
    else:
        return schemas.Response(success=True, data=[torrent.to_dict() for torrent in torrents])


@router.get("/title", summary="模糊搜索资源", response_model=schemas.Response)
def search_by_title(keyword: str = None,
                    page: int = 0,
                    site: int = None,
                    _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据名称模糊搜索站点资源，支持分页，关键词为空是返回首页资源
    """
    torrents = SearchChain().search_by_title(title=keyword, page=page, site=site)
    if not torrents:
        return schemas.Response(success=False, message="未搜索到任何资源")
    return schemas.Response(success=True, data=[torrent.to_dict() for torrent in torrents])
