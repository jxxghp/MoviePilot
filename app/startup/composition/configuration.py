"""把可变部署设置转换成宿主各领域使用的类型化配置快照。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

from app.application.configuration import (
    ApiRuntimeConfig,
    ChainRuntimeConfig,
    RuntimeConfiguration,
    RuntimeSettingsService,
    SchedulerRuntimeConfig,
    SystemConfigService,
    TokenRuntimeConfig,
    configure_runtime_configuration,
    configure_runtime_settings,
    configure_system_config,
    configure_token_runtime_config,
    reset_configuration_services,
)
from app.application.database import AsyncDatabaseExecutor
from app.application.directory import (
    DirectoryAwareSystemConfigService,
    DirectoryConfigurationMutationService,
    normalize_directory_configurations_for_policy,
)
from app.application.security.userconfig import (
    UserConfigurationService,
    configure_user_configuration,
    reset_user_configuration,
)
from app.db.adapters.classification import (
    SystemConfigDirectoryConfigurationStore,
)
from app.db.adapters.configuration import TransactionalUserConfigurationRepository
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.runtime.config import Settings
from app.runtime.settings import (
    configure_runtime_setting_provider,
    configure_runtime_setting_updater,
    reset_runtime_setting_ports,
)
from app.schemas.types import MediaType


@dataclass(frozen=True, slots=True)
class ConfigurationComposition:
    """保存启动阶段加载完成的配置服务与类型化快照门面。"""

    system_config: SystemConfigOper
    system_service: SystemConfigService
    user_service: UserConfigurationService
    runtime: RuntimeConfiguration
    settings: RuntimeSettingsService


async def compose_configuration(
    *,
    executor: AsyncDatabaseExecutor,
    settings: Settings,
) -> ConfigurationComposition:
    """加载系统与用户配置快照，并在全部成功后发布配置服务。"""
    system_config = cast(Callable[[], SystemConfigOper], SystemConfigOper)()
    user_config = TransactionalUserConfigurationRepository(SessionFactory)
    await executor.run(system_config.load_snapshot)
    await executor.run(user_config.load_snapshot)
    directory_mutation = DirectoryConfigurationMutationService(
        SystemConfigDirectoryConfigurationStore(
            SessionFactory,
            system_config.publish_many,
            normalize_directory_configurations_for_policy,
        ),
        async_executor=executor,
    )
    system_service = DirectoryAwareSystemConfigService(
        repository=system_config,
        async_executor=executor,
        directory_mutation=directory_mutation,
    )
    user_service = UserConfigurationService(
        repository=user_config,
        async_executor=executor,
    )
    return ConfigurationComposition(
        system_config=system_config,
        system_service=system_service,
        user_service=user_service,
        runtime=RuntimeConfiguration(
            api=lambda: build_api_runtime_config(settings),
            scheduler=lambda: build_scheduler_runtime_config(settings),
            chain=lambda: build_chain_runtime_config(settings),
        ),
        settings=RuntimeSettingsService(settings),
    )


def publish_configuration(
    composition: ConfigurationComposition,
    settings: Settings,
) -> None:
    """发布 HostRuntime 使用的同一配置对象及兼容设置入口。"""
    configure_system_config(composition.system_service)
    configure_user_configuration(composition.user_service)
    configure_runtime_configuration(composition.runtime)
    configure_runtime_settings(composition.settings)
    configure_runtime_setting_provider(lambda key: getattr(settings, key))
    configure_runtime_setting_updater(composition.settings.update)
    configure_token_runtime_config(lambda: build_token_runtime_config(settings))


def reset_configuration() -> None:
    """撤销当前 lifespan 发布的全部配置服务与 runtime 端口。"""
    reset_runtime_setting_ports()
    reset_user_configuration()
    reset_configuration_services()


def normalize_subscribe_rss_interval(value: object) -> int:
    """把无效或过小的 RSS 间隔收敛为兼容的安全值。"""
    try:
        if not isinstance(value, (str, bytes, bytearray, int, float)):
            return 30
        return max(int(value), 5)
    except (TypeError, ValueError):
        return 30


def build_api_runtime_config(settings: Settings) -> ApiRuntimeConfig:
    """从可热更新的部署设置构建一次 API 请求配置快照。"""
    return ApiRuntimeConfig(
        access_token_expire_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        btrfs_fsid_dedup=settings.BTRFS_FSID_DEDUP,
        ai_agent_enable=settings.AI_AGENT_ENABLE,
        api_token=settings.API_TOKEN,
        temp_path=settings.TEMP_PATH,
        media_recognize_share=settings.MEDIA_RECOGNIZE_SHARE,
        subscribe_mode=settings.SUBSCRIBE_MODE,
        search_source=settings.SEARCH_SOURCE,
        media_extensions=tuple(settings.RMT_MEDIAEXT),
        subtitle_extensions=tuple(settings.RMT_SUBEXT),
        audio_extensions=tuple(settings.RMT_AUDIOEXT),
        movie_rename_format=settings.RENAME_FORMAT(MediaType.MOVIE),
        television_rename_format=settings.RENAME_FORMAT(MediaType.TV),
        music_rename_format=settings.RENAME_FORMAT(MediaType.MUSIC),
        vapid_private_key=settings.VAPID.get("privateKey", ""),
        vapid_subject=settings.VAPID.get("subject", ""),
        cookiecloud_enable_local=bool(settings.COOKIECLOUD_ENABLE_LOCAL),
        cookiecloud_auth_header=settings.COOKIECLOUD_AUTH_HEADER,
        cookie_path=settings.COOKIE_PATH,
        root_path=settings.ROOT_PATH,
        version_flag=settings.VERSION_FLAG,
        app_domain=settings.APP_DOMAIN,
        nginx_port=settings.NGINX_PORT,
        passkey_require_uv=settings.PASSKEY_REQUIRE_UV,
    )


def build_token_runtime_config(settings: Settings) -> TokenRuntimeConfig:
    """从部署设置构建令牌编解码使用的安全配置快照。"""
    return TokenRuntimeConfig(
        secret_key=settings.SECRET_KEY,
        resource_secret_key=settings.RESOURCE_SECRET_KEY,
        access_token_expire_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        resource_access_token_expire_seconds=settings.RESOURCE_ACCESS_TOKEN_EXPIRE_SECONDS,
    )


def build_scheduler_runtime_config(settings: Settings) -> SchedulerRuntimeConfig:
    """从可热更新的部署设置构建一次 Scheduler 配置快照。"""
    return SchedulerRuntimeConfig(
        dev=settings.DEV,
        timezone=settings.TZ,
        scheduler_workers=settings.CONF.scheduler,
        db_backup_enable=settings.DB_BACKUP_ENABLE,
        db_backup_cron=settings.DB_BACKUP_CRON,
        cookiecloud_interval=settings.COOKIECLOUD_INTERVAL,
        mediaserver_sync_interval=settings.MEDIASERVER_SYNC_INTERVAL,
        subscribe_search=settings.SUBSCRIBE_SEARCH,
        subscribe_search_interval=settings.SUBSCRIBE_SEARCH_INTERVAL,
        subscribe_mode=settings.SUBSCRIBE_MODE,
        subscribe_rss_interval=normalize_subscribe_rss_interval(
            settings.SUBSCRIBE_RSS_INTERVAL
        ),
        data_cleanup_enable=settings.DATA_CLEANUP_ENABLE,
        sitedata_refresh_interval=settings.SITEDATA_REFRESH_INTERVAL,
        memory_gc_interval=settings.MEMORY_GC_INTERVAL,
        ai_agent_enable=settings.AI_AGENT_ENABLE,
        ai_agent_job_interval=settings.AI_AGENT_JOB_INTERVAL,
        usage_statistic_share=settings.USAGE_STATISTIC_SHARE,
        site_link=settings.MP_DOMAIN("#/site"),
    )


def build_chain_runtime_config(settings: Settings) -> ChainRuntimeConfig:
    """从部署设置构建 Chain 在本次实例生命周期使用的配置快照。"""
    return ChainRuntimeConfig(
        media_extensions=tuple(
            settings.RMT_MEDIAEXT
            + settings.DOWNLOAD_TMPEXT
            + settings.RMT_SUBEXT
            + settings.RMT_AUDIOEXT
        ),
        api_port=settings.PORT,
        api_token=settings.API_TOKEN,
        video_extensions=tuple(settings.RMT_MEDIAEXT),
        subtitle_extensions=tuple(settings.RMT_SUBEXT),
        audio_extensions=tuple(settings.RMT_AUDIOEXT),
        lyrics_batch_timeout=settings.LYRICS_BATCH_TIMEOUT,
        temporary_path=settings.TEMP_PATH,
        root_path=settings.ROOT_PATH,
        config_path=settings.CONFIG_PATH,
        frontend_path=Path(settings.FRONTEND_PATH),
        superuser=settings.SUPERUSER,
        media_recognize_share=settings.MEDIA_RECOGNIZE_SHARE,
        auxiliary_auth_enable=settings.AUXILIARY_AUTH_ENABLE,
        global_image_cache=settings.GLOBAL_IMAGE_CACHE,
        encoding_detection_performance_mode=settings.ENCODING_DETECTION_PERFORMANCE_MODE,
        encoding_detection_min_confidence=settings.ENCODING_DETECTION_MIN_CONFIDENCE,
        data_cleanup_enable=settings.DATA_CLEANUP_ENABLE,
        data_cleanup_message_days=settings.DATA_CLEANUP_MESSAGE_DAYS,
        data_cleanup_download_history_days=settings.DATA_CLEANUP_DOWNLOAD_HISTORY_DAYS,
        data_cleanup_site_userdata_days=settings.DATA_CLEANUP_SITE_USERDATA_DAYS,
        data_cleanup_transfer_history_days=settings.DATA_CLEANUP_TRANSFER_HISTORY_DAYS,
        data_cleanup_download_failure_days=settings.DATA_CLEANUP_DOWNLOAD_FAILURE_DAYS,
        data_cleanup_subscribe_history_days=settings.DATA_CLEANUP_SUBSCRIBE_HISTORY_DAYS,
        data_cleanup_agent_chat_days=settings.DATA_CLEANUP_AGENT_CHAT_DAYS,
        data_cleanup_agent_task_run_days=settings.DATA_CLEANUP_AGENT_TASK_RUN_DAYS,
        data_cleanup_outbox_completed_days=settings.DATA_CLEANUP_OUTBOX_COMPLETED_DAYS,
        data_cleanup_outbox_dead_days=settings.DATA_CLEANUP_OUTBOX_DEAD_DAYS,
        download_subtitle=settings.DOWNLOAD_SUBTITLE,
        music_metadata_to_simplified=settings.MUSIC_METADATA_TO_SIMPLIFIED,
        recognize_plugin_first=settings.RECOGNIZE_PLUGIN_FIRST,
        ai_agent_enable=settings.AI_AGENT_ENABLE,
        ai_agent_global=settings.AI_AGENT_GLOBAL,
        ai_agent_retry_transfer=settings.AI_AGENT_RETRY_TRANSFER,
        llm_provider=settings.LLM_PROVIDER,
        llm_model=settings.LLM_MODEL,
        search_resource_pages=settings.SEARCH_RESOURCE_PAGES,
        ai_recommend_enabled=settings.AI_RECOMMEND_ENABLED,
        ai_recommend_max_items=settings.AI_RECOMMEND_MAX_ITEMS,
        ai_recommend_user_preference=settings.AI_RECOMMEND_USER_PREFERENCE,
        max_search_name_limit=settings.MAX_SEARCH_NAME_LIMIT,
        search_multiple_name=settings.SEARCH_MULTIPLE_NAME,
        search_source=settings.SEARCH_SOURCE,
        search_threadpool_size=settings.CONF.threadpool,
        transfer_threads=settings.TRANSFER_THREADS,
        transfer_failure_notification_aggregation=(
            settings.TRANSFER_FAILURE_NOTIFICATION_AGGREGATION
        ),
        transfer_task_timeout=settings.TRANSFER_TASK_TIMEOUT,
        scrape_follow_tmdb=settings.SCRAP_FOLLOW_TMDB,
        metadata_cache_ttl=settings.CONF.meta,
        auto_download_user=settings.AUTO_DOWNLOAD_USER,
        resource_url=settings.MP_DOMAIN("#/resource"),
        history_url=settings.MP_DOMAIN("#/history"),
        downloading_url=settings.MP_DOMAIN("#/downloading"),
        movie_subscribe_url=settings.MP_DOMAIN("#/subscribe/movie?tab=mysub"),
        television_subscribe_url=settings.MP_DOMAIN("#/subscribe/tv?tab=mysub"),
        music_subscribe_url=settings.MP_DOMAIN("#/subscribe/music?tab=mysub"),
        user_agent=settings.USER_AGENT,
        normal_user_agent=settings.NORMAL_USER_AGENT,
        proxy=settings.PROXY,
        proxy_server=settings.PROXY_SERVER,
        proxy_host=settings.PROXY_HOST,
        github_headers=settings.GITHUB_HEADERS,
        cookiecloud_blacklist=settings.COOKIECLOUD_BLACKLIST,
        subscribe_mode=settings.SUBSCRIBE_MODE,
        no_cache_site_key=settings.NO_CACHE_SITE_KEY,
        refresh_batch_size=settings.CONF.refresh,
        torrent_cache_size=settings.CONF.torrents,
        site_url=settings.MP_DOMAIN("#/site"),
        workflow_url=settings.MP_DOMAIN("#/workflow"),
        season_zero_names=tuple(settings.RENAME_FORMAT_S0_NAMES),
        movie_rename_format=settings.RENAME_FORMAT(MediaType.MOVIE),
        television_rename_format=settings.RENAME_FORMAT(MediaType.TV),
        music_rename_format=settings.RENAME_FORMAT(MediaType.MUSIC),
        tmdb_image_domain=settings.TMDB_IMAGE_DOMAIN,
        wallpaper=settings.WALLPAPER,
        wallpaper_image_url=settings.WALLPAPER_IMAGE_URL,
        customize_wallpaper_api_url=settings.CUSTOMIZE_WALLPAPER_API_URL,
        security_image_suffixes=tuple(settings.SECURITY_IMAGE_SUFFIXES),
        cache_path=settings.CACHE_PATH,
        global_image_cache_days=settings.GLOBAL_IMAGE_CACHE_DAYS,
    )
