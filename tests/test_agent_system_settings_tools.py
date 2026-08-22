import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.catalog import ToolCatalogSnapshot
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl._system_setting_utils import list_setting_specs
from app.agent.tools.impl.query_system_settings import QuerySystemSettingsTool
from app.agent.tools.impl.update_system_settings import UpdateSystemSettingsTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.runtime.config import Settings, settings
from app.schemas.types import SystemConfigKey


class TestAgentSystemSettingsTools(unittest.TestCase):
    def test_query_system_settings_accepts_injected_reader(self):
        """Agent 配置工具通过窄端口读取授权字段，无需自行构造数据库 Oper。"""
        reader = MagicMock()
        reader.get.return_value = [{"name": "qb", "enabled": True}]
        tool = QuerySystemSettingsTool(
            session_id="session-injected",
            user_id="10001",
            system_config=reader,
        )

        payload = json.loads(asyncio.run(tool.run(setting_key="Downloaders")))

        self.assertTrue(payload["success"])
        reader.get.assert_called_once_with(SystemConfigKey.Downloaders)

    def test_query_system_settings_returns_exact_systemconfig_value(self):
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.query_system_settings.read_system_setting"
        ) as read_setting:
            read_setting.return_value = [{"name": "qb", "enabled": True}]
            result = asyncio.run(tool.run(setting_key="Downloaders"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["matched_count"], 1)
        self.assertEqual(payload["settings"][0]["setting_key"], "Downloaders")
        self.assertEqual(payload["settings"][0]["value"][0]["name"], "qb")
        read_setting.assert_called_once_with(SystemConfigKey.Downloaders)

    def test_query_system_settings_redacts_secret_values_by_default(self):
        """查询系统设置默认应脱敏 API Key、Token、Cookie 等敏感字段。"""
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.query_system_settings.read_system_setting"
        ) as read_setting:
            read_setting.return_value = [
                {
                    "name": "site-a",
                    "apikey": "site-api-key",
                    "token": "site-token",
                    "cookie": "uid=1; passkey=secret",
                    "url": "https://example.com",
                }
            ]
            result = asyncio.run(
                tool.run(setting_key="UserSiteAuthParams", include_values=True)
            )

        payload = json.loads(result)
        item = payload["settings"][0]
        self.assertTrue(item["redacted"])
        self.assertFalse(payload["show_secrets"])
        self.assertEqual("***", item["value"][0]["apikey"])
        self.assertEqual("***", item["value"][0]["token"])
        self.assertEqual("***", item["value"][0]["cookie"])
        self.assertEqual("https://example.com", item["value"][0]["url"])

    def test_query_system_settings_show_secrets_requires_admin_context(self):
        """只有管理员显式请求 show_secrets 时才返回敏感配置原值。"""
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="admin")
        tool.set_agent_context({"is_admin": True})

        with patch(
            "app.agent.tools.impl.query_system_settings.read_system_setting"
        ) as read_setting:
            read_setting.return_value = [
                {"name": "site-a", "apikey": "site-api-key"}
            ]
            result = asyncio.run(
                tool.run(
                    setting_key="UserSiteAuthParams",
                    include_values=True,
                    show_secrets=True,
                )
            )

        payload = json.loads(result)
        item = payload["settings"][0]
        self.assertTrue(payload["show_secrets"])
        self.assertFalse(item["redacted"])
        self.assertEqual("site-api-key", item["value"][0]["apikey"])

    def test_agent_tool_refuses_unconfirmed_secret_read(self):
        """Agent 宿主要求确认时，工具自身也不得直接返回密钥。"""
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")
        tool.set_agent_context(
            {
                "is_admin": True,
                "require_secret_confirmation": True,
                "secret_read_confirmed": False,
            }
        )

        with patch.object(
            QuerySystemSettingsTool,
            "_load_setting_value",
            return_value="must-not-load",
        ) as load_value:
            result = asyncio.run(
                tool.run(setting_key="TMDB_API_KEY", show_secrets=True)
            )

        payload = json.loads(result)
        self.assertFalse(payload["success"])
        self.assertIn("确认", payload["message"])
        load_value.assert_not_called()

    def test_agent_tool_ignores_forged_confirmation_context(self):
        """模型可影响的共享上下文不得伪造宿主确认。"""
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")
        tool.set_agent_context(
            {
                "is_admin": True,
                "require_secret_confirmation": True,
                "secret_read_confirmed": True,
            }
        )

        with patch.object(
            QuerySystemSettingsTool,
            "_load_setting_value",
            return_value="must-not-load",
        ) as load_value:
            result = asyncio.run(
                tool.run(setting_key="TMDB_API_KEY", show_secrets=True)
            )

        payload = json.loads(result)
        self.assertFalse(payload["success"])
        load_value.assert_not_called()

    def test_query_system_settings_group_defaults_to_summary_for_multiple_items(self):
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")

        with patch(
            "app.agent.tools.impl.query_system_settings.read_system_setting"
        ) as read_setting:
            read_setting.return_value = []
            result = asyncio.run(tool.run(group="systemconfig"))

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertFalse(payload["include_values"])
        self.assertGreater(payload["matched_count"], 1)

    def test_settings_group_retains_all_basic_settings(self):
        """settings 分组应继续完整列出基础 Settings 字段。"""
        specs = list_setting_specs(group="settings")

        self.assertSetEqual(
            {spec.key for spec in specs},
            set(Settings.model_fields),
        )
        self.assertTrue(all(spec.source == "settings" for spec in specs))

    def test_query_system_settings_ai_agent_group_spans_both_setting_sources(self):
        """AI Agent 分组应同时返回基础运行配置和 SystemConfig 扩展配置。"""
        tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")
        expected_values = {
            "AI_AGENT_ENABLE": True,
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "gpt-test",
            "LLM_THINKING_LEVEL": "high",
            "LLM_API_KEY": "llm-secret",
            "AUDIO_INPUT_PROVIDER": "openai",
            "AUDIO_OUTPUT_PROVIDER": "openai",
            "AI_RECOMMEND_ENABLED": True,
            "AIAgentConfig": {"chatgpt": {"enabled": True}},
            "AIAgentMcpServers": [],
        }

        with patch.object(
            QuerySystemSettingsTool,
            "_load_setting_value",
            side_effect=lambda spec: expected_values.get(spec.key),
        ):
            result = asyncio.run(
                tool.run(group="ai_agent", include_values=True)
            )

        payload = json.loads(result)
        items = {
            item["setting_key"]: item
            for item in payload["settings"]
        }

        self.assertTrue(payload["success"])
        self.assertEqual(items["AI_AGENT_ENABLE"]["source"], "settings")
        self.assertIs(items["AI_AGENT_ENABLE"]["value"], True)
        self.assertEqual(items["LLM_PROVIDER"]["value"], "openai")
        self.assertEqual(items["LLM_MODEL"]["value"], "gpt-test")
        self.assertEqual(items["LLM_THINKING_LEVEL"]["value"], "high")
        self.assertTrue(items["LLM_API_KEY"]["redacted"])
        self.assertEqual(items["LLM_API_KEY"]["value"], "***")
        self.assertEqual(items["AUDIO_INPUT_PROVIDER"]["value"], "openai")
        self.assertEqual(items["AUDIO_OUTPUT_PROVIDER"]["value"], "openai")
        self.assertIs(items["AI_RECOMMEND_ENABLED"]["value"], True)
        self.assertEqual(items["AIAgentConfig"]["source"], "systemconfig")
        self.assertEqual(items["AIAgentMcpServers"]["source"], "systemconfig")

    def test_update_system_settings_merges_dict_and_emits_event(self):
        tool = UpdateSystemSettingsTool(session_id="session-1", user_id="10001")
        read_setting = MagicMock(side_effect=[
            {"chatgpt": {"enabled": True}},
            {"chatgpt": {"enabled": False}, "gemini": {"enabled": True}},
        ])
        write_setting = AsyncMock(return_value=True)

        with patch(
            "app.agent.tools.impl.update_system_settings.read_system_setting",
            new=read_setting,
        ), patch(
            "app.agent.tools.impl.update_system_settings.async_write_system_setting",
            new=write_setting,
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
        write_setting.assert_awaited_once_with(
            SystemConfigKey.AIAgentConfig,
            {"chatgpt": {"enabled": False}, "gemini": {"enabled": True}},
        )
        send_event.assert_awaited_once()

    def test_update_system_settings_upserts_named_list_item(self):
        tool = UpdateSystemSettingsTool(session_id="session-1", user_id="10001")
        read_setting = MagicMock(side_effect=[
            [{"name": "qb", "enabled": False}],
            [{"name": "qb", "enabled": True}],
        ])
        write_setting = AsyncMock(return_value=True)

        with patch(
            "app.agent.tools.impl.update_system_settings.read_system_setting",
            new=read_setting,
        ), patch(
            "app.agent.tools.impl.update_system_settings.async_write_system_setting",
            new=write_setting,
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
        write_setting.assert_awaited_once_with(
            SystemConfigKey.Downloaders,
            [{"name": "qb", "enabled": True}],
        )

    def test_update_system_settings_redacts_secret_values_in_response(self):
        """更新敏感系统设置后响应不应回显旧值和新值中的密钥。"""
        tool = UpdateSystemSettingsTool(session_id="session-1", user_id="10001")
        read_setting = MagicMock(side_effect=[
            [{"name": "site-a", "apikey": "old-key"}],
            [{"name": "site-a", "apikey": "new-key"}],
        ])
        write_setting = AsyncMock(return_value=True)

        with patch(
            "app.agent.tools.impl.update_system_settings.read_system_setting",
            new=read_setting,
        ), patch(
            "app.agent.tools.impl.update_system_settings.async_write_system_setting",
            new=write_setting,
        ), patch(
            "app.agent.tools.impl.update_system_settings.eventmanager.async_send_event",
            new=AsyncMock(),
        ):
            result = asyncio.run(
                tool.run(
                    setting_key="UserSiteAuthParams",
                    operation="upsert_list_item",
                    value={"name": "site-a", "apikey": "new-key"},
                    match_field="name",
                )
            )

        payload = json.loads(result)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["values_redacted"])
        self.assertEqual("***", payload["previous_value"][0]["apikey"])
        self.assertEqual("***", payload["saved_value"][0]["apikey"])

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
        catalog = ToolCatalogSnapshot.from_tools(
            [tool], plugin_revision=0, factory_revision="test"
        )

        with patch.object(
            MoviePilotToolFactory,
            "create_catalog",
            return_value=catalog,
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


@pytest.mark.parametrize(
    ("setting_key", "should_redact"),
    [
        ("API_TOKEN", True),
        ("LLM_API_KEY", True),
        ("COOKIECLOUD_KEY", True),
        ("COOKIECLOUD_AUTH_HEADER", True),
        ("SUPERUSER_PASSWORD", True),
        ("DB_POSTGRESQL_PASSWORD", True),
        ("GITHUB_TOKEN", True),
        ("FEISHU_VERIFICATION_TOKEN", True),
        ("SECRET_KEY", True),
        ("RESOURCE_SECRET_KEY", True),
        ("PROJECT_NAME", False),
        ("ACCESS_TOKEN_EXPIRE_MINUTES", False),
        ("LLM_MAX_CONTEXT_TOKENS", False),
        ("COOKIECLOUD_INTERVAL", False),
    ],
)
def test_query_system_settings_uses_precise_secret_identity_matrix(
    setting_key: str,
    should_redact: bool,
) -> None:
    """设置查询应隐藏真实凭据，同时保留仅名称相似的普通设置。"""
    marker = "credential-marker" if should_redact else "visible-marker"
    tool = QuerySystemSettingsTool(session_id="session-1", user_id="10001")

    with patch.object(
        QuerySystemSettingsTool,
        "_load_setting_value",
        return_value=marker,
    ):
        payload = json.loads(asyncio.run(tool.run(setting_key=setting_key)))

    item = payload["settings"][0]
    assert item["redacted"] is should_redact
    assert item["value"] == ("***" if should_redact else marker)
    if should_redact:
        assert marker not in json.dumps(payload)
