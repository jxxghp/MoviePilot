"""
验证企业微信接入证明、首次授权和重放边界。
"""

import json
import xml.dom.minidom
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.application.messaging import ingress
from app.application.security import authorization
from app.application.security.authorization import ChannelAuthorizationService, ChannelMember, scoped_admission
from app.chain.interaction import MediaInteractionChain
from app.chain.message import MessageChain
from app.db.adapters.authorization import claim_inbound_event
from app.db.models.authorization import ChannelInboundReceipt
from app.db.session import SessionFactory
from app.domain.admission import AdmissionProof
from app.modules.wechat import WechatModule
from app.modules.wechat.wechatbot import WeChatBot
from app.schemas.channel import AdmissionRule, InteractionMatch, InteractionPolicy
from app.schemas.message import IncomingMessage
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel


@pytest.fixture
def policy():
    """
    返回一份启用的可信群聊策略。
    """
    return InteractionPolicy(
        connection_id="wecom-admission", identity_namespace="wecom-admission", enabled=True,
        admission="manual_rules", revision=3,
        admission_rules=(AdmissionRule(kind="group", target="allowed-group", scopes=("search",)),),
    )


@pytest.fixture
def proof(policy):
    """
    生成固定时间和发言人的证明。
    """
    return AdmissionProof(policy.connection_id, policy.revision, "alice", "allowed-group", 1030)


@pytest.mark.parametrize("mode", ["group", "user"])
def test_first_use_has_only_event_permissions(monkeypatch, policy, proof, mode):
    """
    未批准成员可搜索，但不能获得下载、定时或平台外的权限。
    """
    if mode == "user":
        policy = policy.model_copy(update={
            "admission": "manual_rules",
            "admission_rules": (AdmissionRule(kind="user", target="alice", scopes=("search",)),),
        })
        proof = replace(proof, conversation=None, private=True)
    member = ChannelMember(
        id="member", notification_id=policy.connection_id, channel=NotificationChannel.Wechat.value,
        sender_id="alice", display_name="Alice", status="pending", role="member", scopes=(), user_id=5,
        user_identity="channel-user", linked_user_id=None, created_at=0, updated_at=0, origin="request",
        reviewed_by=None,
    )
    config = NotificationConf(id=policy.connection_id, name="corp", type="wechat", enabled=True,
                              interaction_policy=policy, interaction_enabled=True)
    store = Mock()
    store.find.return_value = None
    store.request.return_value = member
    store.user.return_value = authorization.UserSnapshot(
        id=5, name="channel-alice", email=None, avatar=None, is_otp=False,
        is_active=True, is_superuser=False, settings={}, permissions={},
        notification_identity=member.user_identity, account_type="channel",
    )
    service = ChannelAuthorizationService(lambda: nullcontext(store))
    monkeypatch.setattr(authorization, "get_notification_configs", lambda: [config])
    monkeypatch.setattr(authorization.time, "time", lambda: 1000)
    channel = NotificationChannel.Wechat.value
    with scoped_admission(proof):
        user = service.resolve_operation(channel, "corp", "alice", "search.torrents")
        assert user.name == "channel-alice" and user.permissions == {"search": True}
        assert user.authorization_id is None and not user.is_superuser
        with pytest.raises(PermissionError):
            service.resolve_operation(channel, "corp", "alice", "download.add")
        with pytest.raises(PermissionError):
            service.principal(member.id, capability="schedule")
    with pytest.raises(PermissionError):
        service.resolve_operation(channel, "corp", "alice", "search.torrents")
    for denied in ("disabled", "rejected"):
        store.find.return_value = replace(member, status=denied)
        with scoped_admission(proof), pytest.raises(PermissionError):
            service.resolve_operation(channel, "corp", "alice", "search.torrents")
    original = store.user.return_value
    linked = replace(original, id=6, name="local-alice", account_type="local")
    store.find.return_value = store.request.return_value = replace(
        member, user_id=linked.id, linked_user_id=linked.id,
    )
    store.user.return_value = linked
    with scoped_admission(proof), pytest.raises(PermissionError, match="关联账号没有"):
        service.resolve_operation(channel, "corp", "alice", "search.torrents")


@pytest.mark.parametrize(("preference", "sender", "verified", "expected"), [
    ("off", "alice", True, False),
    ("all", "alice", False, True),
    ("specified", "alice", True, True),
    ("specified", "bob", True, False),
    ("specified", "alice", False, False),
])
def test_channel_auto_download_uses_only_current_connection(monkeypatch, policy, proof, preference, sender, verified, expected):
    """
    指定仅匹配当前连接已验证的事件，全部也不授予下载权限。
    """
    configured = policy.model_copy(update={
        "auto_download": preference,
        "auto_download_rules": (InteractionMatch(kind="group", target="allowed-group"),),
    })
    config = NotificationConf(id=policy.connection_id, name="corp", type="wechat", enabled=True,
                              interaction_policy=configured, interaction_enabled=True)
    monkeypatch.setattr(authorization, "get_notification_configs", lambda: [config])
    monkeypatch.setattr(authorization.time, "time", lambda: 1000)
    with scoped_admission(proof if verified else None):
        assert MediaInteractionChain._should_auto_download(sender, "corp", NotificationChannel.Wechat) is expected
        assert not MediaInteractionChain._should_auto_download(sender, "other", NotificationChannel.Wechat)
        assert not MediaInteractionChain._should_auto_download(sender, "corp", NotificationChannel.Telegram)


@pytest.mark.parametrize("mismatch", [
    {"conversation": "other-group"}, {"connection_id": "different-bot"}, {"revision": 2},
    {"sender": "bob"}, {"expires_at": 1000}, {"conversation": None, "private": True},
])
def test_context_proof_must_match_current_event_and_policy(monkeypatch, policy, proof, mismatch):
    """
    跨群、跨连接、改版、换发言人或过期的证明不能复用。
    """
    config = NotificationConf(id=policy.connection_id, name="corp", type="wechat", enabled=True,
                              interaction_policy=policy)
    monkeypatch.setattr(authorization.time, "time", lambda: 1000)
    with scoped_admission(replace(proof, **mismatch)):
        assert not ChannelAuthorizationService(Mock()).context_scopes(config, "alice")


def test_group_callback_requires_current_authenticated_gateway(monkeypatch, policy):
    """
    官方长连接当前订阅确认生效后，可信群文本保留个人发送者和群回复地址。
    """
    monkeypatch.setattr(WeChatBot, "_start_gateway", lambda self: None)
    monkeypatch.setattr(WeChatBot, "_load_known_targets", lambda self: None)
    bot = WeChatBot(WECHAT_BOT_ID="bot", WECHAT_BOT_SECRET="fixture", name="corp", interaction_policy=policy)
    current = Mock()
    monkeypatch.setattr(bot, "_ws_app", current)
    send = Mock()
    monkeypatch.setattr(bot, "_send_raw", send)
    submit = Mock()
    monkeypatch.setattr(bot, "_message_ingress", submit)
    monkeypatch.setattr("app.modules.wechat.wechatbot.time.time", lambda: 1000)

    bot._on_open(current)
    request_id = send.call_args.args[0]["headers"]["req_id"]
    ack = json.dumps({"headers": {"req_id": request_id}, "errcode": 0})
    body = {"chattype": "group", "chatid": "allowed-group", "msgid": "message", "aibotid": "bot",
            "from": {"userid": "alice"}, "msgtype": "text", "text": {"content": "搜索电影"}}
    def event(payload):
        """
        返回回调对应的网关报文。
        """
        return json.dumps({"cmd": "aibot_msg_callback", "body": payload})

    bot._on_message(Mock(), ack)
    bot._on_message(current, event(body))
    submit.assert_not_called()
    bot._on_message(current, ack)
    bot._on_message(current, event({**body, "aibotid": "other-bot"}))
    bot._on_message(current, event({**body, "compat_info": {"source": "channel"}}))
    bot._on_message(Mock(), event(body))
    submit.assert_not_called()
    bot._on_message(current, event(body))
    incoming = submit.call_args.args[0]["body"]
    assert (incoming["from"]["userid"], incoming["chatid"], incoming["msgid"]) == ("alice", "allowed-group", "message")
    message = WechatModule()._notification_target(
        Mock(private_delivery=False, original_chat_id=f"group:{incoming["chatid"]}"),
        NotificationConf(config={"WECHAT_MODE": "bot"}),
    )
    assert message == "group:allowed-group"
    bot._on_message(current, json.dumps({
        "cmd": "aibot_event_callback", "body": {"event": {"eventtype": "disconnected_event"}},
    }))
    bot._on_message(current, event(body))
    submit.assert_called_once()


def test_verified_sender_proof_checks_decrypted_metadata(monkeypatch, policy):
    """
    应用账号证明只接受匹配的应用 ID、消息 ID 和时间。
    """
    monkeypatch.setattr("app.modules.wechat.time.time", lambda: 1000)
    config = NotificationConf(id=policy.connection_id, config={"WECHAT_APP_ID": "12"}, interaction_policy=policy)
    for agent_id, created_at, expected in ((12, 1000, True), (13, 1000, False), (12, 900, False)):
        root = xml.dom.minidom.parseString(
            f"<xml><AgentID>{agent_id}</AgentID><CreateTime>{created_at}</CreateTime><MsgId>m1</MsgId></xml>",
        ).documentElement
        message = IncomingMessage()
        WechatModule._attach_verified_sender(message, config, root, "alice")
        assert (message._admission_proof is not None) is expected
        assert message.message_id == ("m1" if expected else None)


def test_verified_message_replay_blocked_at_chain_entry(monkeypatch, proof):
    """
    重复平台事件止于链路入口，不重复触发普通用户交互。
    """
    monkeypatch.setattr(ingress.time, "time", lambda: 1000)
    monkeypatch.setattr(ingress, "_verified_events", ingress.TTLCache(maxsize=3, ttl=120))
    chain = MessageChain()
    handle = Mock()
    monkeypatch.setattr(chain, "handle_message", handle)
    info = IncomingMessage(
        channel=NotificationChannel.Wechat, source="corp", userid="alice",
        chat_id="group:allowed-group", message_id="event-1", text="搜索电影",
    )
    info._admission_proof = proof
    chain.process_incoming(info)
    chain.process_incoming(info)
    handle.assert_called_once()


def test_shared_verified_event_claim_survives_restart_and_concurrent_delivery(monkeypatch, proof):
    """
    两个独立事务争抢同一事件时只允许一次，清空进程内状态后仍识别重复事件。
    """
    proof = replace(proof, connection_id=uuid4().hex)
    monkeypatch.setattr(ingress.time, "time", lambda: 1000.0)
    database_claim = partial(claim_inbound_event, SessionFactory)
    fingerprints = set()
    barrier = Barrier(2)

    def claim(fingerprint, now, expires_at):
        """
        仅记录合成样本的摘要，供用例退出时从共享测试库清理。
        """
        fingerprints.add(fingerprint)
        return database_claim(fingerprint, now, expires_at)

    def deliver():
        """
        从独立线程模拟两个宿主同时收到同一事件。
        """
        barrier.wait(timeout=5)
        return ingress.claim_verified_event(proof, "same-event")

    ingress.configure_verified_event_claim(claim)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(deliver) for _ in range(2)]
            assert sorted(job.result(timeout=10) for job in jobs) == [False, True]
        ingress.configure_verified_event_claim(None)
        ingress.configure_verified_event_claim(claim)
        assert not ingress.claim_verified_event(proof, "same-event")
        assert ingress.claim_verified_event(replace(proof, connection_id=uuid4().hex), "same-event")
        monkeypatch.setattr(ingress.time, "time", lambda: 1121.0)
        assert not ingress.claim_verified_event(proof, "expired-proof")
        assert ingress.claim_verified_event(replace(proof, expires_at=1130.0), "same-event")
    finally:
        ingress.configure_verified_event_claim(None)
        with SessionFactory() as session, session.begin():
            session.execute(delete(ChannelInboundReceipt).where(ChannelInboundReceipt.id.in_(fingerprints)))


def test_sdk_rule_proof_requires_current_connection_and_cannot_come_from_json(monkeypatch, policy):
    """
    SDK 与公开接口复用解析器，只有当前稳定连接的 SDK 入口能附加证明。
    """
    channel = NotificationChannel.Telegram
    config = NotificationConf(id=policy.connection_id, name="corp", type="telegram", enabled=True,
                              interaction_policy=policy, interaction_enabled=True)
    monkeypatch.setattr(authorization, "get_notification_configs", lambda: [config])
    monkeypatch.setattr(ingress, "_verified_event_claim", lambda *_: True)
    chain = MessageChain()
    body = b'{"_admission_proof": {"sender": "alice"}}'
    handle = Mock(side_effect=lambda **_: authorization._admission_proof.get())
    received = []

    def capture(**kwargs):
        """
        在作用域内部读取证明。
        """
        received.append(handle(**kwargs))

    monkeypatch.setattr(chain, "handle_message", capture)
    monkeypatch.setattr(chain, "message_parser", lambda **_: IncomingMessage(
        channel=channel, source="corp", userid="alice", text="搜索电影", chat_id="allowed-group", conversation_type="group",
    ))
    monkeypatch.setattr(chain, "run_module", lambda _method, **_: IncomingMessage(
        channel=channel, source="corp", userid="alice", text="搜索电影", chat_id="allowed-group", conversation_type="group",
    ))
    monkeypatch.setattr("app.chain.message.get_channel_authorization", lambda: object())
    chain.process(body, None, {"source": "corp"})
    chain.process_platform_payload(body, "corp", channel, "stale-connection")
    monkeypatch.setattr("app.chain.message.get_channel_authorization", lambda: object())
    chain.process_platform_payload(body, "corp", channel, config.id, "telegram", config.id, "TelegramModule", policy.revision)
    assert len(received) == 2 and received[0] is None
    assert received[1].conversation == "allowed-group"
    assert received[1].connection_id == config.id


@pytest.mark.parametrize("mode", ["trusted_conversation", "verified_sender", "manual_allowlist", "manual_exemption", "exemption"])
def test_intermediate_admission_modes_are_not_part_of_the_final_contract(mode):
    """
    未发布的中间审批模式不再成为最终配置的可保存值。
    """
    with pytest.raises(ValueError):
        InteractionPolicy(connection_id="bot", identity_namespace="bot", enabled=True,
                          revision=1, admission=mode)


def test_rule_admission_does_not_offer_or_create_an_application(monkeypatch, policy):
    """
    规则审批时群外请求先止于准入，管理员已有批准仍可用。
    """
    config = NotificationConf(id=policy.connection_id, name="corp", type="wechat", enabled=True,
                              interaction_policy=policy.model_copy(update={"admission": "rules"}))
    service = Mock()
    service.notification.return_value = config
    service.find.return_value = None
    service.context_scopes.return_value = frozenset()
    chain = MessageChain()
    post = Mock()
    monkeypatch.setattr(chain, "post_message", post)
    monkeypatch.setattr("app.chain.message.get_channel_authorization", lambda: service)
    assert chain._handle_channel_authorization(NotificationChannel.Wechat, "corp", "alice", "Alice", "/apply", None)
    service.request.assert_not_called()
    assert "审批规则" in post.call_args.args[0].title
    service.find.return_value = Mock(status="approved")
    assert not chain._handle_channel_authorization(
        NotificationChannel.Wechat, "corp", "alice", "Alice", "搜索 电影", None,
    )


def test_disabled_rules_and_open_admission_scope(policy, proof):
    """
    停用规则既不授予范围，也不触发自动下载，开放准入只给普通业务权限。
    """
    from app.domain.admission import allowed_context_scopes, matches_auto_download

    disabled = policy.model_copy(update={
        "admission_rules": (replace_rule := policy.admission_rules[0].model_copy(update={"enabled": False}),),
        "auto_download": "specified",
        "auto_download_rules": (replace_rule,),
    })
    assert not allowed_context_scopes(disabled, proof, "alice", 1000)
    assert not matches_auto_download(disabled, proof, "alice", 1000)
    open_policy = policy.model_copy(update={"admission": "open"})
    assert allowed_context_scopes(open_policy, None, "anyone", 1000) == {"search", "subscribe", "download"}
