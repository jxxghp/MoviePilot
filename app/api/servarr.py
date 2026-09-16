from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from app.adapters.web.security.access import verify_apikey
from app.api.dependencies.subscription import (
    get_servarr_subscription_batch_writer,
    get_servarr_subscription_service,
)
from app.api.response import ERROR_RESPONSES
from app.application.servarr import ServarrSubscription, ServarrSubscriptionService
from app.application.subscription.write import (
    SubscriptionBatchWriteError,
    SubscriptionBatchWritePort,
)
from app.chain.media import MediaChain
from app.chain.subscribe.facade import SubscribeChain
from app.chain.tvdb import TvdbChain
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.runtime.version import get_app_version
from app.schemas.response import Response as _SchemaResponse
from app.schemas.servarr import RadarrMovie, SonarrSeries
from app.schemas.servarr import RadarrMovie as _SchemaRadarrMovie
from app.schemas.servarr import ServarrIdResponse as _SchemaServarrIdResponse
from app.schemas.servarr import ServarrLanguageProfile as _SchemaServarrLanguageProfile
from app.schemas.servarr import ServarrQualityProfile as _SchemaServarrQualityProfile
from app.schemas.servarr import ServarrRootFolder as _SchemaServarrRootFolder
from app.schemas.servarr import ServarrSystemStatus as _SchemaServarrSystemStatus
from app.schemas.servarr import ServarrTag as _SchemaServarrTag
from app.schemas.servarr import SonarrSeries as _SchemaSonarrSeries
from app.schemas.types import MediaSource, MediaType

arr_router = APIRouter(tags=["servarr"], responses=ERROR_RESPONSES)


def _subscribe_error_message(error: Optional[object]) -> str:
    """将 Servarr 订阅失败转换为调用方能理解的提示。"""
    from app.runtime.errors import public_error_message

    return public_error_message(
        error or "订阅操作失败",
        context="subscription",
    )


def _subscribe_tmdb_id(subscribe: ServarrSubscription) -> int | None:
    """将通用订阅身份投影为 Servarr 固定使用的 TMDB ID。"""
    if (
        subscribe.media_source == MediaSource.TMDB.value
        and subscribe.media_id
        and str(subscribe.media_id).isdigit()
    ):
        return int(subscribe.media_id)
    return None


def _resolve_series_media(
    tvdbid: Optional[int] = None,
    title: Optional[str] = None,
    year: Optional[str | int] = None,
) -> Optional[MediaInfo]:
    """按 TVDB ID 或标题解析剧集媒体信息，用于补全 Seerr 请求体中缺失的媒体身份。"""
    meta = None
    if tvdbid:
        tvdbinfo = MediaChain().tvdb_info(tvdbid=tvdbid)
        if tvdbinfo and tvdbinfo.get("name"):
            meta = MetaInfo(tvdbinfo.get("name"))
    if not meta and title:
        meta = MetaInfo(title)
    if not meta:
        return None
    meta.type = MediaType.TV
    if year:
        meta.year = year
    return MediaChain().recognize_by_meta(meta, obtain_images=False)


@arr_router.get(
    "/system/status",
    summary="系统状态",
    response_model=_SchemaServarrSystemStatus,
)
async def arr_system_status(
    _: Annotated[str, Depends(verify_apikey)],
) -> _SchemaServarrSystemStatus:
    """
    模拟Radarr、Sonarr系统状态
    """
    return _SchemaServarrSystemStatus.model_validate({
        "appName": "MoviePilot",
        "instanceName": "moviepilot",
        "version": get_app_version(),
        "buildTime": "",
        "isDebug": False,
        "isProduction": True,
        "isAdmin": True,
        "isUserInteractive": True,
        "startupPath": "/app",
        "appData": "/config",
        "osName": "debian",
        "osVersion": "",
        "isNetCore": True,
        "isLinux": True,
        "isOsx": False,
        "isWindows": False,
        "isDocker": True,
        "mode": "console",
        "branch": "main",
        "databaseType": "sqLite",
        "databaseVersion": {
            "major": 0,
            "minor": 0,
            "build": 0,
            "revision": 0,
            "majorRevision": 0,
            "minorRevision": 0,
        },
        "authentication": "none",
        "migrationVersion": 0,
        "urlBase": "",
        "runtimeVersion": {
            "major": 0,
            "minor": 0,
            "build": 0,
            "revision": 0,
            "majorRevision": 0,
            "minorRevision": 0,
        },
        "runtimeName": "",
        "startTime": "",
        "packageVersion": "",
        "packageAuthor": "jxxghp",
        "packageUpdateMechanism": "builtIn",
        "packageUpdateMechanismMessage": "",
    })


@arr_router.get(
    "/qualityProfile",
    summary="质量配置",
    response_model=List[_SchemaServarrQualityProfile],
)
async def arr_qualityProfile(
    _: Annotated[str, Depends(verify_apikey)],
) -> List[_SchemaServarrQualityProfile]:
    """
    模拟Radarr、Sonarr质量配置
    """
    return [
        _SchemaServarrQualityProfile.model_validate({
            "id": 1,
            "name": "默认",
            "upgradeAllowed": True,
            "cutoff": 0,
            "items": [
                {
                    "id": 0,
                    "name": "默认",
                    "quality": {
                        "id": 0,
                        "name": "默认",
                        "source": "0",
                        "resolution": 0,
                    },
                    "items": ["string"],
                    "allowed": True,
                }
            ],
            "minFormatScore": 0,
            "cutoffFormatScore": 0,
            "formatItems": [{"id": 0, "format": 0, "name": "默认", "score": 0}],
        })
    ]


@arr_router.get(
    "/rootfolder",
    summary="根目录",
    response_model=List[_SchemaServarrRootFolder],
)
async def arr_rootfolder(
    _: Annotated[str, Depends(verify_apikey)],
) -> List[_SchemaServarrRootFolder]:
    """
    模拟Radarr、Sonarr根目录
    """
    return [
        _SchemaServarrRootFolder.model_validate({
            "id": 1,
            "path": "/",
            "accessible": True,
            "freeSpace": 0,
            "unmappedFolders": [],
        })
    ]


@arr_router.get("/tag", summary="标签", response_model=List[_SchemaServarrTag])
async def arr_tag(
    _: Annotated[str, Depends(verify_apikey)],
) -> List[_SchemaServarrTag]:
    """
    模拟Radarr、Sonarr标签
    """
    return [_SchemaServarrTag(id=1, label="默认")]


@arr_router.get(
    "/languageprofile",
    summary="语言",
    response_model=List[_SchemaServarrLanguageProfile],
)
async def arr_languageprofile(
    _: Annotated[str, Depends(verify_apikey)],
) -> List[_SchemaServarrLanguageProfile]:
    """
    模拟Radarr、Sonarr语言
    """
    return [
        _SchemaServarrLanguageProfile.model_validate({
            "id": 1,
            "name": "默认",
            "upgradeAllowed": True,
            "cutoff": {"id": 1, "name": "默认"},
            "languages": [
                {"id": 1, "language": {"id": 1, "name": "默认"}, "allowed": True}
            ],
        })
    ]


@arr_router.get(
    "/movie", summary="所有订阅电影", response_model=List[_SchemaRadarrMovie]
)
async def arr_movies(
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
) -> List[_SchemaRadarrMovie]:
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
    # 查询所有电影订阅
    result = []
    subscribes = await subscriptions.list()
    for subscribe in subscribes:
        if subscribe.type != MediaType.MOVIE.value:
            continue
        result.append(
            RadarrMovie(
                id=subscribe.id,
                title=subscribe.name,
                year=subscribe.year,
                isAvailable=True,
                monitored=True,
                tmdbId=_subscribe_tmdb_id(subscribe),
                profileId=1,
                qualityProfileId=1,
                hasFile=False,
            )
        )
    return result


@arr_router.get(
    "/movie/lookup", summary="查询电影", response_model=List[_SchemaRadarrMovie]
)
def arr_movie_lookup(
    term: str,
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
) -> List[_SchemaRadarrMovie]:
    """
    查询Rardar电影 term: `tmdb:${id}`
    存在和不存在均不能返回错误
    """
    tmdbid = term.replace("tmdb:", "")
    # 查询媒体信息
    mediainfo = MediaChain().recognize_media(
        mtype=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id=tmdbid,
    )
    if not mediainfo:
        return [RadarrMovie()]
    # 查询是否已存在
    exists = MediaChain().media_exists(mediainfo=mediainfo)
    if not exists:
        # 文件不存在
        hasfile = False
    else:
        # 文件存在
        hasfile = True
    # 查询是否已订阅
    subscribes = subscriptions.list_by_media_identity_sync(
        MediaSource.TMDB,
        tmdbid,
    )
    if subscribes:
        # 订阅ID
        subid = subscribes[0].id
        # 已订阅
        monitored = True
    else:
        subid = None
        monitored = False

    return [
        RadarrMovie(
            id=subid,
            title=mediainfo.title,
            year=mediainfo.year,
            isAvailable=True,
            monitored=monitored,
            tmdbId=mediainfo.tmdb_id,
            imdbId=mediainfo.imdb_id,
            titleSlug=mediainfo.original_title,
            folderName=mediainfo.title_year,
            profileId=1,
            qualityProfileId=1,
            hasFile=hasfile,
        )
    ]


@arr_router.get(
    "/movie/{mid}", summary="电影订阅详情", response_model=_SchemaRadarrMovie
)
async def arr_movie(
    mid: int,
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
) -> _SchemaRadarrMovie:
    """
    查询Rardar电影订阅
    """
    subscribe = await subscriptions.get(mid)
    if subscribe:
        return RadarrMovie(
            id=subscribe.id,
            title=subscribe.name,
            year=subscribe.year,
            isAvailable=True,
            monitored=True,
            tmdbId=_subscribe_tmdb_id(subscribe),
            profileId=1,
            qualityProfileId=1,
            hasFile=False,
        )
    else:
        raise HTTPException(status_code=404, detail="未找到该电影！")


@arr_router.post(
    "/movie", summary="新增电影订阅", response_model=_SchemaServarrIdResponse
)
async def arr_add_movie(
    _: Annotated[str, Depends(verify_apikey)],
    movie: RadarrMovie,
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
) -> _SchemaServarrIdResponse:
    """
    新增Rardar电影订阅
    """
    # 检查订阅是否已存在
    subscribes = await subscriptions.list_by_media_identity(
        MediaSource.TMDB,
        str(movie.tmdbId),
    )
    if subscribes:
        return _SchemaServarrIdResponse(id=subscribes[0].id)
    # 添加订阅
    sid, message = await SubscribeChain().async_add(
        title=movie.title,
        year=movie.year,
        mtype=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id=str(movie.tmdbId),
        username="Seerr",
    )
    if sid:
        return _SchemaServarrIdResponse(id=sid)
    else:
        raise HTTPException(
            status_code=500,
            detail=f"添加订阅失败：{_subscribe_error_message(message)}",
        )


@arr_router.delete(
    "/movie/{mid}", summary="删除电影订阅", response_model=_SchemaResponse[None]
)
async def arr_remove_movie(
    mid: int,
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
) -> _SchemaResponse[None]:
    """
    删除Rardar电影订阅
    """
    if await subscriptions.delete(mid):
        return _SchemaResponse(success=True)
    else:
        raise HTTPException(status_code=404, detail="未找到该电影！")


@arr_router.get(
    "/series", summary="所有剧集", response_model=List[_SchemaSonarrSeries]
)
async def arr_series(
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
) -> List[_SchemaSonarrSeries]:
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
    # 查询所有电视剧订阅
    result = []
    subscribes = await subscriptions.list()
    for subscribe in subscribes:
        if subscribe.type != MediaType.TV.value:
            continue
        result.append(
            SonarrSeries(
                id=subscribe.id,
                title=subscribe.name,
                seasonCount=1,
                seasons=[
                    {
                        "seasonNumber": subscribe.season,
                        "monitored": True,
                    }
                ],
                remotePoster=subscribe.poster,
                year=subscribe.year,
                tmdbId=_subscribe_tmdb_id(subscribe),
                profileId=1,
                languageProfileId=1,
                qualityProfileId=1,
                isAvailable=True,
                monitored=True,
                hasFile=False,
            )
        )
    return result


@arr_router.get(
    "/series/lookup",
    summary="查询剧集",
    response_model=List[_SchemaSonarrSeries],
)
def arr_series_lookup(
    term: str,
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
) -> List[_SchemaSonarrSeries]:
    """
    查询Sonarr剧集 term: `tvdb:${id}` title
    """
    # tvdbid 列表
    tvdbids: List[int] = []
    # 获取TVDBID
    if not term.startswith("tvdb:"):
        title = term.replace("+", " ")
        tvdbids = TvdbChain().get_tvdbid_by_name(title=title)
    else:
        tvdbid = int(term.replace("tvdb:", ""))
        tvdbids.append(tvdbid)

    sonarr_series_list = []
    for tvdbid in tvdbids:
        # 查询TVDB信息
        tvdbinfo = MediaChain().tvdb_info(tvdbid=tvdbid)
        if not tvdbinfo:
            continue

        # 季信息(只取默认季类型，排除特别季)
        sea_num = len(
            [
                season
                for season in tvdbinfo.get("seasons")
                if season["type"]["id"] == tvdbinfo.get("defaultSeasonType")
                and season["number"] > 0
            ]
        )
        seas = list(range(1, int(sea_num) + 1)) if sea_num else []

        # 根据TVDB查询媒体信息
        meta = MetaInfo(tvdbinfo.get("name"))
        meta.type = MediaType.TV
        mediainfo = MediaChain().recognize_by_meta(
            meta,
            obtain_images=False,
        )
        if not mediainfo:
            continue
        # TVDB 未提供可用季信息时，按 TMDB 季集兜底，避免 Seerr 请求体季列表为空
        if not seas and mediainfo.seasons:
            seas = [season for season in mediainfo.seasons if season > 0]
        # 查询是否存在
        exists = MediaChain().media_exists(mediainfo)
        if exists:
            hasfile = True
        else:
            hasfile = False

        # 查询订阅信息
        seasons: List[dict] = []
        subscribes = subscriptions.list_by_media_identity_sync(
            MediaSource.TMDB,
            str(mediainfo.tmdb_id),
        )
        if subscribes:
            # 已监控
            monitored = True
            # 已监控季
            sub_seas = [sub.season for sub in subscribes]
            for sea in seas:
                if sea in sub_seas:
                    seasons.append(
                        {
                            "seasonNumber": sea,
                            "monitored": True,
                        }
                    )
                else:
                    seasons.append(
                        {
                            "seasonNumber": sea,
                            "monitored": False,
                        }
                    )
            subid = subscribes[-1].id
        else:
            subid = None
            monitored = False
            for sea in seas:
                seasons.append(
                    {
                        "seasonNumber": sea,
                        "monitored": False,
                    }
                )
        sonarr_series = SonarrSeries(
            id=subid,
            title=mediainfo.title,
            seasonCount=len(seasons),
            seasons=seasons,
            remotePoster=mediainfo.get_poster_image(),
            year=mediainfo.year,
            tmdbId=mediainfo.tmdb_id,
            tvdbId=tvdbid,
            imdbId=mediainfo.imdb_id,
            profileId=1,
            languageProfileId=1,
            monitored=monitored,
            hasFile=hasfile,
        )
        sonarr_series_list.append(sonarr_series)

    return sonarr_series_list if sonarr_series_list else [SonarrSeries()]


@arr_router.get(
    "/series/{tid}", summary="剧集详情", response_model=_SchemaSonarrSeries
)
async def arr_serie(
    tid: int,
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
) -> _SchemaSonarrSeries:
    """
    查询Sonarr剧集
    """
    subscribe = await subscriptions.get(tid)
    if subscribe:
        return SonarrSeries(
            id=subscribe.id,
            title=subscribe.name,
            seasonCount=1,
            seasons=[
                {
                    "seasonNumber": subscribe.season,
                    "monitored": True,
                }
            ],
            year=subscribe.year,
            remotePoster=subscribe.poster,
            tmdbId=_subscribe_tmdb_id(subscribe),
            profileId=1,
            languageProfileId=1,
            qualityProfileId=1,
            isAvailable=True,
            monitored=True,
            hasFile=False,
        )
    else:
        raise HTTPException(status_code=404, detail="未找到该电视剧！")


@arr_router.post(
    "/series", summary="新增剧集订阅", response_model=_SchemaServarrIdResponse
)
async def arr_add_series(
    tv: _SchemaSonarrSeries,
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
    batch_writer: Annotated[
        SubscriptionBatchWritePort,
        Depends(get_servarr_subscription_batch_writer),
    ],
) -> _SchemaServarrIdResponse:
    """
    新增Sonarr剧集订阅
    """
    # Seerr 的请求体只携带 tvdbId、不携带 tmdbId，缺失时按 TVDB 信息补全媒体身份；
    # 请求体季列表由 lookup 返回的季列表构造，lookup 季列表为空时请求体也会为空，此时按识别季集兜底
    mediainfo = None
    if not tv.tmdbId or not tv.seasons:
        mediainfo = _resolve_series_media(
            tvdbid=tv.tvdbId, title=tv.title, year=tv.year
        )
        if not tv.tmdbId:
            if not mediainfo:
                raise HTTPException(status_code=500, detail="添加订阅失败：未识别到媒体信息")
            tv.tmdbId = mediainfo.tmdb_id
        if mediainfo:
            if not tv.title:
                tv.title = mediainfo.title
            if not tv.year:
                tv.year = mediainfo.year
    # 提取请求季与监控标记，排除特别季
    seasons = [
        (season.seasonNumber, season.monitored)
        for season in tv.seasons
        if season.seasonNumber
    ]
    if not seasons:
        # 请求体未携带季信息时，订阅已识别的全部季，识别不到季则默认第 1 季
        fallback_seasons = (
            [season for season in (mediainfo.seasons or {}) if season > 0]
            if mediainfo
            else []
        )
        seasons = [(season, True) for season in (fallback_seasons or [1])]
    # 检查订阅是否存在
    left_seasons = []
    for season, monitored in seasons:
        if not monitored:
            continue
        subscribe = await subscriptions.exists(
            media_source=MediaSource.TMDB,
            media_id=str(tv.tmdbId),
            season=season,
        )
        if subscribe:
            continue
        left_seasons.append(season)
    # 全部已存在订阅
    if not left_seasons:
        return _SchemaServarrIdResponse(id=1)

    # TMDB 身份完整时允许空标题交由识别链补全；年份统一为订阅写入合同的字符串。
    subscribe_title = tv.title or ""
    subscribe_year = str(tv.year or "")

    try:
        sid, message = await SubscribeChain().async_add_batch(
            title=subscribe_title,
            year=subscribe_year,
            seasons=left_seasons,
            batch_writer=batch_writer,
            media_source=MediaSource.TMDB,
            media_id=str(tv.tmdbId),
            mtype=MediaType.TV,
            username="Seerr",
        )
    except SubscriptionBatchWriteError as error:
        raise HTTPException(
            status_code=500,
            detail=f"添加订阅失败：{_subscribe_error_message(error)}",
        ) from error

    if sid:
        return _SchemaServarrIdResponse(id=sid)
    raise HTTPException(
        status_code=500,
        detail=f"添加订阅失败：{_subscribe_error_message(message)}",
    )


@arr_router.put(
    "/series", summary="更新剧集订阅", response_model=_SchemaServarrIdResponse
)
async def arr_update_series(
    tv: _SchemaSonarrSeries,
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
    batch_writer: Annotated[
        SubscriptionBatchWritePort,
        Depends(get_servarr_subscription_batch_writer),
    ],
) -> _SchemaServarrIdResponse:
    """
    更新Sonarr剧集订阅
    """
    return await arr_add_series(
        tv=tv,
        _=_,
        subscriptions=subscriptions,
        batch_writer=batch_writer,
    )


@arr_router.delete(
    "/series/{tid}", summary="删除剧集订阅", response_model=_SchemaResponse[None]
)
async def arr_remove_series(
    tid: int,
    _: Annotated[str, Depends(verify_apikey)],
    subscriptions: Annotated[
        ServarrSubscriptionService,
        Depends(get_servarr_subscription_service),
    ],
) -> _SchemaResponse[None]:
    """
    删除Sonarr剧集订阅
    """
    if await subscriptions.delete(tid):
        return _SchemaResponse(success=True)
    else:
        raise HTTPException(status_code=404, detail="未找到该电视剧！")
