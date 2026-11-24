"""
用不在内置平台清单中的最小渠道验证扩展边界、身份隔离和后端校验。
"""

from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import delete

from app.application.messaging.channel.catalog import import_legacy_channels, project_runtime_channels
from app.application.messaging.channel.rules import (
    matches_notification_rule,
    routed_notification_messages,
    validate_notification_rule,
)
from app.application.messaging.channel.settings import ChannelSettingsService, read_channel_resources
from app.application.security import authorization as auth
from app.application.security.authorization import ChannelAuthorizationService
from app.db.adapters.authorization import authorization_scope
from app.db.models.authorization import ChannelAuthorization
from app.db.models.user import User
from app.db.session import SessionFactory
from app.domain.authorization import ChannelActor, channel_actor_ids
from app.modules._base.channel import ChannelPlatformBase
from app.runtime.capabilities.model import ActivationPolicy, CapabilitySpec, SelectorSpec
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.extensions.module.channel import ModuleChannelCatalog
from app.schemas.channel import ChannelPlatformCapabilities, NotificationRule
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import MessageType, SystemConfigKey


class FixturePlatform(ChannelPlatformBase):
    """
    仅用于测试的第三方平台，复用既有 Web 枚举作为传输名称，不修改任何通用业务代码。
    """

    provider = "boundary-fixture"
    channel = "Web"
    interaction = True
    binding_fields = ("fixture_actor",)
    target_patterns = {"user": r"p-[a-z]+", "conversation": r"g-[a-z]+"}
    topic_pattern = r"thread-[a-z]+"
    capabilities_model = ChannelPlatformCapabilities(
        rule_kinds=("user", "group"),
        target_kinds=("bound", "user", "conversation"),
        thread_id=True,
        target_labels={"conversation": "fixture.conversation"},
    )

    def actor(self, value: str) -> ChannelActor:
        """
        解释测试协议的个人、群组和确定性旧别名，绝不产生授权结论。
        """
        if value.startswith("ROOM::"):
            return ChannelActor(value.removeprefix("ROOM::"), "group")
        name = value.removeprefix("PERSON::").removeprefix("p-")
        return ChannelActor("p-" + name, aliases=("PERSON::" + name,))


@pytest.fixture
def extension(monkeypatch, db):
    """
    经现有注册表投影加载测试模块，以真实事务验证授权并恢复测试水位。

    :param monkeypatch: 可撤销端口替换
    :param db: 隔离数据库水位工具
    :yields: 测试目录、通知配置和授权服务
    """
    from app.application import configuration

    db.watermark(User)
    spec = CapabilitySpec(
        schema_version=1,
        id="module.boundary-fixture",
        kind="module",
        entrypoint=__name__ + ":FixturePlatform",
        activation=ActivationPolicy.WHEN_CONFIGURED,
        metadata={
            "name": "Fixture",
            "type": "notification",
            "subtype": "Web",
            "priority": 0,
            "channel_extension": __name__ + ":FixturePlatform",
            "channel_interaction": True,
        },
        selector=SelectorSpec("service", {"match_value": FixturePlatform.provider}),
        watch=(),
        depends_on=(),
        source=Path(__file__),
    )
    directory = ModuleChannelCatalog(CapabilityRegistry({spec.id: spec}, kinds={"module"}, selector_schemas={}))
    monkeypatch.setattr(configuration, "get_channel_platform_catalog", lambda: directory)
    configs = [
        NotificationConf(id=key, name=key, type=FixturePlatform.provider, enabled=True) for key in ("first", "second")
    ]
    monkeypatch.setattr(auth, "get_notification_configs", lambda **_: configs)
    service = ChannelAuthorizationService(partial(authorization_scope, SessionFactory))
    try:
        yield directory, configs, service
    finally:
        with SessionFactory() as session:
            session.execute(
                delete(ChannelAuthorization).where(ChannelAuthorization.notification_id.in_(["first", "second"]))
            )
            session.commit()


def test_new_platform_uses_generic_authorization_and_queries(extension):
    """
    注册新平台即可规范入站及手工身份、读取旧别名；个人不跨连接获得权限。
    """
    _, _, service = extension
    first = service.request("Web", "first", "PERSON::alice", "Alice")
    assert first.sender_id == "p-alice" and first.status == "pending"
    assert service.find("Web", "first", "p-alice").id == first.id
    second = service.request("Web", "second", "PERSON::alice", "Alice")
    service.update(first.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
    assert service.resolve("Web", "first", "PERSON::alice").id == first.user_id
    with pytest.raises(PermissionError):
        service.resolve("Web", "second", "PERSON::alice")
    assert second.user_id != first.user_id
    with pytest.raises(ValueError, match="群地址"):
        service.request("Web", "first", "ROOM::alice", "Fake Alice")
    with SessionFactory() as session:
        row = session.get(ChannelAuthorization, first.id)
        row.sender_id = "PERSON::alice"
        session.commit()
    assert service.find("Web", "first", "p-alice").id == first.id
    # 两条历史授权都声明同一人时，查询和重新审批必须拒绝，而不是合并权限。
    collision = service.request("Web", "first", "PERSON::bob", "Bob")
    with SessionFactory() as session:
        row = session.get(ChannelAuthorization, collision.id)
        row.aliases = ["p-alice"]
        session.commit()
    with pytest.raises(ValueError, match="冲突"):
        service.find("Web", "first", "PERSON::alice")
    with pytest.raises(ValueError, match="冲突"):
        service.update(first.id, status="approved", role="admin", scopes=(), reviewer_id=1)


def test_new_platform_uses_generic_settings_and_rules(extension):
    """
    公共设置页获得新目录和非数字线程能力；规则与仓储无需平台分支。
    """
    _, configs, _ = extension
    snapshot = import_legacy_channels(configs)
    config = Mock()
    config.get.side_effect = lambda key: (
        snapshot.model_dump(mode="json") if key == SystemConfigKey.ChannelSettings else None
    )
    view = ChannelSettingsService(config).read()
    assert view.providers[FixturePlatform.provider] == "Fixture"
    assert view.rule_kinds["first"] == ("user", "group")
    assert view.capabilities["first"].target_labels["conversation"] == "fixture.conversation"
    rule = NotificationRule(kind="conversation", target="g-room", thread_id="thread-red", message_types=("订阅",))
    validate_notification_rule(snapshot.connections["first"], rule)
    with pytest.raises(ValueError):
        validate_notification_rule(snapshot.connections["first"], rule.model_copy(update={"target": "p-alice"}))
    message = Message(mtype=MessageType.Subscribe)
    message.set_notification_route_scope("admin")
    assert matches_notification_rule(message, rule)
    (sent,) = routed_notification_messages(message, configs[0], (rule,), has_bound_target=False)
    assert (sent.userid, sent.thread_id) == ("g-room", "thread-red")
    assert len(project_runtime_channels(snapshot)) == 2
    assert (
        read_channel_resources(snapshot.model_dump(mode="json"), None).connections.keys() == snapshot.connections.keys()
    )


def test_domain_only_accepts_normalized_person_facts():
    """
    任意平台声明群组能力也不能使群组通过领域个人校验。
    """
    assert channel_actor_ids(ChannelActor("one", aliases=("old", "one"))) == ("one", "old")
    for actor in (ChannelActor("room", "group"), ChannelActor("x", "unknown"), ChannelActor(" ")):
        with pytest.raises(ValueError):
            channel_actor_ids(actor)


@pytest.mark.parametrize(
    "provider,config",
    [
        ("telegram", {"TELEGRAM_CHAT_ID": "-123", "TELEGRAM_TOPIC_ID": "09"}),
        ("wechat", {"WECHAT_MODE": "bot", "WECHAT_BOT_CHAT_ID": "group:room"}),
        ("wechat", {"WECHAT_MODE": "app"}),
        ("wechatclawbot", {"WECHATCLAWBOT_DEFAULT_TARGET": "alice"}),
        ("feishu", {"FEISHU_OPEN_ID": "ou_alice", "FEISHU_CHAT_ID": "oc_room", "FEISHU_ENCRYPT_KEY": "old"}),
        ("qqbot", {"QQ_OPENID": "alice", "QQ_GROUP_OPENID": "room"}),
        ("discord", {"DISCORD_GUILD_ID": "12", "DISCORD_CHANNEL_ID": "23"}),
        ("slack", {"SLACK_CHANNEL": "#releases"}),
        ("vocechat", {"VOCECHAT_CHANNEL_ID": 7}),
        ("synologychat", {}),
        ("dingtalk", {}),
        ("webpush", {"WEBPUSH_USERNAME": "alice,bob"}),
        ("apppush", {}),
        ("unknown-plugin", {}),
    ],
)
def test_runtime_platform_import_equals_frozen_offline_upgrade(provider, config, monkeypatch):
    """
    离线升级与运行时协议产生等价最终配置；升级过程不读取目录或访问平台。
    """
    from app.application import configuration
    from database.legacy.channel_resources_v1 import read_channel_resources as frozen_read

    rows = [
        {"id": "parity", "name": "parity", "type": provider, "config": config, "switchs": ["订阅"], "enabled": True}
    ]
    runtime = read_channel_resources(None, rows).model_dump(mode="json")
    # 即使宿主模块注册不可用，已发布迁移仍能离线执行。
    monkeypatch.setattr(
        configuration, "get_channel_platform_catalog", Mock(side_effect=AssertionError("runtime discovery forbidden"))
    )
    assert frozen_read(None, rows).model_dump(mode="json") == runtime


@pytest.mark.parametrize(
    "channel,kind,reference,method,encoding",
    [
        ("Telegram", "audio", "tg://voice_file_id/id", "download_telegram_file_bytes", "bytes"),
        ("Telegram", "image", "tg://file_id/id", "download_telegram_file_to_base64", "base64_jpeg"),
        ("Slack", "file", "https://files.invalid/id", "download_slack_file_to_data_url", "data_url"),
        ("Discord", "file", "discord://file/id", "download_discord_file_bytes", "bytes"),
        ("QQ", "audio", "qq://file/id", "download_qq_file_bytes", "bytes"),
        ("VoceChat", "file", "vocechat://file/id", "download_vocechat_file_bytes", "bytes"),
        ("SynologyChat", "file", "synology://file/id", "download_synologychat_file_bytes", "bytes"),
        ("飞书", "image", "feishu://image/id", "download_feishu_image_to_data_url", "data_url"),
        ("微信", "audio", "wxwork://voice_media_id/id", "download_wechat_media_bytes", "bytes"),
        ("微信ClawBot", "audio", "wxclaw://voice/id", "download_wechat_media_bytes", "bytes"),
    ],
)
def test_platform_attachment_plan_preserves_existing_module_abi(channel, kind, reference, method, encoding):
    """
    调度计划保留旧模块方法、引用和返回编码，传输协议不进入 Chain。
    """
    from app.chain.message import MessageChain
    from app.schemas.types import NotificationChannel

    channel_enum = NotificationChannel(channel)
    plan = MessageChain._attachment_read_plan(kind, reference, channel_enum)
    assert (plan.method, plan.encoding) == (method, encoding)
    if not reference.startswith("http"):
        assert MessageChain._attachment_read_plan(kind, reference, NotificationChannel.WebAgent).method == method


def test_slack_split_preserves_frozen_part_keys_and_confirmed_retry():
    """
    Slack 的模块分段与旧布局一致，部分确认后重试只发送剩余部分且拒绝变更内容和线程。
    """
    from app.application.messaging.attachment import notification_parts
    from app.application.messaging.delivery import (
        NotificationParts,
        claim_notification_part,
        merge_notification_progress,
        notification_part_key,
        observe_notification_progress,
        seal_notification_plan,
    )
    from app.schemas.types import NotificationChannel

    original = Message(
        title="caption",
        text="body",
        native_image=True,
        image="data:image/png;base64,aGVsbG8=",
        buttons=[[{"text": "Open", "url": "https://example.invalid"}]],
        file_path="/tmp/fixture.pdf",
    )
    messages = notification_parts(original, NotificationChannel.Slack)
    assert len(messages) == 3
    assert [
        (item.title, bool(item.image), item.attachment_part_offset, item.attachment_part_final) for item in messages
    ] == [
        ("caption", False, 0, False),
        (None, True, 4096, False),
        (None, False, 8192, True),
    ]
    planned = []
    with observe_notification_progress(lambda _: None, plan=planned.extend):
        for item in messages:
            parts = NotificationParts(
                NotificationChannel.Slack, "slack", "tenant", part_offset=item.attachment_part_offset
            )
            parts.plan((("room/thread-1", 0, item.model_dump_json()),))
    progress = seal_notification_plan({}, tuple(planned))
    first = planned[0].model_copy(update={"status": "confirmed", "message_id": "m1"})
    progress = merge_notification_progress(progress, (first,))
    submit = Mock(return_value=("confirmed", None))

    def reserve(candidate, should_submit):
        """
        使用真实进度保留规则模拟持久化事务，保持冻结计划与内容复核。
        """
        nonlocal progress
        result, progress = claim_notification_part(
            progress, candidate, should_submit=should_submit, initial_attempt=False
        )
        return result

    with observe_notification_progress(lambda results: None, reserve=reserve):
        for item in messages:
            parts = NotificationParts(
                NotificationChannel.Slack, "slack", "tenant", part_offset=item.attachment_part_offset
            )
            parts.send("room/thread-1", 0, item.model_dump_json(), True, submit)
    assert submit.call_count == 2
    assert progress[notification_part_key(first)]["message_id"] == "m1"
    changed = planned[1].model_copy(update={"content_digest": "different"})
    # 已提交但未确认的分段保留 unknown，不重复发送；明确未提交的变更分段被拒绝。
    frozen = seal_notification_plan({}, tuple(planned))
    outcome, _ = claim_notification_part(frozen, changed, initial_attempt=False, should_submit=True)
    assert outcome.code == "content_changed"
    outcome, _ = claim_notification_part(
        frozen, planned[1].model_copy(update={"target_id": "room/thread-2"}), initial_attempt=False, should_submit=True
    )
    assert outcome.code == "unplanned_target"


def test_authenticated_alias_rules_do_not_expand_context_scope(extension):
    """
    原始入站、管理员规则与旧标识统一规范化，连接、群组和过期证明仍独立拒绝。
    """
    import time
    from dataclasses import replace

    from app.domain.admission import AdmissionProof
    from app.schemas.channel import AdmissionRule, InteractionPolicy

    _, configs, service = extension
    policy = InteractionPolicy(
        connection_id="first",
        identity_namespace="first",
        enabled=True,
        revision=3,
        admission="rules",
        admission_rules=(AdmissionRule(kind="user", target="PERSON::alice", scopes=("search",)),),
    )
    config = configs[0].model_copy(update={"interaction_policy": policy})
    proof = AdmissionProof("first", 3, "PERSON::alice", "ROOM::red", time.time() + 60)
    with auth.scoped_admission(proof):
        assert service.context_scopes(config, "p-alice") == {"search"}
        assert service.context_scopes(config, "PERSON::bob") == set()
        assert service.context_scopes(config, "ROOM::alice") == set()
    for invalid in (replace(proof, connection_id="second"), replace(proof, revision=2), replace(proof, expires_at=0)):
        with auth.scoped_admission(invalid):
            assert service.context_scopes(config, "PERSON::alice") == set()
    group_policy = policy.model_copy(
        update={"admission_rules": (AdmissionRule(kind="group", target="ROOM::red", scopes=("search",)),)}
    )
    config = config.model_copy(update={"interaction_policy": group_policy})
    with auth.scoped_admission(proof):
        assert service.context_scopes(config, "p-alice") == {"search"}
    with auth.scoped_admission(replace(proof, conversation="ROOM::blue")):
        assert service.context_scopes(config, "p-alice") == set()


@pytest.mark.parametrize("provider", ["telegram", "discord", "qqbot", "wechat"])
def test_persisted_flat_compatibility_fields_are_platform_owned(provider):
    """
    升级前空字段可读，Discord 旧值保留公开兼容而规范快照无平台 flat 字段。
    """
    rows = [{"id": "compat", "name": "compat", "type": provider, "enabled": True, "config": {}}]
    raw = read_channel_resources(None, rows).model_dump(mode="json")
    raw["interactions"]["compat"].pop("platform_options")
    raw["interactions"]["compat"]["command_guild_id"] = "123" if provider == "discord" else None
    snapshot = read_channel_resources(raw, None)
    assert "command_guild_id" not in snapshot.interactions["compat"].model_dump()
    if provider == "discord":
        assert snapshot.interactions["compat"].platform_options == {"command_guild_id": "123"}


@pytest.mark.parametrize(
    "reference,expected,method",
    [
        ("wxwork://file_media_id/id", b"file", "download_wechat_media_bytes"),
        ("wxbot://image/id", b"image", "download_wechat_image_to_data_url"),
        ("wxbot://file/https%3A%2F%2Ffiles.invalid%2Ffile.bin", b"http", None),
        ("wxbot://unsupported", None, None),
    ],
)
def test_wechat_attachment_decoding_preserves_source_and_host_transport(monkeypatch, reference, expected, method):
    """
    企业微信文件/图片引用由模块解码，沿原 source 下载，HTTP 地址仍交给宿主网络策略。
    """
    from app.chain.message import MessageChain
    from app.schemas.types import NotificationChannel

    chain = MessageChain()
    module = Mock(return_value="data:image/png;base64,aW1hZ2U=" if method and "image" in method else b"file")
    http = Mock(return_value=(b"http", {}))
    monkeypatch.setattr(chain, "run_module", module)
    monkeypatch.setattr("app.chain.message._read_message_http", http)
    assert chain._download_message_file_bytes(reference, NotificationChannel.Wechat, "fixture-source") == expected
    if method:
        assert module.call_args.args[0] == method
        assert module.call_args.kwargs["source"] == "fixture-source"
        http.assert_not_called()
    elif expected:
        http.assert_called_once_with("https://files.invalid/file.bin")
        module.assert_not_called()
    else:
        module.assert_not_called()
        http.assert_not_called()
