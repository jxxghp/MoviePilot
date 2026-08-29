import asyncio
import json
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.impl._plugin_tool_utils import (
    install_plugin_runtime,
    uninstall_plugin_runtime,
)
from app.agent.tools.impl.install_plugin import InstallPluginInput, InstallPluginTool
from app.agent.tools.impl.query_installed_plugins import QueryInstalledPluginsTool
from app.agent.tools.impl.query_market_plugins import QueryMarketPluginsTool
from app.agent.tools.impl.query_plugin_config import QueryPluginConfigTool
from app.agent.tools.impl.query_plugin_data import QueryPluginDataTool
from app.agent.tools.impl.reload_plugin import ReloadPluginTool
from app.agent.tools.impl.uninstall_plugin import UninstallPluginTool
from app.agent.tools.impl.update_plugin_config import UpdatePluginConfigTool
from app.runtime.extensions.plugin.admission import (
    PluginMutationAdmission,
    PluginMutationRejectedError,
)
from app.schemas.plugin import PluginRuntimeStatus


def _plugin_snapshot(state: bool = True) -> dict:
    """
    构造插件运行态快照。
    """
    return {
        "plugin_id": "DemoPlugin",
        "plugin_name": "Demo Plugin",
        "plugin_version": "1.0.0",
        "state": state,
    }


def _market_plugin(
    plugin_id: str,
    plugin_name: str,
    installed: bool = False,
    repo_url: Optional[str] = "https://example.com/market",
) -> SimpleNamespace:
    """
    构造插件市场或已安装插件摘要对象。
    """
    return SimpleNamespace(
        id=plugin_id,
        plugin_name=plugin_name,
        plugin_desc=f"{plugin_name} description",
        plugin_version="1.0.0",
        plugin_author="author",
        installed=installed,
        has_update=False,
        state=installed,
        repo_url=repo_url,
        add_time=1,
    )


def test_query_market_plugins_filters_candidates() -> None:
    """
    查询插件市场时会按关键字返回匹配候选。
    """
    tool = QueryMarketPluginsTool(session_id="session-1", user_id="10001")
    plugins = [
        _market_plugin("DemoPlugin", "Demo Plugin"),
        _market_plugin("OtherPlugin", "Other Plugin"),
    ]

    with patch(
        "app.agent.tools.impl.query_market_plugins.load_market_plugins",
        new=AsyncMock(return_value=plugins),
    ):
        result = asyncio.run(tool.run(query="demo"))

    payload = json.loads(result)
    assert payload["success"]
    assert payload["match_count"] == 1
    assert payload["plugins"][0]["id"] == "DemoPlugin"


def test_query_installed_plugins_filters_candidates() -> None:
    """
    查询已安装插件时会按关键字返回匹配候选。
    """
    tool = QueryInstalledPluginsTool(session_id="session-1", user_id="10001")
    plugins = [
        _market_plugin("DemoPlugin", "Demo Plugin", installed=True),
        _market_plugin("OtherPlugin", "Other Plugin", installed=True),
    ]

    with patch(
        "app.agent.tools.impl.query_installed_plugins.list_installed_plugins",
        return_value=plugins,
    ):
        result = asyncio.run(tool.run(query="demo"))

    payload = json.loads(result)
    assert payload["success"]
    assert payload["match_count"] == 1
    assert payload["plugins"][0]["id"] == "DemoPlugin"


def test_query_installed_plugins_fills_missing_repo_url_from_market() -> None:
    """
    已安装插件缺少来源地址时，会从插件市场元数据补齐 repo_url。
    """
    tool = QueryInstalledPluginsTool(session_id="session-1", user_id="10001")
    installed_plugin = _market_plugin(
        "DemoPlugin", "Demo Plugin", installed=True, repo_url=None
    )
    market_plugin = _market_plugin(
        "DemoPlugin",
        "Demo Plugin",
        installed=True,
        repo_url="https://github.com/demo/plugins",
    )
    plugin_manager = MagicMock()
    plugin_manager.get_local_repo_plugins.return_value = []
    plugin_manager.async_get_online_plugins = AsyncMock(return_value=[market_plugin])

    with (
        patch(
            "app.agent.tools.impl.query_installed_plugins.list_installed_plugins",
            return_value=[installed_plugin],
        ),
        patch(
            "app.agent.tools.impl._plugin_tool_utils.get_plugin_manager",
            return_value=plugin_manager,
        ),
    ):
        result = asyncio.run(tool.run(query="demo"))

    payload = json.loads(result)
    assert payload["success"]
    assert payload["plugins"][0]["repo_url"] == "https://github.com/demo/plugins"
    plugin_manager.async_get_online_plugins.assert_awaited_once_with(force=False)


def test_query_plugin_config_returns_saved_config_and_default_model() -> None:
    """
    查询插件配置会返回保存值和默认配置模型。
    """
    tool = QueryPluginConfigTool(session_id="session-1", user_id="10001")
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_config.return_value = {"enabled": True}
    plugin_instance = MagicMock()
    plugin_instance.get_form.return_value = (None, {"enabled": False, "interval": 10})
    plugin_manager.running_plugins = {"DemoPlugin": plugin_instance}

    with (
        patch(
            "app.agent.tools.impl.query_plugin_config.get_plugin_snapshot",
            return_value=_plugin_snapshot(),
        ),
        patch(
            "app.agent.tools.impl.query_plugin_config.get_plugin_manager",
            return_value=plugin_manager,
        ),
    ):
        result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

    payload = json.loads(result)
    assert payload["success"]
    assert payload["config"] == {"enabled": True}
    assert payload["default_model"] == {"enabled": False, "interval": 10}


def test_update_plugin_config_merges_and_removes_keys_without_reloading() -> None:
    """
    更新插件配置会合并新增键并移除指定旧键。
    """
    tool = UpdatePluginConfigTool(session_id="session-1", user_id="10001")
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_config.return_value = {
        "enabled": False,
        "interval": 30,
        "token": "legacy-token",
    }
    plugin_manager.async_save_plugin_config = AsyncMock(return_value=True)

    with (
        patch(
            "app.agent.tools.impl.update_plugin_config.get_plugin_snapshot",
            return_value=_plugin_snapshot(),
        ),
        patch(
            "app.agent.tools.impl.update_plugin_config.get_plugin_manager",
            return_value=plugin_manager,
        ),
    ):
        result = asyncio.run(
            tool.run(
                plugin_id="DemoPlugin",
                updates={"enabled": True},
                remove_keys=["token"],
            )
        )

    payload = json.loads(result)
    assert payload["success"]
    assert payload["config_requires_reload"]
    assert payload["saved_config"] == {"enabled": True, "interval": 30}
    plugin_manager.async_save_plugin_config.assert_awaited_once_with(
        "DemoPlugin",
        {"enabled": True, "interval": 30},
    )


def test_reload_plugin_triggers_runtime_refresh() -> None:
    """
    重载插件工具会调用运行态刷新流程。
    """
    tool = ReloadPluginTool(session_id="session-1", user_id="10001")

    with (
        patch(
            "app.agent.tools.impl.reload_plugin.get_plugin_snapshot",
            side_effect=[_plugin_snapshot(), _plugin_snapshot(state=False)],
        ),
        patch(
            "app.agent.tools.impl.reload_plugin.reload_plugin_runtime"
        ) as reload_plugin_runtime,
    ):
        reload_plugin_runtime.return_value = PluginRuntimeStatus.ACTIVE
        result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

    payload = json.loads(result)
    assert payload["success"]
    assert not payload["state"]
    reload_plugin_runtime.assert_called_once_with("DemoPlugin")


def test_reload_plugin_reports_runtime_failure() -> None:
    """重载未进入 active 时不得继续向智能体报告成功。"""
    tool = ReloadPluginTool(session_id="session-1", user_id="10001")

    with (
        patch(
            "app.agent.tools.impl.reload_plugin.get_plugin_snapshot",
            side_effect=[_plugin_snapshot(), _plugin_snapshot(state=False)],
        ),
        patch(
            "app.agent.tools.impl.reload_plugin.reload_plugin_runtime",
            return_value=PluginRuntimeStatus.LOAD_FAILED,
        ),
    ):
        result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

    payload = json.loads(result)
    assert payload["success"] is False
    assert payload["runtime_status"] == "load_failed"


def test_install_plugin_installs_market_candidate() -> None:
    """
    安装插件工具会使用市场候选携带的仓库地址。
    """
    tool = InstallPluginTool(session_id="session-1", user_id="10001")
    candidate = _market_plugin("DemoPlugin", "Demo Plugin")

    with (
        patch(
            "app.agent.tools.impl.install_plugin.load_market_plugins",
            new=AsyncMock(return_value=[candidate]),
        ),
        patch(
            "app.agent.tools.impl.install_plugin.install_plugin_runtime",
            new=AsyncMock(return_value=(True, "插件安装完成", False)),
        ) as install_runtime,
        patch(
            "app.agent.tools.impl.install_plugin.get_plugin_snapshot",
            return_value=_plugin_snapshot(),
        ),
    ):
        result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

    payload = json.loads(result)
    assert payload["success"]
    assert payload["plugin"]["id"] == "DemoPlugin"
    install_runtime.assert_awaited_once_with(
        "DemoPlugin",
        None,
        force=False,
        explicit_source=False,
    )


def test_install_plugin_reports_source_conflict_before_retry() -> None:
    """Agent 普通安装遇到多来源时返回候选，等待管理员明确选择。"""
    tool = InstallPluginTool(session_id="session-1", user_id="10001")
    candidate = _market_plugin("DemoPlugin", "Demo Plugin")
    source_candidates = [
        {
            "plugin_id": "DemoPlugin",
            "source_type": "official",
            "source_key": "github:jxxghp/moviepilot-plugins",
            "repo_url": "https://github.com/jxxghp/MoviePilot-Plugins",
            "package_generation": "v3",
            "plugin_version": "1.0.0",
        },
        {
            "plugin_id": "DemoPlugin",
            "source_type": "third_party",
            "source_key": "github:example/plugins",
            "repo_url": "https://github.com/example/plugins",
            "package_generation": "v3",
            "plugin_version": "2.0.0",
        },
    ]

    with (
        patch(
            "app.agent.tools.impl.install_plugin.load_market_plugins",
            new=AsyncMock(return_value=[candidate]),
        ),
        patch(
            "app.agent.tools.impl.install_plugin.install_plugin_runtime",
            new=AsyncMock(return_value=(False, "未安装插件存在多个在线来源", False)),
        ),
        patch(
            "app.agent.tools.impl.install_plugin.inspect_plugin_sources",
            new=AsyncMock(return_value={
                "selection_status": "conflict",
                "selection_reason": "该插件存在于多个仓库，请选择仓库",
                "inventory_complete": True,
                "candidates": source_candidates,
            }),
        ),
    ):
        result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

    payload = json.loads(result)
    assert payload["success"] is False
    assert payload["requires_explicit_source"] is True
    assert payload["source_candidates"] == source_candidates


@pytest.mark.parametrize("repo_url", ["", "   ", "local://DemoPlugin"])
def test_install_plugin_rejects_invalid_explicit_source(repo_url: str) -> None:
    """Agent 不能用空值或本地标识伪造管理员在线选源。"""
    with pytest.raises(ValueError):
        InstallPluginInput(plugin_id="DemoPlugin", repo_url=repo_url)


def test_install_plugin_runtime_uses_application_gateway() -> None:
    """Agent 安装入口只能转发到统一的应用层安装 Gateway。"""
    gateway = MagicMock()
    gateway.install = AsyncMock(
        return_value=SimpleNamespace(
            success=True,
            message="插件已存在，已刷新加载",
            refreshed_only=True,
        )
    )

    with patch(
        "app.agent.tools.impl._plugin_tool_utils.get_plugin_install_service",
        return_value=gateway,
    ):
        success, message, refreshed_only = asyncio.run(
            install_plugin_runtime(
                "DemoPlugin",
                "https://example.com/market",
                force=False,
            )
        )

    assert success
    assert message == "插件已存在，已刷新加载"
    assert refreshed_only
    gateway.install.assert_awaited_once_with(
        plugin_id="DemoPlugin",
        repo_url="https://example.com/market",
        force=False,
        explicit_source=False,
    )


def test_uninstall_plugin_uninstalls_installed_candidate() -> None:
    """
    卸载插件工具会按已安装候选执行卸载流程。
    """
    tool = UninstallPluginTool(session_id="session-1", user_id="10001")
    installed_plugin = _market_plugin(
        "DemoPlugin", "Demo Plugin", installed=True
    )

    with (
        patch(
            "app.agent.tools.impl.uninstall_plugin.list_installed_plugins",
            return_value=[installed_plugin],
        ),
        patch(
            "app.agent.tools.impl.uninstall_plugin.uninstall_plugin_runtime",
            new=AsyncMock(
                return_value={"was_clone": False, "clone_files_removed": False}
            ),
        ) as uninstall_runtime,
    ):
        result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

    payload = json.loads(result)
    assert payload["success"]
    assert payload["plugin"]["id"] == "DemoPlugin"
    uninstall_runtime.assert_awaited_once_with("DemoPlugin")


def test_sealed_agent_uninstall_rejects_before_persistence() -> None:
    """Agent 卸载未获 admission 时不读取实例或修改安装清单。"""
    admission = PluginMutationAdmission()
    admission.seal()
    plugin_manager = MagicMock()
    plugin_manager.mutation.side_effect = admission.hold
    config_oper = MagicMock()

    with (
        patch(
            "app.agent.tools.impl._plugin_tool_utils.get_plugin_manager",
            return_value=plugin_manager,
        ),
        patch(
            "app.agent.tools.impl._plugin_tool_utils.get_configured_system_config",
            return_value=config_oper,
        ) as config_provider,
        pytest.raises(PluginMutationRejectedError),
    ):
        asyncio.run(uninstall_plugin_runtime("DemoPlugin"))

    plugin_manager.get_plugin_instance.assert_not_called()
    config_provider.assert_not_called()
    config_oper.async_set.assert_not_called()


def test_agent_clone_uninstall_delegates_directory_removal_to_package_owner() -> None:
    """Agent 分身卸载与 HTTP 入口共用唯一包文件删除 owner。"""
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_instance.return_value = None
    plugin_manager.get_plugin_source_instances.return_value = []
    plugin_manager.plugins = {"DemoPluginwork": MagicMock(is_clone=True)}
    plugin_manager.remove_plugin_package.return_value = True
    config_oper = MagicMock()
    config_oper.get.return_value = ["DemoPluginwork"]
    config_oper.async_set = AsyncMock()
    blocking = AsyncMock(return_value=True)

    with (
        patch(
            "app.agent.tools.impl._plugin_tool_utils.get_plugin_manager",
            return_value=plugin_manager,
        ),
        patch(
            "app.agent.tools.impl._plugin_tool_utils.get_configured_system_config",
            return_value=config_oper,
        ),
        patch(
            "app.application.plugin.folders.remove_plugin_from_folders",
        ),
        patch("app.application.plugin.routes.remove_plugin_api"),
        patch("app.application.scheduling.remove_plugin_job"),
        patch("app.agent.tools.base.run_agent_blocking", blocking),
    ):
        result = asyncio.run(uninstall_plugin_runtime("DemoPluginwork"))

    assert result == {"was_clone": True, "clone_files_removed": True}
    blocking.assert_awaited_once_with(
        "plugin",
        plugin_manager.remove_plugin_package,
        "DemoPluginwork",
    )
    assert "DemoPluginwork" not in plugin_manager.plugins


def test_query_plugin_data_truncates_large_payload() -> None:
    """
    查询插件数据会截断超长内容并返回预览。
    """
    plugin_data_oper = MagicMock()
    plugin_data_oper.list = AsyncMock(
        return_value={"payload": {"text": "x" * 5000}}
    )

    with (
        patch(
            "app.agent.tools.impl.query_plugin_data.get_plugin_snapshot",
            return_value=_plugin_snapshot(),
        ),
    ):
        tool = QueryPluginDataTool(
            session_id="session-1",
            user_id="10001",
            data=SimpleNamespace(plugin_data=plugin_data_oper),
        )
        result = asyncio.run(tool.run(plugin_id="DemoPlugin", max_chars=200))

    payload = json.loads(result)
    assert payload["success"]
    assert payload["truncated"]
    assert "data_preview" in payload
    assert "data" not in payload
    assert "已截断" in payload["data_preview"]
