import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.tools.impl.query_system_settings import QuerySystemSettingsTool
from app.agent.tools.impl.update_system_settings import UpdateSystemSettingsTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.core.config import settings


class TestAgentSystemSettingsTools(unittest.TestCase):
    def test_query_system_settings_returns_exact_systemconfig_value(self):
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.query_system_settings.SystemConfigOper"
        ) as system_config_oper:
            system_config_oper.return_value.get.return_value = [{"name": "qb", "enabled": True}]
            result = asyncio.run(tool.run(setting_key="Downloaders"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["matched_count"], 1)
        self.assertEqual(payload["settings"][0]["setting_key"], "Downloaders")
        self.assertEqual(payload["settings"][0]["value"][0]["name"], "qb")

    def test_query_system_settings_group_defaults_to_summary_for_multiple_items(self):
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.query_system_settings.SystemConfigOper"
        ) as system_config_oper:
            system_config_oper.return_value.get.return_value = []
            result = asyncio.run(tool.run(group="systemconfig"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertFalse(payload["include_values"])
        self.assertGreater(payload["matched_count"], 1)

    def test_update_system_settings_merges_dict_and_emits_event(self):
        tool = UpdateSystemSettingsTool(session_id="session-1", user_id="10001")
        config_oper = MagicMock()
        config_oper.get.side_effect = [
            {"chatgpt": {"enabled": True}},
            {"chatgpt": {"enabled": False}, "gemini": {"enabled": True}},
        ]
        config_oper.async_set = AsyncMock(return_value=True)

        with patch(
            "app.agent.tools.impl.update_system_settings.SystemConfigOper",
            return_value=config_oper,
        ), patch(
            "app.agent.tools.impl.update_system_settings.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            result = asyncio.run(
                tool.run(
                    setting_key="AIAgentConfig",
                    operation="merge_dict",
                    value={"chatgpt": {"enabled": False}, "gemini": {"enabled": True}},
                )
            )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["changed"])
        config_oper.async_set.assert_awaited_once_with(
            "AIAgentConfig",
            {"chatgpt": {"enabled": False}, "gemini": {"enabled": True}},
        )
        send_event.assert_awaited_once()

    def test_update_system_settings_upserts_named_list_item(self):
        tool = UpdateSystemSettingsTool(session_id="session-1", user_id="10001")
        config_oper = MagicMock()
        config_oper.get.side_effect = [
            [{"name": "qb", "enabled": False}],
            [{"name": "qb", "enabled": True}],
        ]
        config_oper.async_set = AsyncMock(return_value=True)

        with patch(
            "app.agent.tools.impl.update_system_settings.SystemConfigOper",
            return_value=config_oper,
        ), patch(
            "app.agent.tools.impl.update_system_settings.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            result = asyncio.run(
                tool.run(
                    setting_key="downloaders",
                    operation="upsert_list_item",
                    value={"name": "qb", "enabled": True},
                )
            )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["saved_value"], [{"name": "qb", "enabled": True}])

    def test_update_system_settings_updates_basic_settings(self):
        tool = UpdateSystemSettingsTool(session_id="session-1", user_id="10001")

        # settings 是 pydantic 模型实例，不能直接 patch 其实例方法（__setattr__ 会拦截），
        # 改 patch 类上的方法；经实例调用时不带 self，断言参数不受影响。
        with patch.object(
            type(settings),
            "update_setting",
            return_value=(True, ""),
        ) as update_setting, patch.object(
            UpdateSystemSettingsTool,
            "_load_setting_value",
            side_effect=["https://old.example.com", "https://new.example.com"],
        ), patch(
            "app.agent.tools.impl.update_system_settings.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event:
            result = asyncio.run(
                tool.run(setting_key="APP_DOMAIN", value="https://new.example.com")
            )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["changed"])
        update_setting.assert_called_once_with("APP_DOMAIN", "https://new.example.com")
        send_event.assert_awaited_once()

    def test_tool_manager_blocks_admin_tools_for_non_admin_context(self):
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.manager.MoviePilotToolFactory.create_tools",
            return_value=[tool],
        ):
            manager = MoviePilotToolsManager(is_admin=False)
            result = asyncio.run(
                manager.call_tool(
                    "query_system_settings",
                    {"setting_key": "Downloaders"},
                )
            )

        payload = json.loads(result)
        self.assertIn("error", payload)
        self.assertIn("系统管理员", payload["error"])
