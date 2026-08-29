import asyncio
import io
import json
import re
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Optional, Union
from urllib.parse import urlparse

import aiofiles
import anyio
import pillow_avif  # noqa: F401  # pylint: disable=unused-import  # AVIF 注册副作用
from anyio import Path as AsyncPath
from fastapi import Body, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.adapters.external.plugin.client import (
    PLUGIN_MARKET_WIKI_URL,
    extract_plugin_market_repos_from_wiki,
    merge_plugin_market_repos,
    split_plugin_market_repo_urls,
)
from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.adapters.system import rust as rust_accel
from app.adapters.system.update import system_update_manager
from app.adapters.web.security.access import verify_apitoken, verify_resource_token, verify_token
from app.api.context import get_host_runtime
from app.api.dependencies.auth import (
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user_async,
)
from app.api.principal import ApiPrincipal
from app.api.response import ResponseAPIRouter
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
from app.application.plugin.runtime import plugin_system_config_mutation
from app.application.rules import RuleHelper
from app.application.scheduling import get_scheduler
from app.application.security.url import SecurityUtils
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.chain.media import MediaChain
from app.chain.mediaserver import MediaServerChain
from app.chain.search import SearchChain  # pylint: disable=no-name-in-module
from app.domain.metainfo import MetaInfo
from app.foundation.crypto import HashUtils
from app.foundation.environment import is_free_threaded_runtime, is_gil_enabled
from app.runtime.events import eventmanager
from app.runtime.execution import run_in_threadpool_to_completion
from app.runtime.localization import LocaleHelper
from app.runtime.log import logger
from app.runtime.progress import AsyncProgressHelper
from app.runtime.scheduling import TimerUtils
from app.runtime.state import SystemHelper
from app.runtime.stop import runtime_stop_state
from app.runtime.version import get_app_version, get_frontend_version
from app.schemas.common import JsonData
from app.schemas.common import JsonObject as _SchemaJsonObject
from app.schemas.common import JsonObjectList as _SchemaJsonObjectList
from app.schemas.common import TimeData as _SchemaTimeData
from app.schemas.common import ValueData as _SchemaValueData
from app.schemas.event import ConfigChangeEventData
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.response import Response as _SchemaResponse
from app.schemas.system import DatabaseBackupArtifactData as _SchemaDatabaseBackupArtifactData
from app.schemas.system import DatabaseBackupVerificationData as _SchemaDatabaseBackupVerificationData
from app.schemas.system import NetTestTarget as _SchemaNetTestTarget
from app.schemas.system import PluginMarketSyncData as _SchemaPluginMarketSyncData
from app.schemas.system import PluginMarketSyncRequest as _SchemaPluginMarketSyncRequest
from app.schemas.system import RuleTestData as _SchemaRuleTestData
from app.schemas.system import SystemEnvironmentUpdateData as _SchemaSystemEnvironmentUpdateData
from app.schemas.system import SystemModuleListData as _SchemaSystemModuleListData
from app.schemas.system import SystemUpdateStatus as _SchemaSystemUpdateStatus
from app.schemas.system import TorrentInfo as _SchemaTorrentInfo
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.schemas.types import EventType, SystemConfigKey
from app.startup.composition.context import HostRuntime

router = ResponseAPIRouter()

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
_LOG_DOWNLOAD_LIMIT = 10
_LOG_DOWNLOAD_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_DATABASE_BACKUP_SETTING_KEYS = {
    "DB_BACKUP_ENABLE",
    "DB_BACKUP_CRON",
    "DB_BACKUP_ON_UPGRADE",
    "DB_BACKUP_PATH",
    "DB_BACKUP_RETENTION_DAYS",
    "DB_BACKUP_MAX_COUNT",
}


def _database_backup_artifact_data(artifact: Any) -> _SchemaDatabaseBackupArtifactData:
    """将内部备份制品映射为不含宿主路径的 Web DTO。"""
    return _SchemaDatabaseBackupArtifactData(
        name=artifact.name,
        db_type=artifact.db_type,
        created_at=artifact.created_at,
        size=artifact.size,
    )


def _validate_llm_server_tool_config(env: dict) -> Optional[str]:
    """校验强制服务端联网搜索配置，返回用户可读错误信息。"""
    from app.agent.llm.server_tools import (
        ServerToolRegistry,
        ServerToolUnavailableError,
    )

    runtime_settings = get_runtime_settings()
    mode = ServerToolRegistry.normalize_web_search_mode(
        env.get(
            "LLM_WEB_SEARCH_MODE",
            runtime_settings.get("LLM_WEB_SEARCH_MODE", "local"),
        )
    )
    if mode != "builtin":
        return None

    provider = str(
        env.get("LLM_PROVIDER", runtime_settings.get("LLM_PROVIDER", "")) or ""
    ).strip()
    model = str(
        env.get("LLM_MODEL", runtime_settings.get("LLM_MODEL", "")) or ""
    ).strip()
    base_url = env.get("LLM_BASE_URL", runtime_settings.get("LLM_BASE_URL"))
    capability = ServerToolRegistry.get_capability(
        provider=provider,
        model=model,
        base_url=str(base_url or "").strip() or None,
        tool_id="web_search",
    )
    if capability:
        return None

    return str(
        ServerToolUnavailableError(
            provider=provider,
            model=model,
            tool_id="web_search",
        )
    )


def _validate_database_backup_config(env: dict) -> Optional[str]:
    """在批量写入前校验数据库备份策略，避免只保存部分字段。"""
    if not _DATABASE_BACKUP_SETTING_KEYS.intersection(env):
        return None

    runtime_settings = get_runtime_settings()
    cron = str(env.get("DB_BACKUP_CRON", runtime_settings.get("DB_BACKUP_CRON")) or "").strip()
    if cron:
        try:
            TimerUtils.normalize_schedule_trigger(
                "cron",
                cron,
                runtime_settings.get("TZ"),
            )
        except (TypeError, ValueError):
            return "数据库备份周期格式不正确"

    backup_path = env.get("DB_BACKUP_PATH", runtime_settings.get("DB_BACKUP_PATH"))
    if backup_path is not None and not isinstance(backup_path, str):
        return "数据库备份目录必须是路径字符串"

    for key, label in (
        ("DB_BACKUP_RETENTION_DAYS", "数据库备份过期天数"),
        ("DB_BACKUP_MAX_COUNT", "数据库备份最大保留份数"),
    ):
        value = env.get(key, runtime_settings.get(key))
        if isinstance(value, bool):
            return f"{label}必须是大于等于 0 的整数"
        try:
            converted = int(value)
        except (TypeError, ValueError):
            return f"{label}必须是大于等于 0 的整数"
        if converted < 0 or str(value).strip() != str(converted):
            return f"{label}必须是大于等于 0 的整数"
    return None


def _is_allowed_plugin_market_wiki_url(wiki_url: str) -> bool:
    """
    校验插件市场 Wiki 地址是否属于固定文档源。
    """
    parsed_url = urlparse(wiki_url)
    if parsed_url.scheme != "https":
        return False
    if (parsed_url.hostname or "").lower() != "raw.githubusercontent.com":
        return False
    return bool(
        re.fullmatch(
            r"/jxxghp/MoviePilot-Wiki/[^/]+/plugin\.md",
            parsed_url.path,
        )
    )




def _collect_named_log_files(name: str) -> list[Path]:
    """
    根据前端传入的日志标识收集可下载日志文件。

    `moviepilot` 固定表示主程序日志，其余标识按插件 ID 处理并映射到
    `plugins/<plugin_id>.log*`。这里不接收路径或后缀，避免下载入口变成任意
    日志文件选择器；滚动日志按当前文件优先、备份文件按修改时间倒序补足。
    """
    normalized_name = (name or "").strip().lower()
    if not normalized_name or not _LOG_DOWNLOAD_NAME_PATTERN.fullmatch(normalized_name):
        raise HTTPException(status_code=404, detail="Not Found")

    log_root = Path(get_runtime_settings().get("LOG_PATH"))
    if normalized_name == "moviepilot":
        log_dir = log_root
        log_prefix = "moviepilot.log"
    else:
        log_dir = log_root / "plugins"
        log_prefix = f"{normalized_name}.log"

    if not log_dir.exists() or not log_dir.is_dir():
        raise HTTPException(status_code=404, detail="Not Found")

    current_log = log_dir / log_prefix
    backup_logs = [
        item
        for item in log_dir.iterdir()
        if item.is_file() and item.name.startswith(f"{log_prefix}.")
    ]
    backup_logs.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    log_files = []
    if current_log.exists() and current_log.is_file():
        log_files.append(current_log)
    log_files.extend(backup_logs)
    return log_files[:_LOG_DOWNLOAD_LIMIT]


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


async def _build_log_zip_response(name: str) -> StreamingResponse:
    """
    将指定日志标识对应的日志文件打包为 zip 响应。

    打包前逐个校验文件仍位于日志根目录内，避免符号链接或并发文件变更绕过
    `name` 到固定目录的映射约束。zip 内使用日志根目录相对路径，便于区分
    主程序日志与插件日志。
    """
    zip_data, zip_stem = await anyio.to_thread.run_sync(_build_log_zip_data, name)
    headers = {
        "Content-Disposition": f'attachment; filename="{zip_stem}.zip"'
    }
    return StreamingResponse(
        iter([zip_data]),
        media_type="application/zip",
        headers=headers,
    )


def _build_log_zip_data(name: str) -> tuple[bytes, str]:
    """
    同步生成日志 zip 内容和文件名前缀。

    日志收集、路径解析、文件读取和压缩都属于可能阻塞的本地 I/O；调用方需要
    将本函数放到 worker thread 中执行，避免日志下载占用 ASGI 事件循环。
    """
    log_files = _collect_named_log_files(name)
    if not log_files:
        raise HTTPException(status_code=404, detail="Not Found")

    log_root = Path(get_runtime_settings().get("LOG_PATH"))
    zip_buffer = io.BytesIO()
    filename_time = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = (name or "logs").strip().lower() or "logs"
    zip_stem = f"{safe_name}-logs-{filename_time}"
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for log_file in log_files:
            if not SecurityUtils.is_safe_path(
                base_path=log_root,
                user_path=log_file,
            ):
                raise HTTPException(status_code=404, detail="Not Found")
            arcname = f"{zip_stem}/{log_file.name}"
            archive.write(log_file, arcname)

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
        headers = RequestUtils.generate_cache_headers(etag, max_age=86400 * 7)
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
    cookies = (
        MediaServerChain().get_image_cookies(server=None, image_url=imgurl)
        if use_cookies
        else None
    )
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
            "ADVANCED_MODE",
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
async def get_user_global_setting(_: ApiPrincipal = Depends(get_current_active_user_async)):
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
    share_admin = await MoviePilotServerHelper.async_is_admin_user()
    info.update(
        {
            "USER_UNIQUE_ID": MoviePilotServerHelper.get_user_uuid(),
            "SUBSCRIBE_SHARE_MANAGE": share_admin,
            "WORKFLOW_SHARE_MANAGE": share_admin,
            "PYTHON_FREE_THREADED": is_free_threaded_runtime(),
            "PYTHON_GIL_ENABLED": is_gil_enabled(),
        }
    )
    return _SchemaResponse(success=True, data=info)


@router.get(
    "/database/backups",
    summary="查询受管数据库备份",
    response_model=list[_SchemaDatabaseBackupArtifactData],
)
async def list_database_backups(
    _: ApiPrincipal = Depends(get_current_active_superuser_async),
) -> list[_SchemaDatabaseBackupArtifactData]:
    """列出当前备份目录中的正式制品，不触发内容校验。"""
    try:
        artifacts = await run_in_threadpool_to_completion(
            get_database_governance().list_backups
        )
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
        artifact = await run_in_threadpool_to_completion(
            get_database_governance().create_backup
        )
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
) -> _SchemaResponse:
    """
    查询系统环境变量，包括当前版本号（仅管理员）
    """
    info = get_runtime_settings().snapshot(
        exclude={"SECRET_KEY", "RESOURCE_SECRET_KEY"}
    )
    info.update(
        {
            "VERSION": get_app_version(),
            "AUTH_VERSION": SitesHelper().auth_version,
            "INDEXER_VERSION": SitesHelper().indexer_version,
            "FRONTEND_VERSION": get_frontend_version(),
            "RUST_ACCEL": rust_accel.is_config_enabled(),
            "RUST_ACCEL_AVAILABLE": rust_accel.is_available(),
            "RUST_ACCEL_ENABLED": rust_accel.is_enabled(),
            "RUST_ACCEL_REQUIRED": rust_accel.is_required(),
            "PYTHON_FREE_THREADED": is_free_threaded_runtime(),
            "PYTHON_GIL_ENABLED": is_gil_enabled(),
        }
    )
    return _SchemaResponse(success=True, data=info)


@router.get(
    "/usage/statistic",
    summary="查询安装版本统计报表",
    response_model=_SchemaResponse[_SchemaJsonObject],
)
async def usage_statistic(_: ApiPrincipal = Depends(get_current_active_user_async)):
    """
    查询安装版本统计报表
    """
    return _SchemaResponse(success=True, data=await MoviePilotServerHelper.async_get_usage_statistic())


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
    env: dict, _: ApiPrincipal = Depends(get_current_active_superuser_async)
):
    """
    更新系统环境变量（仅管理员）
    """
    validation_error = _validate_llm_server_tool_config(env)
    if validation_error:
        return _SchemaResponse(success=False, message=validation_error)
    validation_error = _validate_database_backup_config(env)
    if validation_error:
        return _SchemaResponse(success=False, message=validation_error)

    result = get_runtime_settings().update_many(env)
    # 统计成功和失败的结果
    success_updates = {k: v for k, v in result.items() if v[0]}
    failed_updates = {k: v for k, v in result.items() if v[0] is False}

    if failed_updates:
        return _SchemaResponse(
            success=False,
            message=f"{', '.join([v[1] for v in failed_updates.values()])}",
            data={"success_updates": success_updates, "failed_updates": failed_updates},
        )

    if success_updates:
        # 发送配置变更事件
        await eventmanager.async_send_event(
            etype=EventType.ConfigChanged,
            data=ConfigChangeEventData(
                key=success_updates.keys(), change_type="update"
            ),
        )

    return _SchemaResponse(
        success=True,
        message="所有配置项更新成功",
        data={"success_updates": success_updates},
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
async def get_public_setting(
    key: str, _: ApiPrincipal = Depends(get_current_active_user_async)
) -> _SchemaResponse:
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
) -> _SchemaResponse:
    """
    从 Wiki 插件文档同步插件市场仓库地址。
    """
    wiki_url = (request.wiki_url if request else None) or PLUGIN_MARKET_WIKI_URL
    wiki_url = wiki_url.strip()
    if not _is_allowed_plugin_market_wiki_url(wiki_url):
        return _SchemaResponse(success=False, message="不支持的 Wiki 同步地址")

    res = await AsyncRequestUtils(
        ua=get_runtime_settings().get("USER_AGENT"),
        proxies=get_runtime_settings().get("PROXY"),
        timeout=30,
        content_type=None,
        accept_type="text/plain,*/*",
    ).get_res(wiki_url)
    if res is None:
        return _SchemaResponse(success=False, message="无法访问 Wiki 插件仓库清单")
    if res.status_code != 200:
        return _SchemaResponse(
            success=False,
            message=f"访问 Wiki 插件仓库清单失败，状态码：{res.status_code}",
        )

    wiki_repos = extract_plugin_market_repos_from_wiki(res.text)
    if not wiki_repos:
        return _SchemaResponse(success=False, message="未在 Wiki 中识别到插件仓库地址")

    local_repos = split_plugin_market_repo_urls(
        get_runtime_settings().get("PLUGIN_MARKET", "")
    )
    local_repo_keys = {repo.lower() for repo in local_repos}
    added_count = len([repo for repo in wiki_repos if repo.lower() not in local_repo_keys])
    merged_repos = merge_plugin_market_repos(local_repos, wiki_repos)
    merged_value = ",".join(merged_repos)

    success, message = get_runtime_settings().update("PLUGIN_MARKET", merged_value)
    if success:
        await eventmanager.async_send_event(
            etype=EventType.ConfigChanged,
            data=ConfigChangeEventData(
                key="PLUGIN_MARKET", value=merged_value, change_type="update"
            ),
        )
    elif success is None:
        success = True

    return _SchemaResponse(
        success=success,
        message=message,
        data={
            "value": merged_value,
            "repos": merged_repos,
            "wiki_repos": wiki_repos,
            "added_count": added_count,
            "total_count": len(merged_repos),
            "source_url": wiki_url,
        },
    )


@router.get(
    "/setting/{key}",
    summary="查询系统设置",
    response_model=_SchemaResponse[_SchemaValueData],
)
async def get_setting(
    key: str, _: ApiPrincipal = Depends(get_current_active_superuser_async)
) -> _SchemaResponse:
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
    runtime_settings = get_runtime_settings()
    if runtime_settings.contains(key):
        success, message = runtime_settings.update(key, value)
        if success:
            # 发送配置变更事件
            await eventmanager.async_send_event(
                etype=EventType.ConfigChanged,
                data=ConfigChangeEventData(key=key, value=value, change_type="update"),
            )
        elif success is None:
            success = True
        return _SchemaResponse(success=success, message=message)
    elif key in {item.value for item in SystemConfigKey}:
        if isinstance(value, list):
            value = list(filter(None, value))
            value = value if value else None
        try:
            with plugin_system_config_mutation(key):
                event_value: JsonData
                if key == SystemConfigKey.UserFilterRuleGroups.value:
                    current_value = get_configured_system_config().get(
                        SystemConfigKey.UserFilterRuleGroups
                    )
                    expected_definitions = [
                        dict(item)
                        for item in current_value or []
                        if isinstance(item, dict)
                    ] if isinstance(current_value, list) else []
                    definitions = [
                        dict(item) for item in value or [] if isinstance(item, dict)
                    ] if isinstance(value, list) else []
                    async with (
                        runtime.subscription.async_rule_group_mutation_scope()
                    ) as mutation:
                        await mutation.apply(
                            definitions,
                            expected_rule_groups=expected_definitions,
                        )
                    event_value = definitions
                    success = True
                else:
                    success = await get_configured_system_config().async_set(key, value)
                    event_value = value
                if success:
                    # 发送配置变更事件
                    await eventmanager.async_send_event(
                        etype=EventType.ConfigChanged,
                        data=ConfigChangeEventData(
                            key=key,
                            value=event_value,
                            change_type="update",
                        ),
                    )
                return _SchemaResponse(success=True)
        except PluginMutationRejectedError as error:
            return _SchemaResponse(success=False, message=str(error))
    else:
        return _SchemaResponse(success=False, message=f"配置项 '{key}' 不存在")


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
):
    """
    实时获取系统日志
    length = -1 时, 返回text/plain
    否则 返回格式SSE
    """
    base_path = AsyncPath(get_runtime_settings().get("LOG_PATH"))
    log_path = base_path / logfile

    if not await SecurityUtils.async_is_safe_path(
        base_path=base_path, user_path=log_path, allowed_suffixes={".log"}
    ):
        raise HTTPException(status_code=404, detail="Not Found")

    if not await log_path.exists() or not await log_path.is_file():
        raise HTTPException(status_code=404, detail="Not Found")

    async def log_generator():
        try:
            # 使用固定大小的双向队列来限制内存使用
            lines_queue = deque(maxlen=max(length, 50))
            # 获取文件大小
            file_stat = await log_path.stat()
            file_size = file_stat.st_size

            # 读取历史日志
            async with aiofiles.open(
                log_path, mode="r", encoding="utf-8", errors="replace"
            ) as f:
                # 优化大文件读取策略
                if file_size > 100 * 1024:
                    # 只读取最后100KB的内容
                    bytes_to_read = min(file_size, 100 * 1024)
                    position = file_size - bytes_to_read
                    await f.seek(position)
                    content = await f.read()
                    # 找到第一个完整的行
                    first_newline = content.find("\n")
                    if first_newline != -1:
                        content = content[first_newline + 1 :]
                else:
                    # 小文件直接读取全部内容
                    content = await f.read()

                # 按行分割并添加到队列，只保留非空行
                lines = [line.strip() for line in content.splitlines() if line.strip()]
                # 只取最后N行
                for line in lines[-max(length, 50) :]:
                    lines_queue.append(line)

            # 输出历史日志
            for line in lines_queue:
                yield f"data: {line}\n\n"

            # 实时监听新日志
            async with aiofiles.open(
                log_path, mode="r", encoding="utf-8", errors="replace"
            ) as f:
                # 移动文件指针到文件末尾，继续监听新增内容
                await f.seek(0, 2)
                # 记录初始文件大小
                initial_stat = await log_path.stat()
                initial_size = initial_stat.st_size
                # 实时监听新日志，使用更短的轮询间隔
                while not runtime_stop_state.is_system_stopped:
                    if await request.is_disconnected():
                        break
                    # 检查文件是否有新内容
                    current_stat = await log_path.stat()
                    current_size = current_stat.st_size
                    if current_size > initial_size:
                        # 文件有新内容，读取新行
                        line = await f.readline()
                        if line:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                        initial_size = current_size
                    else:
                        # 没有新内容，短暂等待
                        await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception as err:
            logger.error(f"日志读取异常: {err}")
            yield f"data: 日志读取异常: {err}\n\n"

    # 根据length参数返回不同的响应
    if length == -1:
        # 返回全部日志作为文本响应
        if not await log_path.exists():
            return Response(content="日志文件不存在！", media_type="text/plain")
        try:
            # 使用 aiofiles 异步读取文件
            async with aiofiles.open(
                log_path, mode="r", encoding="utf-8", errors="replace"
            ) as file:
                text = await file.read()
            # 倒序输出
            text = "\n".join(text.split("\n")[::-1])
            return Response(content=text, media_type="text/plain")
        except Exception as e:
            return Response(content=f"读取日志文件失败: {e}", media_type="text/plain")
    else:
        # 返回SSE流响应
        return StreamingResponse(log_generator(), media_type="text/event-stream")


@router.get(
    "/logging",
    summary="实时日志",
    response_model=None,
    response_class=StreamingResponse,
    responses={200: {"description": "实时日志流或完整日志文本", "content": {"text/event-stream": {"schema": {"type": "string"}}, "text/plain": {"schema": {"type": "string"}}}}},
)
async def get_logging(
    request: Request,
    length: Optional[int] = 50,
    logfile: Optional[str] = "moviepilot.log",
    _: _SchemaTokenPayload = Depends(_verify_log_resource_superuser),
):
    """实时日志的兼容公开入口。"""
    return await _get_logging_impl(request, length, logfile, _)


@router.get(
    "/logging/download/{name}",
    summary="下载日志",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "日志 ZIP 文件",
            "content": {
                "application/zip": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def download_logging(
    name: str,
    _: _SchemaTokenPayload = Depends(_verify_log_resource_superuser),
):
    """
    按日志标识下载主程序或插件滚动日志，返回 zip 文件。
    """
    return await _build_log_zip_response(name)


@router.get(
    "/versions",
    summary="查询Github所有Release版本",
    response_model=_SchemaResponse[_SchemaJsonObjectList],
)
async def latest_version(_: _SchemaTokenPayload = Depends(verify_token)):
    """
    查询Github所有Release版本
    """
    version_res = await AsyncRequestUtils(
        proxies=get_runtime_settings().get("PROXY"),
        headers=get_runtime_settings().get("GITHUB_HEADERS"),
    ).get_res("https://api.github.com/repos/jxxghp/MoviePilot/releases")
    if version_res is not None and version_res.status_code == 200:
        ver_json = version_res.json()
        if ver_json:
            return _SchemaResponse(success=True, data=ver_json)
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
    torrent = _SchemaTorrentInfo(
        title=title,
        description=subtitle,
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
        "torrent_info": torrent.model_dump(),
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
        rule_groups=[rulegroup.name], torrent_list=[torrent], mediainfo=media_info
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
            "torrent_info": result[0].model_dump(),
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
async def nettest_targets(_: _SchemaTokenPayload = Depends(verify_token)):
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


@router.get(
    "/moduletest/{moduleid}", summary="模块可用性测试", response_model=_SchemaResponse[None]
)
def moduletest(moduleid: str, _: _SchemaTokenPayload = Depends(verify_token)):
    """
    模块可用性测试接口
    """
    state, errmsg = get_module_manager().test(moduleid)
    return _SchemaResponse(success=state, message=errmsg)


@router.get("/restart", summary="重启系统", response_model=_SchemaResponse[None])
def restart_system(_: ApiPrincipal = Depends(get_current_active_superuser)):
    """
    重启系统（仅管理员）
    """
    if not SystemHelper.can_restart():
        return _SchemaResponse(success=False, message="当前运行环境不支持重启操作！")
    ret, msg = SystemHelper.restart()
    return _SchemaResponse(success=ret, message=msg)


@router.post("/upgrade", summary="Dev 更新并重启系统", response_model=_SchemaResponse[None])
def upgrade_system(
    mode: Annotated[str | None, Body()] = None,
    _: ApiPrincipal = Depends(get_current_active_superuser),
):
    """保留 Dev 更新入口；Release 更新必须使用后台下载与确认安装流程。"""
    if str(mode or "").strip().lower() != "dev":
        return _SchemaResponse(
            success=False,
            message="Release 更新请使用 /system/update/check、download 和 install 接口",
        )
    if not SystemHelper.can_restart():
        return _SchemaResponse(success=False, message="当前运行环境不支持升级操作！")
    success, message = SystemHelper.upgrade_dev()
    return _SchemaResponse(success=success, message=message)


@router.get(
    "/update/status",
    summary="查询系统更新状态",
    response_model=_SchemaResponse[_SchemaSystemUpdateStatus],
)
def system_update_status(
    _: ApiPrincipal = Depends(get_current_active_superuser),
):
    """返回后台检查、下载或待安装状态（仅管理员）。"""
    return _SchemaResponse(success=True, data=system_update_manager.get_status())


@router.post(
    "/update/check",
    summary="立即检查系统更新",
    response_model=_SchemaResponse[_SchemaSystemUpdateStatus],
)
def check_system_update(
    _: ApiPrincipal = Depends(get_current_active_superuser),
):
    """立即查询 GitHub Release（仅管理员）。"""
    return _SchemaResponse(success=True, data=system_update_manager.check())


@router.post(
    "/update/download",
    summary="后台下载系统更新",
    response_model=_SchemaResponse[_SchemaSystemUpdateStatus],
)
def download_system_update(
    _: ApiPrincipal = Depends(get_current_active_superuser),
):
    """启动后台下载并立即返回当前状态（仅管理员）。"""
    if not SystemHelper.can_restart():
        return _SchemaResponse(success=False, message="当前运行环境不支持升级操作！")
    status = system_update_manager.start_download()
    return _SchemaResponse(success=status.state != "failed", data=status, message=status.error)


@router.post(
    "/update/install",
    summary="确认重启安装系统更新",
    response_model=_SchemaResponse[None],
)
def install_system_update(
    _: ApiPrincipal = Depends(get_current_active_superuser),
):
    """确认消费已校验更新包，并重启进入安装阶段（仅管理员）。"""
    if not SystemHelper.can_restart():
        return _SchemaResponse(success=False, message="当前运行环境不支持升级操作！")
    prepared, message = system_update_manager.request_install()
    if not prepared:
        return _SchemaResponse(success=False, message=message)
    ret, msg = SystemHelper.restart()
    if not ret:
        system_update_manager.cancel_install(msg)
        return _SchemaResponse(success=False, message=msg)
    return _SchemaResponse(success=True, message=message)


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


@router.get(
    "/runscheduler2", summary="运行服务（API_TOKEN）", response_model=_SchemaResponse[None]
)
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
