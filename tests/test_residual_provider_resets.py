"""验证剩余启动 provider 可撤销、幂等且不会跨 lifespan 串用对象。"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app.adapters.system import resource as resource_module
from app.adapters.system.resource import (
    configure_resource_version_provider,
    reset_resource_version_provider,
)
from app.agent.tools.manager import MoviePilotToolsManager
from app.application.database import (
    configure_database_governance,
    get_database_governance,
    reset_database_governance,
)
from app.foundation.singleton import Singleton
from app.runtime import resources as managed_resource_facade
from app.runtime.events import Event, EventHandlerBinding, EventManager
from app.runtime.resources import (
    configure_managed_resource_runtime,
    reset_managed_resource_runtime,
)
from app.runtime.state import SystemHelper
from app.scheduler.facade import Scheduler
from app.schemas.event import ConfigChangeEventData
from app.schemas.types import EventType
from app.startup.composition import resource as managed_resource_composition
from app.startup.initializers.modules import reset_event_services

_MISSING = object()


@pytest.fixture
def database_governance_state() -> Iterator[None]:
    """保存并恢复进程级数据库治理门面，避免污染其它测试。"""
    try:
        previous: Any = get_database_governance()
    except RuntimeError:
        previous = _MISSING
    yield
    reset_database_governance()
    if previous is not _MISSING:
        configure_database_governance(previous)


def test_database_governance_reset_contract(database_governance_state: None) -> None:
    """数据库治理 reset 后拒绝旧对象，并允许下一代重新装配。"""
    first = object()
    second = object()

    configure_database_governance(first)  # type: ignore[arg-type]
    assert get_database_governance() is first

    reset_database_governance()
    reset_database_governance()
    with pytest.raises(RuntimeError, match="数据库治理服务尚未配置"):
        get_database_governance()

    configure_database_governance(second)  # type: ignore[arg-type]
    assert get_database_governance() is second
    assert get_database_governance() is not first


def test_resource_version_provider_reset_contract() -> None:
    """资源版本 reset 恢复空版本，下一代不得调用上一代 provider。"""
    previous = resource_module._resource_version_provider
    calls: list[str] = []
    try:
        configure_resource_version_provider(
            lambda: calls.append("first") or ("1.0", "1.1")
        )
        assert resource_module._resource_version_provider() == ("1.0", "1.1")

        reset_resource_version_provider()
        reset_resource_version_provider()
        assert resource_module._resource_version_provider() == ("0", "0")

        configure_resource_version_provider(
            lambda: calls.append("second") or ("2.0", "2.1")
        )
        assert resource_module._resource_version_provider() == ("2.0", "2.1")
        assert calls == ["first", "second"]
    finally:
        configure_resource_version_provider(previous)


def test_tool_manager_data_context_reset_contract() -> None:
    """工具管理器 reset 应清除旧上下文和由它物化的工具目录。"""
    manager = MoviePilotToolsManager()
    first = object()
    second = object()

    manager.set_data_context(first)  # type: ignore[arg-type]
    manager.tools = [object()]
    manager.catalog = object()  # type: ignore[assignment]
    manager._catalog_materialized = True
    manager._catalog_managed_by_factory = True

    manager.reset_data_context()
    manager.reset_data_context()
    assert manager._data is None
    assert manager.tools == []
    assert manager.catalog is None
    assert manager._plugin_agent_tools_revision == -1
    assert manager._catalog_materialized is False

    manager.set_data_context(second)  # type: ignore[arg-type]
    assert manager._data is second
    assert manager._data is not first


def test_scheduler_runtime_binding_reset_contract() -> None:
    """停止态 Scheduler reset 后拒绝旧仓储和服务，并允许下一代装配。"""
    scheduler = object.__new__(Scheduler)
    Scheduler.__init__(scheduler)
    first_tasks = object()
    first_services = object()
    second_tasks = object()
    second_services = object()

    scheduler.configure_agent_tasks(first_tasks)  # type: ignore[arg-type]
    scheduler.configure_services(first_services)  # type: ignore[arg-type]
    assert scheduler._agent_task_repository() is first_tasks
    assert scheduler._scheduler_services() is first_services

    scheduler.reset_runtime_bindings()
    scheduler.reset_runtime_bindings()
    with pytest.raises(RuntimeError, match="AgentTask 仓储尚未注入"):
        scheduler._agent_task_repository()
    with pytest.raises(RuntimeError, match="业务能力尚未注入"):
        scheduler._scheduler_services()

    scheduler.configure_agent_tasks(second_tasks)  # type: ignore[arg-type]
    scheduler.configure_services(second_services)  # type: ignore[arg-type]
    assert scheduler._agent_task_repository() is second_tasks
    assert scheduler._scheduler_services() is second_services
    assert scheduler._agent_task_repository() is not first_tasks


def test_event_manager_host_binding_reset_contract() -> None:
    """事件总线可分别撤销 resolver、错误通知和宿主 listener。"""
    manager = object.__new__(EventManager)
    EventManager.__init__(manager)

    def first_resolver(_owner: type) -> None:
        """代表上一 lifespan 的实例解析器。"""

    def second_resolver(_owner: type) -> None:
        """代表下一 lifespan 的实例解析器。"""

    def first_notifier(_title: str, _message: str) -> None:
        """代表上一 lifespan 的错误通知器。"""

    def second_notifier(_title: str, _message: str) -> None:
        """代表下一 lifespan 的错误通知器。"""

    def listener(_event: object) -> None:
        """提供可按稳定标识注册和撤销的测试 listener。"""

    manager.register_handler_instance_resolver("host", first_resolver)
    manager.set_error_notifier(first_notifier)
    manager.add_event_listener(EventType.NoticeMessage, listener)

    manager.unregister_handler_instance_resolver("host")
    manager.unregister_handler_instance_resolver("host")
    manager.reset_error_notifier()
    manager.reset_error_notifier()
    manager.remove_event_listener(EventType.NoticeMessage, listener)
    manager.remove_event_listener(EventType.NoticeMessage, listener)
    assert manager._EventManager__handler_instance_resolvers == {}
    assert manager._EventManager__error_notifier is None
    assert not manager._EventManager__broadcast_subscribers.get(EventType.NoticeMessage)

    manager.register_handler_instance_resolver("host", second_resolver)
    manager.set_error_notifier(second_notifier)
    assert manager._EventManager__handler_instance_resolvers["host"] is second_resolver
    assert manager._EventManager__error_notifier is second_notifier
    assert second_resolver is not first_resolver


def test_reset_event_services_unregisters_lifespan_resolvers(monkeypatch) -> None:
    """模块 lifespan 结束时应撤销宿主与配置 owner resolver。"""
    manager = object.__new__(EventManager)
    EventManager.__init__(manager)
    singleton_key = (EventManager, (), frozenset())
    monkeypatch.setitem(Singleton._instances, singleton_key, manager)
    owner = object.__new__(SystemHelper)
    reload_config = Mock()
    owner.on_config_changed = reload_config

    def plugin_resolver(_owner: type) -> None:
        """代表插件运行时独立拥有的 resolver。"""

    def config_resolver(owner_class: type) -> EventHandlerBinding | None:
        """代表当前 lifespan 持有的配置 owner resolver。"""
        if owner_class is SystemHelper:
            return EventHandlerBinding(
                instance=owner,
                owner_name=owner_class.__name__,
            )
        return None

    manager.register_handler_instance_resolver("plugins", plugin_resolver)
    manager.register_handler_instance_resolver("host", lambda _owner: None)
    manager.register_handler_instance_resolver(
        "config_reload",
        config_resolver,
    )
    event = Event(
        EventType.ConfigChanged,
        ConfigChangeEventData(key={"DEBUG"}),
    )
    manager._EventManager__invoke_handler_by_type_sync(
        SystemHelper.handle_config_changed,
        event,
    )

    reset_event_services()
    manager._EventManager__invoke_handler_by_type_sync(
        SystemHelper.handle_config_changed,
        event,
    )

    assert manager._EventManager__handler_instance_resolvers == {
        "plugins": plugin_resolver,
    }
    reload_config.assert_called_once_with()


def test_managed_resource_owner_reset_releases_only_closed_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """托管资源 reset 只释放已关闭引用，不把释放动作伪装为完整重启。"""
    first = SimpleNamespace(is_shutdown=True)
    second = SimpleNamespace(is_shutdown=True)
    monkeypatch.setattr(managed_resource_composition, "_managed_resource_runtime", first)
    monkeypatch.setattr(managed_resource_facade, "_managed_resource_runtime", first)

    managed_resource_composition.reset_managed_resource_composition()
    managed_resource_composition.reset_managed_resource_composition()
    assert managed_resource_composition._managed_resource_runtime is None
    assert managed_resource_facade._managed_resource_runtime is None

    configure_managed_resource_runtime(second)  # type: ignore[arg-type]
    assert managed_resource_facade._managed_resource_runtime is second
    assert managed_resource_facade._managed_resource_runtime is not first
    reset_managed_resource_runtime()


def test_managed_resource_owner_refuses_to_release_live_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """托管资源仍活动时 reset 必须保留 owner，避免隐藏未收口资源。"""
    runtime = SimpleNamespace(is_shutdown=False)
    monkeypatch.setattr(managed_resource_composition, "_managed_resource_runtime", runtime)
    monkeypatch.setattr(managed_resource_facade, "_managed_resource_runtime", runtime)

    with pytest.raises(RuntimeError, match="尚未关闭"):
        managed_resource_composition.reset_managed_resource_composition()

    assert managed_resource_composition._managed_resource_runtime is runtime
    assert managed_resource_facade._managed_resource_runtime is runtime
