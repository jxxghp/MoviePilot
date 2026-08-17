import asyncio
import json
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.tools.impl._plugin_tool_utils import install_plugin_runtime
from app.agent.tools.impl.install_plugin import InstallPluginTool
from app.agent.tools.impl.query_installed_plugins import QueryInstalledPluginsTool
from app.agent.tools.impl.query_market_plugins import QueryMarketPluginsTool
from app.agent.tools.impl.query_plugin_config import QueryPluginConfigTool
from app.agent.tools.impl.query_plugin_data import QueryPluginDataTool
from app.agent.tools.impl.reload_plugin import ReloadPluginTool
from app.agent.tools.impl.uninstall_plugin import UninstallPluginTool
from app.agent.tools.impl.update_plugin_config import UpdatePluginConfigTool


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
            "app.agent.tools.impl._plugin_tool_utils.PluginManager",
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
            "app.agent.tools.impl.query_plugin_config.PluginManager",
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
            "app.agent.tools.impl.update_plugin_config.PluginManager",
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
        result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

    payload = json.loads(result)
    assert payload["success"]
    assert not payload["state"]
    reload_plugin_runtime.assert_called_once_with("DemoPlugin")


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
        "DemoPlugin", "https://example.com/market", force=False
    )


def test_install_plugin_runtime_reloads_in_threadpool() -> None:
    """
    已存在插件刷新加载时会通过插件线程池执行重载。
    """
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_ids.return_value = ["DemoPlugin"]
    plugin_helper = MagicMock()
    config_oper = MagicMock()
    config_oper.get.return_value = ["DemoPlugin"]
    calls = []

    async def fake_run_agent_blocking(bucket, func, *args, **kwargs) -> None:
        calls.append((bucket, func, args, kwargs))
        return None

    with (
        patch(
            "app.agent.tools.impl._plugin_tool_utils.SystemConfigOper",
            return_value=config_oper,
        ),
        patch(
            "app.agent.tools.impl._plugin_tool_utils.PluginManager",
            return_value=plugin_manager,
        ),
        patch(
            "app.agent.tools.impl._plugin_tool_utils.PluginHelper",
            return_value=plugin_helper,
        ),
        patch(
            "app.agent.tools.impl._plugin_tool_utils.refresh_plugin_registrations",
        ) as refresh_registrations,
        patch(
            "app.agent.tools.impl._plugin_tool_utils.MoviePilotServerHelper.async_install_plugin_reg",
            AsyncMock(return_value=True),
        ) as install_reg,
        patch(
            "app.agent.tools.base.run_agent_blocking",
            side_effect=fake_run_agent_blocking,
        ),
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
    install_reg.assert_awaited_once_with(
        plugin_id="DemoPlugin",
        repo_url="https://example.com/market",
    )
    assert len(calls) == 2
    assert calls[0][0] == "plugin"
    assert calls[0][1] == plugin_manager.reload_plugin
    assert calls[0][2] == ("DemoPlugin",)
    assert calls[0][3] == {}
    assert calls[1][0] == "plugin"
    assert calls[1][1] == refresh_registrations
    assert calls[1][2] == ("DemoPlugin",)
    assert calls[1][3] == {}


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


def test_query_plugin_data_truncates_large_payload() -> None:
    """
    查询插件数据会截断超长内容并返回预览。
    """
    tool = QueryPluginDataTool(session_id="session-1", user_id="10001")
    plugin_data_oper = MagicMock()
    plugin_data_oper.async_get_data_all = AsyncMock(return_value=[
        SimpleNamespace(key="payload", value={"text": "x" * 5000})
    ])

    with (
        patch(
            "app.agent.tools.impl.query_plugin_data.get_plugin_snapshot",
            return_value=_plugin_snapshot(),
        ),
        patch(
            "app.agent.tools.impl.query_plugin_data.PluginDataOper",
            return_value=plugin_data_oper,
        ),
    ):
        result = asyncio.run(tool.run(plugin_id="DemoPlugin", max_chars=200))

    payload = json.loads(result)
    assert payload["success"]
    assert payload["truncated"]
    assert "data_preview" in payload
    assert "data" not in payload
    assert "已截断" in payload["data_preview"]
