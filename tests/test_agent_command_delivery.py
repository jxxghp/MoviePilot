"""
验证 Agent 命令经插件事件投递到 Telegram 的收件人边界。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.web.security import access
from app.agent.api.executor import ApiExecutionContext, MoviePilotApiExecutor
from app.api.endpoints import agent as agent_endpoint
from app.application import commands
from app.application.security.token import create_access_token, decode_access_token
from app.chain._messaging import MessageProcessingMixin, NotificationMixin
from app.command import Command
from app.modules.telegram.module import TelegramModule
from app.runtime.events import Event
from app.schemas.message import Message
from app.schemas.types import EventType, NotificationChannel


class _ReceiptChain(MessageProcessingMixin, NotificationMixin):
    """
    使用真实通知路由，隔离用户设置、历史及通知队列边界。
    """

    def __init__(self, targets: dict[str, str], telegram: TelegramModule) -> None:
        """
        注入管理员目标，并同步送入已替换网络客户端的 Telegram 模块。
        """
        self.user_repository = SimpleNamespace(get_notification_settings=Mock(return_value=targets))
        self.runtime_config = SimpleNamespace(superuser="admin")
        self.messageoper = SimpleNamespace(add=Mock())
        self.eventmanager = SimpleNamespace(send_event=Mock())
        self.messagequeue = SimpleNamespace(
            send_message=Mock(side_effect=lambda _method, **kwargs: telegram.post_message(kwargs["message"])),
        )


@pytest.mark.parametrize(
    "channel,channel_user,targets,expected",
    [
        (NotificationChannel.Telegram, "123456789", {}, "123456789"),
        (NotificationChannel.Telegram, "1", {}, "1"),
        (NotificationChannel.WebAgent, "1", {"telegram_userid": "987654321"}, None),
        (None, None, {"telegram_userid": "987654321"}, "987654321"),
        (None, None, {}, None),
    ],
    ids=["signed-channel", "numeric-channel-id", "web-agent", "no-channel-bound", "no-channel-unbound"],
)
def test_agent_plugin_command_uses_real_telegram_target(
    monkeypatch, channel, channel_user, targets, expected,
) -> None:
    """
    签名渠道用户及无渠道回退须经过真实命令、插件载荷和通知路由，不能泄漏内部主键。
    """
    telegram = TelegramModule()
    telegram._channel = NotificationChannel.Telegram
    client = SimpleNamespace(send_msg=Mock(return_value={"success": True}))
    monkeypatch.setattr(telegram, "get_configs", lambda: {"bot": SimpleNamespace(name="bot")})
    monkeypatch.setattr(telegram, "get_instance", lambda _name: client)
    monkeypatch.setattr(telegram, "get_config", lambda _name: None)
    receipt_chain = _ReceiptChain(targets, telegram)
    received_events = []

    def plugin_command(data):
        """
        按插件命令约定把事件渠道和用户用于回执。
        """
        received_events.append(data)
        receipt_chain.post_message(Message(channel=data["channel"], userid=data["user"], title="插件更新完成"))

    registry = object.__new__(Command)
    registry._commands = {
        "/plugin_update": {
            "func": plugin_command,
            "description": "插件更新",
            "data": {"data": {"action": "plugin_update"}},
        },
    }
    monkeypatch.setattr(commands, "_command_class", lambda: registry)

    def publish_event(etype, data):
        """
        同步执行实际命令事件，使测试不依赖后台线程。
        """
        assert etype == EventType.CommandExcute
        registry.command_event(Event(etype, data))

    monkeypatch.setattr(agent_endpoint.eventmanager, "send_event", publish_event)
    monkeypatch.setattr(access, "_token_decoder", decode_access_token)
    monkeypatch.setattr(access, "_token_encoder", create_access_token)
    monkeypatch.setattr(access, "_token_identity_validator", lambda _payload: None)
    app = FastAPI()
    app.include_router(agent_endpoint.router)
    app.dependency_overrides[agent_endpoint.get_current_active_user] = lambda: SimpleNamespace(id=1, is_superuser=True)
    executor = MoviePilotApiExecutor(context=ApiExecutionContext(
        user_id="1", username="admin", is_admin=True,
        channel=channel.value if channel else None, channel_user_id=channel_user,
    ))

    with TestClient(app) as api_client:
        response = api_client.post(
            "/commands/run", json={"command": "/plugin_update"}, headers=executor._build_headers(),
        )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert received_events[0]["user"] == channel_user
    if expected is None:
        client.send_msg.assert_not_called()
    else:
        client.send_msg.assert_called_once()
        assert client.send_msg.call_args.kwargs["userid"] == expected


@pytest.mark.parametrize("claim_channel,claim_user", [(None, None), ("微信", "other-user")])
def test_http_command_rejects_forged_or_mismatched_channel_identity(monkeypatch, claim_channel, claim_user):
    """
    普通令牌或其他渠道的签名声明不能被请求头冒充成 Telegram 发起人。
    """
    monkeypatch.setattr(access, "_token_decoder", decode_access_token)
    monkeypatch.setattr(access, "_token_encoder", create_access_token)
    monkeypatch.setattr(access, "_token_identity_validator", lambda _payload: None)
    dispatch = Mock()
    monkeypatch.setattr(agent_endpoint.web_agent_application, "dispatch_command", dispatch)
    app = FastAPI()
    app.include_router(agent_endpoint.router)
    app.dependency_overrides[agent_endpoint.get_current_active_user] = lambda: SimpleNamespace(id=1, is_superuser=True)
    token = create_access_token(
        userid=1, username="admin", super_user=True,
        agent_channel=claim_channel, agent_channel_user=claim_user,
    )
    with TestClient(app) as client:
        response = client.post(
            "/commands/run", json={"command": "/plugin_update"}, headers={
                "Authorization": f"Bearer {token}",
                "X-MoviePilot-Agent-Channel": "Telegram",
                "X-MoviePilot-Agent-User": "123456789",
            },
        )

    assert response.status_code == 200
    assert response.json()["success"] is False
    dispatch.assert_not_called()
