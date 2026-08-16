from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.modules.slack import SlackModule
from app.modules.slack.slack import Slack
from app.schemas import CommandRegisterEventData


def test_slack_module_register_commands_filters_event_subset():
    """Slack 模块注册命令时应复用渠道级 CommandRegister 事件过滤结果。"""
    module = SlackModule()
    client = SimpleNamespace(register_commands=Mock(), delete_commands=Mock())
    original_commands = {
        "/sites": {"description": "管理站点"},
        "/version": {"description": "当前版本"},
    }
    event = SimpleNamespace(
        event_data=CommandRegisterEventData(
            commands={"/sites": {"description": "管理站点"}, "/unknown": {"description": "无效"}},
            origin="SlackFilter",
            service="slack-main",
        )
    )

    with (
        patch.object(
            module,
            "get_configs",
            return_value={"slack-main": SimpleNamespace(name="slack-main", config={})},
        ),
        patch.object(module, "get_instance", return_value=client),
        patch("app.modules._base.notification.eventmanager.send_event", return_value=event),
    ):
        module.register_commands(original_commands)

    client.register_commands.assert_called_once_with(
        {"/sites": {"description": "管理站点"}}
    )
    client.delete_commands.assert_not_called()


def test_slack_manifest_update_preserves_foreign_commands():
    """Slack Manifest 更新应只替换 MoviePilot 本次管理的 Slash Commands。"""
    client = Slack.__new__(Slack)
    manifest_client = SimpleNamespace(
        apps_manifest_export=Mock(
            return_value={
                "ok": True,
                "manifest": {
                    "features": {
                        "slash_commands": [
                            {"command": "/keep", "description": "保留"},
                            {"command": "/sites", "description": "旧站点"},
                            {
                                "command": "/old_moviepilot",
                                "description": "旧命令",
                                "usage_hint": "MoviePilot 可选参数",
                            },
                        ]
                    }
                },
            }
        ),
        apps_manifest_update=Mock(return_value={"ok": True}),
    )
    client._manifest_client = manifest_client
    client._app_id = "A123"
    client._command_request_url = "https://example.com/slack/commands"
    client._registered_command_names = {"/sites"}

    assert client.register_commands(
        {
            "/sites": {"description": "管理站点"},
            "/version": {"description": "当前版本"},
        }
    )

    manifest_client.apps_manifest_update.assert_called_once()
    _, kwargs = manifest_client.apps_manifest_update.call_args
    assert kwargs["app_id"] == "A123"
    slash_commands = kwargs["manifest"]["features"]["slash_commands"]
    assert [item["command"] for item in slash_commands] == [
        "/keep",
        "/sites",
        "/version",
    ]
    assert slash_commands[1]["url"] == "https://example.com/slack/commands"
    assert client._registered_command_names == {"/sites", "/version"}


def test_slack_command_registration_skips_without_manifest_credentials():
    """未配置 Slack Manifest 凭据时不应尝试自动注册。"""
    client = Slack.__new__(Slack)
    client._manifest_client = None
    client._app_id = ""

    assert client.register_commands({"/sites": {"description": "管理站点"}}) is False


def test_slack_normalizes_slash_command_names():
    """Slack 命令名称应符合平台 Slash Command 约束。"""
    assert Slack._normalize_slack_command("/sites") == "/sites"
    assert Slack._normalize_slack_command("CLEAR_CACHE") == "/clear_cache"
    assert Slack._normalize_slack_command("/中文") == ""
    assert Slack._normalize_slack_command("/" + "a" * 32) == ""
