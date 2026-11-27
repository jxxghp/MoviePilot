"""
渠道资源的引用完整性、时间窗口与纯接入规则边界。
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.domain.admission import AdmissionProof, allowed_context_scopes, matches_auto_download
from app.schemas.channel import (
    AdmissionRule,
    ChannelAddress,
    ChannelConnection,
    ChannelSettings,
    InteractionMatch,
    InteractionPolicy,
    NotificationRoute,
    NotificationSendTime,
)
from app.schemas.webpush import WebPushSubscription


def test_resource_references_reject_recreated_identity_namespace():
    """
    路由只能引用当前连接及其不可变身份空间。
    """
    connection = ChannelConnection(
        id="connection", name="Fixture", provider="fixture", mode="standard",
        identity_namespace="original", enabled=True, revision=1, provider_config={},
    )
    route = NotificationRoute(
        connection_id=connection.id, identity_namespace="recreated", enabled=True, revision=1,
    )
    with pytest.raises(ValidationError):
        ChannelSettings(connections={connection.id: connection}, notifications={connection.id: route})
    with pytest.raises(ValidationError):
        ChannelAddress(connection_id="connection", identity_namespace="original", kind="user", target="alice", thread_id="thread")


@pytest.mark.parametrize("clock,expected", [("21:59", False), ("22:00", True), ("00:30", True), ("06:00", True), ("06:01", False)])
def test_overnight_notification_window(clock, expected):
    """
    跨午夜发送时间含起止分钟，窗口外直接不生效。
    """
    window = NotificationSendTime(start="22:00", end="06:00")
    assert window.allows(datetime.fromisoformat(f"2026-11-27T{clock}:00")) is expected


@pytest.mark.parametrize("mutation", [{"sender": "bob"}, {"connection_id": "other"}, {"revision": 2}, {"expires_at": 99}, {"conversation": "elsewhere"}])
def test_context_rule_requires_current_authenticated_facts(mutation):
    """
    个人、连接、版本、时效或群组任一不符均不能命中规则。
    """
    policy = InteractionPolicy(
        connection_id="first", identity_namespace="namespace", enabled=True, revision=1,
        admission="rules", admission_rules=(AdmissionRule(kind="group", target="group", scopes=("search",)),),
        auto_download="specified", auto_download_rules=(InteractionMatch(kind="group", target="group"),),
    )
    facts = {"connection_id": "first", "revision": 1, "sender": "alice", "conversation": "group", "expires_at": 200}
    proof = AdmissionProof(**facts)
    assert allowed_context_scopes(policy, proof, "alice", 100) == frozenset({"search"})
    assert matches_auto_download(policy, proof, "alice", 100)
    facts.update(mutation)
    invalid = AdmissionProof(**facts)
    assert not allowed_context_scopes(policy, invalid, "alice", 100)
    assert not matches_auto_download(policy, invalid, "alice", 100)


@pytest.mark.parametrize("endpoint", ["http://push.invalid/key", "https://user:secret@push.invalid/key", "https://push.invalid/key#fragment", "https://push.invalid/key\n"])
def test_webpush_contract_rejects_ambiguous_endpoints(endpoint):
    """
    浏览器订阅不能夹带账号凭据、片段或控制字符。
    """
    with pytest.raises(ValidationError):
        WebPushSubscription(endpoint=endpoint, keys={"p256dh": "abc", "auth": "def"})


def test_delivery_receipt_is_immutable_and_roundtrips_extended_result():
    """
    回执保留连接、分段和确认状态，非法分段和未知状态不能进入冻结计划。
    """
    from app.schemas.delivery import DeliveryResult
    from app.schemas.types import NotificationChannel

    receipt = DeliveryResult(channel=NotificationChannel.Telegram, connection_id="first", identity_namespace="space", part=2, status="confirmed", message_id="receipt")
    assert DeliveryResult.model_validate_json(receipt.model_dump_json()) == receipt
    with pytest.raises(ValidationError):
        receipt.part = 3
    for patch in ({"part": -1}, {"status": "success"}):
        with pytest.raises(ValidationError):
            DeliveryResult.model_validate({**receipt.model_dump(), **patch})


@pytest.mark.parametrize("mode,scopes", [("manual", frozenset()), ("open", frozenset({"search", "subscribe", "download"})), ("rules", frozenset())])
def test_policy_modes_do_not_implicitly_grant_scheduled_tasks(mode, scopes):
    """
    无可信事实时规则不产生权限，开放策略仍不隐含定时任务或管理员能力。
    """
    policy = InteractionPolicy(connection_id="first", identity_namespace="space", enabled=True, revision=1, admission=mode)
    assert allowed_context_scopes(policy, None, "alice", 100) == scopes
    assert not matches_auto_download(policy, None, "alice", 100)
    assert not allowed_context_scopes(None, None, "alice", 100)
    assert not matches_auto_download(None, None, "alice", 100)
    assert not allowed_context_scopes(policy.model_copy(update={"enabled": False}), None, "alice", 100)


def test_webpush_subscription_preserves_browser_proof_without_owner():
    """
    有效浏览器证明可往返，客户端不能夹带服务端 owner。
    """
    data = {"endpoint": "https://push.invalid/key", "keys": {"p256dh": "abc", "auth": "def"}}
    subscription = WebPushSubscription.model_validate(data)
    assert subscription.model_dump(exclude_none=True) == data
    with pytest.raises(ValidationError):
        WebPushSubscription.model_validate({**data, "owner": "somebody"})
