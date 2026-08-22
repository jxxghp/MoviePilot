"""系统配置应用服务与组合根注入点。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol


class SystemConfigReader(Protocol):
    """持久化用户配置的最小只读端口。"""

    def get(self, key: Any = None) -> Any:
        """读取配置。"""

    async def async_get(self, key: Any = None) -> Any:
        """异步读取配置。"""


class SystemConfigWriter(Protocol):
    """持久化用户配置的最小写入端口。"""

    def set(self, key: Any, value: Any) -> bool | None:
        """写入配置。"""

    async def async_set(self, key: Any, value: Any) -> bool | None:
        """异步写入配置。"""

    def delete(self, key: Any) -> Any:
        """删除配置。"""


class ConfigurationRepository(SystemConfigReader, SystemConfigWriter, Protocol):
    """兼容同时提供读写能力的旧配置仓储。"""


@dataclass(frozen=True, slots=True)
class TransferRetryConfig:
    """整理失败重试用例在一次调用中使用的稳定配置快照。"""

    max_failed_retries: Any


@dataclass(frozen=True, slots=True)
class ApiRuntimeConfig:
    """单次 API 请求使用的宿主配置快照。"""

    advanced_mode: bool
    access_token_expire_minutes: int
    btrfs_fsid_dedup: bool
    ai_agent_enable: bool
    api_token: str | None = None
    temp_path: Path = Path(".")
    media_recognize_share: bool = False
    subscribe_mode: str = "spider"


@dataclass(frozen=True, slots=True)
class SchedulerRuntimeConfig:
    """一次 Scheduler 初始化或任务注册使用的稳定配置快照。"""

    dev: bool
    timezone: str
    scheduler_workers: int
    db_backup_enable: bool
    db_backup_cron: str
    cookiecloud_interval: Any
    mediaserver_sync_interval: Any
    subscribe_search: bool
    subscribe_search_interval: Any
    subscribe_mode: str
    subscribe_rss_interval: int
    data_cleanup_enable: bool
    sitedata_refresh_interval: Any
    memory_gc_interval: Any
    ai_agent_enable: bool
    ai_agent_job_interval: Any
    usage_statistic_share: bool
    site_link: str | None


@dataclass(frozen=True, slots=True)
class ChainRuntimeConfig:
    """Chain 在一次宿主生命周期内使用的基础配置快照。"""

    media_extensions: tuple[str, ...]
    superuser: str = "admin"
    media_recognize_share: bool = False
    auxiliary_auth_enable: bool = False
    global_image_cache: bool = False
    auto_download_user: Optional[str] = None
    resource_url: Optional[str] = None
    user_agent: str = ""
    proxy: Any = None
    proxy_server: Any = None
    proxy_host: Optional[str] = None
    cookiecloud_blacklist: Any = None
    subscribe_mode: str = "spider"
    no_cache_site_key: str = ""
    refresh_batch_size: int = 50
    torrent_cache_size: int = 1000
    site_url: Optional[str] = None


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """由启动组合根提供的 API、Scheduler 与 Chain 配置快照工厂。"""

    api: Callable[[], ApiRuntimeConfig]
    scheduler: Callable[[], SchedulerRuntimeConfig]
    chain: Callable[[], ChainRuntimeConfig]


class SystemConfigService:
    """系统配置读写应用服务。"""

    def __init__(
        self,
        repository: ConfigurationRepository | None = None,
        *,
        reader: SystemConfigReader | None = None,
        writer: SystemConfigWriter | None = None,
    ) -> None:
        """注入可分离的读写端口，并兼容旧的单仓储装配参数。"""
        resolved_reader = reader or repository
        resolved_writer = writer or repository
        if resolved_reader is None or resolved_writer is None:
            raise ValueError("系统配置服务必须同时提供 reader 与 writer")
        self._reader = resolved_reader
        self._writer = resolved_writer

    def get(self, key: Any = None) -> Any:
        """读取配置。"""
        return self._reader.get(key)

    def set(self, key: Any, value: Any) -> bool | None:
        """写入配置。"""
        return self._writer.set(key, value)

    async def async_get(self, key: Any = None) -> Any:
        """异步读取配置。"""
        return await self._reader.async_get(key)

    async def async_set(self, key: Any, value: Any) -> bool | None:
        """异步写入配置。"""
        return await self._writer.async_set(key, value)

    def delete(self, key: Any) -> Any:
        """删除配置。"""
        return self._writer.delete(key)


_configured_system_config: SystemConfigService | None = None
_transfer_retry_config_provider: Callable[[], TransferRetryConfig] | None = None
_runtime_configuration: RuntimeConfiguration | None = None


def configure_system_config(service: SystemConfigService) -> None:
    """由启动组合根登记系统配置服务。"""
    global _configured_system_config
    _configured_system_config = service


def get_configured_system_config() -> SystemConfigService:
    """返回启动阶段登记的系统配置服务。"""
    if _configured_system_config is None:
        raise RuntimeError("系统配置服务尚未配置")
    return _configured_system_config


def configure_transfer_retry_config(
    provider: Callable[[], TransferRetryConfig],
) -> None:
    """由组合根登记整理失败重试快照工厂。"""
    global _transfer_retry_config_provider
    _transfer_retry_config_provider = provider


def get_transfer_retry_config() -> TransferRetryConfig:
    """为一次整理历史判定创建不可变配置快照。"""
    if _transfer_retry_config_provider is None:
        raise RuntimeError("整理失败重试配置尚未装配")
    return _transfer_retry_config_provider()


def configure_runtime_configuration(configuration: RuntimeConfiguration) -> None:
    """由启动组合根登记各运行面使用的类型化配置快照工厂。"""
    global _runtime_configuration
    _runtime_configuration = configuration


def get_scheduler_runtime_config() -> SchedulerRuntimeConfig:
    """为一次调度操作创建不可变配置快照。"""
    if _runtime_configuration is None:
        raise RuntimeError("运行时配置尚未装配")
    return _runtime_configuration.scheduler()


def get_api_runtime_config_snapshot() -> ApiRuntimeConfig:
    """为一次 API 调用创建不可变配置快照。"""
    if _runtime_configuration is None:
        raise RuntimeError("运行时配置尚未装配")
    return _runtime_configuration.api()
