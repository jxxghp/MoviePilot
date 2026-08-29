"""验证 lifespan provider 可对称撤销且不会串用上一代对象。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import HTTPException

from app.adapters.external import server as server_module
from app.adapters.external.server import (
    MoviePilotServerHelper,
    configure_server_application_services,
    reset_server_application_services,
)
from app.adapters.web.security import access as access_module
from app.adapters.web.security.access import (
    reset_superuser_token_payload_provider,
    set_superuser_token_payload_provider,
)
from app.application import image as image_module
from app.application import service as service_module
from app.application.agenttask import (
    configure_agent_task_execution,
    get_agent_task_execution_service,
    reset_agent_task_execution,
)
from app.application.chain.context import (
    configure_chain_runtime_context_provider,
    get_chain_runtime_context,
)
from app.application.history import (
    configure_transfer_history_repository,
    get_transfer_history_repository,
    reset_transfer_history_repository,
)
from app.application.image import (
    WallpaperHelper,
    configure_image_ports,
    configure_wallpaper_providers,
    reset_image_ports,
    reset_wallpaper_providers,
)
from app.application.messaging import ingress as ingress_module
from app.application.messaging.chat import (
    configure_agent_chat_persistence,
    configure_agent_chat_service,
    get_configured_agent_chat_persistence,
    get_configured_agent_chat_service,
    reset_agent_chat_persistence,
    reset_agent_chat_service,
)
from app.application.messaging.ingress import (
    configure_message_ingress_port,
    reset_message_ingress_port,
)
from app.application.module import (
    configure_module_runtime,
    get_module_manager,
    reset_module_runtime,
)
from app.application.network import (
    configure_network_test_service,
    get_configured_network_test_service,
    reset_network_test_service,
)
from app.application.outbox import (
    configure_outbox_dispatcher,
    dispatch_pending_outbox,
    reset_outbox_dispatcher,
)
from app.application.security.auth import (
    configure_auth_service,
    get_configured_auth_service,
    reset_auth_service,
)
from app.application.security.passkey import (
    PasskeyChallengeStore,
    configure_passkey_challenge_cache,
    configure_passkey_service,
    get_configured_passkey_service,
    reset_passkey_challenge_cache,
    reset_passkey_service,
)
from app.application.security.user import (
    configure_user_lookups,
    get_configured_user_channel_lookup,
    get_configured_user_id_lookup,
    get_configured_user_name_lookup,
    reset_user_lookups,
)
from app.application.service import (
    configure_service_directory,
    get_service_configs,
    reset_service_directory,
)
from app.application.site.health import (
    configure_site_health_service,
    get_configured_site_health_service,
    reset_site_health_service,
)
from app.application.site.query import (
    configure_site_query_service,
    get_configured_site_query_service,
    reset_site_query_service,
)
from app.application.workflow import (
    configure_workflow_execution,
    get_configured_workflow_execution,
    reset_workflow_execution,
)
from app.runtime.extensions import service_config as service_config_module
from app.runtime.extensions.service_config import (
    configure_service_config_reader,
    reset_service_config_reader,
)
from app.schemas.system import DownloaderConf
from app.schemas.types import ModuleType, SystemConfigKey
from app.startup.initializers import agent as agent_initializer_module
from app.startup.initializers.agent import (
    configure_agent_data_context,
    reset_agent_data_context,
)

_MISSING = object()


def _current_or_missing(getter: Callable[[], Any]) -> Any:
    """读取当前 provider；未配置时返回测试专用哨兵。"""
    try:
        return getter()
    except RuntimeError:
        return _MISSING


def _exercise_direct_provider(
    configure: Callable[[Any], None],
    reset: Callable[[], None],
    getter: Callable[[], Any],
) -> None:
    """验证单对象 provider 的撤销、幂等和跨代隔离合同。"""
    previous = _current_or_missing(getter)
    first = object()
    second = object()
    try:
        configure(first)
        assert getter() is first

        reset()
        reset()
        with pytest.raises(RuntimeError):
            getter()

        configure(second)
        assert getter() is second
        assert getter() is not first
    finally:
        if previous is _MISSING:
            reset()
        else:
            configure(previous)


@pytest.mark.parametrize(
    ("configure", "reset", "getter"),
    [
        (
            configure_agent_task_execution,
            reset_agent_task_execution,
            get_agent_task_execution_service,
        ),
        (configure_auth_service, reset_auth_service, get_configured_auth_service),
        (
            configure_network_test_service,
            reset_network_test_service,
            get_configured_network_test_service,
        ),
        (
            configure_workflow_execution,
            reset_workflow_execution,
            get_configured_workflow_execution,
        ),
        (
            configure_site_query_service,
            reset_site_query_service,
            get_configured_site_query_service,
        ),
        (
            configure_site_health_service,
            reset_site_health_service,
            get_configured_site_health_service,
        ),
    ],
    ids=[
        "agent-task",
        "auth",
        "network",
        "workflow",
        "site-query",
        "site-health",
    ],
)
def test_direct_provider_reset_contract(
    configure: Callable[[Any], None],
    reset: Callable[[], None],
    getter: Callable[[], Any],
) -> None:
    """单对象 owner 在 reset 后稳定失败且新一代身份独立。"""
    _exercise_direct_provider(configure, reset, getter)


def test_agent_chat_provider_reset_contract() -> None:
    """Agent 会话查询和持久化端口可分别撤销并重新装配。"""
    _exercise_direct_provider(
        configure_agent_chat_service,
        reset_agent_chat_service,
        get_configured_agent_chat_service,
    )
    _exercise_direct_provider(
        configure_agent_chat_persistence,
        reset_agent_chat_persistence,
        get_configured_agent_chat_persistence,
    )


def test_agent_data_context_reset_discards_cached_manager() -> None:
    """撤销 Agent 数据上下文时同步丢弃上一代 manager 缓存。"""
    previous_context = agent_initializer_module._agent_data_context
    previous_manager = agent_initializer_module._injected_agent_manager
    first_context = object()
    first_manager = object()
    second_context = object()
    second_manager = object()
    try:
        configure_agent_data_context(first_context)  # type: ignore[arg-type]
        agent_initializer_module._injected_agent_manager = first_manager

        reset_agent_data_context()
        reset_agent_data_context()
        with pytest.raises(RuntimeError):
            agent_initializer_module._get_injected_agent_manager()

        configure_agent_data_context(second_context)  # type: ignore[arg-type]
        agent_initializer_module._injected_agent_manager = second_manager
        assert agent_initializer_module._get_injected_agent_manager() is second_manager
        assert agent_initializer_module._get_injected_agent_manager() is not first_manager
    finally:
        agent_initializer_module._agent_data_context = previous_context
        agent_initializer_module._injected_agent_manager = previous_manager


def test_user_lookup_reset_contract() -> None:
    """三类用户查询函数必须作为同一代身份目录整体撤销。"""
    previous = (
        _current_or_missing(get_configured_user_id_lookup),
        _current_or_missing(get_configured_user_name_lookup),
        _current_or_missing(get_configured_user_channel_lookup),
    )
    first = (lambda _value: "first-id", lambda _value: "first-name", lambda **_values: "first-channel")
    second = (lambda _value: "second-id", lambda _value: "second-name", lambda **_values: "second-channel")
    try:
        configure_user_lookups(by_id=first[0], by_name=first[1], by_channel=first[2])
        assert get_configured_user_id_lookup() is first[0]
        assert get_configured_user_name_lookup() is first[1]
        assert get_configured_user_channel_lookup() is first[2]

        reset_user_lookups()
        reset_user_lookups()
        for getter in (
            get_configured_user_id_lookup,
            get_configured_user_name_lookup,
            get_configured_user_channel_lookup,
        ):
            with pytest.raises(RuntimeError):
                getter()

        configure_user_lookups(by_id=second[0], by_name=second[1], by_channel=second[2])
        assert get_configured_user_id_lookup() is second[0]
        assert get_configured_user_id_lookup() is not first[0]
    finally:
        if any(item is _MISSING for item in previous):
            reset_user_lookups()
        else:
            configure_user_lookups(
                by_id=previous[0],
                by_name=previous[1],
                by_channel=previous[2],
            )


def test_passkey_provider_reset_contract() -> None:
    """PassKey 服务和 challenge 缓存可独立撤销且不串代。"""
    previous_service = _current_or_missing(get_configured_passkey_service)
    previous_cache = PasskeyChallengeStore._cache
    first_service = object()
    second_service = object()
    first_cache = object()
    second_cache = object()
    try:
        configure_passkey_service(first_service)  # type: ignore[arg-type]
        configure_passkey_challenge_cache(first_cache)  # type: ignore[arg-type]
        assert get_configured_passkey_service() is first_service
        assert PasskeyChallengeStore._get_cache() is first_cache

        reset_passkey_service()
        reset_passkey_service()
        reset_passkey_challenge_cache()
        reset_passkey_challenge_cache()
        with pytest.raises(RuntimeError):
            get_configured_passkey_service()
        with pytest.raises(RuntimeError):
            PasskeyChallengeStore._get_cache()

        configure_passkey_service(second_service)  # type: ignore[arg-type]
        configure_passkey_challenge_cache(second_cache)  # type: ignore[arg-type]
        assert get_configured_passkey_service() is second_service
        assert get_configured_passkey_service() is not first_service
        assert PasskeyChallengeStore._get_cache() is second_cache
        assert PasskeyChallengeStore._get_cache() is not first_cache
    finally:
        if previous_service is _MISSING:
            reset_passkey_service()
        else:
            configure_passkey_service(previous_service)
        if previous_cache is None:
            reset_passkey_challenge_cache()
        else:
            configure_passkey_challenge_cache(previous_cache)


def test_security_access_reset_clears_cached_identity() -> None:
    """超级用户载荷 reset 同时清缓存，不能返回上一代身份。"""
    previous = access_module._superuser_token_payload_provider
    first = object()
    second = object()
    try:
        set_superuser_token_payload_provider(lambda: first)  # type: ignore[arg-type]
        assert access_module._create_superuser_token_payload() is first

        reset_superuser_token_payload_provider()
        reset_superuser_token_payload_provider()
        with pytest.raises(HTTPException) as error:
            access_module._create_superuser_token_payload()
        assert error.value.status_code == 503

        set_superuser_token_payload_provider(lambda: second)  # type: ignore[arg-type]
        assert access_module._create_superuser_token_payload() is second
        assert access_module._create_superuser_token_payload() is not first
    finally:
        if previous is None:
            reset_superuser_token_payload_provider()
        else:
            set_superuser_token_payload_provider(previous)


def test_server_service_reset_contract() -> None:
    """中心服务上报与分享用例整体撤销后均稳定失败。"""
    previous = (server_module._server_report_service, server_module._server_sharing_service)
    first = (object(), object())
    second = (object(), object())
    try:
        configure_server_application_services(report_service=first[0], sharing_service=first[1])
        assert MoviePilotServerHelper._report_service() is first[0]
        assert MoviePilotServerHelper._sharing_service() is first[1]

        reset_server_application_services()
        reset_server_application_services()
        with pytest.raises(RuntimeError):
            MoviePilotServerHelper._report_service()
        with pytest.raises(RuntimeError):
            MoviePilotServerHelper._sharing_service()

        configure_server_application_services(report_service=second[0], sharing_service=second[1])
        assert MoviePilotServerHelper._report_service() is second[0]
        assert MoviePilotServerHelper._report_service() is not first[0]
        assert MoviePilotServerHelper._sharing_service() is second[1]
    finally:
        if previous[0] is None or previous[1] is None:
            reset_server_application_services()
        else:
            configure_server_application_services(
                report_service=previous[0],
                sharing_service=previous[1],
            )


def test_existing_network_port_resets_are_reused() -> None:
    """图片和消息入口沿用既有 reset，并保持跨代身份隔离。"""
    try:
        previous_image: Any = image_module._image_ports_snapshot()
    except RuntimeError:
        previous_image = _MISSING
    try:
        previous_ingress = ingress_module._message_ingress_snapshot()
    except RuntimeError:
        previous_ingress = _MISSING
    first = (object(), object(), object())
    second = (object(), object(), object())
    try:
        configure_image_ports(
            transport=first[0],  # type: ignore[arg-type]
            internal_address=first[1],  # type: ignore[arg-type]
        )
        configure_message_ingress_port(first[2])  # type: ignore[arg-type]
        assert image_module._image_ports_snapshot() == first[:2]
        assert ingress_module._message_ingress_snapshot() is first[2]

        reset_image_ports()
        reset_image_ports()
        reset_message_ingress_port()
        reset_message_ingress_port()
        with pytest.raises(RuntimeError):
            image_module._image_ports_snapshot()
        with pytest.raises(RuntimeError):
            ingress_module._message_ingress_snapshot()

        configure_image_ports(
            transport=second[0],  # type: ignore[arg-type]
            internal_address=second[1],  # type: ignore[arg-type]
        )
        configure_message_ingress_port(second[2])  # type: ignore[arg-type]
        assert image_module._image_ports_snapshot() == second[:2]
        assert image_module._image_ports_snapshot() != first[:2]
        assert ingress_module._message_ingress_snapshot() is second[2]
    finally:
        if previous_image is _MISSING:
            reset_image_ports()
        else:
            reset_image_ports(*previous_image)
        reset_message_ingress_port(
            None if previous_ingress is _MISSING else previous_ingress
        )


class _Dispatcher:
    """记录 Outbox 调度器是否被当前 provider 创建并关闭。"""

    def __init__(self) -> None:
        """初始化关闭标记。"""
        self.closed = False

    def dispatch_one(self) -> bool:
        """声明当前没有待处理事件。"""
        return False

    def close(self) -> None:
        """记录短生命周期调度器已经释放。"""
        self.closed = True


def test_outbox_dispatcher_reset_contract() -> None:
    """Outbox reset 后拒绝调度，新一代只创建自己的 dispatcher。"""
    from app.application import outbox as outbox_module

    previous = outbox_module._configured_dispatcher
    first = _Dispatcher()
    second = _Dispatcher()
    try:
        configure_outbox_dispatcher(lambda: first)
        assert dispatch_pending_outbox() == 0
        assert first.closed is True

        reset_outbox_dispatcher()
        reset_outbox_dispatcher()
        with pytest.raises(RuntimeError):
            dispatch_pending_outbox()

        configure_outbox_dispatcher(lambda: second)
        assert dispatch_pending_outbox() == 0
        assert second.closed is True
        assert second is not first
    finally:
        if previous is None:
            reset_outbox_dispatcher()
        else:
            configure_outbox_dispatcher(previous)


def test_transfer_history_repository_reset_contract() -> None:
    """整理历史仓储 factory 撤销后不再暴露旧 lifespan 仓储。"""
    from app.application import history as history_module

    previous = history_module._configured_transfer_history_repository
    first = object()
    second = object()
    try:
        configure_transfer_history_repository(lambda: first)  # type: ignore[arg-type]
        assert get_transfer_history_repository() is first

        reset_transfer_history_repository()
        reset_transfer_history_repository()
        with pytest.raises(RuntimeError):
            get_transfer_history_repository()

        configure_transfer_history_repository(lambda: second)  # type: ignore[arg-type]
        assert get_transfer_history_repository() is second
        assert get_transfer_history_repository() is not first
    finally:
        if previous is None:
            reset_transfer_history_repository()
        else:
            configure_transfer_history_repository(previous)


def test_module_runtime_reset_contract() -> None:
    """模块运行时 reset 恢复拒绝隐式抓取的未装配状态。"""
    previous = _current_or_missing(get_module_manager)
    first = object()
    second = object()
    try:
        configure_module_runtime(lambda: first)  # type: ignore[return-value]
        assert get_module_manager() is first

        reset_module_runtime()
        reset_module_runtime()
        with pytest.raises(RuntimeError):
            get_module_manager()

        configure_module_runtime(lambda: second)  # type: ignore[return-value]
        assert get_module_manager() is second
        assert get_module_manager() is not first
    finally:
        if previous is _MISSING:
            reset_module_runtime()
        else:
            configure_module_runtime(lambda: previous)


def test_service_directory_reset_contract() -> None:
    """服务配置与模块目录作为一代组合整体恢复未装配状态。"""
    previous = (service_module._config_loader, service_module._module_loader)
    first_config = object()
    first_module = object()
    second_config = object()
    second_module = object()
    try:
        configure_service_directory(
            configs=lambda _key, _type: [first_config],
            modules=lambda _type: [first_module],
        )
        assert get_service_configs(SystemConfigKey.Downloaders, DownloaderConf) == [first_config]
        assert service_module._module_loader(ModuleType.Downloader) == [first_module]

        reset_service_directory()
        reset_service_directory()
        with pytest.raises(RuntimeError):
            get_service_configs(SystemConfigKey.Downloaders, DownloaderConf)
        with pytest.raises(RuntimeError):
            service_module._module_loader(ModuleType.Downloader)

        configure_service_directory(
            configs=lambda _key, _type: [second_config],
            modules=lambda _type: [second_module],
        )
        assert get_service_configs(SystemConfigKey.Downloaders, DownloaderConf) == [second_config]
        assert service_module._module_loader(ModuleType.Downloader) == [second_module]
        assert second_config is not first_config
        assert second_module is not first_module
    finally:
        configure_service_directory(configs=previous[0], modules=previous[1])


def test_service_config_reader_reset_contract() -> None:
    """可选配置 reader reset 后恢复空目录且不持有旧配置对象。"""
    previous = service_config_module._service_config_reader
    first = object()
    second = object()
    try:
        configure_service_config_reader(lambda _key: first)
        assert service_config_module._service_config_reader(SystemConfigKey.Downloaders) is first

        reset_service_config_reader()
        reset_service_config_reader()
        assert service_config_module._service_config_reader(SystemConfigKey.Downloaders) is None

        configure_service_config_reader(lambda _key: second)
        assert service_config_module._service_config_reader(SystemConfigKey.Downloaders) is second
        assert service_config_module._service_config_reader(SystemConfigKey.Downloaders) is not first
    finally:
        configure_service_config_reader(previous)


def test_existing_chain_context_reset_contract() -> None:
    """Chain context 继续使用传入 None 的既有 reset 语义。"""
    previous = _current_or_missing(get_chain_runtime_context)
    first = object()
    second = object()
    try:
        configure_chain_runtime_context_provider(lambda: first)  # type: ignore[return-value]
        assert get_chain_runtime_context() is first

        configure_chain_runtime_context_provider(None)
        configure_chain_runtime_context_provider(None)
        with pytest.raises(RuntimeError):
            get_chain_runtime_context()

        configure_chain_runtime_context_provider(lambda: second)  # type: ignore[return-value]
        assert get_chain_runtime_context() is second
        assert get_chain_runtime_context() is not first
    finally:
        if previous is _MISSING:
            configure_chain_runtime_context_provider(None)
        else:
            configure_chain_runtime_context_provider(lambda: previous)


def test_wallpaper_provider_reset_contract() -> None:
    """壁纸 reset 恢复空来源并清掉上一代 provider 的缓存结果。"""
    previous = (
        image_module._tmdb_wallpaper_provider,
        image_module._tmdb_wallpaper_list_provider,
        image_module._mediaserver_wallpaper_provider,
        image_module._mediaserver_wallpaper_list_provider,
    )
    helper = WallpaperHelper()
    try:
        configure_wallpaper_providers(
            tmdb_wallpaper=lambda: "first-tmdb",
            tmdb_wallpapers=lambda _count: ["first-tmdb"],
            mediaserver_wallpaper=lambda: "first-media",
            mediaserver_wallpapers=lambda _count: ["first-media"],
        )
        assert helper.get_tmdb_wallpaper() == "first-tmdb"
        assert helper.get_mediaserver_wallpaper() == "first-media"

        reset_wallpaper_providers()
        reset_wallpaper_providers()
        assert helper.get_tmdb_wallpaper() is None
        assert helper.get_tmdb_wallpapers() == []
        assert helper.get_mediaserver_wallpaper() is None
        assert helper.get_mediaserver_wallpapers() == []

        configure_wallpaper_providers(
            tmdb_wallpaper=lambda: "second-tmdb",
            tmdb_wallpapers=lambda _count: ["second-tmdb"],
            mediaserver_wallpaper=lambda: "second-media",
            mediaserver_wallpapers=lambda _count: ["second-media"],
        )
        assert helper.get_tmdb_wallpaper() == "second-tmdb"
        assert helper.get_mediaserver_wallpaper() == "second-media"
    finally:
        configure_wallpaper_providers(
            tmdb_wallpaper=previous[0],
            tmdb_wallpapers=previous[1],
            mediaserver_wallpaper=previous[2],
            mediaserver_wallpapers=previous[3],
        )
