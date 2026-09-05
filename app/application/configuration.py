"""系统配置应用服务与组合根注入点。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Optional, Protocol, TypeVar, cast

from app.application.database import AsyncDatabaseExecutor
from app.schemas.common import JsonData
from app.schemas.types import MediaType, SystemConfigKey

SystemConfigValueNormalizer = Callable[[Any, Any], Any]
"""系统配置值在进入持久化端口前使用的规范化函数。"""

T = TypeVar("T")


class SystemConfigReader(Protocol):
    """持久化用户配置的最小只读端口。"""

    def get(self, key: Any = None) -> Any:
        """读取配置。"""


class SystemConfigWriter(Protocol):
    """持久化用户配置的最小写入端口。"""

    def set(self, key: Any, value: Any) -> bool | None:
        """写入配置。"""

    def delete(self, key: Any) -> Any:
        """删除配置。"""

    def increment(self, key: SystemConfigKey, step: int = 1) -> int:
        """原子递增整数配置并返回递增后的值。"""

    def update_atomically(
        self,
        key: Any,
        mutation: Callable[[Any, Any], tuple[T, Any]],
    ) -> T:
        """在持久化写锁内读取旧值、提交新值并返回业务结果。"""


class ConfigurationRepository(SystemConfigReader, SystemConfigWriter, Protocol):
    """兼容同时提供读写能力的旧配置仓储。"""


class SystemConfigStagingPort(Protocol):
    """跨表应用服务在调用方 Session 内读取并暂存系统配置。"""

    def get_for_update(self, key: SystemConfigKey) -> JsonData:
        """同步锁定并读取一项配置。"""

    def stage_set(self, key: SystemConfigKey, value: JsonData) -> None:
        """同步暂存一项配置，不提交事务。"""

    async def async_get_for_update(self, key: SystemConfigKey) -> JsonData:
        """异步锁定并读取一项配置。"""

    async def async_stage_set(self, key: SystemConfigKey, value: JsonData) -> None:
        """异步暂存一项配置，不提交事务。"""


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
class SystemConfigWriteResult:
    """系统配置一次写入的变更状态和实际规范化值。"""

    changed: bool | None
    normalized_value: Any


@dataclass(frozen=True, slots=True)
class ApiRuntimeConfig:
    """单次 API 请求使用的宿主配置快照。"""

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
    superuser: str = ""
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
    data_cleanup_subscribe_history_days: Any = 0
    data_cleanup_agent_chat_days: Any = 0
    data_cleanup_agent_task_run_days: Any = 0
    data_cleanup_outbox_completed_days: Any = 0
    data_cleanup_outbox_dead_days: Any = 0
    download_subtitle: bool = True
    lyrics_batch_timeout: int = 120
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
    search_source: str = "themoviedb"
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
    wallpaper_image_url: Optional[str] = None
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
        async_executor: AsyncDatabaseExecutor | None = None,
        value_normalizer: SystemConfigValueNormalizer | None = None,
    ) -> None:
        """注入读写端口、异步事务执行能力及可选值规范化边界。"""
        resolved_reader = reader or repository
        resolved_writer = writer or repository
        if resolved_reader is None or resolved_writer is None:
            raise ValueError("系统配置服务必须同时提供 reader 与 writer")
        self._reader = resolved_reader
        self._writer = resolved_writer
        self._async_executor = async_executor
        self._value_normalizer = value_normalizer

    def get(self, key: Any = None) -> Any:
        """读取配置。"""
        return self._reader.get(key)

    def set(self, key: Any, value: Any) -> bool | None:
        """写入配置。"""
        return self.set_with_normalized_value(key, value).changed

    def set_with_normalized_value(
        self,
        key: Any,
        value: Any,
    ) -> SystemConfigWriteResult:
        """规范化一次后同步写入，并返回实际交给持久化端口的值。"""
        normalized_value = self.normalize_value(key, value)
        return SystemConfigWriteResult(
            changed=self._writer.set(key, normalized_value),
            normalized_value=normalized_value,
        )

    def normalize_value(self, key: Any, value: Any) -> Any:
        """在写入和配置事件发布前返回同一份规范化配置值。"""
        if self._value_normalizer is None:
            return value
        return self._value_normalizer(key, value)

    def increment(self, key: SystemConfigKey, step: int = 1) -> int:
        """原子递增整数系统配置，保持计数更新由持久化端口负责。"""
        return self._writer.increment(key, step)

    async def async_set(self, key: Any, value: Any) -> bool | None:
        """异步写入配置，并等待数据库提交或回滚完成。"""
        return (await self.async_set_with_normalized_value(key, value)).changed

    async def async_set_with_normalized_value(
        self,
        key: Any,
        value: Any,
    ) -> SystemConfigWriteResult:
        """规范化一次后异步写入，并返回配置事件应发布的同一值。"""
        if self._async_executor is None:
            raise RuntimeError("系统配置异步数据库执行端口尚未配置")
        normalized_value = self.normalize_value(key, value)
        result = await self._async_executor.run(
            partial(self._writer.set, key, normalized_value)
        )
        return SystemConfigWriteResult(
            changed=cast(bool | None, result),
            normalized_value=normalized_value,
        )

    async def async_update_atomically(
        self,
        key: Any,
        mutation: Callable[[Any], tuple[T, Any]],
    ) -> T:
        """在线程化短事务内原子读取旧值、规范化并写入新值。"""
        if self._async_executor is None:
            raise RuntimeError("系统配置异步数据库执行端口尚未配置")

        def apply(_session: Any, current: Any) -> tuple[T, Any]:
            """把不暴露数据库会话的应用层 mutation 适配到底层原子仓储。"""
            result, value = mutation(current)
            return result, self.normalize_value(key, value)

        return cast(
            T,
            await self._async_executor.run(
                partial(self._writer.update_atomically, key, apply)
            ),
        )

    async def async_delete(self, key: Any) -> Any:
        """异步删除配置，并等待数据库提交或回滚完成。"""
        if self._async_executor is None:
            raise RuntimeError("系统配置异步数据库执行端口尚未配置")
        return await self._async_executor.run(partial(self._writer.delete, key))

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


def reset_system_config() -> None:
    """清除当前 lifespan 的系统配置服务，避免重复启动复用旧实例。"""
    global _configured_system_config
    _configured_system_config = None


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


def reset_transfer_retry_config() -> None:
    """清除整理失败重试快照工厂，使未装配读取重新失败。"""
    global _transfer_retry_config_provider
    _transfer_retry_config_provider = None


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


def reset_token_runtime_config() -> None:
    """清除令牌安全配置快照工厂，禁止复用上一 lifespan 的密钥。"""
    global _token_runtime_config_provider
    _token_runtime_config_provider = None


def get_token_runtime_config() -> TokenRuntimeConfig:
    """返回当前令牌编解码使用的不可变配置快照。"""
    if _token_runtime_config_provider is None:
        raise RuntimeError("令牌运行时配置尚未装配")
    return _token_runtime_config_provider()


def configure_runtime_configuration(configuration: RuntimeConfiguration) -> None:
    """由启动组合根登记各运行面使用的类型化配置快照工厂。"""
    global _runtime_configuration
    _runtime_configuration = configuration


def reset_runtime_configuration() -> None:
    """清除各运行面共享的类型化配置快照工厂。"""
    global _runtime_configuration
    _runtime_configuration = None


def configure_runtime_settings(service: RuntimeSettingsService) -> None:
    """由组合根登记管理 API 使用的部署设置服务。"""
    global _runtime_settings_service
    _runtime_settings_service = service


def reset_runtime_settings() -> None:
    """清除管理 API 使用的部署设置服务。"""
    global _runtime_settings_service
    _runtime_settings_service = None


def reset_configuration_services() -> None:
    """清除当前 lifespan 登记的全部配置服务与快照 provider。"""
    reset_system_config()
    reset_transfer_retry_config()
    reset_token_runtime_config()
    reset_runtime_configuration()
    reset_runtime_settings()


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
