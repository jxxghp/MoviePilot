"""MoviePilot Server 生产组合根测试。"""

from types import SimpleNamespace

import pytest

import app.startup.composition.server as server_composition
from app.schemas.types import SystemConfigKey


class _SystemConfig:
    """记录中心服务组合回调触发的配置读写。"""

    def __init__(self) -> None:
        """初始化插件清单和读写调用记录。"""
        self.values = {SystemConfigKey.UserInstalledPlugins: ["TestPlugin"]}
        self.calls: list[tuple[str, object, object]] = []

    def get(self, key):
        """读取测试配置并记录访问。"""
        self.calls.append(("get", key, None))
        return self.values.get(key)

    def set(self, key, value):
        """同步写入测试配置并记录访问。"""
        self.calls.append(("set", key, value))
        self.values[key] = value
        return True

    async def async_set(self, key, value):
        """异步写入测试配置并记录访问。"""
        self.calls.append(("async_set", key, value))
        self.values[key] = value
        return True


class _SubscriptionRepository:
    """提供同步和异步订阅读取端口的测试仓储。"""

    def list(self):
        """返回同步订阅快照。"""
        return ["sync-list"]

    async def async_list(self):
        """返回异步订阅快照。"""
        return ["async-list"]

    def get(self, subscribe_id):
        """返回同步订阅标记。"""
        return ("sync-subscribe", subscribe_id)

    async def async_get(self, subscribe_id):
        """返回异步订阅标记。"""
        return ("async-subscribe", subscribe_id)


class _WorkflowQuery:
    """提供同步和异步工作流读取端口。"""

    def get_sync(self, workflow_id):
        """返回同步工作流标记。"""
        return ("sync-workflow", workflow_id)

    async def get(self, workflow_id):
        """返回异步工作流标记。"""
        return ("async-workflow", workflow_id)


@pytest.mark.asyncio
async def test_server_composition_wires_lazy_local_and_transport_ports(
    monkeypatch,
) -> None:
    """生产组合仅保存本地与传输回调，构造阶段不得读取配置或发起请求。"""
    configured = {}
    system_config = _SystemConfig()
    configuration_resolutions = []
    transport_calls = []

    def get_system_config():
        """记录配置服务只在回调执行时解析。"""
        configuration_resolutions.append(True)
        return system_config

    def capture_services(*, report_service, sharing_service):
        """捕获生产组合登记的两个应用服务。"""
        configured["report"] = report_service
        configured["sharing"] = sharing_service

    def sync_transport(*args, **kwargs):
        """记录同步中心服务传输调用。"""
        transport_calls.append(("sync", args, kwargs))
        return SimpleNamespace(status_code=200)

    async def async_transport(*args, **kwargs):
        """记录异步中心服务传输调用。"""
        transport_calls.append(("async", args, kwargs))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        server_composition,
        "get_configured_system_config",
        get_system_config,
    )
    monkeypatch.setattr(
        server_composition,
        "configure_server_application_services",
        capture_services,
    )
    helper = server_composition.MoviePilotServerHelper
    for name in (
        "plugin_install_report",
        "subscribe_report",
        "subscribe_share",
        "workflow_share",
    ):
        monkeypatch.setattr(helper, name, sync_transport)
    for name in (
        "async_plugin_install_report",
        "async_subscribe_report",
        "async_subscribe_share",
        "async_workflow_share",
    ):
        monkeypatch.setattr(helper, name, async_transport)
    monkeypatch.setattr(helper, "sanitize_plugin_repo_url", lambda value: value)
    monkeypatch.setattr(helper, "get_user_uuid", lambda: "user-uuid")
    monkeypatch.setattr(helper, "_handle_response", lambda _response, _clear: (True, "ok"))
    monkeypatch.setattr(helper, "_clear_subscribe_share_cache", lambda: None)
    monkeypatch.setattr(helper, "_clear_workflow_share_cache", lambda: None)

    subscriptions = _SubscriptionRepository()
    workflows = _WorkflowQuery()
    server_composition.configure_server_services(workflows, subscriptions)

    assert configuration_resolutions == []
    assert system_config.calls == []
    assert transport_calls == []

    report = configured["report"]
    sharing = configured["sharing"]
    config_key = SystemConfigKey.UserInstalledPlugins
    assert report._config_reader(config_key) == ["TestPlugin"]
    assert report._config_writer(config_key, ["NextPlugin"]) is True
    assert await report._async_config_writer(config_key, ["AsyncPlugin"]) is True
    assert report._installed_plugins_provider() == ["AsyncPlugin"]
    assert report._subscribes_provider() == ["sync-list"]
    assert await report._async_subscribes_provider() == ["async-list"]
    assert sharing._subscribe_provider(7) == ("sync-subscribe", 7)
    assert await sharing._async_subscribe_provider(8) == ("async-subscribe", 8)
    assert sharing._workflow_provider(9) == ("sync-workflow", 9)
    assert await sharing._async_workflow_provider(10) == ("async-workflow", 10)
    assert sharing._user_uuid_provider() == "user-uuid"
    assert len(configuration_resolutions) == 4
    assert transport_calls == []
