"""Chain 无参兼容入口与跨层依赖的宿主组合根。"""

from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional, cast

from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.system.host import SystemUtils
from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.classification.execution import ClassificationExecutionService
from app.application.classification.reference import ClassificationCategoryResolver
from app.application.classification.runtime import ClassificationRuntime
from app.application.configuration import ChainRuntimeConfig
from app.application.directory import (
    configure_directory_classification_resolver,
    reset_directory_classification_resolver,
)
from app.application.image import (
    configure_wallpaper_providers,
    reset_wallpaper_providers,
)
from app.chain._recognition import (
    RecognitionSharePort,
    configure_recognition_share_port,
    reset_recognition_share_port,
)
from app.chain.mediaserver import MediaServerChain
from app.chain.subscribe.notify import (
    SubscriptionSharePort,
    configure_subscription_share_port,
    reset_subscription_share_port,
)
from app.chain.tmdb import TmdbChain
from app.chain.transfer.filter import (
    NetworkFilesystemPort,
    configure_network_filesystem_port,
    reset_network_filesystem_port,
)
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.adapters.download import TransactionalDownloadFailureRepository
from app.db.adapters.mediaserver import TransactionalMediaServerRepository
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.oper.message import MessageOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.cache import AsyncFileCache, FileCache
from app.runtime.events import EventManager
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.module.manager import ModuleManager
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.stop import runtime_stop_state
from app.schemas.common import JsonData
from app.schemas.types import MediaType
from app.startup.composition.database import build_transactional_user_repository
from app.startup.composition.runtime import RuntimeDependencies
from app.startup.composition.subscription import (
    delete_subscribe_scope,
    rule_group_mutation_scope,
    site_reference_mutation_scope,
    subscription_completion_scope,
    subscription_mutation_scope,
    sync_delete_subscribe_scope,
    sync_subscription_mutation_scope,
)

_event_manager_factory: Callable[[], EventManager] = EventManager


def execute_legacy_transfer_command(**kwargs: Any) -> Any:
    """把旧 Chain ABI 延迟转入唯一 TransferChain durable command。"""
    from app.chain.transfer.facade import TransferChain

    return TransferChain().execute_legacy_transfer_command(**kwargs)


def build_chain_runtime_context(
    *,
    dependencies: RuntimeDependencies,
    system_config: SystemConfigOper,
    configuration: Callable[[], ChainRuntimeConfig],
    classification_service: ClassificationExecutionService,
) -> ChainRuntimeContext:
    """创建 Chain 无参兼容入口共享的运行时对象与数据端口。"""
    return ChainRuntimeContext(
        module_manager=ModuleManager(),
        plugin_manager=PluginManager(),
        event_manager=_event_manager_factory(),
        message_oper=MessageOper(),
        message_helper=dependencies.message_helper,
        file_cache=FileCache(),
        async_file_cache=AsyncFileCache(),
        message_queue=dependencies.message_queue,
        module_dispatcher_factory=ModuleInvocationDispatcher,
        site_repository=dependencies.site,
        subscription_repository=dependencies.subscription,
        subscription_search_repository=dependencies.subscription_search,
        subscription_mutation_scope=subscription_mutation_scope,
        sync_subscription_mutation_scope=sync_subscription_mutation_scope,
        subscription_delete_scope=delete_subscribe_scope,
        sync_subscription_delete_scope=sync_delete_subscribe_scope,
        subscription_completion_scope=subscription_completion_scope,
        rule_group_mutation_scope=partial(
            rule_group_mutation_scope,
            system_config.publish_many,
        ),
        site_reference_mutation_scope=partial(
            site_reference_mutation_scope,
            system_config.publish_many,
        ),
        download_history_repository=dependencies.download_history,
        transfer_history_repository=dependencies.transfer_history,
        transfer_admission_repository=TransactionalTransferAdmissionRepository(SessionFactory),
        transfer_execution_repository=dependencies.transfer_execution,
        media_server_repository=TransactionalMediaServerRepository(SessionFactory),
        download_failure_repository=TransactionalDownloadFailureRepository(SessionFactory),
        user_repository=build_transactional_user_repository(),
        classification_service=classification_service,
        legacy_transfer_command=execute_legacy_transfer_command,
        configuration=configuration(),
        durable_event_writer=TransactionalChainDurableEventWriter(SessionFactory),
        stop_state=runtime_stop_state,
    )


def configure_wallpaper_services() -> None:
    """把需要 Chain 编排的壁纸来源注入图片服务。"""
    configure_wallpaper_providers(
        tmdb_wallpaper=lambda: TmdbChain().get_random_wallpager(),
        tmdb_wallpapers=lambda count: TmdbChain().get_trending_wallpapers(count),
        mediaserver_wallpaper=lambda: MediaServerChain().get_latest_wallpaper(),
        mediaserver_wallpapers=lambda count: MediaServerChain().get_latest_wallpapers(count=count),
    )


def configure_directory_classification_service(
    runtime: ClassificationRuntime,
) -> None:
    """把活动分类策略解析能力装配给目录选择与路径生成服务。"""
    configure_directory_classification_resolver(
        ClassificationCategoryResolver(runtime)
    )


def configure_chain_runtime_context(
    *,
    dependencies: RuntimeDependencies,
    system_config: SystemConfigOper,
    configuration: Callable[[], ChainRuntimeConfig],
    classification_service: ClassificationExecutionService,
) -> None:
    """登记按需构造的 Chain 上下文，保持无参 Chain 的插件兼容合同。"""
    configure_chain_runtime_context_provider(
        lambda: build_chain_runtime_context(
            dependencies=dependencies,
            system_config=system_config,
            configuration=configuration,
            classification_service=classification_service,
        )
    )


def reset_chain_services() -> None:
    """按发布逆序撤销 Chain 无参上下文和壁纸来源。"""
    configure_chain_runtime_context_provider(None)
    reset_directory_classification_resolver()
    reset_wallpaper_providers()


class _RecognitionShareAdapter:
    """把 MoviePilot Server 共享识别能力适配为 Chain 窄端口。"""

    def report_recognize_share(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo | MusicInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """同步上报共享识别结果。"""
        return bool(MoviePilotServerHelper.report_recognize_share(
            meta=meta, mediainfo=mediainfo, keyword_meta=keyword_meta
        ))

    async def async_report_recognize_share(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo | MusicInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """异步上报共享识别结果。"""
        return bool(await MoviePilotServerHelper.async_report_recognize_share(
            meta=meta, mediainfo=mediainfo, keyword_meta=keyword_meta
        ))

    def query_recognize_share(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """同步查询共享识别结果。"""
        result = MoviePilotServerHelper.query_recognize_share(
            meta=meta,
            mtype=mtype,
            keyword_meta=keyword_meta,
            **({"music_type": music_type} if music_type is not None else {}),
        )
        return cast(Optional[dict[str, Any]], result)

    async def async_query_recognize_share(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """异步查询共享识别结果。"""
        result = await MoviePilotServerHelper.async_query_recognize_share(
            meta=meta,
            mtype=mtype,
            keyword_meta=keyword_meta,
            **({"music_type": music_type} if music_type is not None else {}),
        )
        return cast(Optional[dict[str, Any]], result)

    def to_recognize_params(
            self,
            item: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """把服务端结果转换为本地识别参数。"""
        return cast(
            Optional[dict[str, Any]],
            MoviePilotServerHelper.to_recognize_params(item),
        )


class _SubscriptionShareAdapter:
    """把 MoviePilot Server 订阅共享能力适配为 Chain 窄端口。"""

    def report_added(self, payload: dict[str, Any]) -> bool:
        """同步上报新增订阅统计。"""
        return bool(MoviePilotServerHelper.sub_reg_durable(payload))

    async def async_report_added(self, payload: dict[str, Any]) -> bool:
        """异步上报新增订阅统计。"""
        return bool(await MoviePilotServerHelper.async_sub_reg_durable(payload))

    def list_shares(self) -> list[dict[str, Any]]:
        """读取当前用户可见的订阅分享。"""
        return cast(list[dict[str, Any]], MoviePilotServerHelper.get_subscribe_shares())

    def report_completed(self, payload: Mapping[str, JsonData]) -> bool:
        """同步上报订阅完成统计。"""
        return bool(MoviePilotServerHelper.sub_done_durable(dict(payload)))


class _NetworkFilesystemAdapter:
    """把宿主文件系统探测能力适配为整理 Chain 窄端口。"""

    def is_network_filesystem(
            self,
            path: Path,
            *,
            include_local_fuse: bool = False,
    ) -> bool:
        """判断路径是否位于网络或指定的本地 FUSE 文件系统。"""
        return bool(SystemUtils.is_network_filesystem(
            path, include_local_fuse=include_local_fuse
        ))


def configure_chain_port_composition() -> None:
    """原子装配 Chain 的共享服务与文件系统端口。"""
    reset_chain_port_composition()
    try:
        configure_recognition_share_port(
            cast(RecognitionSharePort, _RecognitionShareAdapter())
        )
        configure_subscription_share_port(
            cast(SubscriptionSharePort, _SubscriptionShareAdapter())
        )
        configure_network_filesystem_port(
            cast(NetworkFilesystemPort, _NetworkFilesystemAdapter())
        )
    except Exception:
        reset_chain_port_composition()
        raise


def reset_chain_port_composition() -> None:
    """释放 Chain 技术端口，支持重复 lifespan 与启动失败回滚。"""
    reset_network_filesystem_port()
    reset_subscription_share_port()
    reset_recognition_share_port()
