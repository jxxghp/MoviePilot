import asyncio
import io
import json
import zipfile
from datetime import datetime
from typing import Annotated, Any, Optional, Union

import anyio
import pillow_avif  # noqa: F401  # pylint: disable=unused-import  # AVIF 注册副作用
from fastapi import Body, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.adapters.web.security.access import verify_apitoken, verify_resource_token, verify_token
from app.api.context import get_host_runtime
from app.api.dependencies.auth import (
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user_async,
)
from app.api.endpoints.identifier import router as system_identifiers_router
from app.api.principal import ApiPrincipal
from app.api.response import CompatibleCountParam, CompatiblePageParam, ResponseAPIRouter
from app.application.backup import DatabaseBackupInProgressError
from app.application.configuration import (
    get_configured_system_config,
    get_runtime_settings,
)
from app.application.database import get_database_governance
from app.application.image import ImageHelper
from app.application.messaging.message import MessageHelper
from app.application.module import get_module_manager
from app.application.network import get_configured_network_test_service
from app.application.rules import RuleHelper
from app.application.scheduling import get_scheduler
from app.application.security.url import SecurityUtils
from app.application.settings import SystemSettingsService
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.application.system import LogFileData, LogNotFoundError
from app.chain.media import MediaChain
from app.chain.mediaserver import MediaServerChain
from app.chain.search.facade import SearchChain
from app.domain.context import TorrentInfo as _DomainTorrentInfo
from app.domain.metainfo import MetaInfo
from app.foundation.crypto import HashUtils
from app.foundation.environment import is_free_threaded_runtime, is_gil_enabled
from app.runtime.execution import run_in_threadpool_to_completion
from app.runtime.localization import LocaleHelper
from app.runtime.log import logger
from app.runtime.progress import AsyncProgressHelper
from app.runtime.stop import runtime_stop_state
from app.runtime.version import get_app_version, get_frontend_version
from app.schemas.common import JsonObject as _SchemaJsonObject
from app.schemas.common import JsonObjectList as _SchemaJsonObjectList
from app.schemas.common import TimeData as _SchemaTimeData
from app.schemas.common import ValueData as _SchemaValueData
from app.schemas.response import Response as _SchemaResponse
from app.schemas.system import DatabaseBackupArtifactData as _SchemaDatabaseBackupArtifactData
from app.schemas.system import DatabaseBackupVerificationData as _SchemaDatabaseBackupVerificationData
from app.schemas.system import NetTestTarget as _SchemaNetTestTarget
from app.schemas.system import PluginMarketSyncData as _SchemaPluginMarketSyncData
from app.schemas.system import PluginMarketSyncRequest as _SchemaPluginMarketSyncRequest
from app.schemas.system import RuleTestData as _SchemaRuleTestData
from app.schemas.system import SystemEnvironmentUpdateData as _SchemaSystemEnvironmentUpdateData
from app.schemas.system import SystemModuleListData as _SchemaSystemModuleListData
from app.schemas.system import SystemSettingsUpdateRequest as _SchemaSystemSettingsUpdateRequest
from app.schemas.system import SystemUpdateRequest as _SchemaSystemUpdateRequest
from app.schemas.system import SystemUpdateStatus as _SchemaSystemUpdateStatus
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import SystemConfigKey
from app.startup.composition.context import HostRuntime

router = ResponseAPIRouter()
router.routes.extend(system_identifiers_router.routes)

_PUBLIC_SYSTEM_CONFIG_KEYS = {
    item.value: item
    for item in (
        SystemConfigKey.Directories,
        SystemConfigKey.Storages,
        SystemConfigKey.IndexerSites,
        SystemConfigKey.EpisodeFormatRuleTable,
        SystemConfigKey.DefaultMovieSubscribeConfig,
        SystemConfigKey.DefaultTvSubscribeConfig,
        SystemConfigKey.DefaultMusicSubscribeConfig,
        SystemConfigKey.FollowSubscribers,
    )
}
_PUBLIC_SETTINGS_KEYS = {"PLUGIN_MARKET"}


def _database_backup_artifact_data(artifact: Any) -> _SchemaDatabaseBackupArtifactData:
    """将内部备份制品映射为不含宿主路径的 Web DTO。"""
    return _SchemaDatabaseBackupArtifactData(
        name=artifact.name,
        db_type=artifact.db_type,
        created_at=artifact.created_at,
        size=artifact.size,
    )


def _verify_log_resource_superuser(
    token_payload: _SchemaTokenPayload = Depends(verify_resource_token),
) -> _SchemaTokenPayload:
    """
    校验日志资源访问权限。

    日志接口通过浏览器新窗口和 EventSource 访问，不能依赖普通 API 请求头；
    因此这里复用资源 Cookie 完成身份识别，再额外要求管理员身份，避免普通
    登录用户读取可能包含敏感信息的日志。
    """
    if not token_payload.super_user:
        raise HTTPException(status_code=403, detail="用户权限不足")
    return token_payload


async def _build_log_zip_response(name: str, runtime: HostRuntime) -> StreamingResponse:
    """读取受限日志条目并生成兼容 ZIP 响应。"""
    try:
        entries = await runtime.system.collect_logs(name)
    except LogNotFoundError as error:
        raise HTTPException(status_code=404, detail="Not Found") from error
    zip_data, zip_stem = await anyio.to_thread.run_sync(_build_log_zip_data, name, entries)
    headers = {"Content-Disposition": f'attachment; filename="{zip_stem}.zip"'}
    return StreamingResponse(
        iter([zip_data]),
        media_type="application/zip",
        headers=headers,
    )


def _build_log_zip_data(name: str, entries: list[LogFileData]) -> tuple[bytes, str]:
    """只负责把应用服务提供的日志内容编码为 ZIP framing。"""
    zip_buffer = io.BytesIO()
    filename_time = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = (name or "logs").strip().lower() or "logs"
    zip_stem = f"{safe_name}-logs-{filename_time}"
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            archive.writestr(f"{zip_stem}/{entry.name}", entry.content)

    zip_buffer.seek(0)
    return zip_buffer.getvalue(), zip_stem


async def fetch_image(
    url: str,
    proxy: Optional[bool] = None,
    use_cache: bool = False,
    if_none_match: Optional[str] = None,
    cookies: Optional[str | dict] = None,
    allowed_domains: Optional[set[str]] = None,
) -> Optional[Response]:
    """
    处理图片缓存逻辑，支持HTTP缓存和磁盘缓存
    """
    if not url:
        return None

    if allowed_domains is None:
        allowed_domains = set(get_runtime_settings().get("SECURITY_IMAGE_DOMAINS", []))

    fetch_url = SecurityUtils.strip_url_signature(url)
    # 验证URL安全性
    if not await SecurityUtils.is_safe_image_url_async(
        url,
        allowed_domains,
        allowed_private_ranges=get_runtime_settings().get(
            "IMAGE_PROXY_ALLOWED_PRIVATE_RANGES",
            [],
        ),
    ):
        return None

    image_result = await ImageHelper().async_fetch_image_with_mime_type(
        url=fetch_url,
        proxy=proxy,
        use_cache=use_cache,
        cookies=cookies,
    )

    if image_result:
        content, media_type = image_result

        # 检查 If-None-Match
        etag = HashUtils.md5(content)
        headers = {
            "ETag": etag,
            "Cache-Control": f"public, max-age={86400 * 7}",
        }
        headers["Content-Type"] = media_type
        headers["X-Content-Type-Options"] = "nosniff"
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        # 返回缓存图片
        return Response(
            content=content,
            media_type=media_type,
            headers=headers,
        )
    return None


@router.get(
    "/img/{proxy}",
    summary="图片代理",
    response_model=None,
    response_class=Response,
    responses={
        200: {
            "description": "代理图片内容",
            "content": {
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/webp": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        304: {"description": "图片缓存未修改"},
    },
)
async def proxy_img(
    imgurl: str,
    proxy: bool = False,
    cache: bool = False,
    use_cookies: bool = False,
    if_none_match: Annotated[str | None, Header()] = None,
    _: _SchemaTokenPayload = Depends(verify_resource_token),
) -> Response:
    """
    图片代理，可选是否使用代理服务器，支持 HTTP 缓存
    """
    allowed_domains = set(get_runtime_settings().get("SECURITY_IMAGE_DOMAINS", []))
    cookies = MediaServerChain().get_image_cookies(server=None, image_url=imgurl) if use_cookies else None
    return await fetch_image(
        url=imgurl,
        proxy=proxy,
        use_cache=cache,
        cookies=cookies,
        if_none_match=if_none_match,
        allowed_domains=allowed_domains,
    )


@router.get(
    "/cache/image",
    summary="图片缓存",
    response_model=None,
    response_class=Response,
    responses={
        200: {
            "description": "缓存图片内容",
            "content": {
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/webp": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        304: {"description": "图片缓存未修改"},
    },
)
async def cache_img(
    url: str,
    if_none_match: Annotated[str | None, Header()] = None,
    _: _SchemaTokenPayload = Depends(verify_resource_token),
) -> Response:
    """
    本地缓存图片文件，支持 HTTP 缓存，如果启用全局图片缓存，则使用磁盘缓存
    """
    # 如果没有启用全局图片缓存，则不使用磁盘缓存
    return await fetch_image(
        url=url,
        use_cache=bool(get_runtime_settings().get("GLOBAL_IMAGE_CACHE")),
        if_none_match=if_none_match,
    )


@router.get(
    "/global",
    summary="查询非敏感系统设置",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
def get_global_setting(token: str):
    """
    查询非敏感系统设置（默认鉴权）
    仅包含登录前UI初始化必需的字段
    """
    if token != "moviepilot":
        raise HTTPException(status_code=403, detail="Forbidden")

    # 白名单模式，仅包含登录前UI初始化必需的字段
    runtime_settings = get_runtime_settings()
    info = runtime_settings.snapshot(
        include={
            "TMDB_IMAGE_DOMAIN",
            "GLOBAL_IMAGE_CACHE",
            "WALLPAPER_ROTATION_INTERVAL",
        }
    )
    # 追加版本信息（用于版本检查）
    info.update(
        {
            "FRONTEND_VERSION": get_frontend_version(),
            "BACKEND_VERSION": get_app_version(),
        }
    )
    # 仅在后端开发模式下返回该标记，避免生产环境暴露无意义运行态信息
    if runtime_settings.get("DEV"):
        info.update({"BACKEND_DEV": True})
    return _SchemaResponse(success=True, data=info)


@router.get(
    "/global/user",
    summary="查询用户相关系统设置",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def get_user_global_setting(
    _: ApiPrincipal = Depends(get_current_active_user_async),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """
    查询用户相关系统设置（登录后获取）
    包含业务功能相关的配置和用户权限信息
    """
    # 业务功能相关的配置字段
    runtime_settings = get_runtime_settings()
    info = runtime_settings.snapshot(
        include={
            "AI_AGENT_ENABLE",
            "AI_AGENT_HIDE_ENTRY",
            "LLM_SUPPORT_AUDIO_INPUT",
            "LLM_SUPPORT_AUDIO_OUTPUT",
            "RECOGNIZE_SOURCE",
            "SEARCH_SOURCE",
            "AI_RECOMMEND_ENABLED",
        }
    )
    # 智能助手总开关未开启，智能推荐状态强制返回False
    if not runtime_settings.get("AI_AGENT_ENABLE"):
        info["AI_RECOMMEND_ENABLED"] = False
        info["LLM_SUPPORT_AUDIO_INPUT"] = False
        info["LLM_SUPPORT_AUDIO_OUTPUT"] = False

    # 追加用户唯一ID和订阅分享管理权限
    info.update(
        {
            "PYTHON_FREE_THREADED": is_free_threaded_runtime(),
            "PYTHON_GIL_ENABLED": is_gil_enabled(),
        }
    )
    info.update(await runtime.system.user_global())
    return _SchemaResponse(success=True, data=info)


@router.get("/database/backups", summary="查询受管数据库备份", response_model=list[_SchemaDatabaseBackupArtifactData])
async def list_database_backups(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> list[_SchemaDatabaseBackupArtifactData]:
    """列出当前备份目录中的正式制品，不触发内容校验。"""
    try:
        artifacts = await run_in_threadpool_to_completion(get_database_governance().list_backups)
    except Exception as error:
        logger.exception("读取数据库备份列表失败")
        raise HTTPException(status_code=500, detail="读取数据库备份列表失败，请查看日志") from error
    return [_database_backup_artifact_data(artifact) for artifact in artifacts]


@router.post(
    "/database/backups",
    summary="立即创建数据库备份",
    response_model=_SchemaDatabaseBackupArtifactData,
)
async def create_database_backup(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaDatabaseBackupArtifactData:
    """创建、校验并原子发布当前活动数据库的一致快照。"""
    try:
        artifact = await run_in_threadpool_to_completion(get_database_governance().create_backup)
    except DatabaseBackupInProgressError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        logger.exception("创建数据库备份失败")
        raise HTTPException(status_code=500, detail="创建数据库备份失败，请查看日志") from error
    return _database_backup_artifact_data(artifact)


@router.post(
    "/database/backups/{name}/verify",
    summary="校验受管数据库备份",
    response_model=_SchemaDatabaseBackupVerificationData,
)
async def verify_database_backup(
    name: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaDatabaseBackupVerificationData:
    """校验一个受管制品，响应不包含宿主路径或适配器错误明细。"""
    try:
        verification = await run_in_threadpool_to_completion(
            get_database_governance().verify_backup,
            name,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="数据库备份文件名无效") from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="数据库备份不存在") from error
    except Exception as error:
        logger.exception("校验数据库备份失败：%s", name)
        raise HTTPException(status_code=500, detail="校验数据库备份失败，请查看日志") from error
    return _SchemaDatabaseBackupVerificationData(
        valid=verification.valid,
        method=verification.method,
    )


@router.delete(
    "/database/backups/{name}",
    summary="删除受管数据库备份",
    response_model=_SchemaResponse[None],
)
async def delete_database_backup(
    name: str,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> _SchemaResponse[None]:
    """删除一个受管制品，只接受备份目录内的合法文件名。"""
    try:
        await run_in_threadpool_to_completion(
            get_database_governance().delete_backup,
            name,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="数据库备份文件名无效") from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="数据库备份不存在") from error
    except Exception as error:
        logger.exception("删除数据库备份失败：%s", name)
        raise HTTPException(status_code=500, detail="删除数据库备份失败，请查看日志") from error
    return _SchemaResponse(success=True)


@router.get(
    "/env",
    summary="查询系统配置",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def get_env_setting(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse:
    """
    查询系统环境变量，包括当前版本号（仅管理员）
    """
    info = get_runtime_settings().snapshot(exclude={"SECRET_KEY", "RESOURCE_SECRET_KEY"})
    info.update(
        {
            "VERSION": get_app_version(),
            "AUTH_VERSION": SitesHelper().auth_version,
            "INDEXER_VERSION": SitesHelper().indexer_version,
            "FRONTEND_VERSION": get_frontend_version(),
            "PYTHON_FREE_THREADED": is_free_threaded_runtime(),
            "PYTHON_GIL_ENABLED": is_gil_enabled(),
        }
    )
    info.update(runtime.system.runtime_features())
    return _SchemaResponse(success=True, data=info)


@router.get(
    "/usage/statistic",
    summary="查询安装版本统计报表",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def usage_statistic(
    _: ApiPrincipal = Depends(get_current_active_user_async),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """
    查询安装版本统计报表
    """
    return _SchemaResponse(success=True, data=await runtime.system.usage())


@router.get("/ping", summary="服务存活检测", response_model=_SchemaResponse[None])
async def ping(_: ApiPrincipal = Depends(get_current_active_user_async)) -> _SchemaResponse:
    """
    检测服务是否可用
    """
    return _SchemaResponse(success=True)


@router.post(
    "/env",
    summary="更新系统配置",
    response_model=_SchemaResponse[_SchemaSystemEnvironmentUpdateData],
)
async def set_env_setting(
    env: dict,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """
    更新系统环境变量（仅管理员）
    """
    result = await runtime.system.update_environment(env)
    return _SchemaResponse(
        success=result.success,
        message=result.message,
        data=result.data,
    )


@router.get(
    "/progress/{process_type}",
    summary="实时进度",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "处理进度 SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def get_progress(
    request: Request,
    process_type: str,
    _: _SchemaTokenPayload = Depends(verify_resource_token),
):
    """
    实时获取处理进度，返回格式为SSE
    """
    progress = AsyncProgressHelper(process_type)
    locale = LocaleHelper.get_current_locale()

    async def event_generator():
        try:
            while not runtime_stop_state.is_system_stopped:
                if await request.is_disconnected():
                    break
                detail = await progress.get(locale=locale)
                yield f"data: {json.dumps(detail)}\n\n"
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get(
    "/setting/public/{key}",
    summary="查询公开系统设置",
    response_model=_SchemaResponse[_SchemaValueData],
)
async def get_public_setting(key: str, _: ApiPrincipal = Depends(get_current_active_user_async)) -> _SchemaResponse:
    """
    查询普通用户可读取的非敏感系统设置
    """
    if key in _PUBLIC_SETTINGS_KEYS:
        return _SchemaResponse(
            success=True,
            data={"value": get_runtime_settings().get(key)},
        )
    if key not in _PUBLIC_SYSTEM_CONFIG_KEYS:
        raise HTTPException(status_code=404, detail="配置项不存在")
    value = get_configured_system_config().get(_PUBLIC_SYSTEM_CONFIG_KEYS[key])
    return _SchemaResponse(success=True, data={"value": value})


@router.post(
    "/setting/PLUGIN_MARKET/sync-wiki",
    summary="从Wiki同步插件市场仓库",
    response_model=_SchemaResponse[_SchemaPluginMarketSyncData],
)
async def sync_plugin_market_from_wiki(
    request: Optional[_SchemaPluginMarketSyncRequest] = Body(default=None),
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse:
    """
    从 Wiki 插件文档同步插件市场仓库地址。
    """
    result = await runtime.system.sync_plugin_market(request.wiki_url if request else None)
    return _SchemaResponse(
        success=result.success,
        message=result.message,
        data=result.data,
    )


@router.get(
    "/setting/{key}",
    summary="查询系统设置",
    response_model=_SchemaResponse[_SchemaValueData],
)
async def get_setting(key: str, _: ApiPrincipal = Depends(get_current_active_superuser_async)) -> _SchemaResponse:
    """
    查询系统设置（仅管理员）
    """
    runtime_settings = get_runtime_settings()
    if runtime_settings.contains(key):
        value = runtime_settings.get(key)
    else:
        value = get_configured_system_config().get(key)
    return _SchemaResponse(success=True, data={"value": value})


@router.post("/setting/{key}", summary="更新系统设置", response_model=_SchemaResponse[None])
async def set_setting(
    key: str,
    value: Annotated[Union[list, dict, bool, int, str] | None, Body()] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """
    更新系统设置（仅管理员）
    """
    result = await runtime.system.update_setting(key, value)
    return _SchemaResponse(success=result.success, message=result.message)


@router.get(  # type: ignore[misc]
    "/settings",
    summary="Discover or read registered system settings",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def query_settings(
    setting_key: Annotated[
        Optional[str],
        Query(
            description=(
                "Exact setting key. Accepts Settings field names such as APP_DOMAIN or LLM_MODEL, "
                "SystemConfigKey values or enum names such as Downloaders or MediaServers, and "
                "aliases that resolve to one unique setting. Omit it to discover settings."
            )
        ),
    ] = None,
    group: Annotated[
        Optional[str],
        Query(
            description=(
                "Discovery group used when setting_key is omitted. Supported groups are all, settings, systemconfig, downloaders, "
                "media_servers, notifications, notification_switches, storages, directories, "
                "search_sites, subscribe_sites, site_auth, ai_agent, filter_rules, "
                "subscribe_defaults, plugins, customization, transfer, scraping, and misc."
            )
        ),
    ] = "all",
    keyword: Annotated[
        Optional[str],
        Query(description="Case-insensitive substring used to discover matching keys, groups, or labels."),
    ] = None,
    include_values: Annotated[
        Optional[bool],
        Query(description="Return full values. Defaults to true for one exact key and false for discovery results."),
    ] = None,
    show_secrets: Annotated[
        bool,
        Query(description="Return unredacted secret values. Defaults to false and remains confirmation-protected."),
    ] = False,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """按登记元数据查询设置，并默认对敏感值递归脱敏。"""
    try:
        data = SystemSettingsService(
            get_runtime_settings(),
            get_configured_system_config(),
            runtime.system.publish_config_changed,
        ).query(
            setting_key=setting_key,
            group=group,
            keyword=keyword,
            include_values=include_values,
            show_secrets=show_secrets,
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, data=data)


@router.post(  # type: ignore[misc]
    "/settings",
    summary="Update one registered system setting",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def update_settings(
    payload: _SchemaSystemSettingsUpdateRequest,
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> _SchemaResponse[Any]:
    """按替换、字典合并或列表项操作更新一个登记设置。"""
    try:
        data = await SystemSettingsService(
            get_runtime_settings(),
            get_configured_system_config(),
            runtime.system.publish_config_changed,
        ).update(**payload.model_dump())
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)


@router.get(
    "/message",
    summary="实时消息",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "系统消息 SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def get_message(
    request: Request,
    role: Optional[str] = "system",
    _: _SchemaTokenPayload = Depends(verify_resource_token),
):
    """
    实时获取系统消息，返回格式为SSE
    """
    message = MessageHelper()

    async def event_generator():
        try:
            while not runtime_stop_state.is_system_stopped:
                if await request.is_disconnected():
                    break
                detail = message.get(role)
                yield f"data: {detail or ''}\n\n"
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _get_logging_impl(
    request: Request,
    length: Optional[int] = 50,
    logfile: Optional[str] = "moviepilot.log",
    _: _SchemaTokenPayload = Depends(_verify_log_resource_superuser),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """把应用服务提供的日志数据映射为文本或 SSE 响应。"""
    resolved_logfile = logfile or "moviepilot.log"
    if length == -1:
        try:
            return Response(
                content=await runtime.system.read_log(resolved_logfile),
                media_type="text/plain",
            )
        except LogNotFoundError as error:
            raise HTTPException(status_code=404, detail="Not Found") from error
        except Exception as error:
            return Response(content=f"读取日志文件失败: {error}", media_type="text/plain")
    try:
        source = await runtime.system.follow_log(resolved_logfile, length or 50, request.is_disconnected)
    except LogNotFoundError as error:
        raise HTTPException(status_code=404, detail="Not Found") from error

    async def event_generator():
        """只为原始日志行添加 SSE data framing。"""
        try:
            async for line in source:
                yield f"data: {line}\n\n"
        except asyncio.CancelledError:
            return
        except Exception as error:
            logger.error("日志读取异常: %s", error)
            yield f"data: 日志读取异常: {error}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get(
    "/logging",
    summary="实时日志",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "实时日志流或完整日志文本",
            "content": {
                "text/event-stream": {"schema": {"type": "string"}},
                "text/plain": {"schema": {"type": "string"}},
            },
        }
    },
)
async def get_logging(
    request: Request,
    length: Optional[int] = 50,
    logfile: Optional[str] = "moviepilot.log",
    _: _SchemaTokenPayload = Depends(_verify_log_resource_superuser),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """实时日志的兼容公开入口。"""
    return await _get_logging_impl(request, length, logfile, _, runtime)


@router.get(
    "/logging/download/{name}",
    summary="下载日志",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "日志 ZIP 文件",
            "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def download_logging(
    name: str,
    _: _SchemaTokenPayload = Depends(_verify_log_resource_superuser),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """
    按日志标识下载主程序或插件滚动日志，返回 zip 文件。
    """
    return await _build_log_zip_response(name, runtime)


@router.get(
    "/versions",
    summary="查询Github所有Release版本",
    response_model=_SchemaResponse[_SchemaJsonObjectList],
)
async def latest_version(
    _: _SchemaTokenPayload = Depends(verify_token),
    runtime: HostRuntime = Depends(get_host_runtime),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
):
    """
    查询Github所有Release版本
    """
    releases = await runtime.system.releases()
    if releases:
        return _SchemaResponse(success=True, data=releases)
    return _SchemaResponse(success=False)


@router.get(
    "/ruletest",
    summary="过滤规则测试",
    response_model=_SchemaResponse[_SchemaRuleTestData],
)
def ruletest(
    title: str,
    rulegroup_name: str,
    subtitle: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
):
    """
    过滤规则测试，规则类型 1-订阅，2-洗版，3-搜索
    """
    metainfo = MetaInfo(title=title, subtitle=subtitle)
    torrent = _DomainTorrentInfo(
        title=title,
        description=subtitle or "",
    )
    # 查询规则组详情
    rulegroup = RuleHelper().get_rule_group(rulegroup_name)
    result_data = {
        "title": title,
        "subtitle": subtitle,
        "rulegroup_name": rulegroup_name,
        "rulegroup": rulegroup.model_dump() if rulegroup else None,
        "meta_info": metainfo.to_dict(),
        "media_info": None,
        "torrent_info": torrent.to_dict(),
        "priority": None,
        "matched": False,
    }
    if not rulegroup:
        return _SchemaResponse(
            success=False,
            message=f"过滤规则组 {rulegroup_name} 不存在！",
            data=result_data,
        )

    # 根据标题查询媒体信息
    media_info = MediaChain().recognize_by_meta(
        metainfo,
        obtain_images=False,
    )
    result_data["media_info"] = media_info.to_dict() if media_info else None
    if not media_info:
        return _SchemaResponse(
            success=False,
            message="未识别到媒体信息！",
            data=result_data,
        )

    # 过滤
    result = SearchChain().filter_torrents(
        rule_groups=[rulegroup.name or rulegroup_name],
        torrent_list=[torrent],
        mediainfo=media_info,
    )
    if not result:
        return _SchemaResponse(
            success=False,
            message="不符合过滤规则！",
            data=result_data,
        )
    result_data.update(
        {
            "matched": True,
            "priority": 100 - result[0].pri_order + 1,
            "torrent_info": result[0].to_dict(),
        }
    )
    return _SchemaResponse(
        success=True,
        data=result_data,
    )


@router.get(
    "/nettest/targets",
    summary="获取网络测试目标",
    response_model=_SchemaResponse[list[_SchemaNetTestTarget]],
)
async def nettest_targets(
    _: _SchemaTokenPayload = Depends(verify_token), page: CompatiblePageParam = None, count: CompatibleCountParam = None
):
    """
    获取网络测试目标。

    这里只返回前端渲染所需的最小信息，避免把可请求 URL、内容校验规则和
    跳转白名单暴露给客户端。
    """
    targets = get_configured_network_test_service().list_targets()
    return _SchemaResponse(
        success=True,
        data=[{"id": item.id, "name": item.name, "icon": item.icon} for item in targets],
    )


@router.get(
    "/nettest",
    summary="测试网络连通性",
    response_model=_SchemaResponse[_SchemaTimeData],
)
async def nettest(
    target_id: Optional[str] = None,
    url: Optional[str] = None,
    include: Optional[str] = None,
    _: _SchemaTokenPayload = Depends(verify_token),
):
    """
    测试内置目标的网络连通性。

    `target_id` 是当前前端使用的正式入口。`url/proxy/include` 仅作兼容保留，
    其中 `include` 不再参与客户端可控的内容匹配，具体校验由服务端规则决定。
    """
    result = await get_configured_network_test_service().execute(
        target_id=target_id,
        url=url,
        include=include,
    )
    data = {"time": result.elapsed_ms} if result.elapsed_ms is not None else None
    return _SchemaResponse(
        success=result.success,
        message=result.message,
        data=data,
    )


@router.get(
    "/modulelist",
    summary="查询已加载的模块ID列表",
    response_model=_SchemaResponse[_SchemaSystemModuleListData],
)
def modulelist(_: _SchemaTokenPayload = Depends(verify_token)):
    """
    查询已加载的模块ID列表
    """
    modules = []
    for spec in get_module_manager().list_specs():
        module_id = spec.id
        name = str(spec.metadata["name"])
        modules.append(
            {
                "id": module_id,
                "name": name,
                "name_i18n": LocaleHelper.translate(
                    f"system.modules.{module_id}.name",
                    default=name,
                ),
                "name_key": f"system.modules.{module_id}.name",
            }
        )
    return _SchemaResponse(success=True, data={"modules": modules})


@router.get("/moduletest/{moduleid}", summary="模块可用性测试", response_model=_SchemaResponse[None])
def moduletest(moduleid: str, _: _SchemaTokenPayload = Depends(verify_token)):
    """
    模块可用性测试接口
    """
    state, errmsg = get_module_manager().test(moduleid)
    return _SchemaResponse(success=state, message=errmsg)


@router.get("/restart", summary="重启系统", response_model=_SchemaResponse[None])
def restart_system(
    _: ApiPrincipal = Depends(get_current_active_superuser),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """
    重启系统（仅管理员）
    """
    result = runtime.system.restart()
    return _SchemaResponse(success=result.success, message=result.message)


@router.post("/upgrade", summary="Dev 更新并重启系统", response_model=_SchemaResponse[None])
def upgrade_system(
    mode: Annotated[str | None, Body()] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """保留 Dev 更新入口；Release 更新必须使用后台下载与确认安装流程。"""
    result = runtime.system.upgrade(mode)
    return _SchemaResponse(success=result.success, message=result.message)


@router.get(
    "/update/status",
    summary="查询系统更新状态",
    response_model=_SchemaResponse[_SchemaSystemUpdateStatus],
)
def system_update_status(
    _: ApiPrincipal = Depends(get_current_active_superuser),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """返回后台检查、下载或待安装状态（仅管理员）。"""
    return _SchemaResponse(success=True, data=runtime.system.update_status())


@router.post(
    "/update/check",
    summary="立即检查系统更新",
    response_model=_SchemaResponse[_SchemaSystemUpdateStatus],
)
def check_system_update(
    _: ApiPrincipal = Depends(get_current_active_superuser),
    runtime: HostRuntime = Depends(get_host_runtime),
):
    """立即查询 GitHub Release（仅管理员）。"""
    return _SchemaResponse(success=True, data=runtime.system.check_update())


@router.post(
    "/update/download",
    summary="后台下载系统更新",
    response_model=_SchemaResponse[_SchemaSystemUpdateStatus],
)
def download_system_update(
    _: ApiPrincipal = Depends(get_current_active_superuser),
    runtime: HostRuntime = Depends(get_host_runtime),
    request: Annotated[_SchemaSystemUpdateRequest | None, Body()] = None,
):
    """启动指定类型的后台下载并立即返回当前状态（仅管理员）。"""
    result = runtime.system.download_update(request.target if request else "application")
    return _SchemaResponse(success=result.success, data=result.data, message=result.message)


@router.post(
    "/update/install",
    summary="确认重启安装系统更新",
    response_model=_SchemaResponse[None],
)
def install_system_update(
    _: ApiPrincipal = Depends(get_current_active_superuser),
    runtime: HostRuntime = Depends(get_host_runtime),
    request: Annotated[_SchemaSystemUpdateRequest | None, Body()] = None,
):
    """确认消费指定类型的已校验制品，并重启进入安装阶段（仅管理员）。"""
    result = runtime.system.install_update(request.target if request else "application")
    return _SchemaResponse(success=result.success, message=result.message)


@router.get("/runscheduler", summary="运行服务", response_model=_SchemaResponse[None])
def run_scheduler(jobid: str, _: ApiPrincipal = Depends(get_current_active_superuser)):
    """
    执行命令（仅管理员）
    """
    if not jobid:
        return _SchemaResponse(success=False, message="命令不能为空！")
    if jobid in {"recommend_refresh", "cookiecloud"}:
        get_scheduler().start(jobid, manual=True)
    else:
        get_scheduler().start(jobid)
    return _SchemaResponse(success=True)


@router.get("/runscheduler2", summary="运行服务（API_TOKEN）", response_model=_SchemaResponse[None])
def run_scheduler2(jobid: str, _: Annotated[str, Depends(verify_apitoken)]):
    """
    执行命令（API_TOKEN认证）
    """
    if not jobid:
        return _SchemaResponse(success=False, message="命令不能为空！")

    if jobid in {"recommend_refresh", "cookiecloud"}:
        get_scheduler().start(jobid, manual=True)
    else:
        get_scheduler().start(jobid)
    return _SchemaResponse(success=True)
