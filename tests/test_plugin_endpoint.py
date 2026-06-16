import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from app import schemas
from app.api.endpoints.plugin import plugin_history
from app.api.endpoints.plugin import reset_plugin
from app.api.endpoints.system import sync_plugin_market_from_wiki
from app.schemas.event import PluginDataResetEventData
from app.schemas.types import ChainEventType


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

    def delete_config(plugin_id):
        calls.append(("delete_config", plugin_id))
        return True

    def delete_data(plugin_id):
        calls.append(("delete_data", plugin_id))
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
        ("delete_config", "SubscribeAssistantEnhanced"),
        ("delete_data", "SubscribeAssistantEnhanced"),
    ]
    reload_plugin_mock.assert_called_once_with("SubscribeAssistantEnhanced")
