"""Synology Chat 不支持命令菜单时的渠道注册回归测试。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.modules.synologychat import SynologyChatModule
from app.modules.synologychat.synologychat import SynologyChat


@pytest.mark.parametrize("commands", [{}, {"/version": {"description": "当前版本"}}])
def test_synologychat_skips_command_menu_registration(commands):
    """空菜单和非空菜单均应跳过客户端调用及菜单事件，重复注册也不报错。"""
    module = SynologyChatModule()
    client = SynologyChat.__new__(SynologyChat)
    configs = {
        name: SimpleNamespace(name=name, config={"SYNOLOGYCHAT_TOKEN": "test-token"})
        for name in ("synology-main", "synology-other")
    }
    with (
        patch.object(module, "get_configs", return_value=configs),
        patch.object(module, "get_instance", return_value=client) as get_instance,
        patch("app.modules._base.notification.eventmanager.send_event", return_value=None) as send_event,
    ):
        module.register_commands(commands)
        module.register_commands(commands)

    get_instance.assert_not_called()
    send_event.assert_not_called()
