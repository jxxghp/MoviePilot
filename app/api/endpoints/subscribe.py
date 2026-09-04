from typing import Annotated, Any, List, Optional

import cn2an
from fastapi import Depends, Header, HTTPException, Request, Response

from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.web.security.access import (
    validate_api_credential_identity,
    verify_apitoken,
    verify_token,
)
from app.api.context import (
    get_background_task_registry,
    get_sync_subscription_repository,
    resolve_background_task_registry,
)
from app.api.dependencies.auth import (
    get_current_active_user,
    get_current_active_user_async,
)
from app.api.dependencies.subscription import (
    get_delete_subscribe_command,
    get_delete_subscriptions_by_identity_command,
    get_search_subscriptions_command,
    get_subscription_execution_status_service,
    get_subscription_mutation_service,
    get_subscription_query_service,
)
from app.api.principal import ApiPrincipal
from app.api.response import (
    COLLECTION_TOTAL_HEADER,
    COLLECTION_TOTAL_OPENAPI_KEY,
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
    resolve_compatible_pagination,
)
from app.application.configuration import (
    get_api_runtime_config_snapshot,
    get_configured_system_config,
)
from app.application.scheduling import get_scheduler
from app.application.subscription.contract import SubscriptionQueryPort
from app.application.subscription.delete import (
    DeleteSubscribeCommand,
    SubscribeDeletionActor,
)
from app.application.subscription.identity import (
    DeleteSubscriptionsByIdentityCommand,
)
from app.application.subscription.mutation import (
    SubscriptionActor,
    SubscriptionMutationService,
)
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.search import (
    SearchSubscriptionsCommand,
    SubscribeSearchActor,
)
from app.application.subscription.status import SubscriptionExecutionStatusService
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.runtime.errors import public_error_message
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.tasks import TaskRegistry
from app.schemas.common import IdData as _SchemaIdData
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.schemas.response import Response as _SchemaResponse
from app.schemas.subscribe import SubscrbieInfo as _SchemaSubscrbieInfo
from app.schemas.subscribe import SubscribeDeletionResult as _SchemaSubscribeDeletionResult
from app.schemas.subscribe import SubscribeShare as _SchemaSubscribeShare
from app.schemas.subscribe import SubscribeShareStatistics as _SchemaSubscribeShareStatistics
from app.schemas.subscribe import SubscriptionExecutionStatus as _SchemaSubscriptionExecutionStatus
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
    SystemConfigKey,
)
from app.schemas.workflow import MediaInfo as _SchemaMediaInfo
from app.schemas.workflow import Subscribe as _SchemaSubscribe

router = ResponseAPIRouter()


async def _attach_execution_status(
    subscribes: list[_SchemaSubscribe],
    status_service: object,
) -> list[_SchemaSubscribe]:
    """批量附加当前执行状态，保持无执行记录时的旧响应形状。"""
    loader = getattr(status_service, "for_subscriptions", None)
    if not callable(loader):
        return subscribes
    statuses = await loader(
        tuple(item.id for item in subscribes if item.id is not None)
    )
    for subscribe in subscribes:
        if subscribe.id is not None and (status := statuses.get(subscribe.id)) is not None:
            subscribe.execution_status = _SchemaSubscriptionExecutionStatus.model_validate(status)
    return subscribes


def start_subscribe_add(
    title: str,
    year: str,
    mtype: MediaType,
    media_source: MediaSource,
    media_id: str,
    season: int,
    username: str,
):
    """
    启动订阅任务
    """
    SubscribeChain().add(
        title=title,
        year=year,
        mtype=mtype,
        media_source=media_source,
        media_id=media_id,
        season=season,
        username=username,
    )


def build_subscribe_event_payload(subscribe: Any) -> dict:
    """
    从 ORM 已加载字段构造订阅事件快照，避免异步接口里属性懒加载触发隐式 IO。
    """
    return subscribe.to_dict()


def can_access_subscribe(
    subscribe: Any, current_user: ApiPrincipal
) -> bool:
    """
    判断当前用户是否可访问订阅及其历史记录。

    超级用户拥有全局订阅管理能力；普通用户只能访问 username 精确匹配自己的订阅。
    空 username 表示无法归属的 legacy 订阅，只能由超级用户管理。
    """
    if not subscribe:
        return False
    if current_user.is_superuser:
        return True
    username = subscribe.username
    return bool(username) and username == current_user.name


def select_accessible_subscribe(
    subscribes: List[Any], current_user: ApiPrincipal
) -> Any:
    """
    从候选订阅中选择当前用户可访问的第一条记录。
    """
    for subscribe in subscribes or []:
        if can_access_subscribe(subscribe, current_user):
            return subscribe
    return None


def matches_subscribe_music_type(
        subscribe: Any,
        music_type: Optional[str],
) -> bool:
    """匹配订阅音乐实体，并把迁移前未标注类型的历史记录兼容为单曲。"""
    if not music_type:
        return True
    subscribe_music_type = getattr(subscribe, "music_type", None)
    return subscribe_music_type == music_type \
        or (music_type == MUSIC_ENTITY_RECORDING and subscribe_music_type is None)


@router.get(
    "/",
    summary="查询所有订阅",
    response_model=List[_SchemaSubscribe],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def read_subscribes(
    response: Response = None,
    query: SubscriptionQueryService = Depends(get_subscription_query_service),
    status_service: SubscriptionExecutionStatusService = Depends(
        get_subscription_execution_status_service
    ),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询所有订阅
    """
    username = None if current_user.is_superuser else current_user.name
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_public(username)
        )
    subscribes = await query.list_public(username, page=page, count=count)
    return await _attach_execution_status(subscribes, status_service)


@router.get(
    "/list",
    summary="查询所有订阅（API_TOKEN）",
    response_model=List[_SchemaSubscribe],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def list_subscribes(
    response: Response = None,
    query: SubscriptionQueryService = Depends(get_subscription_query_service),
    status_service: SubscriptionExecutionStatusService = Depends(
        get_subscription_execution_status_service
    ),
    _: Annotated[str, Depends(verify_apitoken)] = None,
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询所有订阅 API_TOKEN认证（?token=xxx）
    """
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(await query.count_public())
    subscribes = await query.list_public(page=page, count=count)
    return await _attach_execution_status(subscribes, status_service)


@router.post(
    "/",
    summary="新增订阅",
    response_model=_SchemaResponse[_SchemaIdData],
)
async def create_subscribe(
    *,
    subscribe_in: _SchemaSubscribe,
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> _SchemaResponse:
    """
    新增订阅
    """
    # 类型转换
    if subscribe_in.type:
        mtype = MediaType(subscribe_in.type)
    else:
        mtype = None
    # 非 TMDB 来源的标题可能自带季标记，入库前统一拆分。
    if (
            mtype != MediaType.MUSIC
            and normalize_media_source(subscribe_in.media_source)
            not in (None, MediaSource.TMDB)
    ):
        meta = MetaInfo(subscribe_in.name)
        subscribe_in.name = meta.name
        if subscribe_in.season is None:
            subscribe_in.season = meta.begin_season
    # 空标题由订阅识别链按显式媒体身份补全，但调用契约始终使用字符串。
    title = subscribe_in.name or ""
    subscribe_dict = subscribe_in.to_public_write_payload()
    identity_fields = {"media_source", "media_id"}.intersection(
        subscribe_in.model_fields_set
    )
    if identity_fields:
        media_source, media_id = resolve_media_identity(
            media_source=subscribe_in.media_source,
            media_id=subscribe_in.media_id,
        )
        if media_source and media_id:
            subscribe_dict["media_source"] = media_source
            subscribe_dict["media_id"] = media_id
        elif subscribe_in.media_source is None and subscribe_in.media_id is None:
            # 完整空对表示订阅暂无可用身份，与只提交其中一个字段语义不同。
            subscribe_dict["media_source"] = None
            subscribe_dict["media_id"] = None
        else:
            return _SchemaResponse(
                success=False,
                message="新增订阅时必须同时提供有效的 media_source 和 media_id",
            )
    subscribe_dict["username"] = current_user.name
    sid, message = await SubscribeChain().async_add(
        mtype=mtype,
        title=title,
        exist_ok=True,
        owner_scope=not current_user.is_superuser,
        **subscribe_dict,
    )
    return _SchemaResponse(
        success=bool(sid),
        message=(
            public_error_message(message, context="subscription")
            if message
            else ""
        ),
        data={"id": sid},
    )


@router.put("/", summary="更新订阅", response_model=_SchemaResponse[None])
async def update_subscribe(
    *,
    subscribe_in: _SchemaSubscribe,
    mutation: SubscriptionMutationService = Depends(get_subscription_mutation_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    更新订阅信息
    """
    actor = SubscriptionActor(
        name=current_user.name,
        is_superuser=current_user.is_superuser,
    )
    subscribe = await mutation.get_accessible(subscribe_in.id, actor)
    if not subscribe:
        return _SchemaResponse(success=False, message="订阅不存在")
    subscribe_dict = subscribe_in.to_public_write_payload(exclude_unset=True)
    identity_fields = {"media_source", "media_id"}.intersection(
        subscribe_in.model_fields_set
    )
    if identity_fields:
        media_source, media_id = resolve_media_identity(
            media_source=subscribe_in.media_source,
            media_id=subscribe_in.media_id,
        )
        if media_source and media_id:
            subscribe_dict["media_source"] = media_source
            subscribe_dict["media_id"] = media_id
        elif subscribe_in.media_source is None and subscribe_in.media_id is None:
            # 只有两个身份键都显式为空时才清空；全部省略则保留存量身份。
            subscribe_dict["media_source"] = None
            subscribe_dict["media_id"] = None
        else:
            return _SchemaResponse(
                success=False,
                message="更新媒体身份时必须同时提供有效的 media_source 和 media_id",
            )
    subscribe_dict["username"] = subscribe.username
    if getattr(subscribe, "type", None) == MediaType.MUSIC.value:
        # 音乐实体与曲目总数来自识别链，编辑接口不得把专辑改成单曲而提前完成订阅。
        subscribe_dict["type"] = subscribe.type
        subscribe_dict["music_type"] = subscribe.music_type
        subscribe_dict["total_tracks"] = subscribe.total_tracks \
            if subscribe.music_type == MUSIC_ENTITY_ALBUM else None
    total_episode_updated = "total_episode" in subscribe_in.model_fields_set
    if (
            total_episode_updated
            and subscribe_in.total_episode
            and subscribe_in.total_episode > (subscribe.total_episode or 0)
    ):
        # 扩大目标范围时，新增加的集数尚无下载事实，应同步计入缺失集数。
        subscribe_dict["lack_episode"] = (subscribe.lack_episode or 0) + (
            subscribe_in.total_episode - (subscribe.total_episode or 0)
        )
    # 是否手动修改过总集数
    if total_episode_updated and subscribe_in.total_episode != subscribe.total_episode:
        subscribe_dict["manual_total_episode"] = 1
    # 更新到数据库
    try:
        change = await mutation.update(
            subscribe_in.id,
            subscribe_dict,
            actor,
            existing=subscribe,
        )
    except ValueError as error:
        logger.error(f"订阅分类设置无效：{error}", exc_info=True)
        return _SchemaResponse(
            success=False,
            message="订阅分类设置无效，请重新选择分类后重试",
        )
    if not change:
        return _SchemaResponse(success=False, message="订阅不存在")
    return _SchemaResponse(success=True)


@router.put("/status/{subid}", summary="更新订阅状态", response_model=_SchemaResponse[None])
async def update_subscribe_status(
    subid: int,
    state: str,
    mutation: SubscriptionMutationService = Depends(get_subscription_mutation_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    更新订阅状态
    """
    valid_states = ["R", "P", "S"]
    if state not in valid_states:
        return _SchemaResponse(success=False, message="无效的订阅状态")
    actor = SubscriptionActor(
        name=current_user.name,
        is_superuser=current_user.is_superuser,
    )
    change = await mutation.update_status(subid, state, actor)
    if not change:
        return _SchemaResponse(success=False, message="订阅不存在")
    return _SchemaResponse(success=True)


@router.get("/media/{media_id}", summary="查询订阅", response_model=_SchemaSubscribe)
async def subscribe_media_identity(
    media_id: str,
    media_source: MediaSource,
    season: Optional[int] = None,
    title: Optional[str] = None,
    music_type: Optional[str] = None,
    query: SubscriptionQueryService = Depends(get_subscription_query_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    根据媒体来源和原生 ID 查询订阅。
    """
    subscribes = await query.list_by_media_identity(media_source, media_id, music_type)
    if season is not None:
        subscribes = [subscribe for subscribe in subscribes if subscribe.season == season]
    result = select_accessible_subscribe(subscribes, current_user)
    return result if result else _SchemaSubscribe()


@router.get("/refresh", summary="刷新订阅", response_model=_SchemaResponse[None])
def refresh_subscribes(
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> Any:
    """
    刷新所有订阅
    """
    if not current_user.is_superuser:
        return _SchemaResponse(success=False, message="订阅不存在")
    get_scheduler().start("subscribe_refresh")
    return _SchemaResponse(success=True)


@router.get("/reset/{subid}", summary="重置订阅", response_model=_SchemaResponse[None])
async def reset_subscribes(
    subid: int,
    mutation: SubscriptionMutationService = Depends(get_subscription_mutation_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    重置订阅
    """
    actor = SubscriptionActor(
        name=current_user.name,
        is_superuser=current_user.is_superuser,
    )
    change = await mutation.reset(subid, actor)
    if change:
        return _SchemaResponse(success=True)
    return _SchemaResponse(success=False, message="订阅不存在")


@router.get("/check", summary="刷新订阅 TMDB 信息", response_model=_SchemaResponse[None])
def check_subscribes(
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> Any:
    """
    刷新订阅 TMDB 信息
    """
    if not current_user.is_superuser:
        return _SchemaResponse(success=False, message="订阅不存在")
    get_scheduler().start("subscribe_tmdb")
    return _SchemaResponse(success=True)


@router.get("/search", summary="搜索所有订阅", response_model=_SchemaResponse[None])
async def search_subscribes(
    command: SearchSubscriptionsCommand = Depends(get_search_subscriptions_command),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    搜索所有订阅
    """
    await command.execute(
        SubscribeSearchActor(
            username=current_user.name,
            is_superuser=current_user.is_superuser,
        )
    )
    return _SchemaResponse(success=True)


@router.get(
    "/search/{subscribe_id}", summary="搜索订阅", response_model=_SchemaResponse[None]
)
async def search_subscribe(
    subscribe_id: int,
    command: SearchSubscriptionsCommand = Depends(get_search_subscriptions_command),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    根据订阅编号搜索订阅
    """
    found = await command.execute(
        SubscribeSearchActor(
            username=current_user.name,
            is_superuser=current_user.is_superuser,
        ),
        subscribe_id=subscribe_id,
    )
    if not found:
        return _SchemaResponse(success=False, message="订阅不存在")
    return _SchemaResponse(success=True)


@router.delete("/media/{media_id}", summary="删除订阅", response_model=_SchemaResponse[None])
async def delete_subscribe_by_media_identity(
    media_id: str,
    media_source: MediaSource,
    season: Optional[int] = None,
    music_type: Optional[str] = None,
    command: DeleteSubscriptionsByIdentityCommand = Depends(
        get_delete_subscriptions_by_identity_command
    ),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    根据任意媒体数据源 ID 删除订阅。
    """
    await command.execute(
        media_source,
        media_id,
        season,
        music_type,
        SubscribeDeletionActor(
            username=current_user.name,
            is_superuser=current_user.is_superuser,
        ),
    )
    return _SchemaResponse(success=True)


@router.post(
    "/seerr", summary="OverSeerr/JellySeerr通知订阅", response_model=_SchemaResponse[None]
)
async def seerr_subscribe(
    request: Request,
    task_registry: Annotated[TaskRegistry, Depends(get_background_task_registry)],
    authorization: Annotated[str | None, Header()] = None,
) -> Any:
    """
    Jellyseerr/Overseerr网络勾子通知订阅
    """
    if not authorization or authorization != get_api_runtime_config_snapshot().api_token:
        raise HTTPException(
            status_code=401,
            detail="授权失败",
            headers={"WWW-Authenticate": "Bearer"},
        )
    await run_in_threadpool(validate_api_credential_identity)
    req_json = await request.json()
    if not req_json:
        raise HTTPException(
            status_code=500,
            detail="报文内容为空",
        )
    notification_type = req_json.get("notification_type")
    if notification_type not in ["MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"]:
        return _SchemaResponse(success=False, message="不支持的通知类型")
    subject = req_json.get("subject")
    media_type = (
        MediaType.MOVIE
        if req_json.get("media", {}).get("media_type") == "movie"
        else MediaType.TV
    )
    tmdbId = req_json.get("media", {}).get("tmdbId")
    if not media_type or not tmdbId or not subject:
        return _SchemaResponse(success=False, message="请求参数不正确")
    user_name = req_json.get("request", {}).get("requestedBy_username")
    # 添加订阅
    if media_type == MediaType.MOVIE:
        resolve_background_task_registry(task_registry).create_sync(
            start_subscribe_add,
            mtype=media_type,
            media_source=MediaSource.TMDB,
            media_id=str(tmdbId),
            title=subject,
            year="",
            # 电影不传季号，避免被误判为剧集（S00）并污染通知标题
            season=None,
            username=user_name,
            owner="api.subscribe.seerr",
        )
    else:
        seasons = []
        for extra in req_json.get("extra", []):
            if extra.get("name") == "Requested Seasons":
                seasons = [
                    int(str(sea).strip())
                    for sea in extra.get("value").split(", ")
                    if str(sea).isdigit()
                ]
                break
        for season in seasons:
            resolve_background_task_registry(task_registry).create_sync(
                start_subscribe_add,
                mtype=media_type,
                media_source=MediaSource.TMDB,
                media_id=str(tmdbId),
                title=subject,
                year="",
                season=season,
                username=user_name,
                owner="api.subscribe.seerr",
            )

    return _SchemaResponse(success=True)


@router.get(
    "/history/{mtype}",
    summary="查询订阅历史",
    response_model=List[_SchemaSubscribe],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def subscribe_history(
    mtype: str,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    query: SubscriptionQueryService = Depends(get_subscription_query_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
    response: Response = None,
) -> Any:
    """
    查询电影、电视剧或音乐订阅历史
    """
    username = None if current_user.is_superuser else current_user.name
    results = await query.list_history(
        mtype,
        page=page,
        count=count,
        username=username,
    )
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_history(mtype, username=username)
        )
    return results


@router.delete(
    "/history/{history_id}",
    summary="删除订阅历史",
    response_model=_SchemaResponse[_SchemaSubscribeDeletionResult],
)
async def delete_subscribe_history(
    history_id: int,
    mutation: SubscriptionMutationService = Depends(get_subscription_mutation_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    删除订阅历史
    """
    actor = SubscriptionActor(
        name=current_user.name,
        is_superuser=current_user.is_superuser,
    )
    status = await mutation.delete_history_with_status(history_id, actor)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="订阅历史不存在")
    if status == "forbidden":
        raise HTTPException(status_code=403, detail="无权删除该订阅历史")
    return _SchemaResponse(
        success=True,
        data=_SchemaSubscribeDeletionResult(status="deleted"),
    )


@router.get(
    "/popular",
    summary="热门订阅（基于用户共享数据）",
    response_model=List[_SchemaMediaInfo],
)
async def popular_subscribes(
    stype: str,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    min_sub: Optional[int] = None,
    genre_id: Optional[int] = None,
    min_rating: Optional[float] = None,
    max_rating: Optional[float] = None,
    sort_type: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    查询热门订阅
    """
    subscribes = await MoviePilotServerHelper.async_get_subscribe_statistic(
        stype=stype,
        page=page,
        count=count,
        genre_id=genre_id,
        min_rating=min_rating,
        max_rating=max_rating,
        sort_type=sort_type,
    )
    if subscribes:
        ret_medias = []
        for sub in subscribes:
            # 订阅人数
            count = sub.get("count")
            if min_sub and count < min_sub:
                continue
            media = MediaInfo()
            media.type = MediaType(sub.get("type"))
            media.media_source = normalize_media_source(sub.get("media_source"))
            media.media_id = str(sub.get("media_id")) if sub.get("media_id") is not None else None
            # 处理标题
            title = sub.get("name")
            season = sub.get("season")
            if season not in (None, "") and int(season) != 1:
                # 小写数据转大写
                season_str = cn2an.an2cn(season, "low")
                title = f"{title} 第{season_str}季"
            media.title = title
            media.year = sub.get("year")
            media.season = sub.get("season")
            media.overview = sub.get("description")
            media.vote_average = sub.get("vote")
            media.poster_path = sub.get("poster")
            media.backdrop_path = sub.get("backdrop")
            media.popularity = count
            ret_medias.append(media)
        return [media.to_dict() for media in ret_medias]
    return []


@router.get(
    "/user/{username}",
    summary="用户订阅",
    response_model=List[_SchemaSubscribe],
    openapi_extra={COLLECTION_TOTAL_OPENAPI_KEY: True},
)
async def user_subscribes(
    username: str,
    response: Response = None,
    query: SubscriptionQueryService = Depends(get_subscription_query_service),
    status_service: SubscriptionExecutionStatusService = Depends(
        get_subscription_execution_status_service
    ),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询用户订阅
    """
    if not current_user.is_superuser and username != current_user.name:
        return []
    page, count = resolve_compatible_pagination(page, count)
    if response is not None:
        response.headers[COLLECTION_TOTAL_HEADER] = str(
            await query.count_public(username)
        )
    subscribes = await query.list_public(username, page=page, count=count)
    return await _attach_execution_status(subscribes, status_service)


@router.get(
    "/files/{subscribe_id}",
    summary="订阅相关文件信息",
    response_model=_SchemaSubscrbieInfo,
)
def subscribe_files(
    subscribe_id: int,
    repository: SubscriptionQueryPort = Depends(get_sync_subscription_repository),
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> Any:
    """
    订阅相关文件信息
    """
    actor = SubscriptionActor(
        name=current_user.name,
        is_superuser=current_user.is_superuser,
    )
    subscribe = repository.get(subscribe_id)
    if subscribe is not None and SubscriptionMutationService.can_access(subscribe, actor):
        return SubscribeChain().subscribe_files_info(subscribe)
    return _SchemaSubscrbieInfo()


@router.post("/share", summary="分享订阅", response_model=_SchemaResponse[None])
async def subscribe_share(
    sub: _SchemaSubscribeShare,
    mutation: SubscriptionMutationService = Depends(get_subscription_mutation_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    分享订阅
    """
    actor = SubscriptionActor(
        name=current_user.name,
        is_superuser=current_user.is_superuser,
    )
    subscribe = await mutation.get_accessible(sub.subscribe_id, actor)
    if not subscribe:
        return _SchemaResponse(success=False, message="订阅不存在")
    state, errmsg = await MoviePilotServerHelper.async_sub_share(
        subscribe_id=sub.subscribe_id,
        share_title=sub.share_title,
        share_comment=sub.share_comment,
        share_user=sub.share_user,
    )
    return _SchemaResponse(
        success=state,
        message=(
            public_error_message(errmsg, context="subscription")
            if errmsg
            else ""
        ),
    )


@router.delete("/share/{share_id}", summary="删除分享", response_model=_SchemaResponse[None])
async def subscribe_share_delete(
    share_id: int, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    删除分享
    """
    state, errmsg = await MoviePilotServerHelper.async_share_delete(share_id=share_id)
    return _SchemaResponse(
        success=state,
        message=(
            public_error_message(errmsg, context="subscription")
            if errmsg
            else ""
        ),
    )


@router.post("/fork", summary="复用订阅", response_model=_SchemaResponse[None])
async def subscribe_fork(
    sub: _SchemaSubscribeShare,
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    复用订阅
    """
    sub_dict = sub.model_dump()
    sub_dict.pop("id")
    for key in list(sub_dict.keys()):
        if not hasattr(_SchemaSubscribe(), key):
            sub_dict.pop(key)
    result = await create_subscribe(
        subscribe_in=_SchemaSubscribe(**sub_dict), current_user=current_user
    )
    if result.success:
        await MoviePilotServerHelper.async_sub_fork(share_id=sub.id)
    return result


@router.get("/follow", summary="查询已Follow的订阅分享人", response_model=List[str])
async def followed_subscribers(_: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    查询已Follow的订阅分享人
    """
    return get_configured_system_config().get(SystemConfigKey.FollowSubscribers) or []


@router.post("/follow", summary="Follow订阅分享人", response_model=_SchemaResponse[None])
async def follow_subscriber(
    share_uid: Optional[str] = None, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    Follow订阅分享人
    """
    subscribers = get_configured_system_config().get(SystemConfigKey.FollowSubscribers) or []
    if share_uid and share_uid not in subscribers:
        subscribers.append(share_uid)
        await get_configured_system_config().async_set(
            SystemConfigKey.FollowSubscribers, subscribers
        )
    return _SchemaResponse(success=True)


@router.delete(
    "/follow", summary="取消Follow订阅分享人", response_model=_SchemaResponse[None]
)
async def unfollow_subscriber(
    share_uid: Optional[str] = None, _: _SchemaTokenPayload = Depends(verify_token)
) -> Any:
    """
    取消Follow订阅分享人
    """
    subscribers = get_configured_system_config().get(SystemConfigKey.FollowSubscribers) or []
    if share_uid and share_uid in subscribers:
        subscribers.remove(share_uid)
        await get_configured_system_config().async_set(
            SystemConfigKey.FollowSubscribers, subscribers
        )
    return _SchemaResponse(success=True)


@router.get(
    "/shares", summary="查询分享的订阅", response_model=List[_SchemaSubscribeShare]
)
async def subscribe_shares(
    name: Optional[str] = None,
    page: Optional[int] = 1,
    count: Optional[int] = 30,
    genre_id: Optional[int] = None,
    min_rating: Optional[float] = None,
    max_rating: Optional[float] = None,
    sort_type: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """
    查询分享的订阅
    """
    return await MoviePilotServerHelper.async_get_subscribe_shares(
        name=name,
        page=page,
        count=count,
        genre_id=genre_id,
        min_rating=min_rating,
        max_rating=max_rating,
        sort_type=sort_type,
    )


@router.get(
    "/share/statistics",
    summary="查询订阅分享统计",
    response_model=List[_SchemaSubscribeShareStatistics],
)
async def subscribe_share_statistics(
    _: _SchemaTokenPayload = Depends(verify_token),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> Any:
    """
    查询订阅分享统计
    返回每个分享人分享的媒体数量以及总的复用人次
    """
    return await MoviePilotServerHelper.async_get_subscribe_share_statistics()


@router.get("/{subscribe_id}", summary="订阅详情", response_model=_SchemaSubscribe)
async def read_subscribe(
    subscribe_id: int,
    query: SubscriptionQueryService = Depends(get_subscription_query_service),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    根据订阅编号查询订阅信息
    """
    if not subscribe_id:
        return _SchemaSubscribe()
    subscribe = await query.get_public(subscribe_id)
    return (
        subscribe
        if subscribe and can_access_subscribe(subscribe, current_user)
        else _SchemaSubscribe()
    )


@router.delete(
    "/{subscribe_id}",
    summary="删除订阅",
    response_model=_SchemaResponse[_SchemaSubscribeDeletionResult],
)
async def delete_subscribe(
    subscribe_id: int,
    command: DeleteSubscribeCommand = Depends(get_delete_subscribe_command),
    current_user: ApiPrincipal = Depends(get_current_active_user_async),
) -> Any:
    """
    删除订阅信息
    """
    status = await command.execute_with_status(
        subscribe_id,
        SubscribeDeletionActor(
            username=current_user.name,
            is_superuser=current_user.is_superuser,
        ),
    )
    if status == "not_found":
        raise HTTPException(status_code=404, detail="订阅不存在")
    if status == "forbidden":
        raise HTTPException(status_code=403, detail="无权删除该订阅")
    return _SchemaResponse(
        success=True,
        data=_SchemaSubscribeDeletionResult(status="deleted"),
    )
