"""
命令门面与真实注册表的缺省值契约回归。
"""

from unittest.mock import Mock

import pytest

from app.application import commands
from app.command import Command
from app.schemas.types import EventType, NotificationChannel


@pytest.fixture
def command_registry(monkeypatch):
    """
    保留真实注册表查询逻辑，跳过单例初始化和外部服务装配。
    """
    registry = object.__new__(Command)
    registry._commands = {
        "/demo": {"description": "示例命令", "pid": "demo-plugin"},
    }
    monkeypatch.setattr(commands, "_command_class", lambda: registry)
    return registry


@pytest.mark.parametrize("command", ["/missing", " missing 参数 "])
def test_missing_command_is_rejected_before_publishing(command_registry, command):
    """
    真实注册表用空字典表示缺失，门面应报错且不发送执行事件。
    """
    assert command_registry.get("/missing") == {}
    publish_event = Mock()

    with pytest.raises(ValueError, match="命令 /missing 不存在.*示例命令"):
        commands.dispatch_command(command, user_id="1", publish_event=publish_event)

    publish_event.assert_not_called()


def test_registered_command_keeps_arguments_and_channel(command_registry):
    """
    已注册命令仍传递原参数、用户和渠道，并返回插件描述。
    """
    publish_event = Mock()
    assert commands.get_commands() is command_registry._commands

    result = commands.dispatch_command(
        " demo 参数 ",
        user_id="7",
        channel=NotificationChannel.Wechat,
        source="企业微信",
        publish_event=publish_event,
    )

    publish_event.assert_called_once_with(
        EventType.CommandExcute,
        {
            "cmd": "/demo 参数",
            "user": "7",
            "channel": NotificationChannel.Wechat,
            "source": "企业微信",
        },
    )
    assert result["command"] == "/demo 参数"
    assert result["command_desc"] == "示例命令"
    assert result["plugin_id"] == "demo-plugin"
