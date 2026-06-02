import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.tools.impl.install_plugin import InstallPluginTool
from app.agent.tools.impl._plugin_tool_utils import install_plugin_runtime
from app.agent.tools.impl.query_installed_plugins import QueryInstalledPluginsTool
from app.agent.tools.impl.query_market_plugins import QueryMarketPluginsTool
from app.agent.tools.impl.query_plugin_config import QueryPluginConfigTool
from app.agent.tools.impl.query_plugin_data import QueryPluginDataTool
from app.agent.tools.impl.reload_plugin import ReloadPluginTool
from app.agent.tools.impl.uninstall_plugin import UninstallPluginTool
from app.agent.tools.impl.update_plugin_config import UpdatePluginConfigTool


class TestAgentPluginTools(unittest.TestCase):
    @staticmethod
    def _plugin_snapshot(state: bool = True) -> dict:
        return {
            "plugin_id": "DemoPlugin",
            "plugin_name": "Demo Plugin",
            "plugin_version": "1.0.0",
            "state": state,
        }

    @staticmethod
    def _market_plugin(plugin_id: str, plugin_name: str, installed: bool = False):
        return SimpleNamespace(
            id=plugin_id,
            plugin_name=plugin_name,
            plugin_desc=f"{plugin_name} description",
            plugin_version="1.0.0",
            plugin_author="author",
            installed=installed,
            has_update=False,
            state=installed,
            repo_url="https://example.com/market",
            add_time=1,
        )

    def test_query_market_plugins_filters_candidates(self):
        tool = QueryMarketPluginsTool(session_id="session-1", user_id="10001")
        plugins = [
            self._market_plugin("DemoPlugin", "Demo Plugin"),
            self._market_plugin("OtherPlugin", "Other Plugin"),
        ]

        with patch(
            "app.agent.tools.impl.query_market_plugins.load_market_plugins",
            new=AsyncMock(return_value=plugins),
        ):
            result = asyncio.run(tool.run(query="demo"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["match_count"], 1)
        self.assertEqual(payload["plugins"][0]["id"], "DemoPlugin")

    def test_query_installed_plugins_filters_candidates(self):
        tool = QueryInstalledPluginsTool(session_id="session-1", user_id="10001")
        plugins = [
            self._market_plugin("DemoPlugin", "Demo Plugin", installed=True),
            self._market_plugin("OtherPlugin", "Other Plugin", installed=True),
        ]

        with patch(
            "app.agent.tools.impl.query_installed_plugins.list_installed_plugins",
            return_value=plugins,
        ):
            result = asyncio.run(tool.run(query="demo"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["match_count"], 1)
        self.assertEqual(payload["plugins"][0]["id"], "DemoPlugin")

    def test_query_plugin_config_returns_saved_config_and_default_model(self):
        tool = QueryPluginConfigTool(session_id="session-1", user_id="10001")
        plugin_manager = MagicMock()
        plugin_manager.get_plugin_config.return_value = {"enabled": True}
        plugin_instance = MagicMock()
        plugin_instance.get_form.return_value = (None, {"enabled": False, "interval": 10})
        plugin_manager.running_plugins = {"DemoPlugin": plugin_instance}

        with patch(
            "app.agent.tools.impl.query_plugin_config.get_plugin_snapshot",
            return_value=self._plugin_snapshot(),
        ), patch(
            "app.agent.tools.impl.query_plugin_config.PluginManager",
            return_value=plugin_manager,
        ):
            result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["config"], {"enabled": True})
        self.assertEqual(payload["default_model"], {"enabled": False, "interval": 10})

    def test_update_plugin_config_merges_and_removes_keys_without_reloading(self):
        tool = UpdatePluginConfigTool(session_id="session-1", user_id="10001")
        plugin_manager = MagicMock()
        plugin_manager.get_plugin_config.return_value = {
            "enabled": False,
            "interval": 30,
            "token": "legacy-token",
        }
        plugin_manager.async_save_plugin_config = AsyncMock(return_value=True)

        with patch(
            "app.agent.tools.impl.update_plugin_config.get_plugin_snapshot",
            return_value=self._plugin_snapshot(),
        ), patch(
            "app.agent.tools.impl.update_plugin_config.PluginManager",
            return_value=plugin_manager,
        ):
            result = asyncio.run(
                tool.run(
                    plugin_id="DemoPlugin",
                    updates={"enabled": True},
                    remove_keys=["token"],
                )
            )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["config_requires_reload"])
        self.assertEqual(payload["saved_config"], {"enabled": True, "interval": 30})
        plugin_manager.async_save_plugin_config.assert_awaited_once_with(
            "DemoPlugin",
            {"enabled": True, "interval": 30},
        )

    def test_reload_plugin_triggers_runtime_refresh(self):
        tool = ReloadPluginTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.reload_plugin.get_plugin_snapshot",
            side_effect=[self._plugin_snapshot(), self._plugin_snapshot(state=False)],
        ), patch(
            "app.agent.tools.impl.reload_plugin.reload_plugin_runtime"
        ) as reload_plugin_runtime:
            result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertFalse(payload["state"])
        reload_plugin_runtime.assert_called_once_with("DemoPlugin")

    def test_install_plugin_installs_market_candidate(self):
        tool = InstallPluginTool(session_id="session-1", user_id="10001")
        candidate = self._market_plugin("DemoPlugin", "Demo Plugin")

        with patch(
            "app.agent.tools.impl.install_plugin.load_market_plugins",
            new=AsyncMock(return_value=[candidate]),
        ), patch(
            "app.agent.tools.impl.install_plugin.install_plugin_runtime",
            new=AsyncMock(return_value=(True, "插件安装完成", False)),
        ) as install_runtime, patch(
            "app.agent.tools.impl.install_plugin.get_plugin_snapshot",
            return_value=self._plugin_snapshot(),
        ):
            result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["plugin"]["id"], "DemoPlugin")
        install_runtime.assert_awaited_once_with(
            "DemoPlugin", "https://example.com/market", force=False
        )

    def test_install_plugin_runtime_reloads_in_threadpool(self):
        plugin_manager = MagicMock()
        plugin_manager.get_plugin_ids.return_value = ["DemoPlugin"]
        plugin_helper = MagicMock()
        config_oper = MagicMock()
        config_oper.get.return_value = ["DemoPlugin"]
        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append((func, args, kwargs))
            return None

        with patch(
            "app.agent.tools.impl._plugin_tool_utils.SystemConfigOper",
            return_value=config_oper,
        ), patch(
            "app.agent.tools.impl._plugin_tool_utils.PluginManager",
            return_value=plugin_manager,
        ), patch(
            "app.agent.tools.impl._plugin_tool_utils.PluginHelper",
            return_value=plugin_helper,
        ), patch(
            "app.agent.tools.impl._plugin_tool_utils.reload_plugin_runtime",
        ) as reload_runtime, patch(
            "app.agent.tools.impl._plugin_tool_utils.MoviePilotServerHelper.async_install_plugin_reg",
            AsyncMock(return_value=True),
        ) as install_reg, patch(
            "app.agent.tools.impl._plugin_tool_utils.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            success, message, refreshed_only = asyncio.run(
                install_plugin_runtime(
                    "DemoPlugin",
                    "https://example.com/market",
                    force=False,
                )
            )

        self.assertTrue(success)
        self.assertEqual("插件已存在，已刷新加载", message)
        self.assertTrue(refreshed_only)
        install_reg.assert_awaited_once_with(
            plugin_id="DemoPlugin",
            repo_url="https://example.com/market",
        )
        self.assertEqual(1, len(calls))
        self.assertEqual(reload_runtime, calls[0][0])
        self.assertEqual(("DemoPlugin",), calls[0][1])
        self.assertEqual({}, calls[0][2])

    def test_uninstall_plugin_uninstalls_installed_candidate(self):
        tool = UninstallPluginTool(session_id="session-1", user_id="10001")
        installed_plugin = self._market_plugin(
            "DemoPlugin", "Demo Plugin", installed=True
        )

        with patch(
            "app.agent.tools.impl.uninstall_plugin.list_installed_plugins",
            return_value=[installed_plugin],
        ), patch(
            "app.agent.tools.impl.uninstall_plugin.uninstall_plugin_runtime",
            new=AsyncMock(
                return_value={"was_clone": False, "clone_files_removed": False}
            ),
        ) as uninstall_runtime:
            result = asyncio.run(tool.run(plugin_id="DemoPlugin"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["plugin"]["id"], "DemoPlugin")
        uninstall_runtime.assert_awaited_once_with("DemoPlugin")

    def test_query_plugin_data_truncates_large_payload(self):
        tool = QueryPluginDataTool(session_id="session-1", user_id="10001")
        plugin_data_oper = MagicMock()
        plugin_data_oper.async_get_data_all = AsyncMock(return_value=[
            SimpleNamespace(key="payload", value={"text": "x" * 5000})
        ])

        with patch(
            "app.agent.tools.impl.query_plugin_data.get_plugin_snapshot",
            return_value=self._plugin_snapshot(),
        ), patch(
            "app.agent.tools.impl.query_plugin_data.PluginDataOper",
            return_value=plugin_data_oper,
        ):
            result = asyncio.run(tool.run(plugin_id="DemoPlugin", max_chars=200))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["truncated"])
        self.assertIn("data_preview", payload)
        self.assertNotIn("data", payload)
        self.assertIn("已截断", payload["data_preview"])
