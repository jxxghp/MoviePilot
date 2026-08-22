"""系统配置应用服务与组合根注入点。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from app.schemas.types import MediaType


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


class MutableRuntimeSettings(Protocol):
    """部署设置对象对管理 API 暴露的最小可变合同。"""

    def model_dump(
        self,
        *,
        include: Optional[set[str]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        """按白名单或排除列表导出当前设置。"""
        ...

    def update_settings(
        self,
        env: dict[str, Any],
    ) -> dict[str, tuple[Optional[bool], str]]:
        """批量更新部署设置并返回逐项结果。"""
        ...

    def update_setting(
        self,
        key: str,
        value: Any,
    ) -> tuple[Optional[bool], str]:
        """更新单个部署设置。"""
        ...


@dataclass(frozen=True, slots=True)
class TransferRetryConfig:
    """整理失败重试用例在一次调用中使用的稳定配置快照。"""

    max_failed_retries: Any


@dataclass(frozen=True, slots=True)
class TokenRuntimeConfig:
    """令牌编解码在一次宿主生命周期内使用的安全配置快照。"""

    secret_key: str
    resource_secret_key: str
    access_token_expire_minutes: int
    resource_access_token_expire_seconds: int


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
    search_source: str = ""
    media_extensions: tuple[str, ...] = ()
    subtitle_extensions: tuple[str, ...] = ()
    audio_extensions: tuple[str, ...] = ()
    movie_rename_format: str = ""
    television_rename_format: str = ""
    music_rename_format: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""
    cookiecloud_enable_local: bool = False
    cookiecloud_auth_header: Optional[str] = None
    cookie_path: Path = Path(".")
    root_path: Path = Path(".")
    version_flag: str = "v3"
    app_domain: str | None = None
    nginx_port: int = 3000
    passkey_require_uv: bool = True

    def rename_format(self, media_type: MediaType) -> str:
        """从请求快照返回指定媒体类型的稳定重命名格式。"""
        if media_type == MediaType.TV:
            return self.television_rename_format
        if media_type == MediaType.MUSIC:
            return self.music_rename_format
        return self.movie_rename_format


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
    api_port: int = 3000
    api_token: str | None = None
    video_extensions: tuple[str, ...] = ()
    subtitle_extensions: tuple[str, ...] = ()
    audio_extensions: tuple[str, ...] = ()
    temporary_path: Path = Path(".")
    root_path: Path = Path(".")
    config_path: Path = Path(".")
    frontend_path: Path = Path(".")
    superuser: str = "admin"
    media_recognize_share: bool = False
    auxiliary_auth_enable: bool = False
    global_image_cache: bool = False
    encoding_detection_performance_mode: bool = False
    encoding_detection_min_confidence: float = 0.5
    data_cleanup_enable: bool = False
    data_cleanup_message_days: Any = 0
    data_cleanup_download_history_days: Any = 0
    data_cleanup_site_userdata_days: Any = 0
    data_cleanup_transfer_history_days: Any = 0
    data_cleanup_download_failure_days: Any = 0
    download_subtitle: bool = True
    music_metadata_to_simplified: bool = True
    recognize_plugin_first: bool = False
    ai_agent_enable: bool = False
    ai_agent_global: bool = False
    ai_agent_retry_transfer: bool = False
    llm_provider: str = ""
    llm_model: str = ""
    search_resource_pages: int = 1
    ai_recommend_enabled: bool = False
    ai_recommend_max_items: int = 50
    ai_recommend_user_preference: str = ""
    max_search_name_limit: int = 3
    search_multiple_name: bool = False
    search_threadpool_size: int = 1
    transfer_threads: int = 1
    transfer_failure_notification_aggregation: bool = True
    transfer_task_timeout: int = 120
    scrape_follow_tmdb: bool = True
    metadata_cache_ttl: int = 3600
    auto_download_user: Optional[str] = None
    resource_url: Optional[str] = None
    history_url: Optional[str] = None
    downloading_url: Optional[str] = None
    movie_subscribe_url: Optional[str] = None
    television_subscribe_url: Optional[str] = None
    music_subscribe_url: Optional[str] = None
    user_agent: str = ""
    normal_user_agent: str = ""
    proxy: Any = None
    proxy_server: Any = None
    proxy_host: Optional[str] = None
    github_headers: Any = None
    cookiecloud_blacklist: Any = None
    subscribe_mode: str = "spider"
    no_cache_site_key: str = ""
    refresh_batch_size: int = 50
    torrent_cache_size: int = 1000
    site_url: Optional[str] = None
    workflow_url: Optional[str] = None
    season_zero_names: tuple[str, ...] = ()
    movie_rename_format: str = ""
    television_rename_format: str = ""
    music_rename_format: str = ""
    tmdb_image_domain: str = "image.tmdb.org"
    wallpaper: str = "bing"
    customize_wallpaper_api_url: Optional[str] = None
    security_image_suffixes: tuple[str, ...] = ()
    cache_path: Path = Path(".")
    global_image_cache_days: int = 7

    def rename_format(self, media_type: MediaType) -> str:
        """从快照返回指定媒体类型的稳定重命名格式。"""
        if media_type == MediaType.TV:
            return self.television_rename_format
        if media_type == MediaType.MUSIC:
            return self.music_rename_format
        return self.movie_rename_format

    def tmdb_image_url(
        self,
        file_path: Optional[str],
        file_size: str = "original",
    ) -> Optional[str]:
        """使用快照中的 TMDB 图片域名构造完整图片地址。"""
        if not file_path:
            return None
        normalized_path = file_path.removeprefix("/")
        return f"https://{self.tmdb_image_domain}/t/p/{file_size}/{normalized_path}"


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """由启动组合根提供的 API、Scheduler 与 Chain 配置快照工厂。"""

    api: Callable[[], ApiRuntimeConfig]
    scheduler: Callable[[], SchedulerRuntimeConfig]
    chain: Callable[[], ChainRuntimeConfig]


class RuntimeSettingsService:
    """隔离管理 API 与全局 Settings 实例的读写适配服务。"""

    def __init__(self, settings: MutableRuntimeSettings) -> None:
        """保存由 Startup 注入的唯一部署设置对象。"""
        self._settings = settings

    def contains(self, key: str) -> bool:
        """判断部署设置是否声明指定字段或属性。"""
        return hasattr(self._settings, key)

    def get(self, key: str, default: Any = None) -> Any:
        """读取一个部署设置，缺失时返回调用方默认值。"""
        return getattr(self._settings, key, default)

    def snapshot(
        self,
        *,
        include: Optional[set[str]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        """导出脱离可变 Settings 对象的请求级字典快照。"""
        return self._settings.model_dump(include=include, exclude=exclude)

    def update_many(
        self,
        env: dict[str, Any],
    ) -> dict[str, tuple[Optional[bool], str]]:
        """批量更新部署设置。"""
        return self._settings.update_settings(env=env)

    def update(self, key: str, value: Any) -> tuple[Optional[bool], str]:
        """更新单个部署设置。"""
        return self._settings.update_setting(key, value)


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
_token_runtime_config_provider: Callable[[], TokenRuntimeConfig] | None = None
_runtime_configuration: RuntimeConfiguration | None = None
_runtime_settings_service: RuntimeSettingsService | None = None


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


def configure_token_runtime_config(
    provider: Callable[[], TokenRuntimeConfig],
) -> None:
    """由启动组合根登记令牌安全配置快照工厂。"""
    global _token_runtime_config_provider
    _token_runtime_config_provider = provider


def get_token_runtime_config() -> TokenRuntimeConfig:
    """返回当前令牌编解码使用的不可变配置快照。"""
    if _token_runtime_config_provider is None:
        raise RuntimeError("令牌运行时配置尚未装配")
    return _token_runtime_config_provider()


def configure_runtime_configuration(configuration: RuntimeConfiguration) -> None:
    """由启动组合根登记各运行面使用的类型化配置快照工厂。"""
    global _runtime_configuration
    _runtime_configuration = configuration


def configure_runtime_settings(service: RuntimeSettingsService) -> None:
    """由组合根登记管理 API 使用的部署设置服务。"""
    global _runtime_settings_service
    _runtime_settings_service = service


def get_runtime_settings() -> RuntimeSettingsService:
    """返回组合根登记的部署设置服务。"""
    if _runtime_settings_service is None:
        raise RuntimeError("部署设置服务尚未装配")
    return _runtime_settings_service


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


def get_chain_runtime_config_snapshot() -> ChainRuntimeConfig:
    """为无实例兼容入口创建一次稳定的 Chain 配置快照。"""
    if _runtime_configuration is None:
        raise RuntimeError("运行时配置尚未装配")
    return _runtime_configuration.chain()
