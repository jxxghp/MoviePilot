"""
验证 QQ Gateway 重连与 HTTP 主动通知的独立生命周期。
"""

import json
import threading
from unittest.mock import Mock

import pytest

from app.modules.qqbot import gateway, qqbot
from app.modules.qqbot.module import QQBotModule
from app.schemas.system import NotificationConf


@pytest.mark.parametrize("opcode", [7, 9])
@pytest.mark.parametrize("ready_event", ["READY", "RESUMED"])
def test_gateway_reconnect_restores_dispatch_and_resets_backoff(
    monkeypatch: pytest.MonkeyPatch, opcode: int, ready_event: str,
) -> None:
    """
    服务端重连指令必须退出旧连接，成功鉴权后恢复短退避及入站派发。
    """
    stop_event = threading.Event()
    sockets = []
    sleeps = []
    received = Mock()
    token = Mock(return_value="token")
    heartbeat_sequences = []

    def build_websocket(_url, **callbacks):
        """
        用四轮连接覆盖成功、鉴权前失败和再次成功后的重连。
        """
        ws = Mock()
        sockets.append(ws)
        index = len(sockets)
        heartbeat_received = threading.Event()

        def send(message):
            """
            记录新连接的首个心跳，检查旧会话序号不会泄漏。
            """
            payload = json.loads(message)
            if payload["op"] == 1:
                if not heartbeat_received.is_set():
                    heartbeat_sequences.append(payload["d"])
                heartbeat_received.set()

        ws.send.side_effect = send

        def run_forever(**_kwargs):
            """
            模拟完整协议回调，不连接真实 QQ 服务。
            """
            callbacks["on_message"](ws, json.dumps({
                "op": 10, "d": {"heartbeat_interval": 1},
            }))
            assert heartbeat_received.wait(1)
            if index != 2:
                callbacks["on_message"](ws, json.dumps({
                    "op": 0, "t": ready_event, "s": 42,
                    "d": {"session_id": "session"},
                }))
            if index < 4:
                callbacks["on_message"](ws, json.dumps({"op": opcode}))
                ws.close.assert_called_once()
                callbacks["on_close"](ws, 1000, "reconnect")
            else:
                callbacks["on_message"](ws, json.dumps({
                    "op": 0, "t": "C2C_MESSAGE_CREATE", "s": 43,
                    "d": {"id": "message", "content": "hello",
                          "author": {"user_openid": "user"}},
                }))
                stop_event.set()

        ws.run_forever.side_effect = run_forever
        return ws

    monkeypatch.setattr(gateway.websocket, "WebSocketApp", build_websocket)
    monkeypatch.setattr(gateway.time, "sleep", sleeps.append)
    gateway.run_gateway(
        "app", "secret", "test", token,
        lambda _token: "wss://gateway.test", received, stop_event, [],
    )

    assert len(sockets) == token.call_count == 4
    # 第一轮成功后 1 秒，鉴权前失败后 2 秒，第三轮成功后恢复为 1 秒。
    assert sleeps == [0.1] * 40
    assert heartbeat_sequences == [None] * 4
    received.assert_called_once_with({
        "type": "C2C_MESSAGE_CREATE", "content": "hello",
        "author": {"user_openid": "user"}, "id": "message", "timestamp": "",
    })
    for ws in sockets:
        assert json.loads(ws.send.call_args_list[0].args[0])["op"] == 2


@pytest.mark.parametrize("is_group", [False, True])
@pytest.mark.parametrize("interaction_enabled", [False, True])
def test_proactive_notification_does_not_depend_on_gateway(
    monkeypatch: pytest.MonkeyPatch, is_group: bool, interaction_enabled: bool,
) -> None:
    """
    通过模块配置控制网关启动，两个模式均能通过 HTTP 向用户或群主动通知。
    """
    start_gateway = Mock()
    monkeypatch.setattr(qqbot.QQBot, "_start_gateway", start_gateway)
    monkeypatch.setattr(qqbot.QQBot, "_load_known_targets", Mock())
    conf = NotificationConf(name="qq", type="qqbot", interaction_enabled=interaction_enabled, config={
        "QQ_APP_ID": "app", "QQ_APP_SECRET": "secret",
        "QQ_OPENID": None if is_group else "user", "QQ_GROUP_OPENID": "group" if is_group else None,
    })
    module = QQBotModule()
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    module.init_module()
    client = module.get_instance(conf.name)
    assert client is not None
    send = Mock(return_value={"id": "sent"})
    monkeypatch.setattr(qqbot, "get_access_token", Mock(return_value="token"))
    monkeypatch.setattr(
        qqbot,
        "send_proactive_group_message" if is_group else "send_proactive_c2c_message",
        send,
    )

    assert client.send_msg(title="入库通知", text="整理完成") is True
    assert send.call_args.args[:2] == ("token", "group" if is_group else "user")
    assert start_gateway.call_count == int(interaction_enabled)
    assert module.stop() is True
