from typing import List, Any, Optional

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.core.security import verify_token
from app.schemas import MediaRecognizeConvertEventData
from app.schemas.types import MediaType, ChainEventType

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
                 mtype: Optional[str] = None,
                 area: Optional[str] = "title",
                 title: Optional[str] = None,
                 year: Optional[str] = None,
                 season: Optional[str] = None,
                 sites: Optional[str] = None,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID/豆瓣ID精确搜索站点资源 tmdb:/douban:/bangumi:
    """
    if mtype:
        media_type = MediaType(mtype)
    else:
        media_type = None
    if season:
        media_season = int(season)
    else:
        media_season = None
    if sites:
        site_list = [int(site) for site in sites.split(",") if site]
    else:
        site_list = None
    torrents = None
    # 根据前缀识别媒体ID
    if mediaid.startswith("tmdb:"):
        tmdbid = int(mediaid.replace("tmdb:", ""))
        if settings.RECOGNIZE_SOURCE == "douban":
            # 通过TMDBID识别豆瓣ID
            doubaninfo = MediaChain().get_doubaninfo_by_tmdbid(tmdbid=tmdbid, mtype=media_type)
            if doubaninfo:
                torrents = SearchChain().search_by_id(doubanid=doubaninfo.get("id"),
                                                      mtype=media_type, area=area, season=media_season,
                                                      sites=site_list, cache_local=True)
            else:
                return schemas.Response(success=False, message="未识别到豆瓣媒体信息")
        else:
            torrents = SearchChain().search_by_id(tmdbid=tmdbid, mtype=media_type, area=area, season=media_season,
                                                  sites=site_list, cache_local=True)
    elif mediaid.startswith("douban:"):
        doubanid = mediaid.replace("douban:", "")
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # 通过豆瓣ID识别TMDBID
            tmdbinfo = MediaChain().get_tmdbinfo_by_doubanid(doubanid=doubanid, mtype=media_type)
            if tmdbinfo:
                if tmdbinfo.get('season') and not media_season:
                    media_season = tmdbinfo.get('season')
                torrents = SearchChain().search_by_id(tmdbid=tmdbinfo.get("id"),
                                                      mtype=media_type, area=area, season=media_season,
                                                      sites=site_list, cache_local=True)
            else:
                return schemas.Response(success=False, message="未识别到TMDB媒体信息")
        else:
            torrents = SearchChain().search_by_id(doubanid=doubanid, mtype=media_type, area=area, season=media_season,
                                                  sites=site_list, cache_local=True)
    elif mediaid.startswith("bangumi:"):
        bangumiid = int(mediaid.replace("bangumi:", ""))
        if settings.RECOGNIZE_SOURCE == "themoviedb":
            # 通过BangumiID识别TMDBID
            tmdbinfo = MediaChain().get_tmdbinfo_by_bangumiid(bangumiid=bangumiid)
            if tmdbinfo:
                torrents = SearchChain().search_by_id(tmdbid=tmdbinfo.get("id"),
                                                      mtype=media_type, area=area, season=media_season,
                                                      sites=site_list, cache_local=True)
            else:
                return schemas.Response(success=False, message="未识别到TMDB媒体信息")
        else:
            # 通过BangumiID识别豆瓣ID
            doubaninfo = MediaChain().get_doubaninfo_by_bangumiid(bangumiid=bangumiid)
            if doubaninfo:
                torrents = SearchChain().search_by_id(doubanid=doubaninfo.get("id"),
                                                      mtype=media_type, area=area, season=media_season,
                                                      sites=site_list, cache_local=True)
            else:
                return schemas.Response(success=False, message="未识别到豆瓣媒体信息")
    else:
        # 未知前缀，广播事件解析媒体信息
        event_data = MediaRecognizeConvertEventData(
            mediaid=mediaid,
            convert_type=settings.RECOGNIZE_SOURCE
        )
        event = eventmanager.send_event(ChainEventType.MediaRecognizeConvert, event_data)
        # 使用事件返回的上下文数据
        if event and event.event_data:
            event_data: MediaRecognizeConvertEventData = event.event_data
            if event_data.media_dict:
                search_id = event_data.media_dict.get("id")
                if event_data.convert_type == "themoviedb":
                    torrents = SearchChain().search_by_id(tmdbid=search_id, mtype=media_type, area=area,
                                                          season=media_season, cache_local=True)
                elif event_data.convert_type == "douban":
                    torrents = SearchChain().search_by_id(doubanid=search_id, mtype=media_type, area=area,
                                                          season=media_season, cache_local=True)
        else:
            if not title:
                return schemas.Response(success=False, message="未知的媒体ID")
            # 使用名称识别兜底
            meta = MetaInfo(title)
            if year:
                meta.year = year
            if media_type:
                meta.type = media_type
            if media_season:
                meta.type = MediaType.TV
                meta.begin_season = media_season
            mediainfo = MediaChain().recognize_media(meta=meta)
            if mediainfo:
                if settings.RECOGNIZE_SOURCE == "themoviedb":
                    torrents = SearchChain().search_by_id(tmdbid=mediainfo.tmdb_id, mtype=media_type, area=area,
                                                          season=media_season, cache_local=True)
                else:
                    torrents = SearchChain().search_by_id(doubanid=mediainfo.douban_id, mtype=media_type, area=area,
                                                          season=media_season, cache_local=True)
    # 返回搜索结果
    if not torrents:
        return schemas.Response(success=False, message="未搜索到任何资源")
    else:
        return schemas.Response(success=True, data=[torrent.to_dict() for torrent in torrents])


@router.get("/title", summary="模糊搜索资源", response_model=schemas.Response)
def search_by_title(keyword: Optional[str] = None,
                    page: Optional[int] = 0,
                    sites: Optional[str] = None,
                    _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据名称模糊搜索站点资源，支持分页，关键词为空是返回首页资源
    """
    torrents = SearchChain().search_by_title(title=keyword, page=page,
                                             sites=[int(site) for site in sites.split(",") if site] if sites else None,
                                             cache_local=True)
    if not torrents:
        return schemas.Response(success=False, message="未搜索到任何资源")
    return schemas.Response(success=True, data=[torrent.to_dict() for torrent in torrents])
