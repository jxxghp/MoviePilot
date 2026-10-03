import json
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest

from app.testing.bootstrap import ensure_optional_stub

# 可选三方依赖在 CI / 全新环境可能未安装，补占位避免 app.modules.feishu 导入失败
ensure_optional_stub("psutil")
ensure_optional_stub("dateparser")
ensure_optional_stub("Pinyin2Hanzi", is_pinyin=lambda value: False)

from app.modules.feishu.feishu import Feishu  # noqa: E402
from app.modules.feishu.module import FeishuModule  # noqa: E402
from app.modules.feishu.openapi import FeishuOpenApi, FeishuResponse  # noqa: E402
from app.schemas.message import (  # noqa: E402
    Message,
    MessageResponse,
)
from app.schemas.notification import (  # noqa: E402
    ChannelCapability,
    ChannelCapabilityManager,
)
from app.schemas.types import MessageType, NotificationChannel  # noqa: E402

# 返回 FeishuResponse 的 OpenAPI 方法；桩默认返回失败，用例须显式声明期望成功的调用。
_JSON_API_METHODS = (
    "create_message",
    "reply_message",
    "patch_message",
    "create_message_reaction",
    "delete_message_reaction",
    "upload_image",
    "upload_file",
    "create_card",
    "update_card_element_content",
    "update_card_settings",
)

_STREAM_METADATA_TEMPLATE = {
    "card_id": "card_stream",
    "element_id": Feishu.STREAM_CARD_BODY_ELEMENT_ID,
    "sequence": 0,
}


def _ok(**data) -> FeishuResponse:
    """构造开放平台成功响应。"""
    return FeishuResponse(code=0, msg="success", data=data, log_id="log_test")


def _message_ok(message_id="om_test", chat_id="oc_test", msg_type="interactive") -> FeishuResponse:
    """构造发送/回复消息接口的成功响应。"""
    return _ok(message_id=message_id, chat_id=chat_id, msg_type=msg_type)


def _build_api() -> MagicMock:
    """构造按 FeishuOpenApi 契约约束的桩，所有 JSON 接口默认返回失败。"""
    api = MagicMock(spec=FeishuOpenApi)
    for method in _JSON_API_METHODS:
        getattr(api, method).return_value = FeishuResponse(code=99999, msg="未设置桩响应")
    return api


def _build_client(**kwargs) -> Feishu:
    """构造不会启动长连接、OpenAPI 为桩的飞书客户端。"""
    with (
        patch.object(Feishu, "_build_api_client", return_value=_build_api()),
        patch.object(Feishu, "_start_ws_client"),
    ):
        return Feishu(
            FEISHU_APP_ID="cli_test_app_id",
            FEISHU_APP_SECRET="cli_test_app_secret",
            name="feishu-test",
            **kwargs,
        )


@pytest.fixture
def client() -> Feishu:
    """默认飞书客户端。"""
    return _build_client()


@pytest.fixture
def api(client: Feishu) -> MagicMock:
    """客户端持有的 OpenAPI 桩。"""
    return client._api_client


def _sent_content(call) -> dict:
    """解析一次 create_message / reply_message 调用中的消息内容 JSON。"""
    return json.loads(call.kwargs["content"])


def _stream_metadata(**overrides) -> dict:
    """构造流式卡片 metadata，每个用例独立一份，避免共享可变状态。"""
    return {"feishu_streaming": {**_STREAM_METADATA_TEMPLATE, **overrides}}


def _image_response(content: bytes = b"png-bytes", content_type: str = "image/png") -> MagicMock:
    """构造远程图片下载响应。"""
    response = MagicMock()
    response.content = content
    response.headers = {"Content-Type": content_type}
    return response


def _event(event_type: str, event: dict) -> bytes:
    """构造长连接收到的 2.0 版事件报文。"""
    return json.dumps(
        {"schema": "2.0", "header": {"event_type": event_type, "event_id": "ev_test"}, "event": event},
        ensure_ascii=False,
    ).encode("utf-8")


def _message_event(message: dict, open_id: str = "ou_user_evt", user_id=None) -> dict:
    """构造 im.message.receive_v1 事件体。"""
    return {
        "sender": {"sender_id": {"open_id": open_id, "user_id": user_id}},
        "message": message,
    }


# ---------------------------------------------------------------- 入站消息解析


def test_parse_message_returns_callback_message(client):
    with patch(
        "app.modules.feishu.feishu.get_configured_user_channel_lookup",
        return_value=lambda **_bindings: None,
    ):
        result = client.parse_message(
            {
                "type": "cardAction",
                "callback_data": "approve",
                "message_id": "om_123",
                "chat_id": "oc_123",
                "sender": {
                    "open_id": "ou_user_1",
                    "user_id": "u_user_1",
                    "name": "tester",
                },
            }
        )

    assert result is not None
    assert result.channel == NotificationChannel.Feishu
    assert result.userid == "ou_user_1"
    assert result.text == "CALLBACK:approve"
    assert result.is_callback
    assert result.chat_id == "oc_123"


def test_extract_card_callback_data_supports_new_and_legacy_values():
    assert Feishu._extract_card_callback_data({"callback_data": "approve"}) == "approve"
    assert Feishu._extract_card_callback_data({"value": "legacy"}) == "legacy"
    assert Feishu._extract_card_callback_data("direct") == "direct"
    assert Feishu._extract_card_callback_data({}, name="fallback") == "fallback"


def test_parse_message_blocks_non_admin_command():
    client = _build_client(FEISHU_ADMINS="ou_admin")

    with (
        patch(
            "app.modules.feishu.feishu.get_configured_user_channel_lookup",
            return_value=lambda **_bindings: None,
        ),
        patch.object(client, "send_text", return_value={"success": True}) as send_text,
    ):
        result = client.parse_message(
            {
                "type": "message",
                "text": "/help",
                "chat_id": "oc_chat_1",
                "sender": {
                    "open_id": "ou_user_2",
                    "user_id": "u_user_2",
                    "name": "tester",
                },
            }
        )

    assert result is None
    send_text.assert_called_once_with(
        "只有管理员才有权限执行此命令",
        userid="ou_user_2",
        chat_id="oc_chat_1",
        receive_id_type="open_id",
    )


def test_parse_message_maps_feishu_ids_to_moviepilot_username(client):
    get_name = MagicMock(return_value="moviepilot-user")
    with patch(
        "app.modules.feishu.feishu.get_configured_user_channel_lookup",
        return_value=get_name,
    ):
        result = client.parse_message(
            {
                "type": "message",
                "text": "/ai 添加黑客帝国订阅",
                "sender": {
                    "open_id": "ou_bound_user",
                    "user_id": "u_bound_user",
                    "name": "ou_bound_user",
                },
            }
        )

    assert result is not None
    assert result.userid == "ou_bound_user"
    assert result.username == "moviepilot-user"
    get_name.assert_called_once_with(
        feishu_openid="ou_bound_user",
        feishu_userid="u_bound_user",
    )


def test_parse_message_supports_image_and_file_payloads(client):
    with patch(
        "app.modules.feishu.feishu.get_configured_user_channel_lookup",
        return_value=lambda **_bindings: None,
    ):
        image_message = client.parse_message(
            {
                "type": "message",
                "text": "",
                "images": [{"ref": "feishu://image/img_v2_test"}],
                "message_id": "om_img",
                "chat_id": "oc_chat",
                "sender": {"open_id": "ou_user_5", "name": "tester"},
            }
        )
        file_message = client.parse_message(
            {
                "type": "message",
                "text": "",
                "files": [{"ref": "feishu://file/file_key/report.pdf", "name": "report.pdf"}],
                "message_id": "om_file",
                "chat_id": "oc_chat",
                "sender": {"open_id": "ou_user_6", "name": "tester"},
            }
        )

    assert image_message.images[0].ref == "feishu://image/img_v2_test"
    assert file_message.files[0].ref == "feishu://file/file_key/report.pdf"


# ---------------------------------------------------------------- 长连接事件分发


def test_event_handlers_cover_common_im_and_card_events(client):
    """长连接需要应答的 IM 与卡片事件都应有处理函数，避免飞书按未处理事件重推。"""
    handlers = client._event_handlers()

    assert set(handlers) >= {
        "im.message.receive_v1",
        "im.message.message_read_v1",
        "im.message.reaction.created_v1",
        "im.message.reaction.deleted_v1",
        "im.message.recalled_v1",
        "im.chat.access_event.bot_p2p_chat_entered_v1",
        "card.action.trigger",
    }
    assert all(callable(handler) for handler in handlers.values())


def test_dispatch_event_forwards_received_message_to_message_chain(client):
    event = _message_event(
        {
            "message_id": "om_text_evt",
            "chat_id": "oc_chat_evt",
            "chat_type": "group",
            "message_type": "text",
            "content": json.dumps({"text": " /search 黑客帝国 "}, ensure_ascii=False),
        },
        open_id="ou_user_evt",
        user_id="u_user_evt",
    )

    with patch.object(client, "_forward_to_message_chain") as forward:
        result = client._dispatch_event(_event("im.message.receive_v1", event))

    assert result is None
    payload = forward.call_args.args[0]
    assert payload["type"] == "message"
    assert payload["source"] == "feishu-test"
    assert payload["message_id"] == "om_text_evt"
    assert payload["chat_id"] == "oc_chat_evt"
    assert payload["chat_type"] == "group"
    assert payload["text"] == "/search 黑客帝国"
    assert payload["sender"] == {"open_id": "ou_user_evt", "user_id": "u_user_evt", "name": "ou_user_evt"}
    # 收到消息后回复同一用户应落到其所在会话。
    assert client._user_chat_mapping["ou_user_evt"] == "oc_chat_evt"


def test_dispatch_card_action_returns_toast_and_forwards_callback(client):
    """卡片回调必须同步返回 toast（由长连接编码进应答帧），并转发 callback_data。"""
    event = {
        "operator": {"open_id": None, "user_id": "u_operator"},
        "action": {"value": {"callback_data": "/subscribe 1"}, "name": "btn"},
        "context": {"open_message_id": "om_card", "open_chat_id": "oc_card"},
    }

    with patch.object(client, "_forward_to_message_chain") as forward:
        result = client._dispatch_event(_event("card.action.trigger", event))

    assert result == {"toast": {"type": "info", "content": "操作已提交"}}
    payload = forward.call_args.args[0]
    assert payload["type"] == "cardAction"
    assert payload["callback_data"] == "/subscribe 1"
    assert payload["message_id"] == "om_card"
    assert payload["chat_id"] == "oc_card"
    assert payload["sender"]["user_id"] == "u_operator"
    # 仅有 user_id 的操作者，后续回复应按 user_id 类型投递。
    assert client._resolve_target(userid="u_operator") == ("u_operator", "user_id")


@pytest.mark.parametrize(
    "event_type, event",
    [
        ("im.message.message_read_v1", {"reader": {"reader_id": {"open_id": "ou_r"}}, "message_id_list": ["om_1"]}),
        ("im.message.reaction.created_v1", {"message_id": "om_1", "reaction_type": {"emoji_type": "OK"}}),
        ("im.message.reaction.deleted_v1", {"message_id": "om_1", "reaction_type": {"emoji_type": "OK"}}),
        ("im.message.recalled_v1", {"message_id": "om_1", "chat_id": "oc_1"}),
        ("im.chat.access_event.bot_p2p_chat_entered_v1", {"chat_id": "oc_1", "operator_id": {"open_id": "ou_1"}}),
        ("contact.user.created_v3", {"object": {}}),
    ],
)
def test_dispatch_event_ignores_informational_and_unknown_events(client, event_type, event):
    """已读、表情、撤回等通知类事件与未知事件只记录日志，不进入消息链也不产生应答数据。"""
    with patch.object(client, "_forward_to_message_chain") as forward:
        result = client._dispatch_event(_event(event_type, event))

    assert result is None
    forward.assert_not_called()


def test_on_message_wraps_feishu_image_ref_with_message_id(client):
    event = _message_event(
        {
            "message_id": "om_img_evt",
            "chat_id": "oc_chat_evt",
            "chat_type": "p2p",
            "message_type": "image",
            "content": json.dumps({"image_key": "img_v2_evt"}),
        }
    )

    with patch.object(client, "_forward_to_message_chain") as forward:
        client._on_message(event)

    payload = forward.call_args.args[0]
    assert payload["images"][0]["ref"] == "feishu://image/om_img_evt/img_v2_evt"


def test_on_message_wraps_feishu_audio_ref_with_message_id(client):
    event = _message_event(
        {
            "message_id": "om_audio_evt",
            "chat_id": "oc_chat_evt",
            "chat_type": "p2p",
            "message_type": "audio",
            "content": json.dumps({"file_key": "file_audio_evt", "file_name": "voice.opus"}),
        }
    )

    with patch.object(client, "_forward_to_message_chain") as forward:
        client._on_message(event)

    payload = forward.call_args.args[0]
    assert payload["audio_refs"] == ["feishu://file/om_audio_evt/file_audio_evt/voice.opus"]


# ---------------------------------------------------------------- 长连接生命周期


class _BlockingLongConnection:
    """替身长连接：run() 阻塞到 stop()，不访问网络；可配置为忽略 stop 模拟卡死线程。"""

    instances = []

    def __init__(self, app_id, app_secret, on_event, domain, name, honor_stop=True):
        """记录构造参数，供用例核对客户端传入的配置与事件回调。"""
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_event = on_event
        self.domain = domain
        self.name = name
        self.honor_stop = honor_stop
        self.running = threading.Event()
        self.release = threading.Event()
        self.stop_calls = 0
        _BlockingLongConnection.instances.append(self)

    def run(self) -> None:
        """阻塞直到被释放。"""
        self.running.set()
        self.release.wait(timeout=10)

    def stop(self) -> None:
        """记录停止请求，按配置决定是否让 run() 返回。"""
        self.stop_calls += 1
        if self.honor_stop:
            self.release.set()


@pytest.fixture
def long_connection_factory():
    """把 Feishu 使用的 FeishuLongConnection 替换为阻塞替身，结束时释放所有线程。"""
    _BlockingLongConnection.instances = []
    options = {"honor_stop": True}

    def _factory(*args, **kwargs):
        return _BlockingLongConnection(*args, honor_stop=options["honor_stop"], **kwargs)

    with patch("app.modules.feishu.feishu.FeishuLongConnection", side_effect=_factory):
        yield options
    for instance in _BlockingLongConnection.instances:
        instance.release.set()


def _start_client() -> Feishu:
    """构造会真实启动长连接线程的客户端（长连接为替身，OpenAPI 为桩）。"""
    with patch.object(Feishu, "_build_api_client", return_value=_build_api()):
        return Feishu(
            FEISHU_APP_ID="cli_test_app_id",
            FEISHU_APP_SECRET="cli_test_app_secret",
            name="feishu-test",
        )


def test_start_ws_client_runs_long_connection_in_background_thread(long_connection_factory):
    client = _start_client()
    connection = _BlockingLongConnection.instances[0]

    assert connection.running.wait(timeout=2)
    assert client._ws_thread.is_alive()
    assert client._ws_thread is not threading.current_thread()
    assert client.get_state()
    assert (connection.app_id, connection.app_secret, connection.name) == (
        "cli_test_app_id",
        "cli_test_app_secret",
        "feishu-test",
    )
    # 长连接收到的事件应交给该客户端自己的分发入口。
    with patch.object(client, "_forward_to_message_chain"):
        toast = connection.on_event(
            _event("card.action.trigger", {"operator": {"open_id": "ou_1"}, "action": {"value": "ok"}})
        )
    assert toast["toast"]["content"] == "操作已提交"

    assert client.stop()


def test_stop_stops_long_connection_joins_thread_and_closes_api(long_connection_factory):
    client = _start_client()
    connection = _BlockingLongConnection.instances[0]
    assert connection.running.wait(timeout=2)
    api = client._api_client

    assert client.stop() is True

    assert connection.stop_calls == 1
    assert not client._ws_thread.is_alive()
    assert not client.get_state()
    api.close.assert_called_once()


def test_stop_returns_false_when_thread_exceeds_join_budget(long_connection_factory):
    long_connection_factory["honor_stop"] = False
    client = _start_client()
    client._ws_join_timeout_seconds = 0.05
    connection = _BlockingLongConnection.instances[0]
    assert connection.running.wait(timeout=2)
    api = client._api_client

    assert client.stop() is False

    assert connection.stop_calls == 1
    assert client._ws_thread.is_alive()
    # 线程仍可能在使用 OpenAPI 客户端，未退出时不得关闭 HTTP 会话。
    api.close.assert_not_called()


def test_run_ws_client_marks_ready_only_while_running(client):
    observed = []
    client._ws_client = MagicMock()
    client._ws_client.run.side_effect = lambda: observed.append(client.get_state())

    client._run_ws_client()

    assert observed == [True]
    assert not client.get_state()


def test_run_ws_client_swallows_long_connection_error(client):
    client._ws_client = MagicMock()
    client._ws_client.run.side_effect = RuntimeError("fatal")

    client._run_ws_client()

    assert not client.get_state()


# ---------------------------------------------------------------- 发送通知与卡片


def test_send_notification_uses_direct_card_content(client, api):
    api.create_message.return_value = _message_ok()

    result = client.send_notification(
        Message(
            title="测试标题",
            text="测试正文",
            buttons=[[{"text": "确认", "callback_data": "confirm"}]],
        ),
        userid="ou_user_3",
    )

    assert result["success"]
    call = api.create_message.call_args
    assert call.kwargs["receive_id_type"] == "open_id"
    assert call.kwargs["receive_id"] == "ou_user_3"
    assert call.kwargs["msg_type"] == "interactive"
    assert call.kwargs["uuid"]

    content = _sent_content(call)
    assert "card" not in content
    assert content["schema"] == "2.0"
    assert content["config"]["update_multi"]
    assert content["body"]["padding"] == "12px 12px 12px 12px"
    assert content["body"]["elements"][0]["text_size"] == "heading"
    assert content["body"]["elements"][0]["tag"] == "markdown"
    button = content["body"]["elements"][-1]["columns"][0]["elements"][0]
    assert button["tag"] == "button"
    assert "value" not in button
    assert button["behaviors"] == [{"type": "callback", "value": {"callback_data": "confirm"}}]


def test_send_notification_returns_failure_when_api_rejects(client, api):
    api.create_message.return_value = FeishuResponse(code=230002, msg="bot not in chat")

    result = client.send_notification(Message(title="标题", text="正文"), userid="ou_user_3")

    assert result == {"success": False}


def test_send_notification_keeps_markdown_images_for_normal_card(client, api):
    api.create_message.return_value = _message_ok()

    result = client.send_notification(
        Message(
            title="普通通知",
            text="海报：![poster](https://example.com/poster.jpg)",
        ),
        userid="ou_user_img_md",
    )

    assert result["success"]
    content = _sent_content(api.create_message.call_args)
    assert content["body"]["elements"][1]["content"] == "海报：![poster](https://example.com/poster.jpg)"


def test_send_notification_embeds_remote_image_in_card(client, api):
    uploaded = {}

    def _upload_image(file_path: Path):
        uploaded["path"] = file_path
        uploaded["bytes"] = file_path.read_bytes()
        return _ok(image_key="img_v2_remote")

    api.upload_image.side_effect = _upload_image
    api.create_message.return_value = _message_ok()
    response = _image_response()

    with patch("app.modules.feishu.feishu.RequestUtils") as request_utils:
        request_utils.return_value.get_res.return_value = response
        result = client.send_notification(
            Message(
                title="测试标题",
                text="测试正文",
                image="https://example.com/poster.png",
                buttons=[[{"text": "确认", "callback_data": "confirm"}]],
            ),
            userid="ou_user_img",
        )

    assert result["success"]
    response.close.assert_called_once()
    api.upload_image.assert_called_once()
    assert uploaded["bytes"] == b"png-bytes"
    assert uploaded["path"].suffix == ".png"
    # 下载到的临时图片上传后必须清理。
    assert not uploaded["path"].exists()
    call = api.create_message.call_args
    assert call.kwargs["msg_type"] == "interactive"
    content = _sent_content(call)
    assert content["body"]["padding"] == "0px 0px 0px 0px"
    image_element = content["body"]["elements"][0]
    assert image_element["tag"] == "img"
    assert image_element["img_key"] == "img_v2_remote"
    assert content["body"]["elements"][1]["margin"] == "12px 12px 0px 12px"
    assert content["body"]["elements"][2]["margin"] == "4px 12px 12px 12px"
    assert content["body"]["elements"][-1]["margin"] == "0px 12px 12px 12px"
    assert content["body"]["elements"][-1]["tag"] == "column_set"


def test_send_notification_supports_user_id_target(client, api):
    api.create_message.return_value = _message_ok()

    client.send_notification(
        Message(title="测试标题", text="测试正文"),
        userid="u_user_4",
        receive_id_type="user_id",
    )

    call = api.create_message.call_args
    assert call.kwargs["receive_id_type"] == "user_id"
    assert call.kwargs["receive_id"] == "u_user_4"


def test_edit_message_uses_patch_api_for_cards(client, api):
    api.patch_message.return_value = _ok()

    success = client.edit_message(
        message_id="om_456",
        title="测试标题",
        text="测试正文",
        buttons=[[{"text": "确认", "callback_data": "confirm"}]],
    )

    assert success
    api.patch_message.assert_called_once()
    # 带按钮的普通卡片走 IM PATCH，不得误入 CardKit 流式更新。
    api.update_card_element_content.assert_not_called()

    message_id, card_json = api.patch_message.call_args.args
    assert message_id == "om_456"
    content = json.loads(card_json)
    assert "card" not in content
    assert content["schema"] == "2.0"
    assert content["config"]["update_multi"]
    assert content["body"]["elements"][0]["tag"] == "markdown"
    button = content["body"]["elements"][-1]["columns"][0]["elements"][0]
    assert button["behaviors"] == [{"type": "callback", "value": {"callback_data": "confirm"}}]


def test_send_notification_replies_when_original_message_id_is_present(client, api):
    api.reply_message.return_value = _message_ok(message_id="om_reply")

    result = client.send_notification(
        Message(title="回复标题", text="回复正文"),
        userid="ou_user_9",
        original_message_id="om_origin",
    )

    assert result["success"]
    assert result["message_id"] == "om_reply"
    api.create_message.assert_not_called()
    api.reply_message.assert_called_once()
    call = api.reply_message.call_args
    assert call.kwargs["message_id"] == "om_origin"
    assert call.kwargs["msg_type"] == "interactive"
    assert call.kwargs["reply_in_thread"] is False
    assert _sent_content(call)["schema"] == "2.0"


def test_message_reaction_create_and_delete_use_official_api(client, api):
    api.create_message_reaction.return_value = _ok(reaction_id="reaction_1")
    api.delete_message_reaction.return_value = _ok()

    reaction_id = client.add_message_reaction("om_origin", Feishu.PROCESSING_REACTION_EMOJI)
    deleted = client.delete_message_reaction("om_origin", "reaction_1")

    assert reaction_id == "reaction_1"
    assert deleted
    api.create_message_reaction.assert_called_once_with("om_origin", Feishu.PROCESSING_REACTION_EMOJI)
    api.delete_message_reaction.assert_called_once_with("om_origin", "reaction_1")


def test_message_reaction_failures_return_empty_results(client, api):
    api.create_message_reaction.return_value = FeishuResponse(code=231001, msg="denied")
    api.delete_message_reaction.return_value = FeishuResponse(code=231001, msg="denied")

    assert client.add_message_reaction("om_origin", Feishu.PROCESSING_REACTION_EMOJI) is None
    assert client.delete_message_reaction("om_origin", "reaction_1") is False


# ---------------------------------------------------------------- 流式卡片


def test_send_notification_uses_streaming_card_for_agent_text(client, api):
    api.create_card.return_value = _ok(card_id="card_stream")
    api.create_message.return_value = _message_ok(message_id="om_stream", chat_id="oc_stream")

    result = client.send_notification(
        Message(mtype=MessageType.Agent, title="MoviePilot助手", text="第一帧内容"),
        userid="ou_user_stream",
    )

    assert result["success"]
    assert result["metadata"]["feishu_streaming"]["card_id"] == "card_stream"
    assert result["metadata"]["feishu_streaming"]["sequence"] == 0
    assert result["metadata"]["feishu_streaming"]["sent_image_urls"] == []
    card_payload = json.loads(api.create_card.call_args.args[0])
    assert card_payload["config"]["streaming_mode"]
    assert card_payload["body"]["elements"][-1]["element_id"] == Feishu.STREAM_CARD_BODY_ELEMENT_ID
    assert card_payload["body"]["elements"][-1]["content"] == "第一帧内容"
    call = api.create_message.call_args
    assert call.kwargs["msg_type"] == "interactive"
    assert _sent_content(call) == {"type": "card", "data": {"card_id": "card_stream"}}


def test_streaming_card_not_sent_when_card_creation_fails(client, api):
    api.create_card.return_value = FeishuResponse(code=200740, msg="invalid card")

    result = client.send_notification(
        Message(mtype=MessageType.Agent, title="MoviePilot助手", text="第一帧内容"),
        userid="ou_user_stream",
    )

    assert result == {"success": False}
    api.create_message.assert_not_called()


def test_streaming_card_sends_markdown_images_separately(client, api):
    api.create_card.return_value = _ok(card_id="card_stream")
    api.create_message.return_value = _message_ok(message_id="om_stream", chat_id="oc_stream")
    api.upload_image.return_value = _ok(image_key="img_v2_stream")

    with patch("app.modules.feishu.feishu.RequestUtils") as request_utils:
        request_utils.return_value.get_res.return_value = _image_response(content_type="image/jpeg")
        result = client.send_notification(
            Message(
                mtype=MessageType.Agent,
                title="MoviePilot助手",
                text="找到海报 ![poster](https://example.com/poster.jpg)\n[详情](https://example.com/detail)",
            ),
            userid="ou_user_stream",
        )

    assert result["success"]
    card_payload = json.loads(api.create_card.call_args.args[0])
    body_content = card_payload["body"]["elements"][-1]["content"]
    assert "![poster]" not in body_content
    assert "poster.jpg" not in body_content
    assert "poster" in body_content
    assert "[详情](https://example.com/detail)" in body_content
    assert api.upload_image.call_count == 1
    assert api.create_message.call_count == 2
    assert result["metadata"]["feishu_streaming"]["sent_image_urls"] == ["https://example.com/poster.jpg"]
    image_payload = _sent_content(api.create_message.call_args_list[-1])
    assert image_payload["body"]["elements"][0]["img_key"] == "img_v2_stream"


def test_streaming_card_sends_notification_image_separately(client, api):
    api.create_card.return_value = _ok(card_id="card_stream")
    api.create_message.return_value = _message_ok(message_id="om_stream", chat_id="oc_stream")
    api.upload_image.return_value = _ok(image_key="img_v2_agent_image")

    with patch("app.modules.feishu.feishu.RequestUtils") as request_utils:
        request_utils.return_value.get_res.return_value = _image_response()
        result = client.send_notification(
            Message(
                mtype=MessageType.Agent,
                title="MoviePilot助手",
                text="第一帧内容",
                image="https://example.com/agent.png",
            ),
            userid="ou_user_stream",
        )

    assert result["success"]
    assert api.upload_image.call_count == 1
    assert api.create_message.call_count == 2
    assert result["metadata"]["feishu_streaming"]["sent_image_urls"] == ["https://example.com/agent.png"]
    stream_call, image_call = api.create_message.call_args_list
    assert _sent_content(stream_call)["data"]["card_id"] == "card_stream"
    assert _sent_content(image_call)["body"]["elements"][0]["img_key"] == "img_v2_agent_image"


def test_send_notification_replies_with_streaming_card_for_agent_text(client, api):
    api.create_card.return_value = _ok(card_id="card_stream")
    api.reply_message.return_value = _message_ok(message_id="om_reply", chat_id="oc_stream")

    result = client.send_notification(
        Message(mtype=MessageType.Agent, title="MoviePilot助手", text="第一帧内容"),
        userid="ou_user_stream",
        original_message_id="om_origin",
    )

    assert result["success"]
    api.create_message.assert_not_called()
    call = api.reply_message.call_args
    assert call.kwargs["message_id"] == "om_origin"
    assert call.kwargs["msg_type"] == "interactive"
    assert _sent_content(call)["data"]["card_id"] == "card_stream"
    assert result["metadata"]["feishu_streaming"]["sequence"] == 0


def test_edit_replied_streaming_card_uses_first_increment_sequence(client, api):
    api.update_card_element_content.return_value = _ok()
    metadata = _stream_metadata(sent_image_urls=["https://example.com/poster.jpg"])

    success = client.edit_message(message_id="om_reply", text="补充内容", metadata=metadata)

    assert success
    api.patch_message.assert_not_called()
    assert api.update_card_element_content.call_args.kwargs["sequence"] == 1
    assert metadata["feishu_streaming"]["sequence"] == 1


def test_edit_message_uses_cardkit_content_for_streaming_card(client, api):
    api.update_card_element_content.return_value = _ok()

    success = client.edit_message(
        message_id="om_stream",
        text="第二帧内容",
        metadata=_stream_metadata(sent_image_urls=["https://example.com/poster.jpg"]),
    )

    assert success
    api.update_card_element_content.assert_called_once()
    api.patch_message.assert_not_called()
    kwargs = api.update_card_element_content.call_args.kwargs
    assert kwargs["card_id"] == "card_stream"
    assert kwargs["element_id"] == Feishu.STREAM_CARD_BODY_ELEMENT_ID
    assert kwargs["content"] == "第二帧内容"
    assert kwargs["sequence"] == 1
    assert kwargs["uuid"]


def test_edit_streaming_card_sequence_keeps_increasing_after_failed_update(client, api):
    """某次流式更新失败后，下一次仍须使用更大的 sequence，否则会被 CardKit 持续拒绝。"""
    api.update_card_element_content.side_effect = [
        FeishuResponse(code=-1, msg="timeout"),
        _ok(),
    ]
    metadata = _stream_metadata(sequence=4, sent_image_urls=[])

    first = client.edit_message(message_id="om_stream", text="第一次", metadata=metadata)
    second = client.edit_message(message_id="om_stream", text="第二次", metadata=metadata)

    assert first is False
    assert second is True
    sequences = [call.kwargs["sequence"] for call in api.update_card_element_content.call_args_list]
    assert sequences == [5, 6]
    # 流式更新失败不得降级为普通卡片 PATCH。
    api.patch_message.assert_not_called()


def test_edit_streaming_card_removes_markdown_image_syntax(client, api):
    api.update_card_element_content.return_value = _ok()

    success = client.edit_message(
        message_id="om_stream",
        text="第二帧 ![poster](https://example.com/poster.jpg)",
        metadata=_stream_metadata(sent_image_urls=["https://example.com/poster.jpg"]),
    )

    assert success
    api.patch_message.assert_not_called()
    assert api.update_card_element_content.call_args.kwargs["content"] == "第二帧 poster"


def test_edit_streaming_card_keeps_normal_markdown_links(client, api):
    api.update_card_element_content.return_value = _ok()

    success = client.edit_message(
        message_id="om_stream",
        text="第二帧 [详情](https://example.com/detail)",
        chat_id="oc_stream",
        metadata=_stream_metadata(sent_image_urls=[]),
    )

    assert success
    api.patch_message.assert_not_called()
    api.upload_image.assert_not_called()
    assert api.update_card_element_content.call_args.kwargs["content"] == "第二帧 [详情](https://example.com/detail)"


def test_edit_streaming_card_hides_incomplete_markdown_image(client, api):
    api.update_card_element_content.return_value = _ok()

    success = client.edit_message(
        message_id="om_stream",
        text="第二帧 ![poster](https://example.com/poster",
        chat_id="oc_stream",
        metadata=_stream_metadata(sent_image_urls=[]),
    )

    assert success
    api.patch_message.assert_not_called()
    api.upload_image.assert_not_called()
    assert api.update_card_element_content.call_args.kwargs["content"] == "第二帧"


def test_edit_streaming_card_hides_incomplete_markdown_image_alt_text(client, api):
    api.update_card_element_content.return_value = _ok()

    success = client.edit_message(
        message_id="om_stream",
        text="第二帧 ![poster",
        chat_id="oc_stream",
        metadata=_stream_metadata(sent_image_urls=[]),
    )

    assert success
    api.patch_message.assert_not_called()
    api.upload_image.assert_not_called()
    assert api.update_card_element_content.call_args.kwargs["content"] == "第二帧"


def test_edit_streaming_card_sends_completed_markdown_image_once(client, api):
    api.update_card_element_content.return_value = _ok()
    api.create_message.return_value = _message_ok(message_id="om_img", chat_id="oc_stream")
    api.upload_image.return_value = _ok(image_key="img_v2_stream_edit")
    metadata = _stream_metadata(sent_image_urls=[])

    with patch("app.modules.feishu.feishu.RequestUtils") as request_utils:
        request_utils.return_value.get_res.return_value = _image_response(b"jpg-bytes", "image/jpeg")
        first_success = client.edit_message(
            message_id="om_stream",
            text="第二帧 ![poster](https://example.com/poster.jpg)",
            chat_id="oc_stream",
            metadata=metadata,
        )
        second_success = client.edit_message(
            message_id="om_stream",
            text="第二帧 ![poster](https://example.com/poster.jpg)",
            chat_id="oc_stream",
            metadata=metadata,
        )

    assert first_success
    assert second_success
    assert api.upload_image.call_count == 1
    assert metadata["feishu_streaming"]["sent_image_urls"] == ["https://example.com/poster.jpg"]
    api.create_message.assert_called_once()
    call = api.create_message.call_args
    assert call.kwargs["receive_id"] == "oc_stream"
    assert call.kwargs["receive_id_type"] == "chat_id"
    assert _sent_content(call)["body"]["elements"][0]["img_key"] == "img_v2_stream_edit"


def test_edit_streaming_card_skips_non_image_markdown_target(client, api):
    api.update_card_element_content.return_value = _ok()
    metadata = _stream_metadata(sent_image_urls=[])

    with patch("app.modules.feishu.feishu.RequestUtils") as request_utils:
        request_utils.return_value.get_res.return_value = _image_response(b"<html></html>", "text/html")
        success = client.edit_message(
            message_id="om_stream",
            text="第二帧 ![link](https://example.com/detail)",
            chat_id="oc_stream",
            metadata=metadata,
        )

    assert success
    api.upload_image.assert_not_called()
    assert metadata["feishu_streaming"]["sent_image_urls"] == []
    api.create_message.assert_not_called()


def test_close_streaming_card_updates_card_settings(client, api):
    api.update_card_settings.return_value = _ok()

    success = client.close_streaming_card(card_id="card_stream", sequence=3)

    assert success
    kwargs = api.update_card_settings.call_args.kwargs
    assert kwargs["card_id"] == "card_stream"
    assert kwargs["sequence"] == 3
    assert json.loads(kwargs["settings"]) == {"config": {"streaming_mode": False}}


def test_close_streaming_card_reports_failure(client, api):
    api.update_card_settings.return_value = FeishuResponse(code=300309, msg="sequence")

    assert client.close_streaming_card(card_id="card_stream", sequence=3) is False


# ---------------------------------------------------------------- 文件、语音与下载


def test_feishu_channel_capabilities_enable_images_and_files():
    assert ChannelCapabilityManager.supports_capability(
        NotificationChannel.Feishu,
        ChannelCapability.IMAGES,
    )
    assert ChannelCapabilityManager.supports_capability(
        NotificationChannel.Feishu,
        ChannelCapability.FILE_SENDING,
    )


def test_send_file_uploads_image_then_sends_mixed_card(client, api):
    api.upload_image.return_value = _ok(image_key="img_v2_uploaded")
    api.create_message.return_value = _message_ok(message_id="om_image")

    with tempfile.NamedTemporaryFile(suffix=".png") as fp:
        fp.write(b"png-bytes")
        fp.flush()
        result = client.send_file(
            file_path=fp.name,
            userid="ou_user_7",
            title="图片标题",
            text="图片说明",
        )

    assert result["success"]
    api.upload_image.assert_called_once_with(Path(fp.name))
    api.upload_file.assert_not_called()
    call = api.create_message.call_args
    assert call.kwargs["msg_type"] == "interactive"
    content = _sent_content(call)
    assert content["body"]["padding"] == "0px 0px 0px 0px"
    assert content["body"]["elements"][0]["img_key"] == "img_v2_uploaded"
    assert content["body"]["elements"][1]["content"] == "图片标题"
    assert content["body"]["elements"][1]["margin"] == "12px 12px 0px 12px"
    assert content["body"]["elements"][2]["content"] == "图片说明"
    assert content["body"]["elements"][2]["margin"] == "4px 12px 12px 12px"


def test_send_file_keeps_non_image_file_message_and_caption(client, api):
    api.upload_file.return_value = _ok(file_key="file_doc")
    api.create_message.return_value = _message_ok(message_id="om_file")

    with (
        tempfile.NamedTemporaryFile(suffix=".txt") as fp,
        patch.object(client, "send_text", return_value={"success": True}) as send_text,
    ):
        fp.write(b"text-bytes")
        fp.flush()
        result = client.send_file(
            file_path=fp.name,
            userid="ou_user_7",
            title="文件标题",
            text="文件说明",
        )

    assert result["success"]
    api.upload_file.assert_called_once_with(
        Path(fp.name),
        file_type="stream",
        file_name=Path(fp.name).name,
        duration=None,
    )
    call = api.create_message.call_args
    assert call.kwargs["msg_type"] == "file"
    assert _sent_content(call) == {"file_key": "file_doc"}
    send_text.assert_called_once()
    assert "文件标题" in send_text.call_args.args[0]
    assert "文件说明" in send_text.call_args.args[0]


def test_send_file_fails_without_sending_when_upload_rejected(client, api):
    api.upload_file.return_value = FeishuResponse(code=234001, msg="too large")

    with tempfile.NamedTemporaryFile(suffix=".pdf") as fp:
        fp.write(b"pdf-bytes")
        fp.flush()
        result = client.send_file(file_path=fp.name, userid="ou_user_7")

    assert result == {"success": False}
    assert api.upload_file.call_args.kwargs["file_type"] == "pdf"
    api.create_message.assert_not_called()


def test_send_voice_uploads_audio_file_and_optionally_sends_caption(client, api):
    api.upload_file.return_value = _ok(file_key="file_audio")
    api.create_message.return_value = _message_ok(message_id="om_audio")

    with tempfile.NamedTemporaryFile(suffix=".opus") as fp:
        fp.write(b"opus-bytes")
        fp.flush()
        with patch.object(client, "send_text", return_value={"success": True}) as send_text:
            result = client.send_voice(
                voice_path=fp.name,
                userid="ou_user_8",
                caption="这是说明",
            )

    assert result["success"]
    assert api.upload_file.call_args.kwargs["file_type"] == "opus"
    call = api.create_message.call_args
    assert call.kwargs["msg_type"] == "audio"
    assert _sent_content(call) == {"file_key": "file_audio"}
    send_text.assert_called_once()
    assert send_text.call_args.args[0] == "这是说明"


def test_download_helpers_return_bytes_and_metadata(client, api):
    api.download_image.return_value = (b"image-bytes", "poster.png", "image/png")
    api.download_file.return_value = (b"file-bytes", "report.txt", "text/plain")
    api.download_message_resource.return_value = (b"resource-bytes", "voice.opus", "audio/ogg")

    image_download = client.download_image_bytes("img_v2_test")
    file_download = client.download_file_bytes("file_test")
    resource_download = client.download_message_resource_bytes("om_test", "file_test", "audio")

    assert image_download == (b"image-bytes", "poster.png", "image/png")
    assert file_download == (b"file-bytes", "report.txt", "text/plain")
    assert resource_download == (b"resource-bytes", "voice.opus", "audio/ogg")
    api.download_image.assert_called_once_with("img_v2_test")
    api.download_file.assert_called_once_with("file_test")
    api.download_message_resource.assert_called_once_with("om_test", "file_test", "audio")


def test_download_helpers_skip_api_for_missing_keys(client, api):
    assert client.download_image_bytes("") is None
    assert client.download_file_bytes("") is None
    assert client.download_message_resource_bytes("", "file_test", "audio") is None
    api.download_image.assert_not_called()
    api.download_file.assert_not_called()
    api.download_message_resource.assert_not_called()


# ---------------------------------------------------------------- 广播与目标解析


def test_send_notification_broadcasts_to_remembered_chats_without_target(client, api):
    """无显式目标且未配置默认目标时，应回退向最近互动过的会话发送。"""
    api.create_message.return_value = _message_ok()
    client._user_chat_mapping = {
        "ou_user_a": "oc_group_a",
        "ou_user_b": "oc_group_a",
        "ou_user_c": "oc_p2p_c",
    }

    result = client.send_notification(Message(title="插件通知", text="无目标通知"))

    assert result["success"]
    assert api.create_message.call_count == 2
    calls = api.create_message.call_args_list
    assert sorted(call.kwargs["receive_id"] for call in calls) == ["oc_group_a", "oc_p2p_c"]
    assert all(call.kwargs["receive_id_type"] == "chat_id" for call in calls)
    # 每次发送携带独立幂等 uuid，避免飞书把不同会话的消息判为重复请求。
    assert len({call.kwargs["uuid"] for call in calls}) == 2


def test_send_without_target_raises_config_hint_when_nothing_available(client, api):
    """既无目标又无历史互动时，应报出含配置指引的明确错误。"""
    api.create_message.return_value = _message_ok()

    with pytest.raises(ValueError) as exc_info:
        client._send_with_fallback_broadcast("text", {"text": "hello"})

    assert "FEISHU_OPEN_ID" in str(exc_info.value)
    assert "FEISHU_CHAT_ID" in str(exc_info.value)
    api.create_message.assert_not_called()


# ---------------------------------------------------------------- FeishuModule


def test_module_send_direct_message_prefers_open_id_target():
    module = FeishuModule()
    module._channel = NotificationChannel.Feishu
    conf = SimpleNamespace(name="feishu-main")
    client = MagicMock()
    client.send_notification.return_value = {
        "success": True,
        "message_id": "om_789",
        "chat_id": "oc_789",
    }

    with (
        patch.object(module, "get_configs", return_value={"feishu-main": conf}),
        patch.object(module, "check_message", return_value=True),
        patch.object(module, "get_instance", return_value=client),
    ):
        response = module.send_direct_message(
            Message(
                targets={
                    "feishu_userid": "u_target",
                    "feishu_openid": "ou_target",
                }
            )
        )

    client.send_notification.assert_called_once_with(
        message=ANY,
        userid="ou_target",
        chat_id=None,
        receive_id_type="open_id",
        original_message_id=None,
    )
    assert response.success
    assert response.message_id == "om_789"
    assert response.chat_id == "oc_789"


def test_module_plain_direct_message_uses_literal_text_transport():
    """纯文本直发不得进入会解释密钥字符的 Markdown 卡片路径。"""
    module = FeishuModule()
    module._channel = NotificationChannel.Feishu
    conf = SimpleNamespace(name="feishu-main")
    client = MagicMock()
    client.send_text.return_value = {
        "success": True,
        "message_id": "om_plain",
        "chat_id": "oc_plain",
    }
    literal_text = "G2A1_PROTECTED_MARKER_20260812\n**literal markdown**\n<img src=x>"

    with (
        patch.object(module, "get_configs", return_value={"feishu-main": conf}),
        patch.object(module, "check_message", return_value=True),
        patch.object(module, "get_instance", return_value=client),
    ):
        response = module.send_direct_message(
            Message(
                channel=NotificationChannel.Feishu,
                source="feishu-main",
                userid="ou_target",
                text=literal_text,
                private_delivery=True,
                parse_mode="plain",
            )
        )

    client.send_text.assert_called_once_with(
        text=literal_text,
        userid="ou_target",
        chat_id=None,
        receive_id_type=None,
        original_message_id=None,
    )
    client.send_notification.assert_not_called()
    assert response.success


def test_module_download_helpers_delegate_to_client():
    module = FeishuModule()
    client = MagicMock()
    client.download_image_bytes.return_value = (b"image", "poster.png", "image/png")
    client.download_file_bytes.return_value = (b"file", "note.txt", "text/plain")
    client.download_message_resource_bytes.return_value = (b"image", "poster.png", "image/png")

    with (
        patch.object(module, "get_config", return_value=SimpleNamespace(name="feishu-main")),
        patch.object(module, "get_instance", return_value=client),
    ):
        data_url = module.download_feishu_image_to_data_url("feishu://image/om_msg/img_v2_xxx", "feishu-main")
        file_bytes = module.download_feishu_file_bytes("feishu://file/file_xxx/note.txt", "feishu-main")
        audio_bytes = module.download_feishu_file_bytes(
            "feishu://file/om_audio/file_audio/voice.opus",
            "feishu-main",
        )

    assert data_url.startswith("data:image/png;base64,")
    assert file_bytes == b"file"
    assert audio_bytes == b"image"
    client.download_message_resource_bytes.assert_any_call(
        message_id="om_msg",
        file_key="img_v2_xxx",
        resource_type="image",
    )
    client.download_message_resource_bytes.assert_any_call(
        message_id="om_audio",
        file_key="file_audio",
        resource_type="audio",
    )


def test_module_message_reaction_helpers_delegate_to_client():
    module = FeishuModule()
    client = MagicMock()
    client.add_message_reaction.return_value = "reaction_2"
    client.delete_message_reaction.return_value = True

    with (
        patch.object(module, "get_config", return_value=SimpleNamespace(name="feishu-main")),
        patch.object(module, "get_instance", return_value=client),
    ):
        reaction_id = module.add_feishu_message_reaction("om_x", "GLANCE", "feishu-main")
        deleted = module.delete_feishu_message_reaction("om_x", "reaction_2", "feishu-main")

    assert reaction_id == "reaction_2"
    assert deleted


def test_module_processing_status_uses_reaction_helpers():
    module = FeishuModule()
    module._channel = NotificationChannel.Feishu

    with (
        patch.object(module, "add_feishu_message_reaction", return_value="reaction_processing") as add_reaction,
        patch.object(module, "delete_feishu_message_reaction", return_value=True) as delete_reaction,
    ):
        status = module.mark_message_processing_started(
            channel=NotificationChannel.Feishu,
            source="feishu-main",
            userid="ou_x",
            message_id="om_x",
            chat_id="oc_x",
            text="hello",
        )
        deleted = module.mark_message_processing_finished(
            channel=NotificationChannel.Feishu,
            source="feishu-main",
            userid="ou_x",
            status=status,
        )

    add_reaction.assert_called_once_with(
        message_id="om_x",
        emoji_type="GLANCE",
        source="feishu-main",
    )
    delete_reaction.assert_called_once_with(
        message_id="om_x",
        reaction_id="reaction_processing",
        source="feishu-main",
    )
    assert status["metadata"]["reaction_id"] == "reaction_processing"
    assert deleted


def test_module_finalize_message_closes_streaming_card():
    module = FeishuModule()
    module._channel = NotificationChannel.Feishu
    client = MagicMock()
    client.close_streaming_card.return_value = True

    with (
        patch.object(module, "get_config", return_value=SimpleNamespace(name="feishu-main")),
        patch.object(module, "get_instance", return_value=client),
    ):
        success = module.finalize_message(
            MessageResponse(
                message_id="om_stream",
                chat_id="oc_stream",
                channel=NotificationChannel.Feishu,
                source="feishu-main",
                metadata={"feishu_streaming": {"card_id": "card_stream", "sequence": 2}},
                success=True,
            )
        )

    assert success
    client.close_streaming_card.assert_called_once_with(card_id="card_stream", sequence=3)


def test_module_post_message_prefers_file_and_voice_paths():
    module = FeishuModule()
    conf = SimpleNamespace(name="feishu-main")
    client = MagicMock()

    with (
        patch.object(module, "get_configs", return_value={"feishu-main": conf}),
        patch.object(module, "check_message", return_value=True),
        patch.object(module, "get_instance", return_value=client),
    ):
        module.post_message(
            Message(
                file_path="/tmp/demo.txt",
                text="说明",
                title="标题",
                userid="ou_user",
            )
        )
        module.post_message(
            Message(
                voice_path="/tmp/demo.opus",
                voice_caption="语音说明",
                userid="ou_user",
            )
        )

    client.send_file.assert_called_once()
    client.send_voice.assert_called_once()


def test_module_post_message_sends_image_card_before_file_attachment():
    module = FeishuModule()
    conf = SimpleNamespace(name="feishu-main")
    client = MagicMock()

    with (
        patch.object(module, "get_configs", return_value={"feishu-main": conf}),
        patch.object(module, "check_message", return_value=True),
        patch.object(module, "get_instance", return_value=client),
    ):
        module.post_message(
            Message(
                file_path="/tmp/demo.txt",
                file_name="demo.txt",
                image="https://example.com/poster.png",
                text="说明",
                title="标题",
                userid="ou_user",
            )
        )

    client.send_notification.assert_called_once()
    sent_message = client.send_notification.call_args.kwargs["message"]
    assert sent_message.image == "https://example.com/poster.png"
    assert sent_message.file_path is None
    client.send_file.assert_called_once()
    assert client.send_file.call_args.kwargs.get("title") is None
    assert client.send_file.call_args.kwargs.get("text") is None


def test_module_send_direct_message_sends_image_card_before_file_attachment():
    module = FeishuModule()
    module._channel = NotificationChannel.Feishu
    conf = SimpleNamespace(name="feishu-main")
    client = MagicMock()
    client.send_notification.return_value = {
        "success": True,
        "message_id": "om_card",
        "chat_id": "oc_card",
    }
    client.send_file.return_value = {"success": True, "message_id": "om_file"}

    with (
        patch.object(module, "get_configs", return_value={"feishu-main": conf}),
        patch.object(module, "check_message", return_value=True),
        patch.object(module, "get_instance", return_value=client),
    ):
        response = module.send_direct_message(
            Message(
                channel=NotificationChannel.Feishu,
                source="feishu-main",
                file_path="/tmp/demo.txt",
                file_name="demo.txt",
                image="https://example.com/poster.png",
                text="说明",
                title="标题",
                userid="ou_user",
            )
        )

    assert response.success
    assert response.message_id == "om_card"
    client.send_notification.assert_called_once()
    client.send_file.assert_called_once()


def test_module_post_message_passes_original_message_id_for_reply():
    module = FeishuModule()
    conf = SimpleNamespace(name="feishu-main")
    client = MagicMock()

    with (
        patch.object(module, "get_configs", return_value={"feishu-main": conf}),
        patch.object(module, "check_message", return_value=True),
        patch.object(module, "get_instance", return_value=client),
    ):
        module.post_message(
            Message(
                title="标题",
                text="正文",
                userid="ou_user",
                original_message_id="om_source",
                original_chat_id="oc_source",
            )
        )

    client.send_notification.assert_called_once()
    assert client.send_notification.call_args.kwargs["original_message_id"] == "om_source"


def test_module_resolve_message_target_prefers_original_chat_id():
    """群聊@回复必须优先发回原会话，而不是按 open_id 发到私聊。"""
    userid, chat_id, receive_id_type = FeishuModule._resolve_message_target(
        Message(userid="ou_user", original_chat_id="oc_group")
    )
    assert userid is None
    assert chat_id == "oc_group"
    assert receive_id_type == "chat_id"

    # 非回复类消息仍按原有逻辑优先 open_id。
    userid, chat_id, receive_id_type = FeishuModule._resolve_message_target(Message(userid="ou_user"))
    assert userid == "ou_user"
    assert chat_id is None
    assert receive_id_type == "open_id"

    # 无用户ID时回退 targets 中的飞书字段。
    userid, chat_id, receive_id_type = FeishuModule._resolve_message_target(
        Message(targets={"feishu_chat_id": "oc_config"})
    )
    assert userid is None
    assert chat_id == "oc_config"


def test_module_private_delivery_ignores_original_group_chat():
    """私聊投递只保留用户身份，并让客户端按已记录 ID 类型发送。"""
    userid, chat_id, receive_id_type = FeishuModule._resolve_message_target(
        Message(
            userid="user_target",
            original_chat_id="oc_group",
            private_delivery=True,
        )
    )

    assert userid == "user_target"
    assert chat_id is None
    assert receive_id_type is None


def test_module_post_message_replies_to_original_chat_for_group_message():
    """携带原会话上下文的回复应定向到原会话（群聊）。"""
    module = FeishuModule()
    conf = SimpleNamespace(name="feishu-main")
    client = MagicMock()

    with (
        patch.object(module, "get_configs", return_value={"feishu-main": conf}),
        patch.object(module, "check_message", return_value=True),
        patch.object(module, "get_instance", return_value=client),
    ):
        module.post_message(
            Message(
                title="标题",
                text="正文",
                userid="ou_user",
                original_chat_id="oc_group",
            )
        )

    client.send_notification.assert_called_once_with(
        message=ANY,
        userid=None,
        chat_id="oc_group",
        receive_id_type="chat_id",
        original_message_id=None,
    )
