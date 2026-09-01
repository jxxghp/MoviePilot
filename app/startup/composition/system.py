"""System API 外部能力的唯一启动组合根。"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import aiofiles  # type: ignore[import-untyped]
from anyio import Path as AsyncPath

from app.adapters.external.plugin.client import extract_plugin_market_repos_from_wiki
from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.network.http import AsyncRequestUtils
from app.adapters.system import rust as rust_accel
from app.adapters.system.update import system_update_manager
from app.application.configuration import RuntimeSettingsService, SystemConfigService
from app.application.plugin.runtime import plugin_system_config_mutation
from app.application.security.url import SecurityUtils
from app.application.system import (
    ConfigurationEventPort,
    LlmCapabilityPort,
    LogFileData,
    LogNotFoundError,
    MarketFetchResult,
    PluginMarketPort,
    ReleaseCatalogPort,
    RuntimeFeaturePort,
    ServerInfoPort,
    SystemControlPort,
    SystemLogPort,
    SystemService,
    SystemUpdatePort,
)
from app.runtime.events import eventmanager
from app.runtime.state import SystemHelper
from app.runtime.stop import runtime_stop_state
from app.schemas.event import ConfigChangeEventData
from app.schemas.system import SystemUpdateStatus, SystemUpdateType
from app.schemas.types import EventType

_LOG_DOWNLOAD_LIMIT = 10
_LOG_DOWNLOAD_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class _FileLogAdapter(SystemLogPort):
    """以受限日志根实现文件读取、尾随和归档数据源。"""

    def __init__(self, settings: RuntimeSettingsService) -> None:
        """保存日志根设置提供器。"""
        self._settings = settings

    async def read(self, logfile: str) -> str:
        """校验路径后异步读取完整日志文本。"""
        path = await self._resolve_logfile(logfile)
        async with aiofiles.open(path, mode="r", encoding="utf-8", errors="replace") as file:
            return cast(str, await file.read())

    async def follow(
        self,
        logfile: str,
        length: int,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[str]:
        """先返回历史尾部，再轮询并返回新增日志行。"""
        path = await self._resolve_logfile(logfile)
        return self._follow_path(path, length, disconnected)

    async def collect(self, name: str) -> list[LogFileData]:
        """在线程中发现并读取允许归档的日志文件。"""
        return await asyncio.to_thread(self._collect_sync, name)

    async def _resolve_logfile(self, logfile: str) -> Path:
        """把相对日志名解析为日志根内的现存 .log 文件。"""
        root = Path(self._settings.get("LOG_PATH"))
        path = root / str(logfile or "")
        safe = await SecurityUtils.async_is_safe_path(
            base_path=AsyncPath(root),
            user_path=AsyncPath(path),
            allowed_suffixes={".log"},
        )
        if not safe or not await asyncio.to_thread(path.is_file):
            raise LogNotFoundError(logfile)
        return path

    async def _follow_path(
        self,
        path: Path,
        length: int,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[str]:
        """对一个已准入文件执行有界历史读取和新增内容轮询。"""
        lines_queue: deque[str] = deque(maxlen=max(length, 50))
        file_size = (await asyncio.to_thread(path.stat)).st_size
        async with aiofiles.open(path, mode="r", encoding="utf-8", errors="replace") as file:
            if file_size > 100 * 1024:
                position = file_size - min(file_size, 100 * 1024)
                await file.seek(position)
                content = await file.read()
                first_newline = content.find("\n")
                if first_newline != -1:
                    content = content[first_newline + 1 :]
            else:
                content = await file.read()
            for line in (item.strip() for item in content.splitlines() if item.strip()):
                lines_queue.append(line)
        for line in lines_queue:
            yield line
        async with aiofiles.open(path, mode="r", encoding="utf-8", errors="replace") as file:
            await file.seek(0, 2)
            previous_size = (await asyncio.to_thread(path.stat)).st_size
            while not runtime_stop_state.is_system_stopped:
                if await disconnected():
                    break
                current_size = (await asyncio.to_thread(path.stat)).st_size
                if current_size > previous_size:
                    line = (await file.readline()).strip()
                    if line:
                        yield line
                    previous_size = current_size
                else:
                    await asyncio.sleep(0.5)

    def _collect_sync(self, name: str) -> list[LogFileData]:
        """同步发现并读取最多十个主程序或指定插件日志。"""
        normalized = str(name or "").strip().lower()
        if not normalized or not _LOG_DOWNLOAD_NAME_PATTERN.fullmatch(normalized):
            raise LogNotFoundError(name)
        root = Path(self._settings.get("LOG_PATH"))
        directory = root if normalized == "moviepilot" else root / "plugins"
        prefix = "moviepilot.log" if normalized == "moviepilot" else f"{normalized}.log"
        if not directory.is_dir():
            raise LogNotFoundError(name)
        current = directory / prefix
        backups = [
            item
            for item in directory.iterdir()
            if item.is_file() and item.name.startswith(f"{prefix}.")
        ]
        backups.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        paths = ([current] if current.is_file() else []) + backups
        entries: list[LogFileData] = []
        for path in paths[:_LOG_DOWNLOAD_LIMIT]:
            if not SecurityUtils.is_safe_path(base_path=root, user_path=path):
                raise LogNotFoundError(name)
            entries.append(LogFileData(name=path.name, content=path.read_bytes()))
        if not entries:
            raise LogNotFoundError(name)
        return entries


class _PluginMarketAdapter(PluginMarketPort):
    """通过统一异步 HTTP 客户端读取并解析 Wiki 市场清单。"""

    def __init__(self, settings: RuntimeSettingsService) -> None:
        """保存代理与 User-Agent 设置提供器。"""
        self._settings = settings

    async def fetch(self, url: str) -> MarketFetchResult:
        """读取 Wiki 文本并把解析异常收敛为空仓库列表。"""
        response = await AsyncRequestUtils(
            ua=self._settings.get("USER_AGENT"),
            proxies=self._settings.get("PROXY"),
            timeout=30,
            accept_type="text/plain,*/*",
        ).get_res(url)
        if response is None:
            return MarketFetchResult(status_code=None, repos=[])
        repos = (
            extract_plugin_market_repos_from_wiki(response.text)
            if response.status_code == 200
            else []
        )
        return MarketFetchResult(status_code=response.status_code, repos=repos)


class _ConfigurationEventAdapter(ConfigurationEventPort):
    """把已提交设置变更发布到宿主事件总线。"""

    async def publish(self, key: Any, value: Any = None) -> None:
        """发布兼容 ConfigChanged 事件。"""
        await eventmanager.async_send_event(
            etype=EventType.ConfigChanged,
            data=ConfigChangeEventData(key=key, value=value, change_type="update"),
        )


class _ServerInfoAdapter(ServerInfoPort):
    """适配 MoviePilot Server 的用户权限与统计接口。"""

    async def user_global(self) -> dict[str, Any]:
        """合并用户 UUID 与两项共享管理权限。"""
        is_admin = await MoviePilotServerHelper.async_is_admin_user()
        return {
            "USER_UNIQUE_ID": MoviePilotServerHelper.get_user_uuid(),
            "SUBSCRIBE_SHARE_MANAGE": is_admin,
            "WORKFLOW_SHARE_MANAGE": is_admin,
        }

    async def usage(self) -> dict[str, Any]:
        """读取安装版本统计。"""
        return await MoviePilotServerHelper.async_get_usage_statistic()


class _ReleaseCatalogAdapter(ReleaseCatalogPort):
    """通过 GitHub API 读取主程序 Release 列表。"""

    def __init__(self, settings: RuntimeSettingsService) -> None:
        """保存 GitHub 代理与请求头设置提供器。"""
        self._settings = settings

    async def list(self) -> list[dict[str, Any]] | None:
        """读取有效 JSON 列表，失败时返回空值。"""
        response = await AsyncRequestUtils(
            proxies=self._settings.get("PROXY"),
            headers=self._settings.get("GITHUB_HEADERS"),
        ).get_res("https://api.github.com/repos/jxxghp/MoviePilot/releases")
        if response is None or response.status_code != 200:
            return None
        payload = response.json()
        return payload if isinstance(payload, list) else None


class _RuntimeFeatureAdapter(RuntimeFeaturePort):
    """适配本机 Rust 加速能力探测。"""

    def snapshot(self) -> dict[str, bool]:
        """返回现有环境接口使用的四项 Rust 状态。"""
        return {
            "RUST_ACCEL": rust_accel.is_config_enabled(),
            "RUST_ACCEL_AVAILABLE": rust_accel.is_available(),
            "RUST_ACCEL_ENABLED": rust_accel.is_enabled(),
            "RUST_ACCEL_REQUIRED": rust_accel.is_required(),
        }


class _SystemControlAdapter(SystemControlPort):
    """适配宿主进程的重启与开发分支更新控制。"""

    def can_restart(self) -> bool:
        """查询部署环境重启能力。"""
        return SystemHelper.can_restart()

    def restart(self) -> tuple[bool, str]:
        """请求宿主重启。"""
        return SystemHelper.restart()

    def upgrade_dev(self) -> tuple[bool, str]:
        """执行开发分支更新。"""
        return SystemHelper.upgrade_dev()


class _SystemUpdateAdapter(SystemUpdatePort):
    """适配后台 Release 更新状态机。"""

    def status(self) -> SystemUpdateStatus:
        """读取当前更新状态。"""
        return system_update_manager.get_status()

    def check(self) -> SystemUpdateStatus:
        """立即检查更新。"""
        return system_update_manager.check()

    def download(self, target: SystemUpdateType = "application") -> SystemUpdateStatus:
        """启动指定升级类型的后台下载。"""
        return system_update_manager.start_download(target)

    def prepare_install(self, target: SystemUpdateType = "application") -> tuple[bool, str]:
        """确认指定升级类型的安装准备。"""
        return system_update_manager.request_install(target)

    def cancel_install(self, message: str) -> None:
        """撤销失败的安装请求。"""
        system_update_manager.cancel_install(message)


class _LlmCapabilityAdapter(LlmCapabilityPort):
    """适配 Agent 目录中的服务端工具能力注册表。"""

    def validate(self, env: dict[str, Any], settings: RuntimeSettingsService) -> str | None:
        """拒绝所选模型不支持的强制内建联网搜索。"""
        from app.agent.llm.tools import ServerToolRegistry, ServerToolUnavailableError

        mode = ServerToolRegistry.normalize_web_search_mode(
            env.get("LLM_WEB_SEARCH_MODE", settings.get("LLM_WEB_SEARCH_MODE", "local"))
        )
        if mode != "builtin":
            return None
        provider = str(env.get("LLM_PROVIDER", settings.get("LLM_PROVIDER", "")) or "").strip()
        model = str(env.get("LLM_MODEL", settings.get("LLM_MODEL", "")) or "").strip()
        base_url = env.get("LLM_BASE_URL", settings.get("LLM_BASE_URL"))
        if ServerToolRegistry.get_capability(
            provider=provider,
            model=model,
            base_url=str(base_url or "").strip() or None,
            tool_id="web_search",
        ):
            return None
        return str(ServerToolUnavailableError(provider=provider, model=model, tool_id="web_search"))


def compose_system_service(
    *,
    settings: RuntimeSettingsService,
    system_config: SystemConfigService,
    rule_group_mutation: Callable[[], Any],
) -> SystemService:
    """构造 System API 应用服务并注入全部具体外部能力。"""
    return SystemService(
        settings=settings,
        system_config=system_config,
        logs=_FileLogAdapter(settings),
        market=_PluginMarketAdapter(settings),
        events=_ConfigurationEventAdapter(),
        server=_ServerInfoAdapter(),
        releases=_ReleaseCatalogAdapter(settings),
        features=_RuntimeFeatureAdapter(),
        control=_SystemControlAdapter(),
        updates=_SystemUpdateAdapter(),
        llm=_LlmCapabilityAdapter(),
        plugin_mutation=plugin_system_config_mutation,
        rule_group_mutation=rule_group_mutation,
    )
