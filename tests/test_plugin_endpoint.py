import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from app import schemas
from app.api.endpoints.plugin import plugin_history
from app.api.endpoints.plugin import plugin_releases
from app.api.endpoints.plugin import reset_plugin
from app.api.endpoints.system import sync_plugin_market_from_wiki
from app.core.config import settings
from app.core.plugin import PluginManager
from app.schemas.event import PluginDataResetEventData
from app.schemas.types import ChainEventType
from app.utils.singleton import Singleton


def test_plugin_history_merges_remote_metadata():
    """
    已安装插件点击更新说明时，接口会按需合并远端仓库中的更新记录。
    """
    installed_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_name="Demo Plugin",
        plugin_version="1.0.0",
        installed=True,
        history={},
    )
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        repo_url="https://github.com/demo/plugins",
        history={"v1.1.0": "- 新增更新说明"},
        system_version=">=2.0.0",
        system_version_compatible=True,
        has_update=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.get_local_plugins.return_value = [installed_plugin]
    plugin_manager.get_local_repo_plugins.return_value = []
    plugin_manager.async_get_online_plugins = AsyncMock(return_value=[market_plugin])

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        result = asyncio.run(plugin_history("DemoPlugin", None, True))

    assert result.repo_url == "https://github.com/demo/plugins"
    assert result.history == {"v1.1.0": "- 新增更新说明"}
    assert result.system_version == ">=2.0.0"
    assert result.has_update


def test_plugin_history_returns_installed_plugin_when_remote_missing():
    """
    远端仓库不可用时，接口仍返回本地已安装插件信息，前端可继续展示兜底状态。
    """
    installed_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_name="Demo Plugin",
        plugin_version="1.0.0",
        installed=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.get_local_plugins.return_value = [installed_plugin]
    plugin_manager.get_local_repo_plugins.return_value = []
    plugin_manager.async_get_online_plugins = AsyncMock(return_value=[])

    with patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager):
        result = asyncio.run(plugin_history("DemoPlugin", None, True))

    assert result.id == "DemoPlugin"
    assert result.history == {}


def test_plugin_releases_returns_supported_versions_with_latest_and_current(monkeypatch):
    """
    release 列表接口返回可安装版本，并标记当前 package 最新版本与本地已安装版本。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_online_plugins = AsyncMock(return_value=[market_plugin])
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = "1.2.0"
    plugin_helper = MagicMock()
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[
        {"version": "1.2.3", "tag_name": "DemoPlugin_v1.2.3", "asset_name": "demoplugin_v1.2.3.zip"},
        {"version": "1.2.0", "tag_name": "DemoPlugin_v1.2.0", "asset_name": "demoplugin_v1.2.0.zip"},
    ])

    with (
        patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager),
        patch("app.api.endpoints.plugin.PluginHelper", return_value=plugin_helper),
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", False))

    assert result["release_supported"] is True
    assert result["latest_version"] == "1.2.3"
    assert result["current_version"] == "1.2.0"
    assert result["items"][0]["is_latest"] is True
    assert result["items"][0]["is_current"] is False
    assert result["items"][1]["is_latest"] is False
    assert result["items"][1]["is_current"] is True
    plugin_manager.async_get_plugins_from_market.assert_awaited_once_with(
        "https://github.com/demo/plugins", settings.VERSION_FLAG, False
    )
    plugin_manager.async_get_online_plugins.assert_not_awaited()
    plugin_manager.get_local_plugins.assert_not_called()


def test_plugin_releases_does_not_mutate_cached_release_items(monkeypatch):
    """
    接口标记当前/最新版本时不能修改 helper 返回对象，避免污染缓存中的 release 列表。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    release_items = [
        {"version": "1.2.3", "tag_name": "DemoPlugin_v1.2.3", "asset_name": "demoplugin_v1.2.3.zip"},
    ]
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = "1.2.0"
    plugin_helper = MagicMock()
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=release_items)

    with (
        patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager),
        patch("app.api.endpoints.plugin.PluginHelper", return_value=plugin_helper),
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", False))

    assert result["items"][0]["is_latest"] is True
    assert "is_latest" not in release_items[0]
    assert "is_current" not in release_items[0]


def test_plugin_releases_falls_back_to_compatible_base_package(monkeypatch):
    """
    当前版本 package 未包含插件时，再读取基础 package 兼容项，不扫描其他市场。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(
        side_effect=[[], [market_plugin]]
    )
    plugin_manager.get_local_plugin_version.return_value = None
    plugin_helper = MagicMock()
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[])

    with (
        patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager),
        patch("app.api.endpoints.plugin.PluginHelper", return_value=plugin_helper),
    ):
        result = asyncio.run(
            plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", False)
        )

    assert result["latest_version"] == "1.2.3"
    assert plugin_manager.async_get_plugins_from_market.await_args_list == [
        (("https://github.com/demo/plugins", settings.VERSION_FLAG, False), {}),
        (("https://github.com/demo/plugins", None, False), {}),
    ]


def test_plugin_releases_uses_force_refresh_for_market_metadata(monkeypatch):
    """
    release 列表接口沿用插件市场的 force 语义，供前端手动刷新时绕过缓存。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = None
    plugin_helper = MagicMock()
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[])

    with (
        patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager),
        patch("app.api.endpoints.plugin.PluginHelper", return_value=plugin_helper),
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", True))

    assert result["release_supported"] is False
    plugin_manager.async_get_plugins_from_market.assert_awaited_once_with(
        "https://github.com/demo/plugins", settings.VERSION_FLAG, True
    )
    assert plugin_helper.async_get_plugin_release_versions.await_args.args == (
        "DemoPlugin",
        "https://github.com/demo/plugins",
    )


def test_plugin_releases_hides_items_when_market_plugin_does_not_enable_release(monkeypatch):
    """
    接口是否支持 Release 安装要与当前 package 的 release 声明保持一致。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=False,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = None
    plugin_helper = MagicMock()
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[
        {"version": "1.2.3", "tag_name": "DemoPlugin_v1.2.3", "asset_name": "demoplugin_v1.2.3.zip"},
    ])

    with (
        patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager),
        patch("app.api.endpoints.plugin.PluginHelper", return_value=plugin_helper),
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", False))

    assert result["release_supported"] is False
    assert result["items"] == []
    plugin_helper.async_get_plugin_release_versions.assert_not_awaited()


def test_sync_plugin_market_from_wiki_merges_and_deduplicates_repos():
    """
    Wiki 同步会提取标记区域内的 GitHub 仓库地址，并与本地配置合并去重后写入。
    """
    markdown = """
<!-- plugin-market-repos:start -->
- https://github.com/local/existing/
- https://github.com/wiki/new-repo/
- https://github.com/wiki/new-repo
<!-- plugin-market-repos:end -->
- https://github.com/wiki/ignored-outside-marker
"""
    response = MagicMock(status_code=200, text=markdown)
    request_utils = MagicMock()
    request_utils.get_res = AsyncMock(return_value=response)
    with (
        patch("app.api.endpoints.system.AsyncRequestUtils", return_value=request_utils),
        patch("app.api.endpoints.system.settings.PLUGIN_MARKET", "https://github.com/local/existing"),
        patch(
            "app.core.config.Settings.update_setting",
            autospec=True,
            return_value=(True, ""),
        ) as update_setting,
        patch("app.api.endpoints.system.eventmanager.async_send_event", new=AsyncMock()) as send_event,
    ):
        result = asyncio.run(sync_plugin_market_from_wiki(None, None))

    assert result.success
    assert result.data["repos"] == [
        "https://github.com/local/existing",
        "https://github.com/wiki/new-repo",
    ]
    assert result.data["added_count"] == 1
    assert result.data["total_count"] == 2
    update_setting.assert_called_once_with(
        ANY,
        "PLUGIN_MARKET",
        "https://github.com/local/existing,https://github.com/wiki/new-repo",
    )
    send_event.assert_awaited_once()


def test_reset_plugin_sends_pre_reset_chain_event_before_deleting_data():
    """
    插件重置会先触发同步链式事件，让插件在数据被清空前完成自有状态补偿。
    """
    plugin_manager = MagicMock()
    calls = []

    def delete_config(plugin_id, force=False):
        calls.append(("delete_config", plugin_id, force))
        return True

    def delete_data(plugin_id, force=False):
        calls.append(("delete_data", plugin_id, force))
        return True

    def stop_plugin(plugin_id):
        calls.append(("stop", plugin_id))
        return True

    plugin_manager.stop.side_effect = stop_plugin
    plugin_manager.delete_plugin_config.side_effect = delete_config
    plugin_manager.delete_plugin_data.side_effect = delete_data

    with (
        patch("app.api.endpoints.plugin.PluginManager", return_value=plugin_manager),
        patch("app.api.endpoints.plugin.eventmanager") as eventmanager,
        patch("app.api.endpoints.plugin.reload_plugin") as reload_plugin_mock,
    ):
        eventmanager.send_event.side_effect = lambda etype, data: calls.append(("event", etype, data))
        result = reset_plugin("SubscribeAssistantEnhanced", None)

    assert result.success is True
    assert len(calls) == 4
    event_call = calls[0]
    assert event_call[0] == "event"
    assert event_call[1] is ChainEventType.PluginDataReset
    assert isinstance(event_call[2], PluginDataResetEventData)
    assert event_call[2].plugin_id == "SubscribeAssistantEnhanced"
    assert event_call[2].reset_config is True
    assert event_call[2].reset_data is True
    assert calls[1:] == [
        ("stop", "SubscribeAssistantEnhanced"),
        ("delete_config", "SubscribeAssistantEnhanced", True),
        ("delete_data", "SubscribeAssistantEnhanced", True),
    ]
    reload_plugin_mock.assert_called_once_with("SubscribeAssistantEnhanced")


def test_delete_plugin_config_can_force_delete_after_plugin_is_stopped():
    """
    重置入口会先停止插件；配置删除需要能处理运行态注册已清理的插件 ID。
    """
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()

    with patch("app.core.plugin.SystemConfigOper") as system_config_oper:
        system_config_oper.return_value.delete.return_value = True
        assert manager.delete_plugin_config("DemoPlugin", force=True) is True

    system_config_oper.return_value.delete.assert_called_once_with("plugin.DemoPlugin")
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def test_delete_plugin_data_can_force_delete_after_plugin_is_stopped():
    """
    重置入口会先停止插件；插件数据删除不能依赖运行态注册仍存在。
    """
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    calls = []

    with patch("app.core.plugin.PluginDataOper") as plugin_data_oper:
        plugin_data_oper.return_value.del_data.side_effect = lambda pid: calls.append(pid)
        assert manager.delete_plugin_data("DemoPlugin", force=True) is True

    assert calls == ["DemoPlugin"]
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
