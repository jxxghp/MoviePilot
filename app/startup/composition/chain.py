"""Chain 无参兼容入口与跨层依赖的宿主组合根。"""

from functools import partial
from typing import Any, Callable

from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.configuration import ChainRuntimeConfig
from app.application.image import (
    configure_wallpaper_providers,
    reset_wallpaper_providers,
)
from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.adapters.download import TransactionalDownloadFailureRepository
from app.db.adapters.mediaserver import TransactionalMediaServerRepository
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.oper.message import MessageOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory
from app.runtime.cache import AsyncFileCache, FileCache
from app.runtime.events import EventManager
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.module.manager import ModuleManager
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.stop import runtime_stop_state
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


def configure_chain_runtime_context(
    *,
    dependencies: RuntimeDependencies,
    system_config: SystemConfigOper,
    configuration: Callable[[], ChainRuntimeConfig],
) -> None:
    """登记按需构造的 Chain 上下文，保持无参 Chain 的插件兼容合同。"""
    configure_chain_runtime_context_provider(
        lambda: build_chain_runtime_context(
            dependencies=dependencies,
            system_config=system_config,
            configuration=configuration,
        )
    )


def reset_chain_services() -> None:
    """按发布逆序撤销 Chain 无参上下文和壁纸来源。"""
    configure_chain_runtime_context_provider(None)
    reset_wallpaper_providers()
