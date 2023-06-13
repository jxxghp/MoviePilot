from typing import Any, List

from fastapi import APIRouter, HTTPException, Depends
from requests import Session

from app import schemas
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.db import get_db
from app.db.models.subscribe import Subscribe
from app.schemas import RadarrMovie
from app.utils.types import MediaType
from version import APP_VERSION

arr_router = APIRouter()


@arr_router.get("/system/status")
async def arr_system_status(apikey: str) -> Any:
    """
    模拟Radarr、Sonarr系统状态
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
    return {
        "appName": "MoviePilot",
        "instanceName": "moviepilot",
        "version": APP_VERSION,
        "urlBase": ""
    }


@arr_router.get("/qualityProfile")
async def arr_qualityProfile(apikey: str) -> Any:
    """
    模拟Radarr、Sonarr质量配置
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
    return [
        {
            "id": 1,
            "name": "默认"
        }
    ]


@arr_router.get("/rootfolder")
async def arr_rootfolder(apikey: str) -> Any:
    """
    模拟Radarr、Sonarr根目录
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
    return [
        {
            "id": 1,
            "path": settings.LIBRARY_PATH,
            "accessible": True,
            "freeSpace": 0,
            "unmappedFolders": []
        }
    ]


@arr_router.get("/tag")
async def arr_tag(apikey: str) -> Any:
    """
    模拟Radarr、Sonarr标签
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
    return [
        {
            "id": 1,
            "label": "默认"
        }
    ]


@arr_router.get("/languageprofile")
async def arr_languageprofile(apikey: str) -> Any:
    """
    模拟Radarr、Sonarr语言
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
    return [{
        "id": 1,
        "name": "默认",
        "upgradeAllowed": True,
        "cutoff": {
            "id": 1,
            "name": "默认"
        },
        "languages": [
            {
                "id": 1,
                "language": {
                    "id": 1,
                    "name": "默认"
                },
                "allowed": True
            }
        ]
    }]


@arr_router.get("/movie", response_model=List[schemas.RadarrMovie])
async def arr_movies(apikey: str, db: Session = Depends(get_db)) -> Any:
    """
    查询Rardar电影
    """
    """
    [
      {
        "id": 0,
        "title": "string",
        "originalTitle": "string",
        "originalLanguage": {
          "id": 0,
          "name": "string"
        },
        "secondaryYear": 0,
        "secondaryYearSourceId": 0,
        "sortTitle": "string",
        "sizeOnDisk": 0,
        "status": "tba",
        "overview": "string",
        "inCinemas": "2023-06-13T09:23:41.494Z",
        "physicalRelease": "2023-06-13T09:23:41.494Z",
        "digitalRelease": "2023-06-13T09:23:41.494Z",
        "physicalReleaseNote": "string",
        "images": [
          {
            "coverType": "unknown",
            "url": "string",
            "remoteUrl": "string"
          }
        ],
        "website": "string",
        "remotePoster": "string",
        "year": 0,
        "hasFile": true,
        "youTubeTrailerId": "string",
        "studio": "string",
        "path": "string",
        "qualityProfileId": 0,
        "monitored": true,
        "minimumAvailability": "tba",
        "isAvailable": true,
        "folderName": "string",
        "runtime": 0,
        "cleanTitle": "string",
        "imdbId": "string",
        "tmdbId": 0,
        "titleSlug": "string",
        "rootFolderPath": "string",
        "folder": "string",
        "certification": "string",
        "genres": [
          "string"
        ],
        "tags": [
          0
        ],
        "added": "2023-06-13T09:23:41.494Z",
        "addOptions": {
          "ignoreEpisodesWithFiles": true,
          "ignoreEpisodesWithoutFiles": true,
          "monitor": "movieOnly",
          "searchForMovie": true,
          "addMethod": "manual"
        },
        "popularity": 0
      }
    ]
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
    # 查询所有电影订阅
    result = []
    subscribes = Subscribe.list(db)
    for subscribe in subscribes:
        if subscribe.type != "电影":
            continue
        result.append(RadarrMovie(
            id=subscribe.id,
            title=subscribe.name,
            isAvailable=True,
            monitored=True,
            tmdbId=subscribe.tmdbid,
            profileId=1,
            qualityProfileId=1,
            added=True,
            hasFile=False,
        ))
    return result


@arr_router.get("/movie/lookup", response_model=List[schemas.RadarrMovie])
async def arr_movie_lookup(apikey: str, term: str, db: Session = Depends(get_db)) -> Any:
    """
    查询Rardar电影 term: `tmdb:${id}`
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
    tmdbid = term.replace("tmdb:", "")
    subscribe = Subscribe.get_by_tmdbid(db, int(tmdbid))
    if subscribe:
        return [RadarrMovie(
            id=subscribe.id,
            title=subscribe.name,
            isAvailable=True,
            monitored=True,
            tmdbId=subscribe.tmdbid,
            profileId=1,
            qualityProfileId=1,
            added=True,
            hasFile=False,
        )]
    else:
        return [RadarrMovie(
            title=subscribe.name,
            isAvailable=False,
            monitored=False,
            tmdbId=subscribe.tmdbid,
            profileId=1,
            qualityProfileId=1,
            added=False,
            hasFile=False,
        )]


@arr_router.get("/movie/{mid}", response_model=schemas.RadarrMovie)
async def arr_movie(apikey: str, mid: int, db: Session = Depends(get_db)) -> Any:
    """
    查询Rardar电影
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
    subscribe = Subscribe.get(db, mid)
    if subscribe:
        return RadarrMovie(
            id=subscribe.id,
            title=subscribe.name,
            isAvailable=True,
            monitored=True,
            tmdbId=subscribe.tmdbid,
            profileId=1,
            qualityProfileId=1,
            added=True,
            hasFile=False,
        )
    else:
        raise HTTPException(
            status_code=404,
            detail="未找到该电影！"
        )


@arr_router.post("/movie", response_model=schemas.Response)
async def arr_add_movie(apikey: str, movie: RadarrMovie) -> Any:
    """
    新增Rardar电影订阅
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
    if SubscribeChain().process(title=movie.title,
                                year=str(movie.year) if movie.year else None,
                                mtype=MediaType.MOVIE,
                                tmdbid=movie.tmdbId):
        return {"success": True, "msg": "添加订阅成功！"}
    else:
        raise HTTPException(
            status_code=500,
            detail="添加订阅失败！"
        )


@arr_router.delete("/movie/{mid}", response_model=schemas.Response)
async def arr_remove_movie(apikey: str, mid: int) -> Any:
    """
    删除Rardar电影订阅
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )


@arr_router.get("/series", response_model=List[schemas.SonarrSeries])
async def arr_series(apikey: str) -> Any:
    """
    查询Sonarr剧集
    """
    """
    [
      {
        "id": 0,
        "title": "string",
        "sortTitle": "string",
        "status": "continuing",
        "ended": true,
        "profileName": "string",
        "overview": "string",
        "nextAiring": "2023-06-13T09:08:17.624Z",
        "previousAiring": "2023-06-13T09:08:17.624Z",
        "network": "string",
        "airTime": "string",
        "images": [
          {
            "coverType": "unknown",
            "url": "string",
            "remoteUrl": "string"
          }
        ],
        "originalLanguage": {
          "id": 0,
          "name": "string"
        },
        "remotePoster": "string",
        "seasons": [
          {
            "seasonNumber": 0,
            "monitored": true,
            "statistics": {
              "nextAiring": "2023-06-13T09:08:17.624Z",
              "previousAiring": "2023-06-13T09:08:17.624Z",
              "episodeFileCount": 0,
              "episodeCount": 0,
              "totalEpisodeCount": 0,
              "sizeOnDisk": 0,
              "releaseGroups": [
                "string"
              ],
              "percentOfEpisodes": 0
            },
            "images": [
              {
                "coverType": "unknown",
                "url": "string",
                "remoteUrl": "string"
              }
            ]
          }
        ],
        "year": 0,
        "path": "string",
        "qualityProfileId": 0,
        "seasonFolder": true,
        "monitored": true,
        "useSceneNumbering": true,
        "runtime": 0,
        "tvdbId": 0,
        "tvRageId": 0,
        "tvMazeId": 0,
        "firstAired": "2023-06-13T09:08:17.624Z",
        "seriesType": "standard",
        "cleanTitle": "string",
        "imdbId": "string",
        "titleSlug": "string",
        "rootFolderPath": "string",
        "folder": "string",
        "certification": "string",
        "genres": [
          "string"
        ],
        "tags": [
          0
        ],
        "added": "2023-06-13T09:08:17.624Z",
        "addOptions": {
          "ignoreEpisodesWithFiles": true,
          "ignoreEpisodesWithoutFiles": true,
          "monitor": "unknown",
          "searchForMissingEpisodes": true,
          "searchForCutoffUnmetEpisodes": true
        },
        "ratings": {
          "votes": 0,
          "value": 0
        },
        "statistics": {
          "seasonCount": 0,
          "episodeFileCount": 0,
          "episodeCount": 0,
          "totalEpisodeCount": 0,
          "sizeOnDisk": 0,
          "releaseGroups": [
            "string"
          ],
          "percentOfEpisodes": 0
        },
        "episodesChanged": true
      }
    ]
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )


@arr_router.get("/series/{tid}")
async def arr_serie(apikey: str) -> Any:
    """
    查询Sonarr剧集
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )


@arr_router.get("/series/lookup")
async def arr_series_lookup(apikey: str, term: str) -> Any:
    """
    查询Sonarr剧集 term: `tvdb:${id}` title
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )


@arr_router.put("/series")
async def arr_add_series(apikey: str, title: str, seasons: list, year: int) -> Any:
    """
    新增Sonarr剧集订阅
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )


@arr_router.delete("/series/{tid}")
async def arr_remove_series(apikey: str, tid: int) -> Any:
    """
    删除Sonarr剧集订阅
    """
    if not apikey or apikey != settings.API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="认证失败！",
        )
