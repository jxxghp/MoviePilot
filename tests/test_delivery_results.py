"""
统一回执对局部失败、静默失败和未确认的发送给出不同结果。
"""

import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import unquote

import discord
import pytest
from slack_sdk.errors import SlackApiError

from app.adapters.network.webpush import WebPushDeliveryError
from app.application.messaging.delivery import (
    NotificationParts,
    claim_notification_part,
    collect_notification_preflight,
    delivery_status,
    freeze_notification_parts,
    merge_notification_progress,
    observe_notification_progress,
    uses_legacy_attachment_layout,
)
from app.application.webpush import WebPushRegistration
from app.chain.message import MessageChain
from app.modules.apppush import AppPushModule
from app.modules.dingtalk import DingTalkModule
from app.modules.discord import DiscordModule
from app.modules.discord.discord import Discord
from app.modules.feishu.feishu import Feishu
from app.modules.feishu.module import FeishuModule
from app.modules.feishu.openapi import FeishuResponse
from app.modules.qqbot.module import QQBotModule
from app.modules.qqbot.qqbot import QQBot
from app.modules.slack import SlackModule
from app.modules.slack.slack import Slack
from app.modules.synologychat import SynologyChatModule
from app.modules.synologychat.synologychat import SynologyChat
from app.modules.telegram.botapi import TelegramApiError, TelegramNetworkError
from app.modules.telegram.module import TelegramModule
from app.modules.telegram.telegram import Telegram
from app.modules.vocechat import VoceChatModule
from app.modules.vocechat.vocechat import VoceChat
from app.modules.webpush import WebPushModule
from app.modules.wechat import WechatModule
from app.modules.wechat.wechat import WeChat
from app.modules.wechat.wechatbot import WeChatBot
from app.modules.wechatclawbot import WechatClawBotModule
from app.modules.wechatclawbot.wechatclawbot import ILinkClient, WechatClawBot
from app.runtime.correlation import correlation_scope
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import MessageType, NotificationChannel


@pytest.mark.parametrize("module_type,channel,provider,provider_config", [
    (DingTalkModule, NotificationChannel.DingTalk, "dingtalk", {}),
    (DiscordModule, NotificationChannel.Discord, "discord", {}),
    (FeishuModule, NotificationChannel.Feishu, "feishu", {}),
    (QQBotModule, NotificationChannel.QQ, "qqbot", {}),
    (SlackModule, NotificationChannel.Slack, "slack", {}),
    (SynologyChatModule, NotificationChannel.SynologyChat, "synologychat", {}),
    (TelegramModule, NotificationChannel.Telegram, "telegram", {}),
    (VoceChatModule, NotificationChannel.VoceChat, "vocechat", {}),
    (WechatModule, NotificationChannel.Wechat, "wechat", {"WECHAT_MODE": "app"}),
    (WechatModule, NotificationChannel.Wechat, "wechat", {"WECHAT_MODE": "bot"}),
    (WechatClawBotModule, NotificationChannel.WechatClawBot, "wechatclawbot", {}),
])
def test_provider_reports_client_unavailable(monkeypatch, module_type, channel, provider, provider_config):
    """
    活跃配置指向尚未就绪的客户端时，派发回执保留连接和可重试状态。
    """
    module = module_type()
    module._channel = channel
    conf = NotificationConf(id="original", name="fixture", type=provider, enabled=True, config=provider_config,
                            identity_namespace="binding")
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    monkeypatch.setattr(module, "get_instance", lambda _: None)
    message = Message(channel=channel, source=conf.name, userid="user:42", text="正文")
    receipts = message.collect_delivery_results()

    if channel == NotificationChannel.Telegram:
        with pytest.raises(RuntimeError, match="通知发送未确认"):
            module.post_message(message)
    else:
        module.post_message(message)

    result, = receipts
    assert (result.connection_id, result.identity_namespace, result.status, result.code) == (
        "original", "binding", "retryable_failure", "client_unavailable",
    )


@pytest.mark.parametrize("channel,owner,service_available,expected", [
    (NotificationChannel.WebPush, None, False, ("skipped", "no_target")),
    (NotificationChannel.WebPush, "owner", False, ("retryable_failure", "client_unavailable")),
    (NotificationChannel.WebPush, "owner", True, ("skipped", "no_device")),
    (NotificationChannel.AppPush, None, False, ("skipped", "no_target")),
])
def test_push_receipt_distinguishes_no_target_no_device_and_unavailable_service(
    monkeypatch, channel, owner, service_available, expected,
):
    """
    账号和设备为空时不投递，明确目标的推送服务未就绪时报告可重试。
    """
    module = WebPushModule() if channel == NotificationChannel.WebPush else AppPushModule()
    module._channel = channel
    conf = NotificationConf(id="push", name="fixture", type=channel.value.lower(), enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    lookup = Mock(return_value=SimpleNamespace(for_owners=Mock(return_value=())))
    if not service_available:
        lookup.side_effect = RuntimeError("服务未就绪")
    monkeypatch.setattr("app.modules.webpush.get_webpush_service", lookup)
    send = Mock()
    monkeypatch.setattr("app.modules.webpush.send_webpush", send)
    monkeypatch.setattr(AppPushModule, "_request", send)
    message = Message(channel=channel, source=conf.name, title="通知",
                      targets={conf.type: owner} if owner else None)
    message.set_notification_route_scope("all")
    receipts = message.collect_delivery_results()

    module.post_message(message)

    result, = receipts
    assert (result.status, result.code) == expected
    assert lookup.call_count == int(channel == NotificationChannel.WebPush and owner is not None)
    send.assert_not_called()


@pytest.mark.parametrize("provider,channel,client_type,module_type", [
    ("discord", NotificationChannel.Discord, Discord, DiscordModule),
    ("feishu", NotificationChannel.Feishu, Feishu, FeishuModule),
    ("slack", NotificationChannel.Slack, Slack, SlackModule),
])
def test_ordinary_adapter_dispatch_does_not_reuse_an_unrelated_notification_key(
    monkeypatch, provider, channel, client_type, module_type,
):
    """
    两次共用调用关联 ID 的正常发送各自提交，并保留实际分段的平台消息标识。
    """
    client = object.__new__(client_type)
    if provider == "discord":
        client._loop = Mock()
        client.get_state = Mock(return_value=True)
        destination = Mock(spec=discord.abc.Messageable, id=12)
        client._resolve_channel = AsyncMock(return_value=destination)
        resolved = Mock(**{"result.return_value": destination})

        def resolve(coroutine, _loop):
            """
            隔离 Discord 线程并返回指定频道。
            """
            coroutine.close()
            return resolved

        monkeypatch.setattr("app.modules.discord.discord.asyncio.run_coroutine_threadsafe", resolve)
        submit = Mock(return_value=("confirmed", None, "fixture-message"))
        client._submit_tracked_part = submit
    elif provider == "feishu":
        submit = Mock(return_value=("confirmed", None, "fixture-message"))
        client._submit_tracked_message = submit
        client._resolve_target = Mock(return_value=("C1", "chat_id"))
    else:
        submit = Mock(return_value={"ok": True, "ts": "fixture-message"})
        client._notification_client = SimpleNamespace(chat_postMessage=submit)

    conf = NotificationConf(id=provider, name=provider, type=provider, enabled=True)
    module = module_type()
    module._channel = channel
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    with correlation_scope("fixture-shared-correlation"):
        for body in ("第一条正文", "第二条正文"):
            message = Message(channel=channel, source=conf.name, userid="C1", text=body)
            receipts = message.collect_delivery_results()
            module.post_message(message)
            receipt, = receipts
            assert (receipt.status, receipt.message_id) == ("confirmed", "fixture-message")

    first, second = submit.call_args_list
    if provider in {"discord", "feishu"}:
        assert first.args[3] != second.args[3]
    else:
        assert [first.kwargs["text"], second.kwargs["text"]] == ["第一条正文", "第二条正文"]


def _observe_recovery(state, *, initial):
    """
    让真实分段算法将内存字典作为独立通知意图的进度存储。
    """
    def record(results):
        """
        合并新回执，并保留已确认的记录。
        """
        state.update(merge_notification_progress(state, results))

    def reserve(part, should_submit):
        """
        按本次派发所处阶段保护网络提交。
        """
        recorded, updated = claim_notification_part(
            state, part, initial_attempt=initial, should_submit=should_submit,
        )
        state.update(updated)
        return recorded

    def plan(parts):
        """
        仅首次派发记录目标。
        """
        if initial:
            state.update(freeze_notification_parts(state, parts))

    return observe_notification_progress(record, reserve=reserve, plan=plan)


def test_webpush_resumes_only_original_subscription_version(monkeypatch):
    """
    恢复时不重发到已确认设备，不发送到新增、重新登记或临近发送时被撤销的设备。
    """
    def registration(name, revision="v1"):
        """
        生成具有确定账号和登记版本的设备。
        """
        return WebPushRegistration(f"https://push.example/{name}", "key", "secret", 1, "alice", revision)

    devices = [registration(name) for name in ("sent", "limited", "changed", "removed")]
    service = SimpleNamespace(for_owners=lambda _: tuple(devices), is_current=Mock(return_value=True))
    monkeypatch.setattr("app.modules.webpush.get_webpush_service", lambda: service)
    status = SimpleNamespace(status_code=429, headers={"Retry-After": "17"})
    send = Mock(side_effect=[None, WebPushDeliveryError("limit", status),
                            WebPushDeliveryError("limit", status), WebPushDeliveryError("limit", status), None])
    monkeypatch.setattr("app.modules.webpush.send_webpush", send)
    conf = NotificationConf(id="web", name="web", type="webpush", enabled=True, identity_namespace="binding")
    module = WebPushModule()
    module._channel = NotificationChannel.WebPush
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    message = Message(channel=NotificationChannel.WebPush, title="完成", targets={"webpush": "alice"})
    message.set_notification_route_scope("user")
    results = message.collect_delivery_results()
    progress = {}
    with _observe_recovery(progress, initial=True):
        module.post_message(message)
    assert [(item.status, item.retry_after) for item in results] == [
        ("confirmed", None), ("retryable_failure", 17), ("retryable_failure", 17), ("retryable_failure", 17),
    ]
    devices[2] = registration("changed", "v2")
    devices.append(registration("new"))
    service.is_current.side_effect = lambda item: item.endpoint.rsplit("/", 1)[-1] != "removed"
    results.clear()
    with _observe_recovery(progress, initial=False):
        module.post_message(message)
    assert [(item.status, item.code) for item in results] == [
        ("confirmed", None), ("confirmed", None), ("permanent_failure", "unplanned_target"),
        ("skipped", "subscription_changed"), ("permanent_failure", "unplanned_target"),
    ]
    assert [call.kwargs["subscription"]["endpoint"].rsplit("/", 1)[-1] for call in send.call_args_list] == [
        "sent", "limited", "changed", "removed", "limited",
    ]


def test_apppush_partial_relay_report_is_not_replayed(monkeypatch):
    """
    中转仅确认部分设备时保留未知，恢复不再次提交该账号或新增账号。
    """
    module = AppPushModule()
    module._channel = NotificationChannel.AppPush
    conf = NotificationConf(id="app", name="app", type="apppush", enabled=True, identity_namespace="binding")
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    partial_delivery = {"total": 2, "accepted": 1, "failed": ["device"]}
    complete_delivery = {"total": 1, "accepted": 1}
    request = Mock(side_effect=[partial_delivery, complete_delivery, partial_delivery, complete_delivery])
    monkeypatch.setattr(module, "_request", request)
    progress = {}

    def dispatch(owners, *, initial=None):
        """
        从通知提供方入口发送显式选中的账号。
        """
        msg = Message(channel=NotificationChannel.AppPush, title="完成", targets={"apppush": owners})
        msg.set_notification_route_scope("all")
        receipts = msg.collect_delivery_results()
        with nullcontext() if initial is None else _observe_recovery(progress, initial=initial):
            module.post_message(msg)
        return [(receipt.target_id, receipt.status, receipt.code) for receipt in receipts]

    planned = [("alice", "unknown", "relay_devices_unconfirmed"), ("bob", "confirmed", None)]
    assert dispatch(("alice", "bob")) == planned
    assert dispatch(("alice", "bob"), initial=True) == planned
    assert dispatch(("alice", "bob", "carol"), initial=False) == [
        *planned, ("carol", "permanent_failure", "unplanned_target"),
    ]
    assert [call.args[3]["owner"] for call in request.call_args_list] == ["alice", "bob", "alice", "bob"]


@pytest.mark.parametrize("http_status,expected", [
    (200, ("confirmed", None, None)),
    (401, ("permanent_failure", "relay_rejected", None)),
    (429, ("retryable_failure", "rate_limited", 17)),
    (503, ("unknown", "relay_unconfirmed", None)),
    (None, ("unknown", "relay_unconfirmed", None)),
])
def test_apppush_normal_delivery_preserves_relay_http_result(monkeypatch, http_status, expected):
    """
    实际中转适配器保留 HTTP 拒绝和限流状态，不把超时视为已投递或可盲重试。
    """
    conf = NotificationConf(id="app", name="app", type="apppush", enabled=True, config={
        "PUSH_SERVER_URL": "https://fixture.example/", "PUSH_RELAY_KEY": "synthetic" * 4,
    })
    response = Mock(status_code=http_status, headers={"Retry-After": "17"})
    response.json.return_value = {"total": 1, "accepted": 1, "failed": []}
    request = Mock(return_value=response if http_status is not None else None)
    monkeypatch.setattr("app.adapters.external.server.RequestUtils", Mock(**{"return_value.request": request}))
    module = AppPushModule()
    module._channel = NotificationChannel.AppPush
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    message = Message(channel=NotificationChannel.AppPush, source=conf.name, text="仅正文", targets={"apppush": "alice"})
    message.set_notification_route_scope("user")
    receipts = message.collect_delivery_results()
    module.post_message(message)

    receipt, = receipts
    assert (receipt.status, receipt.code, receipt.retry_after) == expected
    assert request.call_args.kwargs["json"]["body"] == "仅正文"
    assert request.call_args.kwargs["json"]["owner"] == receipt.target_id == "alice"


@pytest.mark.parametrize("provider,channel,client_type,module_type", [
    ("vocechat", NotificationChannel.VoceChat, VoceChat, VoceChatModule),
    ("synologychat", NotificationChannel.SynologyChat, SynologyChat, SynologyChatModule),
])
@pytest.mark.parametrize("status,expected", [
    (None, "unknown"), (200, "confirmed"), (403, "permanent_failure"), (429, "retryable_failure"),
])
def test_chat_adapter_reports_the_same_receipt_for_normal_and_managed_delivery(
    monkeypatch, provider, channel, client_type, module_type, status, expected,
):
    """
    实际客户端在普通派发与受管派发中保留相同目标、正文和确认结果。
    """
    client = object.__new__(client_type)
    response = Mock(status_code=status)
    response.json.return_value = {"success": True}
    request = Mock()
    request.post_res.return_value = response if status is not None else None
    if provider == "vocechat":
        client._client = request
        client._host = "https://fixture.example/"
        client._channel_id = "77"
    else:
        client._req = request
        client._token = "synthetic"
        client._webhook_url = "https://fixture.example/chat"

    module = module_type()
    module._channel = channel
    config = NotificationConf(id=provider, name=provider, type=provider, enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {provider: config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    message = Message(channel=channel, source=provider, userid="42", text="仅正文")
    scoped = message.model_copy(deep=True)
    receipts = message.collect_delivery_results()
    module.post_message(message)
    assert len(receipts) == 1
    assert receipts[0].status == expected and receipts[0].target_id.endswith(":42")
    first_request = request.post_res.call_args
    if provider == "vocechat":
        assert first_request.args[0].endswith("/api/bot/send_to_user/42")
        assert first_request.kwargs["data"].decode() == "仅正文"
    else:
        payload = json.loads(first_request.kwargs["data"].removeprefix("payload="))
        assert payload["user_ids"] == [42] and unquote(payload["text"]) == "仅正文"

    managed_receipts = scoped.collect_delivery_results()
    with observe_notification_progress(lambda _results: None, reserve=lambda _part, _submit: None):
        module.post_message(scoped)
    assert managed_receipts == receipts and request.post_res.call_args == first_request
    assert request.post_res.call_count == 2


def test_synologychat_reports_individual_failures_for_explicit_broadcast(monkeypatch):
    """
    群晖广播逐人投递，业务拒绝独立于同批其他用户的成功回执。
    """
    client = SynologyChat(SYNOLOGYCHAT_WEBHOOK="https://fixture.example/chat", SYNOLOGYCHAT_TOKEN="synthetic")
    users = Mock(status_code=200)
    users.json.return_value = {"data": {"users": [
        {"user_id": 42, "deleted": False}, {"user_id": 43, "deleted": False}, {"user_id": 44, "deleted": True},
    ]}}
    accepted = Mock(status_code=200, **{"json.return_value": {"success": True}})
    rejected = Mock(status_code=200, **{"json.return_value": {"error": {"code": 119}}})
    transport = Mock(**{"get_res.return_value": users, "post_res.side_effect": [accepted, rejected]})
    monkeypatch.setattr(client, "_req", transport)
    module = SynologyChatModule()
    module._channel = NotificationChannel.SynologyChat
    conf = NotificationConf(id="chat", name="chat", type="synologychat", enabled=True)
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    message = Message(channel=NotificationChannel.SynologyChat, source=conf.name, text="仅正文")
    message.set_notification_route_scope("all")
    receipts = message.collect_delivery_results()
    module.post_message(message)

    assert [(result.target_id, result.status) for result in receipts] == [
        ("user:42", "confirmed"), ("user:43", "permanent_failure"),
    ]
    assert [json.loads(call.kwargs["data"].removeprefix("payload="))["user_ids"]
            for call in transport.post_res.call_args_list] == [[42], [43]]


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("transient", [True, False])
def test_clawbot_durable_text_recovery_uses_one_protocol_address(monkeypatch, transient, managed):
    """
    iLink 限流仅补发未发分段，响应丢失不会触发兼容报文或地址回退。
    """
    sender = ILinkClient("https://ilink.example", "fixture-token", "fixture-bot")
    bot = WechatClawBot.__new__(WechatClawBot)
    bot._default_target = None
    bot._state = {"known_targets": {"unconfigured": {}}}
    monkeypatch.setattr(bot, "_build_client", lambda: sender)
    acknowledged = Mock(status_code=200, content=b'{"ret":0}', **{"json.return_value": {"ret": 0}})
    post = Mock(side_effect=[acknowledged, Mock(status_code=429) if transient else None, acknowledged])
    monkeypatch.setattr("app.modules.wechatclawbot.wechatclawbot.RequestUtils", Mock(**{"return_value.post_res": post}))
    module = WechatClawBotModule()
    module._channel = NotificationChannel.WechatClawBot
    conf = NotificationConf(id="claw", name="claw", type="wechatclawbot", enabled=True, identity_namespace="binding")
    monkeypatch.setattr(module, "get_configs", lambda: {conf.name: conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    monkeypatch.setattr(module, "get_instance", lambda _: bot)
    progress = {}

    def deliver(*, initial, target="alice"):
        """
        以发送模块的真实入口投递长文本。
        """
        message = Message(channel=NotificationChannel.WechatClawBot, userid=target, text="甲" * 1200)
        if not target:
            message.set_notification_route_scope("all")
        results = message.collect_delivery_results()
        with _observe_recovery(progress, initial=initial) if managed else nullcontext():
            module.post_message(message)
        return [result.status for result in results]

    assert deliver(initial=True) == ["confirmed", "retryable_failure" if transient else "unknown"]
    if managed:
        assert deliver(initial=False) == ["confirmed", "confirmed" if transient else "unknown"]
    assert deliver(initial=False, target=None) == ["skipped"]
    assert post.call_count == (3 if transient and managed else 2)
    assert all(call.kwargs["json"]["msg"]["to_user_id"] == "alice" for call in post.call_args_list)
    assert all(call.args[0] == "https://ilink.example/ilink/bot/sendmessage" for call in post.call_args_list)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, "confirmed"),
        (False, "retryable_failure"),
        (None, "unknown"),
        ({"success": False, "message_id": "old"}, "retryable_failure"),
        ({"message_id": "new"}, "confirmed"),
        ((False, {"ok": True}), "retryable_failure"),
        ({"detail": "accepted?"}, "unknown"),
    ],
)
def test_provider_result_requires_confirmation(raw, expected):
    """
    未知结构保持未确认，显式失败优先于其它返回字段。
    """
    assert delivery_status(raw) == expected


def test_wechat_partial_delivery_is_not_a_successful_strict_dispatch(monkeypatch):
    """
    同一轮内两个企业微信实例各发送一次，失败实例的结果不会掩盖成功实例。
    """
    wechat = WechatModule()
    wechat._channel = NotificationChannel.Wechat
    configs = {
        "office": NotificationConf(id="corp-app", name="office", type="wechat", enabled=True),
        "bot": NotificationConf(id="corp-bot", name="bot", type="wechat", enabled=True, config={"WECHAT_MODE": "bot"}),
    }
    clients = {"office": Mock(), "bot": Mock()}
    clients["office"].send_msg.return_value = True
    clients["bot"].send_msg.return_value = False
    monkeypatch.setattr(wechat, "get_configs", lambda: configs)
    monkeypatch.setattr(wechat, "get_config", configs.get)
    monkeypatch.setattr(wechat, "get_instance", clients.get)
    message = Message(channel=NotificationChannel.Wechat, userid="member", text="测试投递", save_history=False)
    chain = MessageChain()
    strict = Mock(side_effect=lambda _method, message: wechat.post_message(message))
    monkeypatch.setattr(chain, "run_module_strict", strict)

    with pytest.raises(RuntimeError, match="部分通知投递尚未确认"):
        chain._deliver_notification(message, strict_delivery=True, immediately=True, kwargs={})

    strict.assert_called_once()
    for client in clients.values():
        client.send_msg.assert_called_once()
    results = message.collect_delivery_results()
    assert [(item.connection_id, item.status) for item in results] == [
        ("corp-app", "confirmed"), ("corp-bot", "retryable_failure"),
    ]
    assert "delivery_results" not in message.model_dump()


def test_wechat_bot_reports_each_target_and_part(monkeypatch):
    """
    已确认的分段、结果未知和没有提交的后续分段均保留，其他目标继续发送。
    """
    monkeypatch.setattr(WeChatBot, "_start_gateway", lambda self: None)
    monkeypatch.setattr(WeChatBot, "_load_known_targets", lambda self: None)
    clients = {
        target: WeChatBot(WECHAT_BOT_ID=target, WECHAT_BOT_SECRET="fixture", WECHAT_BOT_CHAT_ID=target)
        for target in ("alice", "bob")
    }
    ack = Mock(side_effect=[
        ("confirmed", None), ("unknown", "ack_timeout"),
        ("confirmed", None), ("confirmed", None), ("confirmed", None),
    ])
    for client in clients.values():
        monkeypatch.setattr(client, "_split_content", lambda _: ["开始", "中间", "结束"])
        monkeypatch.setattr(client, "_send_with_ack", ack)
    module = WechatModule()
    module._channel = NotificationChannel.Wechat
    configs = {
        target: NotificationConf(id=target, name=target, type="wechat", enabled=True,
                                 config={"WECHAT_MODE": "bot"}, switchs=[MessageType.Subscribe.value])
        for target in clients
    }
    monkeypatch.setattr(module, "get_configs", lambda: configs)
    monkeypatch.setattr(module, "get_config", configs.get)
    monkeypatch.setattr(module, "get_instance", clients.get)
    message = Message(channel=NotificationChannel.Wechat, text="测试", mtype=MessageType.Subscribe,
                      delivery_kind="event_notification")
    results = message.collect_delivery_results()

    module.post_message(message)

    assert [(item.target_id, item.part, item.status, item.code) for item in results] == [
        ("user:alice", 0, "confirmed", None),
        ("user:alice", 1, "unknown", "ack_timeout"),
        ("user:alice", 2, "retryable_failure", "previous_part_unconfirmed"),
        ("user:bob", 0, "confirmed", None),
        ("user:bob", 1, "confirmed", None),
        ("user:bob", 2, "confirmed", None),
    ]
    assert ack.call_count == 5


def test_wechat_bot_host_notification_needs_explicit_or_configured_target(monkeypatch):
    """
    常规通知没有受众时，不向曾使用机器人的用户广播。
    """
    monkeypatch.setattr(WeChatBot, "_start_gateway", lambda self: None)
    monkeypatch.setattr(WeChatBot, "_load_known_targets", lambda self: None)
    client = WeChatBot(WECHAT_BOT_ID="bot", WECHAT_BOT_SECRET="fixture")
    client._known_targets.add("alice")
    send = Mock()
    receipt = Mock()
    monkeypatch.setattr(client, "_send_with_ack", send)

    assert client.send_msg(title="测试通知", receipt=receipt) is False
    send.assert_not_called()
    receipt.assert_called_once_with(None, 0, "skipped", "no_target")


@pytest.mark.parametrize(("module_class", "provider", "channel"), [
    (FeishuModule, "feishu", NotificationChannel.Feishu), (QQBotModule, "qqbot", NotificationChannel.QQ),
])
def test_configured_chat_platform_skips_host_notification_without_target(monkeypatch, module_class, provider, channel):
    """
    飞书和 QQ 宿主通知无受众时，不进入客户端的旧会话回退。
    """
    module = module_class()
    module._channel = channel
    conf = NotificationConf(id="fixture", name="fixture", type=provider, enabled=True, switchs=["订阅"])
    monkeypatch.setattr(module, "get_configs", lambda: {"fixture": conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    client = Mock()
    monkeypatch.setattr(module, "get_instance", Mock(return_value=client))
    message = Message(channel=channel, text="通知", mtype=MessageType.Subscribe)
    results = message.collect_delivery_results()

    module.post_message(message)

    assert [(item.connection_id, item.status, item.code) for item in results] == [
        ("fixture", "skipped", "no_target"),
    ]
    assert client.mock_calls == []


@pytest.mark.parametrize("interrupted", ["retryable_failure", "unknown"])
def test_wechat_bot_recovery_sends_only_known_incomplete_parts(monkeypatch, interrupted):
    """
    恢复只提交同一连接身份和内容下可重试的分段。
    """
    monkeypatch.setattr(WeChatBot, "_start_gateway", lambda self: None)
    monkeypatch.setattr(WeChatBot, "_load_known_targets", lambda self: None)
    client = WeChatBot(WECHAT_BOT_ID="bot", WECHAT_BOT_SECRET="fixture", WECHAT_BOT_CHAT_ID="group:alice")
    monkeypatch.setattr(client, "_split_content", lambda _: ["第一段", "第二段"])
    send = Mock(side_effect=[("confirmed", None), (interrupted, "interrupted"), ("confirmed", None)])
    monkeypatch.setattr(client, "_send_with_ack", send)
    config = NotificationConf(id="bot", name="bot", type="wechat", enabled=True,
                              identity_namespace="binding", config={"WECHAT_MODE": "bot"})
    module = WechatModule()
    module._channel = NotificationChannel.Wechat
    progress = {}
    initial = True

    def observe(results):
        """
        以当前测试记录模拟 Outbox 提交。
        """
        nonlocal progress
        progress = merge_notification_progress(progress, results)

    def reserve(part, should_submit):
        """
        由真实规则决定在本轮可以提交哪些分段。
        """
        nonlocal progress
        result, progress = claim_notification_part(
            progress, part, initial_attempt=initial, should_submit=should_submit,
        )
        return result

    def deliver(current_config, target):
        """
        调用模块正常发送路径并汇总本次回执。
        """
        message = Message(text="内容")
        results = message.collect_delivery_results()
        with observe_notification_progress(observe, reserve=reserve):
            module._post_tracked_notification(client, current_config, message, target)
        return [item.status for item in results]

    assert deliver(config, "group:alice") == ["confirmed", interrupted]
    initial = False
    assert deliver(config, "group:alice") == [
        "confirmed", "confirmed" if interrupted == "retryable_failure" else "unknown",
    ]
    count_before = send.call_count
    assert [call.args[0]["body"]["markdown"]["content"] for call in send.call_args_list] == (
        ["第一段", "第二段", "第二段"] if interrupted == "retryable_failure" else ["第一段", "第二段"]
    )
    assert deliver(config, "group:bob") == ["permanent_failure", "permanent_failure"]
    assert deliver(config.model_copy(update={"identity_namespace": "new"}), "group:alice") == [
        "permanent_failure", "permanent_failure",
    ]
    assert send.call_count == count_before


@pytest.mark.parametrize(("interrupted", "status"), [(None, "unknown"), (45009, "retryable_failure")])
def test_wechat_app_recovers_per_target_and_text_part(monkeypatch, interrupted, status):
    """
    自建应用仅重试平台明确未接受的分段，其他目标仍可投递且恢复时不重发。
    """
    client = WeChat.__new__(WeChat)
    client._appid = "fixture-app"
    monkeypatch.setattr(client, "_WeChat__get_access_token", lambda: "fixture-token")
    responses = [0, interrupted, 0, 0, 0, 0]
    post = Mock(side_effect=[
        None if reply is None else Mock(status_code=200, **{"json.return_value": {"errcode": reply}})
        for reply in responses
    ])
    monkeypatch.setattr("app.modules.wechat.wechat.RequestUtils", Mock(**{"return_value.post": post}))
    config = NotificationConf(id="app", name="app", type="wechat", enabled=True, identity_namespace="binding")
    module = WechatModule()
    module._channel = NotificationChannel.Wechat
    monkeypatch.setattr(module, "get_configs", lambda: {"app": config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    progress = {}
    initial = True

    def observe(results):
        """
        模拟由当前 Outbox 认领持久化分段结果。
        """
        nonlocal progress
        progress = merge_notification_progress(progress, results)

    def reserve(part, should_submit):
        """
        根据真实分段规则决定本次网络调用是否获准。
        """
        nonlocal progress
        result, progress = claim_notification_part(
            progress, part, initial_attempt=initial, should_submit=should_submit,
        )
        return result

    def deliver(recipient):
        """
        经通知模块实际入口发送超过单段字节限制的中文通知。
        """
        message = Message(channel=NotificationChannel.Wechat, userid=recipient, text="甲" * 800)
        results = message.collect_delivery_results()
        with observe_notification_progress(observe, reserve=reserve):
            module.post_message(message)
        return [(item.target_id, item.part, item.status) for item in results]

    assert deliver("alice|bob") == [
        ("alice", 0, "confirmed"), ("alice", 1, status),
        ("bob", 0, "confirmed"), ("bob", 1, "confirmed"),
    ]
    initial = False
    assert deliver("alice|bob") == [
        ("alice", 0, "confirmed"), ("alice", 1, "unknown" if interrupted is None else "confirmed"),
        ("bob", 0, "confirmed"), ("bob", 1, "confirmed"),
    ]
    assert deliver("carol") == [("carol", 0, "permanent_failure"), ("carol", 1, "permanent_failure")]
    posted = [json.loads(call.kwargs["data"]) for call in post.call_args_list]
    assert [payload["touser"] for payload in posted] == (
        ["alice", "alice", "bob", "bob"] if interrupted is None else ["alice", "alice", "bob", "bob", "alice"]
    )
    assert all(len(payload["text"]["content"].encode()) <= 2048 for payload in posted)
    if interrupted is not None:
        assert posted[-1] == posted[1]


@pytest.mark.parametrize("content_kind", ["file", "image"])
def test_wechat_app_retries_attachment_without_repeating_caption(monkeypatch, tmp_path, content_kind):
    """
    冻结附件及说明的分段后，限流只重试附件，保留已确认说明及原受众。
    """
    client = WeChat.__new__(WeChat)
    client._appid = "app"
    monkeypatch.setattr(client, "_WeChat__get_access_token", lambda: "fixture-token")
    upload = Mock(return_value="media")
    monkeypatch.setattr(client, "_upload_temp_media", upload)
    post = Mock(side_effect=[
        Mock(status_code=200, **{"json.return_value": {"errcode": code}}) for code in (0, 45009, 0)
    ])
    monkeypatch.setattr("app.modules.wechat.wechat.RequestUtils", Mock(**{"return_value.post": post}))
    module = WechatModule()
    module._channel = NotificationChannel.Wechat
    config = NotificationConf(id="app", name="app", type="wechat", enabled=True, identity_namespace="binding")
    monkeypatch.setattr(module, "get_configs", lambda: {"app": config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    path = tmp_path / "movie.srt"
    path.write_bytes(b"subtitle bytes")
    attachment = (
        {"file_path": str(path)} if content_kind == "file"
        else {"image": "data:image/png;base64,iVBORw0KGgoAAQ==", "native_image": True}
    )
    progress = {}
    initial = True

    def freeze(parts):
        """
        保存初次尝试生成的分段清单。
        """
        nonlocal progress
        assert [(part.target_id, part.part) for part in parts] == [("alice", 0), ("alice", 1)]
        progress = freeze_notification_parts(progress, parts)

    def observe(results):
        """
        保存已提交的回执，供后续认领查询。
        """
        nonlocal progress
        progress = merge_notification_progress(progress, results)

    def reserve(part, should_submit):
        """
        通过真实恢复规则复用说明回执，允许附件失败后重试。
        """
        nonlocal progress
        result, progress = claim_notification_part(
            progress, part, initial_attempt=initial, should_submit=should_submit,
        )
        return result

    def deliver():
        """
        从通知模块的公开发送入口获取逐段回执。
        """
        message = Message(channel=NotificationChannel.Wechat, userid="alice", title="说明", **attachment)
        results = message.collect_delivery_results()
        module.post_message(message)
        return [(item.target_id, item.part, item.status) for item in results]

    with observe_notification_progress(observe, reserve=reserve, freeze=freeze):
        with collect_notification_preflight():
            deliver()
        upload.assert_not_called()
        post.assert_not_called()
        assert deliver() == [("alice", 0, "confirmed"), ("alice", 1, "retryable_failure")]
        initial = False
        assert deliver() == [("alice", 0, "confirmed"), ("alice", 1, "confirmed")]
    posted = [json.loads(call.kwargs["data"]) for call in post.call_args_list]
    assert [(payload["touser"], payload["msgtype"]) for payload in posted] == [
        ("alice", "text"), ("alice", content_kind), ("alice", content_kind),
    ]
    assert upload.call_count == 2


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("scenario", ["rate_limited", "timeout", "first_timeout", "markdown_denied"])
def test_qq_recovers_known_parts_and_only_falls_back_on_explicit_rejection(monkeypatch, scenario, managed):
    """
    QQ 恢复保留群地址和已确认分段，遇到超时不重复发送。
    """
    client = QQBot.__new__(QQBot)
    client._ready = True
    client._app_id = "fixture-app"
    client._app_secret = "fixture-secret"
    acknowledged = Mock(status_code=200, **{"json.return_value": {"id": "fixture-message"}})
    limited = Mock(status_code=429, **{"json.return_value": {"code": 1}})
    markdown_denied = Mock(status_code=403, **{"json.return_value": {"code": 11244}})
    replies = {
        "rate_limited": [acknowledged, limited, acknowledged],
        "timeout": [acknowledged, None],
        "first_timeout": [None],
        "markdown_denied": [markdown_denied, acknowledged],
    }
    post = Mock(side_effect=replies[scenario])
    monkeypatch.setattr("app.modules.qqbot.api.RequestUtils", Mock(**{"return_value.post_res": post}))
    monkeypatch.setattr("app.modules.qqbot.qqbot.get_access_token", lambda *_: "fixture-token")
    config = NotificationConf(id="qq", name="qq", type="qqbot", enabled=True, identity_namespace="binding")
    module = QQBotModule()
    module._channel = NotificationChannel.QQ
    monkeypatch.setattr(module, "get_configs", lambda: {"qq": config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    monkeypatch.setattr(Message, "channel_text_chunks", lambda *_: ["第一段", "第二段"])
    progress = {}
    initial = True

    def observe(results):
        """
        保存当前认领取得的各分段结果。
        """
        nonlocal progress
        progress = merge_notification_progress(progress, results)

    def reserve(part, should_submit):
        """
        使用真实认领规则限制重复提交。
        """
        nonlocal progress
        result, progress = claim_notification_part(
            progress, part, initial_attempt=initial, should_submit=should_submit,
        )
        return result

    def plan(parts):
        """
        冻结首次解析出的群地址和分段。
        """
        nonlocal progress
        if initial:
            progress = freeze_notification_parts(progress, parts)

    def deliver(recipient):
        """
        从通知模块入口运行一次派发。
        """
        message = Message(channel=NotificationChannel.QQ, userid=recipient, text="完整正文")
        results = message.collect_delivery_results()
        with observe_notification_progress(observe, reserve=reserve, plan=plan) if managed else nullcontext():
            module.post_message(message)
        assert all(item.content_digest for item in results)
        if results[0].status == "confirmed":
            assert results[0].message_id == "fixture-message"
        return [item.status for item in results]

    initial_status = {
        "rate_limited": ["confirmed", "retryable_failure"],
        "timeout": ["confirmed", "unknown"],
        "first_timeout": ["unknown", "retryable_failure"],
        "markdown_denied": ["confirmed", "confirmed"],
    }[scenario]
    assert deliver("group:room") == initial_status
    if managed:
        initial = False
        assert deliver("group:room") == (["confirmed", "confirmed"] if scenario == "rate_limited" else initial_status)
        assert deliver("room") == ["permanent_failure", "permanent_failure"]
    assert all(call.args[0].endswith("/groups/room/messages") for call in post.call_args_list)
    payloads = [call.kwargs["json"] for call in post.call_args_list]
    if scenario == "markdown_denied":
        assert payloads == [{"markdown": {"content": "第一段"}, "msg_type": 2},
                            {"content": "完整正文", "msg_type": 0}]
    else:
        expected = {
            "rate_limited": ["第一段", "第二段", "第二段"] if managed else ["第一段", "第二段"],
            "timeout": ["第一段", "第二段"],
            "first_timeout": ["第一段"],
        }
        assert [item["markdown"]["content"] for item in payloads] == expected[scenario]


@pytest.mark.parametrize(
    ("recipient", "expected"), [
        (None, (None, 0, "skipped", "no_target")),
        ("member|@all", (None, 0, "permanent_failure", "invalid_target")),
    ],
)
def test_wechat_app_tracked_notification_requires_explicit_audience(recipient, expected):
    """
    缺少目标或混合全员地址时，在触达发送边界前结束通知。
    """
    client = WeChat.__new__(WeChat)
    part_sender = Mock()
    receipt = Mock()
    assert client.send_tracked_message(
        "测试", None, None, recipient, None, receipt=receipt, part_sender=part_sender,
    ) is False
    receipt.assert_called_once_with(*expected)
    part_sender.assert_not_called()


def test_wechat_app_voice_timeout_preserves_attachment_and_publishes_unknown(monkeypatch, tmp_path):
    """
    语音提交超时后不发送同一消息的文本，也不删除调用方持有的源附件。
    """
    source = tmp_path / "speech.mp3"
    source.write_bytes(b"audio")
    client = WeChat.__new__(WeChat)
    client._appid = "fixture-app"
    def convert(_source, destination):
        """
        在本次派发独占的输出路径写入转换结果。
        """
        destination.write_bytes(b"converted")
        return destination

    monkeypatch.setattr(client, "_convert_voice_to_amr", convert)
    upload = Mock(return_value="temporary-media")
    monkeypatch.setattr(client, "_upload_temp_media", upload)
    send = Mock(return_value=("unknown", "response_unconfirmed"))
    monkeypatch.setattr(client, "_send_tracked_part", send)
    config = NotificationConf(id="app", name="app", type="wechat", enabled=True, identity_namespace="binding")
    module = WechatModule()
    message = Message(channel=NotificationChannel.Wechat, userid="member", text="正文", voice_path=str(source))
    results = message.collect_delivery_results()

    module._post_tracked_notification(client, config, message, "member")
    assert [(item.status, item.code) for item in results] == [("unknown", "response_unconfirmed")]
    assert [call.args[0]["msgtype"] for call in send.call_args_list] == ["voice"]
    assert source.is_file() and not upload.call_args.args[0].exists()

    source.unlink()
    missing = Message(channel=NotificationChannel.Wechat, userid="member", text="正文", voice_path=str(source))
    results = missing.collect_delivery_results()
    module._post_tracked_notification(client, config, missing, "member")
    assert [(item.status, item.code) for item in results] == [("permanent_failure", "attachment_unavailable")]
    assert send.call_count == 1


def test_wechat_voice_conversion_failure_uses_original_target_and_tracked_text(monkeypatch, tmp_path):
    """
    本地转换失败时，正文走原地址和同一份持久清单，恢复不重复发送。
    """
    source = tmp_path / "speech.mp3"
    source.write_bytes(b"audio")
    client = WeChat.__new__(WeChat)
    client._appid = "fixture-app"
    monkeypatch.setattr(client, "_convert_voice_to_amr", Mock(return_value=None))
    upload = Mock()
    monkeypatch.setattr(client, "_upload_temp_media", upload)
    send = Mock(return_value=("confirmed", None))
    monkeypatch.setattr(client, "_send_tracked_part", send)
    config = NotificationConf(id="app", name="app", type="wechat", enabled=True, identity_namespace="binding")
    module = WechatModule()
    progress = {}
    for initial in (True, False):
        message = Message(channel=NotificationChannel.Wechat, userid="member", text="原正文",
                          voice_path=str(source), private_delivery=True)
        results = message.collect_delivery_results()
        with _observe_recovery(progress, initial=initial):
            module._post_tracked_notification(client, config, message, "member")
        result, = results
        assert (result.target_id, result.status) == ("member", "confirmed")
    assert send.call_count == 1
    payload = send.call_args.args[0]
    assert (payload["touser"], payload["msgtype"], payload["text"]["content"]) == ("member", "text", "原正文")
    upload.assert_not_called()
    assert source.is_file()


@pytest.mark.parametrize("long_caption", [False, True])
def test_telegram_combined_image_and_file_retry_preserves_original_topic(monkeypatch, tmp_path, long_caption):
    """
    同一业务通知的原图和文件拥有不同分段，限流恢复只再次上传文件。
    """
    api = Mock()
    api.send_photo.return_value = {"message_id": 100}
    api.send_message.return_value = {"message_id": 103}
    detail = "文" * 1300 if long_caption else None
    api.send_document.side_effect = [TelegramApiError("sendDocument", "limited", error_code=429), {"message_id": 101}]
    monkeypatch.setattr("app.modules.telegram.telegram.TelegramBotApi", Mock(return_value=api))
    client = Telegram(TELEGRAM_TOKEN="fixture", TELEGRAM_CHAT_ID="-100", auto_start_polling=False)
    monkeypatch.setattr(client, "_is_chat_rate_limited", lambda *_a, **_kw: False)
    config = NotificationConf(id="telegram", name="telegram", type="telegram", enabled=True, identity_namespace="binding")
    module = TelegramModule()
    module._channel = NotificationChannel.Telegram
    monkeypatch.setattr(module, "get_configs", lambda: {"telegram": config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    path = tmp_path / "report.txt"
    path.write_text("file content")
    image = tmp_path / "original.png"
    content = b"\x89PNG\r\n\x1a\noriginal"
    image.write_bytes(content)
    snapshot = {}
    first = True

    def plan(parts):
        """
        首次保存两组不同分段。
        """
        nonlocal snapshot
        if first:
            snapshot = freeze_notification_parts(snapshot, parts)

    def record(results):
        """
        记录已经获得的真实确认结果。
        """
        nonlocal snapshot
        snapshot = merge_notification_progress(snapshot, results)

    def reserve(part, should_submit):
        """
        恢复沿用同一目标的已保存发送记录。
        """
        nonlocal snapshot
        result, snapshot = claim_notification_part(snapshot, part, initial_attempt=first, should_submit=should_submit)
        return result

    def run():
        """
        由真实的模块拆分并发给同一话题。
        """
        message = Message(
            channel=NotificationChannel.Telegram, source="telegram", userid="123", original_chat_id="-789", thread_id="17",
            title="说明", text=detail, native_image=True, image=str(image), file_path=str(path), parse_mode="plain",
        )
        results = message.collect_delivery_results()
        with observe_notification_progress(record, reserve=reserve, plan=plan):
            module.post_message(message)
        expected_parts = [0, 1, 4096] if long_caption else [0, 4096]
        assert [item.part for item in results] == expected_parts
        assert {item.target_id for item in results} == {"chat:-789:topic:17"}
        return [item.status for item in results]

    confirmed = ["confirmed"] * (2 if long_caption else 1)
    assert run() == [*confirmed, "retryable_failure"]
    first = False
    assert run() == [*confirmed, "confirmed"]
    api.send_photo.assert_called_once()
    assert api.send_photo.call_args.kwargs["photo"] == content
    assert api.send_photo.call_args.kwargs["caption"] == (None if long_caption else "说明")
    if long_caption:
        api.send_message.assert_called_once()
        assert api.send_message.call_args.kwargs["text"] == f"说明\n{detail}"
    else:
        api.send_message.assert_not_called()
    assert api.send_document.call_count == 2
    assert all(call.kwargs["chat_id"] == "-789" and call.kwargs["message_thread_id"] == 17
               and not call.kwargs["caption"] for call in api.send_document.call_args_list)
    assert path.read_text() == "file content"


def test_telegram_resumes_frozen_legacy_file_without_new_image_part(monkeypatch, tmp_path):
    """
    旧组合消息只冻结了文件时，升级后按原受众、编号和摘要恢复该分段。
    """
    api = Mock()
    api.send_document.side_effect = [TelegramApiError("sendDocument", "limited", error_code=429), {"message_id": 102}]
    monkeypatch.setattr("app.modules.telegram.telegram.TelegramBotApi", Mock(return_value=api))
    client = Telegram(TELEGRAM_TOKEN="fixture", TELEGRAM_CHAT_ID="-100", auto_start_polling=False)
    monkeypatch.setattr(client, "_is_chat_rate_limited", lambda *_a, **_kw: False)
    config = NotificationConf(id="telegram", name="telegram", type="telegram", enabled=True, identity_namespace="binding")
    module = TelegramModule()
    module._channel = NotificationChannel.Telegram
    monkeypatch.setattr(module, "get_configs", lambda: {"telegram": config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    path = tmp_path / "report.txt"
    path.write_text("file content")
    snapshot = {}
    first = True

    def reserve(part, should_submit):
        """
        按原认领与分段记录校验提交。
        """
        nonlocal snapshot
        result, snapshot = claim_notification_part(snapshot, part, initial_attempt=first, should_submit=should_submit)
        return result

    def record(results):
        """
        存下原接口的结果供升级后恢复。
        """
        nonlocal snapshot
        snapshot = merge_notification_progress(snapshot, results)

    message = Message(
        channel=NotificationChannel.Telegram, source="telegram", userid="123", title="说明",
        image="https://fixture.example/cover.png", file_path=str(path), parse_mode="plain",
    )
    old_parts = NotificationParts(NotificationChannel.Telegram, config.id, config.identity_namespace)
    with observe_notification_progress(record, reserve=reserve):
        assert not client.send_tracked_attachment(
            message, "123", receipt=lambda *_: None, part_sender=old_parts.send, plan=old_parts.plan,
        )
    assert uses_legacy_attachment_layout(snapshot)
    first = False
    results = message.collect_delivery_results()
    with observe_notification_progress(record, reserve=reserve, legacy_attachment_layout=True):
        module.post_message(message)
    assert [(result.part, result.status) for result in results] == [(0, "confirmed")]
    assert api.send_document.call_count == 2
    api.send_photo.assert_not_called()


def test_telegram_managed_text_does_not_repeat_confirmed_or_unknown_parts(monkeypatch):
    """
    Telegram 普通文本恢复时保留话题和确认状态，未知提交仅发送一次。
    """
    api = Mock()
    api.send_message.side_effect = [
        {"message_id": 123}, TelegramNetworkError("sendMessage", "timeout"),
    ]
    monkeypatch.setattr("app.modules.telegram.telegram.TelegramBotApi", Mock(return_value=api))
    client = Telegram(TELEGRAM_TOKEN="fixture", TELEGRAM_CHAT_ID="-100", TELEGRAM_TOPIC_ID="7", auto_start_polling=False)
    config = NotificationConf(id="telegram", name="telegram", type="telegram", enabled=True, identity_namespace="binding")
    module = TelegramModule()
    module._channel = NotificationChannel.Telegram
    monkeypatch.setattr(module, "get_configs", lambda: {"telegram": config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    snapshot = {}
    first = True

    def plan(parts):
        """
        首次调用时保存连接内的完整发送清单。
        """
        nonlocal snapshot
        if first:
            snapshot = freeze_notification_parts(snapshot, parts)

    def record(results):
        """
        保存当前认领获得的新发送状态。
        """
        nonlocal snapshot
        snapshot = merge_notification_progress(snapshot, results)

    def reserve(part, should_submit):
        """
        根据当前认领和保存的地址决定是否可以发送。
        """
        nonlocal snapshot
        result, snapshot = claim_notification_part(snapshot, part, initial_attempt=first, should_submit=should_submit)
        return result

    def run():
        """
        从通知模块发送需要两段的纯文本。
        """
        message = Message(
            channel=NotificationChannel.Telegram, mtype=MessageType.Subscribe, parse_mode="plain", text="文" * 4200,
        )
        results = message.collect_delivery_results()
        with observe_notification_progress(record, reserve=reserve, plan=plan):
            module.post_message(message)
        return [(item.target_id, item.status) for item in results]

    monkeypatch.setattr(config, "switchs", [MessageType.Subscribe.value])
    expected = [("chat:-100:topic:7", "confirmed"), ("chat:-100:topic:7", "unknown")]
    assert run() == expected
    first = False
    assert run() == expected
    assert api.send_message.call_count == 2
    assert {item["message_id"] for item in snapshot.values() if item["status"] == "confirmed"} == {"123"}
    assert all(call.kwargs["chat_id"] == "-100" and call.kwargs["message_thread_id"] == 7
               for call in api.send_message.call_args_list)


@pytest.mark.parametrize("received_reply", [True, False])
@pytest.mark.parametrize("force_reply", [True, False])
def test_telegram_rich_fallback_requires_confirmed_rejection(monkeypatch, received_reply, force_reply):
    """
    富文本降级只发生在平台明确拒绝后，并保留原话题。
    """
    api = Mock()
    api.send_rich_message.side_effect = (
        TelegramApiError("sendRichMessage", "method unavailable", 404)
        if received_reply else TelegramNetworkError("sendRichMessage", "timeout")
    )
    api.send_message.return_value = {"message_id": 31}
    monkeypatch.setattr("app.modules.telegram.telegram.TelegramBotApi", Mock(return_value=api))
    client = Telegram(TELEGRAM_TOKEN="fixture", TELEGRAM_CHAT_ID="-100", TELEGRAM_TOPIC_ID="7", auto_start_polling=False)
    planned = Mock()
    delivered = Mock()

    def submit(_target, _part, _content, _should_submit, send):
        """
        在独立平台测试中立即调用已组装的计划提交操作。
        """
        result = send()
        return result[0], result[1]

    message = Message(text="标题", force_reply=force_reply, original_chat_id="-100" if force_reply else None,
                      original_message_id=81 if force_reply else None, thread_id="7" if force_reply else None)
    assert client.send_tracked_message(
        message, None, rich_message="**标题**", receipt=delivered, part_sender=submit, plan=planned,
    ) is received_reply
    api.send_rich_message.assert_called_once()
    if force_reply:
        assert api.send_rich_message.call_args.kwargs["reply_parameters"] == {"message_id": 81}
        assert api.send_rich_message.call_args.kwargs["reply_markup"]["force_reply"]
    if received_reply:
        context = {"reply_markup": api.send_rich_message.call_args.kwargs["reply_markup"], "reply_to_message_id": 81} if force_reply else {}
        api.send_message.assert_called_once_with(
            chat_id="-100", text="**标题**", parse_mode="", message_thread_id=7, **context,
        )
        delivered.assert_called_once_with("chat:-100:topic:7", 0, "confirmed", None)
    else:
        api.send_message.assert_not_called()
        delivered.assert_called_once_with("chat:-100:topic:7", 0, "unknown", "response_unconfirmed")


def test_telegram_managed_edit_preserves_original_message_after_restart(monkeypatch):
    """
    确认原群消息已经编辑，恢复时跳过确认，旧连接或新目标不能替代原消息。
    """
    api = Mock()
    api.edit_message_text.side_effect = TelegramApiError("editMessageText", "message is not modified", 400)
    monkeypatch.setattr("app.modules.telegram.telegram.TelegramBotApi", Mock(return_value=api))
    client = Telegram(TELEGRAM_TOKEN="fixture", TELEGRAM_CHAT_ID="-100", auto_start_polling=False)
    config = NotificationConf(id="telegram", name="telegram", type="telegram", enabled=True, identity_namespace="binding")
    module = TelegramModule()
    module._channel = NotificationChannel.Telegram
    monkeypatch.setattr(module, "get_configs", lambda: {"telegram": config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    message = Message(
        channel=NotificationChannel.Telegram, userid="111", original_chat_id="-200", original_message_id=41,
        thread_id="7", text="选项", parse_mode="plain", buttons=[[{"text": "确认", "callback_data": "fixture"}]],
    )
    results = message.collect_delivery_results()
    progress = {}
    for initial in (True, False):
        results.clear()
        with _observe_recovery(progress, initial=initial):
            module.post_message(message)
        assert [(item.target_id, item.status) for item in results] == [("chat:-200:topic:7", "confirmed")]
    api.edit_message_text.assert_called_once()
    assert api.edit_message_text.call_args.kwargs["message_id"] == 41
    assert api.edit_message_text.call_args.kwargs["chat_id"] == "-200"
    assert api.edit_message_text.call_args.kwargs["reply_markup"]["inline_keyboard"]
    api.send_message.assert_not_called()


def test_telegram_tracked_voice_preserves_source_after_unknown_result(monkeypatch, tmp_path):
    """
    语音读取后提交一次，超时状态和源文件都可供后台恢复核对。
    """
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"fixture-audio")
    api = Mock()
    api.send_voice.side_effect = TelegramNetworkError("sendVoice", "timeout")
    monkeypatch.setattr("app.modules.telegram.telegram.TelegramBotApi", Mock(return_value=api))
    client = Telegram(TELEGRAM_TOKEN="fixture", TELEGRAM_CHAT_ID="-100", auto_start_polling=False)
    receipt = Mock()
    plan = Mock()

    def submit(_target, _part, _content, _should_submit, send):
        """
        调用真实附件发送并返回平台分类。
        """
        result = send()
        return result[0], result[1]

    assert not client.send_tracked_attachment(
        Message(voice_path=str(audio)), "42", receipt=receipt, part_sender=submit, plan=plan,
    )
    assert audio.read_bytes() == b"fixture-audio"
    api.send_voice.assert_called_once()
    assert api.send_voice.call_args.kwargs["chat_id"] == "42"
    receipt.assert_called_once_with("chat:42", 0, "unknown", "response_unconfirmed")


@pytest.mark.parametrize("platform", [NotificationChannel.Telegram, NotificationChannel.WechatClawBot])
@pytest.mark.parametrize("status", ["confirmed", "retryable_failure", "unknown"])
def test_expired_attachment_keeps_frozen_address_and_original_acceptance_state(monkeypatch, tmp_path, platform, status):
    """
    附件过期后不向其他目标发送。已拒绝的分段永久失败，平台可能已接受的分段保持未知。
    """
    path = tmp_path / "fixture.txt"
    path.write_text("附件正文", encoding="utf-8")
    if platform == NotificationChannel.Telegram:
        api = Mock()
        submit = api.send_document
        submit.side_effect = [{
            "confirmed": {"message_id": 123},
            "retryable_failure": TelegramApiError("sendDocument", "ratelimit", 429),
            "unknown": TelegramNetworkError("sendDocument", "timeout"),
        }[status]]
        monkeypatch.setattr("app.modules.telegram.telegram.TelegramBotApi", Mock(return_value=api))
        client = Telegram(TELEGRAM_TOKEN="fixture", TELEGRAM_CHAT_ID="-100", auto_start_polling=False)
        deliver = client.send_tracked_attachment
        target = "chat:42"
    else:
        sender = Mock()
        submit = sender.send_tracked_media
        submit.return_value = (status, None)
        bot = WechatClawBot.__new__(WechatClawBot)
        bot._default_target = "other"
        bot._state = {}
        monkeypatch.setattr(bot, "_build_client", lambda: sender)
        deliver = bot.send_tracked_notification
        target = "42"
    progress = {}

    def send(*, first):
        """
        用持久投递合同再次准备原附件。
        """
        from functools import partial

        message = Message(file_path=str(path), userid="42", private_delivery=True)
        results = message.collect_delivery_results()
        parts = NotificationParts(platform, "channel", "account")
        with _observe_recovery(progress, initial=first):
            accepted = deliver(message, "42", receipt=partial(parts.record, message), part_sender=parts.send,
                               plan=parts.plan)
        assert accepted is (results[0].status == "confirmed")
        return results[0]

    original = send(first=True)
    assert original.status == status
    path.unlink()
    expired = send(first=False)
    assert expired.target_id == original.target_id == target
    assert expired.content_digest == original.content_digest
    if status == "retryable_failure":
        assert (expired.status, expired.code) == ("permanent_failure", "content_changed")
        assert next(iter(progress.values()))["status"] == "permanent_failure"
    else:
        assert expired == original
    submit.assert_called_once()


@pytest.mark.parametrize("original", [None, "om-parent"])
def test_feishu_tracked_cards_reuse_platform_dedup_for_retry(monkeypatch, original):
    """
    有限流证明时只重试该卡片，接收标识类型和平台 UUID 都保持稳定。
    """
    client = Feishu()
    client._api_client = Mock()
    submit = client._api_client.reply_message if original else client._api_client.create_message
    submit.side_effect = [
        FeishuResponse(0, "ok", {"message_id": "om-first"}),
        FeishuResponse(99991400, "rate limited"),
        FeishuResponse(0, "ok", {"message_id": "om-second"}),
    ]
    monkeypatch.setattr(client, "_build_notification_cards", lambda *_: [{"card": 1}, {"card": 2}])
    parts = NotificationParts(NotificationChannel.Feishu, "app", "tenant")
    progress = {}
    first = True

    def reserve(candidate, should_submit):
        """
        按真实恢复规则保留确认，限流的卡片可再次提交。
        """
        nonlocal progress
        existing, progress = claim_notification_part(
            progress, candidate, initial_attempt=first, should_submit=should_submit,
        )
        return existing

    def plan(deliveries):
        """
        首次派发保存卡片清单。
        """
        nonlocal progress
        if first:
            progress = freeze_notification_parts(progress, deliveries)

    def record(deliveries):
        """
        保存平台确认结果。
        """
        nonlocal progress
        progress = merge_notification_progress(progress, deliveries)

    receipt = Mock()
    message = Message(title="卡片", original_message_id=original)
    target = ("same-user", None, "user_id")
    with observe_notification_progress(record, reserve=reserve, plan=plan):
        assert not client.send_tracked_notification(
            message, target, nonce="fixture-event", receipt=receipt, plan=parts.plan, part_sender=parts.send,
        )
        first = False
        assert client.send_tracked_notification(
            message, target, nonce="fixture-event", receipt=receipt, plan=parts.plan, part_sender=parts.send,
        )
    requests = [call.kwargs for call in submit.call_args_list]
    assert len(requests) == 3 and requests[1] == requests[2]
    assert requests[0]["uuid"] != requests[1]["uuid"]
    if original:
        assert all(request["message_id"] == original for request in requests)
        client._api_client.create_message.assert_not_called()
    else:
        assert all(request["receive_id_type"] == "user_id" for request in requests)
        client._api_client.reply_message.assert_not_called()
    assert {part["message_id"] for part in progress.values()} == {"om-first", "om-second"}


def test_feishu_native_image_does_not_retry_replaced_source(monkeypatch, tmp_path):
    """
    限流后同路径的图像字节改变时拒绝续发，已确认的说明也不会再次发送。
    """
    from app.adapters.system import attachment

    client = Feishu()
    client._api_client = Mock()
    client._api_client.create_message.side_effect = [
        FeishuResponse(0, "ok", {"message_id": "caption"}), FeishuResponse(99991400, "limited"),
    ]
    source = tmp_path / "image.png"
    content = b"\x89PNG\r\n\x1a\noriginal"
    source.write_bytes(content)
    monkeypatch.setattr(attachment, "get_runtime_setting", lambda _: tmp_path)
    uploaded = []

    def upload(path):
        """
        记录实际上传的字节。
        """
        uploaded.append(path.read_bytes())
        return "key"

    monkeypatch.setattr(client, "_upload_image", upload)
    parts = NotificationParts(NotificationChannel.Feishu, "feishu", "binding")
    receipt = Mock()
    progress = {}
    message = Message(title="说明", native_image=True, image=str(source))
    for initial in (True, False):
        with _observe_recovery(progress, initial=initial):
            assert not client.send_tracked_notification(
                message, ("member", None, "user_id"), nonce="fixture",
                receipt=receipt, plan=parts.plan, part_sender=parts.send,
            )
        source.write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    assert uploaded == [content]
    assert client._api_client.create_message.call_count == 2
    assert receipt.call_args.args[-2:] == ("permanent_failure", "content_changed")


@pytest.mark.parametrize("case", ["image", "file", "missing", "upload-failed"])
def test_feishu_tracked_attachment_keeps_recipient_and_caption(tmp_path, monkeypatch, case):
    """
    图片保留卡片正文，文件单独发送正文，附件失败时保持原接收者并报告原因。
    """
    client = Feishu()
    client._api_client = Mock()
    submit = client._api_client.create_message
    submit.return_value = FeishuResponse(0, "ok", {"message_id": "om-ok"})
    source = tmp_path / ("fixture.png" if case == "image" else "fixture.srt")
    if case != "missing":
        source.write_bytes(b"fixture")
    monkeypatch.setattr(client, "_upload_image", Mock(return_value="image-key"))
    monkeypatch.setattr(client, "_upload_file", Mock(return_value=None if case == "upload-failed" else "file-key"))
    parts = NotificationParts(NotificationChannel.Feishu, "app", "tenant")
    receipt = Mock()
    result = client.send_tracked_notification(
        Message(file_path=str(source), title="附件", text="说明"),
        ("fixture-user", None, "user_id"), nonce="file-event",
        receipt=receipt, plan=parts.plan, part_sender=parts.send,
    )
    expected = {
        "image": ["interactive"], "file": ["file", "text"], "missing": [], "upload-failed": [],
    }
    requests = [call.kwargs for call in submit.call_args_list]
    assert [request["msg_type"] for request in requests] == expected[case]
    assert all(request["receive_id"] == "fixture-user" for request in requests)
    assert result is bool(expected[case])
    if result:
        assert "说明" in requests[-1]["content"]
    else:
        error = ("permanent_failure", "attachment_unavailable") if case == "missing" else (
            "retryable_failure", "attachment_upload_failed",
        )
        assert receipt.call_args_list[0].args == ("user_id:fixture-user", 0, *error)


@pytest.mark.parametrize("original", [None, "2.3"])
def test_slack_tracked_batches_restore_thread_and_disable_sdk_retries(monkeypatch, original):
    """
    仅通知模式无需 Socket Mode，新认领只补发 Slack 明确限流的批次。
    """
    client_factory = Mock()
    socket_factory = Mock()
    monkeypatch.setattr("app.modules.slack.slack.WebClient", client_factory)
    monkeypatch.setattr("app.modules.slack.slack.SocketModeHandler", socket_factory)
    slack = Slack(SLACK_OAUTH_TOKEN="fixture", start_socket=False)
    client_factory.assert_called_once_with(token="fixture", retry_handlers=[])
    socket_factory.assert_not_called()
    api = client_factory.return_value
    first_response = {"ok": True, "ts": "1.2"}
    retries = [SlackApiError("rate limit", Mock(status_code=429)), {"ok": True, "ts": "3.4"}]
    api.chat_update.return_value = first_response
    api.chat_postMessage.side_effect = retries if original else [first_response, *retries]
    parts = NotificationParts(NotificationChannel.Slack, "app", "workspace")
    progress = {}
    initial = True

    def plan(deliveries):
        """
        保存首次派发已确定的全部批次。
        """
        nonlocal progress
        if initial:
            progress = freeze_notification_parts(progress, deliveries)

    def record(deliveries):
        """
        保存具体批次的确认记录。
        """
        nonlocal progress
        progress = merge_notification_progress(progress, deliveries)

    def reserve(part, should_submit):
        """
        使用已有确认决定当前批次是否应发送。
        """
        nonlocal progress
        recorded, progress = claim_notification_part(
            progress, part, initial_attempt=initial, should_submit=should_submit,
        )
        return recorded

    message = Message(text="文" * 4200, original_chat_id="C1", thread_id="10.20", original_message_id=original)
    receipt = Mock()
    with observe_notification_progress(record, reserve=reserve, plan=plan):
        assert not slack.send_tracked_message(message, None, receipt=receipt, part_sender=parts.send, plan=parts.plan)
        initial = False
        assert slack.send_tracked_message(message, None, receipt=receipt, part_sender=parts.send, plan=parts.plan)
    calls = [call.kwargs for call in api.chat_postMessage.call_args_list]
    if original:
        assert api.chat_update.call_count == 1
        edit = api.chat_update.call_args.kwargs
        assert edit["channel"] == "C1" and edit["ts"] == original
        assert len(calls) == 2
    else:
        api.chat_update.assert_not_called()
        assert len(calls) == 3
    assert calls[-2] == calls[-1]
    assert all(call["channel"] == "C1" and call["thread_ts"] == "10.20" for call in calls)
    assert {part["message_id"] for part in progress.values()} == {"1.2", "3.4"}


def test_discord_tracked_edit_keeps_thread_and_retries_only_rate_limited_part(monkeypatch):
    """
    编辑和续发固定在原线程，明确限流后的重试跳过已确认的编辑。
    """
    client = Discord.__new__(Discord)
    client._loop = Mock()
    client._channel_id = 99
    client.get_state = Mock(return_value=True)
    channel = Mock(spec=discord.abc.Messageable)
    channel.id = 12
    platform_response = SimpleNamespace(status=429, reason="Too Many Requests")
    original = Mock()
    original.edit = AsyncMock(return_value=SimpleNamespace(id=34))
    channel.fetch_message = AsyncMock(return_value=original)
    channel.send = AsyncMock(side_effect=[discord.HTTPException(platform_response, "rate limit"), SimpleNamespace(id=56)])
    client._resolve_channel = AsyncMock(return_value=channel)
    message = Message(original_chat_id="12", original_message_id=34)
    monkeypatch.setattr(Message, "channel_text_chunks", Mock(return_value=["首段", "续段"]))
    parts = NotificationParts(NotificationChannel.Discord, "app", "account")
    progress = {}
    receipt = Mock()

    def submit(coroutine, _loop):
        """
        执行真实发送协程并保留结果或异常，隔离 Discord 线程和网络。
        """
        future = Mock()
        try:
            future.result.return_value = asyncio.run(coroutine)
        except discord.HTTPException as error:
            future.result.side_effect = error
        return future

    monkeypatch.setattr("app.modules.discord.discord.asyncio.run_coroutine_threadsafe", submit)
    with _observe_recovery(progress, initial=True):
        assert not client.send_tracked_notification(
            message, None, event="fixture-event", receipt=receipt, plan=parts.plan, part_sender=parts.send,
        )
    with _observe_recovery(progress, initial=False):
        assert client.send_tracked_notification(
            message, None, event="fixture-event", receipt=receipt, plan=parts.plan, part_sender=parts.send,
        )
    original.edit.assert_awaited_once()
    channel.fetch_message.assert_awaited_once_with(34)
    assert client._resolve_channel.await_args.args == (None, "12")
    assert channel.send.await_count == 2
    assert channel.send.await_args_list[0].kwargs["nonce"] == channel.send.await_args_list[1].kwargs["nonce"]
    assert {part["message_id"] for part in progress.values()} == {"34", "56"}
