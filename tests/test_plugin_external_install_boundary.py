"""插件包安装的外部调用边界测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.adapters.external import market
from app.adapters.external.market import PluginHelper
from app.adapters.system.plugin.package import PluginPackageManager
from app.agent.tools.impl import _plugin_tool_utils
from app.api.endpoints import plugin as plugin_endpoint
from app.runtime.config import global_vars
from app.schemas.plugin import PluginSourceChangeRequest, PluginSourceIdentity
from app.startup.initializers import plugins as plugins_initializer

REPO_URL = "https://github.com/example/moviepilot-plugins"


def test_package_manager_sync_preserves_external_install_contract() -> None:
    """同步包适配器必须调用包级入口，不能再次进入公开 Gateway。"""
    helper = Mock()
    helper._PluginHelper__install_package.return_value = (True, "installed")
    manager = PluginPackageManager(helper=helper)

    result = manager.install(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        release_version="1.2.3",
        force_install=False,
    )

    assert result == (True, "installed")
    helper._PluginHelper__install_package.assert_called_once_with(
        pid="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        release_version="1.2.3",
        force_install=False,
    )
    helper.install.assert_not_called()


@pytest.mark.asyncio
async def test_package_manager_async_preserves_external_install_contract() -> None:
    """异步包适配器必须调用包级入口，不能再次进入公开 Gateway。"""
    helper = Mock()
    helper._PluginHelper__async_install_package = AsyncMock(return_value=(True, "installed"))
    manager = PluginPackageManager(helper=helper)

    result = await manager.async_install(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        release_version="1.2.3",
        force_install=False,
    )

    assert result == (True, "installed")
    helper._PluginHelper__async_install_package.assert_awaited_once_with(
        pid="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        release_version="1.2.3",
        force_install=False,
    )
    helper.async_install.assert_not_called()


def test_external_sync_helper_rejects_until_gateway_is_configured(
    monkeypatch,
) -> None:
    """外部同步入口在宿主未装配来源门禁时不得直接写入插件包。"""
    helper = PluginHelper()
    monkeypatch.setattr(
        market,
        "_plugin_install_gateway",
        market._unconfigured_plugin_install_gateway,
    )

    success, message = helper.install(
        "DemoPlugin",
        REPO_URL,
        "v3",
        "1.2.3",
        True,
    )
    assert success is False
    assert message


@pytest.mark.asyncio
async def test_external_async_helper_rejects_until_gateway_is_configured(
    monkeypatch,
) -> None:
    """外部异步入口在宿主未装配来源门禁时不得直接写入插件包。"""
    helper = PluginHelper()
    monkeypatch.setattr(
        market,
        "_async_plugin_install_gateway",
        market._unconfigured_async_plugin_install_gateway,
    )

    success, message = await helper.async_install(
        "DemoPlugin",
        REPO_URL,
        "v3",
        "1.2.3",
        True,
    )
    assert success is False
    assert message


def test_external_sync_helper_uses_configured_gateway(monkeypatch) -> None:
    """外部同步调用必须把所有参数交给宿主统一安装用例。"""
    gateway = Mock(return_value=(False, "source conflict"))
    monkeypatch.setattr(market, "_plugin_install_gateway", gateway)

    result = PluginHelper().install("DemoPlugin", REPO_URL, "v3", "1.2.3", True)

    assert result == (False, "source conflict")
    gateway.assert_called_once_with("DemoPlugin", REPO_URL, "v3", "1.2.3", True)


def test_sync_gateway_returns_failure_when_runtime_loop_is_unavailable(
    monkeypatch,
) -> None:
    """主事件循环释放后，同步兼容入口应稳定返回失败结果。"""
    gateway = Mock()
    monkeypatch.setattr(global_vars, "CURRENT_EVENT_LOOP", None)

    result = plugins_initializer._run_plugin_install_sync(
        gateway,
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        package_version="v3",
        release_version="1.2.3",
        force=False,
        local_sync=False,
        explicit_source=True,
    )

    assert result == (False, "插件安装服务当前不可用")
    gateway.install.assert_not_called()


@pytest.mark.asyncio
async def test_external_async_helper_uses_configured_gateway(monkeypatch) -> None:
    """外部异步调用必须把所有参数交给宿主统一安装用例。"""

    async def gateway(*args):
        """返回统一 Gateway 的结果。"""
        seen.append(args)
        return False, "source conflict"

    seen = []
    monkeypatch.setattr(market, "_async_plugin_install_gateway", gateway)

    result = await PluginHelper().async_install(
        "DemoPlugin",
        REPO_URL,
        "v3",
        "1.2.3",
        True,
    )

    assert result == (False, "source conflict")
    assert seen == [("DemoPlugin", REPO_URL, "v3", "1.2.3", True)]


@pytest.mark.asyncio
async def test_external_async_helper_preserves_failure_tuple_on_gateway_error(
    monkeypatch,
) -> None:
    """公开异步 Helper 在持久化等内部异常下仍返回兼容二元组。"""
    gateway = Mock()
    gateway.install = AsyncMock(side_effect=RuntimeError("persistence unavailable"))

    async def install(*args):
        """按组合根的真实参数映射进入公开异步兼容包装层。"""
        plugin_id, repo_url, package_version, release_version, force = args
        return await plugins_initializer._run_plugin_install_async(
            gateway,
            plugin_id=plugin_id,
            repo_url=repo_url,
            package_version=package_version,
            release_version=release_version,
            force=force,
            local_sync=False,
            explicit_source=bool(repo_url),
        )

    monkeypatch.setattr(market, "_async_plugin_install_gateway", install)

    result = await PluginHelper().async_install(
        "DemoPlugin",
        REPO_URL,
        "v3",
        "1.2.3",
        True,
    )

    assert result == (False, "persistence unavailable")


@pytest.mark.asyncio
async def test_http_install_uses_application_gateway(
    monkeypatch,
) -> None:
    """HTTP 安装入口只能转发到唯一 Application Gateway。"""
    gateway = Mock()
    gateway.install = AsyncMock(
        return_value=SimpleNamespace(success=True, message="")
    )
    monkeypatch.setattr(
        plugin_endpoint,
        "get_plugin_install_service",
        lambda: gateway,
    )

    result = await plugin_endpoint.install(
        "DemoPlugin",
        REPO_URL,
        "1.2.3",
        False,
        None,
    )

    assert result.success is True
    gateway.install.assert_awaited_once_with(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        release_version="1.2.3",
        force=False,
        explicit_source=True,
    )


@pytest.mark.asyncio
async def test_http_source_change_requires_revision_and_explicit_gateway_mode(
    monkeypatch,
) -> None:
    """管理员换源入口必须把目标仓库和精确 revision 交给统一 Gateway。"""
    gateway = Mock()
    gateway.install = AsyncMock(
        return_value=SimpleNamespace(success=True, message="")
    )
    monkeypatch.setattr(
        plugin_endpoint,
        "get_plugin_install_service",
        lambda: gateway,
    )

    result = await plugin_endpoint.change_plugin_source(
        "DemoPlugin",
        PluginSourceChangeRequest(
            repo_url=REPO_URL,
            expected_revision=7,
            release_version="1.2.3",
        ),
        None,
    )

    assert result.success is True
    gateway.install.assert_awaited_once_with(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        release_version="1.2.3",
        force=True,
        explicit_source=True,
        source_change=True,
        expected_revision=7,
    )


@pytest.mark.asyncio
async def test_http_source_identity_returns_current_cas_evidence(
    monkeypatch,
) -> None:
    """来源查询只公开确认和显式换源所需的最小身份字段。"""
    identity = SimpleNamespace(
        plugin_id="DemoPlugin",
        trusted_source_type=SimpleNamespace(value="official"),
        trusted_source_key="github:jxxghp/moviepilot-plugins",
        binding_basis=SimpleNamespace(value="official_default"),
        payload_source_type=SimpleNamespace(value="local"),
        payload_source_key=None,
        revision=7,
    )
    persistence = Mock()
    persistence.get_identity = AsyncMock(return_value=identity)
    monkeypatch.setattr(
        plugin_endpoint,
        "get_plugin_persistence",
        lambda: persistence,
    )

    result = await plugin_endpoint.get_plugin_source_identity(
        "DemoPlugin",
        None,
    )

    assert result.success is True
    assert isinstance(result.data, PluginSourceIdentity)
    assert result.data.plugin_id == "DemoPlugin"
    assert result.data.trusted_source_key == "github:jxxghp/moviepilot-plugins"
    assert result.data.payload_source_type == "local"
    assert result.data.revision == 7


def test_source_change_schema_rejects_invalid_revision_and_blank_repo() -> None:
    """显式换源请求在进入业务层前拒绝无来源或无效 revision。"""
    with pytest.raises(ValidationError):
        PluginSourceChangeRequest(repo_url="  ", expected_revision=1)
    with pytest.raises(ValidationError):
        PluginSourceChangeRequest(repo_url=REPO_URL, expected_revision=0)


def test_source_change_openapi_uses_structured_request_body() -> None:
    """换源 API 必须公开稳定的 Pydantic 请求与响应模型。"""
    app = FastAPI()
    app.include_router(plugin_endpoint.router, prefix="/api/v1/plugin")

    operation = app.openapi()["paths"]["/api/v1/plugin/source/{plugin_id}"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]

    assert request_schema["$ref"].endswith("/PluginSourceChangeRequest")


@pytest.mark.asyncio
async def test_agent_install_uses_application_gateway(
    monkeypatch,
) -> None:
    """Agent 安装入口只能转发到唯一 Application Gateway。"""
    gateway = Mock()
    gateway.install = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            message="installed",
            refreshed_only=False,
        )
    )
    monkeypatch.setattr(
        _plugin_tool_utils,
        "get_plugin_install_service",
        lambda: gateway,
    )

    result = await _plugin_tool_utils.install_plugin_runtime(
        "DemoPlugin",
        REPO_URL,
        force=False,
    )

    assert result == (True, "installed", False)
    gateway.install.assert_awaited_once_with(
        plugin_id="DemoPlugin",
        repo_url=REPO_URL,
        force=False,
        explicit_source=True,
    )


def test_startup_composition_configures_external_helper_gateway(monkeypatch) -> None:
    """启动组合根必须向 Application 与公开 Helper 发布同一 Gateway。"""
    helper = Mock()
    gateway_calls = []
    application_calls = []
    gateway = Mock()
    sync_runner = Mock(return_value=(True, "installed"))
    async_runner = AsyncMock(return_value=(True, "installed"))

    monkeypatch.setattr(plugins_initializer, "PluginHelper", lambda: helper)
    monkeypatch.setattr(
        plugins_initializer,
        "PluginMarketClient",
        lambda _helper: Mock(),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "PluginPackageManager",
        lambda _helper: Mock(),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "PluginCandidateInventoryReader",
        lambda **_kwargs: Mock(),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "PluginInstallCommand",
        lambda **_kwargs: Mock(),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "PluginInstallGateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        plugins_initializer,
        "PluginInstallationRecoveryService",
        lambda **_kwargs: Mock(),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "PluginIdentityMigrationService",
        lambda **_kwargs: Mock(),
    )
    monkeypatch.setattr(plugins_initializer, "get_plugin_manager", Mock())
    monkeypatch.setattr(plugins_initializer, "get_plugin_persistence", Mock())
    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_install_service",
        application_calls.append,
    )
    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_installation_recovery",
        Mock(),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_identity_migration",
        Mock(),
    )
    monkeypatch.setattr(
        plugins_initializer,
        "_run_plugin_install_sync",
        sync_runner,
    )
    monkeypatch.setattr(
        plugins_initializer,
        "_run_plugin_install_async",
        async_runner,
    )
    monkeypatch.setattr(
        plugins_initializer,
        "PluginDependencyInstaller",
        lambda *_args, **_kwargs: Mock(),
    )
    for name in (
        "configure_plugin_legacy_import_services",
        "configure_plugin_resource_import_preparer",
        "configure_site_auth_level_provider",
        "configure_installed_plugins_provider",
        "configure_plugin_catalog_factory",
        "configure_plugin_route_refresher",
        "configure_plugin_system",
        "configure_plugin_storage",
    ):
        monkeypatch.setattr(plugins_initializer, name, Mock())

    def configure_gateway(**kwargs) -> None:
        """记录组合根提供给外部 Helper 的同步/异步端口。"""
        gateway_calls.append(kwargs)

    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_install_gateway",
        configure_gateway,
        raising=False,
    )

    plugins_initializer.configure_plugin_services()

    assert application_calls == [gateway]
    assert len(gateway_calls) == 1
    assert callable(gateway_calls[0]["install"])
    assert callable(gateway_calls[0]["async_install"])

    assert gateway_calls[0]["install"](
        "DemoPlugin",
        REPO_URL,
        "v3",
        "1.2.3",
        False,
    ) == (True, "installed")
    assert asyncio.run(
        gateway_calls[0]["async_install"](
            "DemoPlugin",
            REPO_URL,
            "v3",
            "1.2.3",
            False,
        )
    ) == (True, "installed")
    expected = {
        "plugin_id": "DemoPlugin",
        "repo_url": REPO_URL,
        "package_version": "v3",
        "release_version": "1.2.3",
        "force": False,
        "local_sync": False,
        "explicit_source": True,
    }
    sync_runner.assert_called_once_with(gateway, **expected)
    async_runner.assert_awaited_once_with(gateway, **expected)
