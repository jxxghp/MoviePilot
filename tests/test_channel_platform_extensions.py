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
from app.domain.authorization import channel_actor_ids
from app.modules import _MessageBase
from app.modules._base.channel import ChannelPlatformBase
from app.runtime.capabilities.model import ActivationPolicy, CapabilitySpec, SelectorSpec
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.extensions.module.channel import ModuleChannelCatalog
from app.schemas.channel import NotificationRule
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import MessageType, NotificationChannel, SystemConfigKey
from app.sdk.channel import CHANNEL_EXTENSION_API_VERSION, ChannelActor, ChannelPlatformCapabilities


class FixturePlatform(ChannelPlatformBase):
    """
    仅用于测试的第三方平台，使用独立命名空间传输标识，与全部内置模块清单共存。
    """

    provider = "boundary-fixture"
    channel = "platform:boundary-fixture"
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

    def conversation(self, address: str) -> str:
        """
        测试平台拥有群组地址格式。
        """
        return "g-" + address.removeprefix("ROOM::") if address.startswith("ROOM::") else address

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
        entrypoint=__name__ + ":BoundaryFixtureModule",
        activation=ActivationPolicy.WHEN_CONFIGURED,
        metadata={
            "name": "Fixture",
            "type": "notification",
            "subtype": FixturePlatform.channel,
            "priority": 0,
            "channel_extension": __name__ + ":FixturePlatform",
            "channel_interaction": True,
            "channel_api_version": CHANNEL_EXTENSION_API_VERSION,
        },
        selector=SelectorSpec("service", {"match_value": FixturePlatform.provider}),
        watch=(),
        depends_on=(),
        source=Path(__file__),
    )
    from app.runtime.extensions.module.adapter import (
        HOST_MODULE_KIND,
        HOST_MODULE_SELECTOR_SCHEMAS,
        build_host_module_registry,
    )

    builtins = build_host_module_registry()
    base_specs = {entry.id: entry for entry in builtins.list_specs()}
    spec = __import__("dataclasses").replace(spec, id="BoundaryFixtureModule", kind=HOST_MODULE_KIND)
    directory = ModuleChannelCatalog(
        CapabilityRegistry(
            base_specs | {spec.id: spec}, kinds={HOST_MODULE_KIND}, selector_schemas=HOST_MODULE_SELECTOR_SCHEMAS
        )
    )
    assert {entry.provider for entry in directory.providers()} >= {"telegram", "wechat", "qqbot", "webpush", "apppush"}
    assert len(directory.providers()) == 13
    from dataclasses import replace

    monkeypatch.setattr(
        configuration,
        "_runtime_configuration",
        replace(configuration._runtime_configuration, channels=lambda: directory),
    )
    configs = [
        NotificationConf(id=key, name=key, type=FixturePlatform.provider, enabled=True) for key in ("first", "second")
    ]
    configs.extend(
        NotificationConf(
            id="builtin-" + entry.provider,
            name="builtin-" + entry.provider,
            type=entry.provider,
            enabled=True,
            interaction_enabled=entry.interaction,
        )
        for entry in directory.providers()
        if entry.provider != FixturePlatform.provider
    )
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
    first = service.request(FixturePlatform.channel, "first", "PERSON::alice", "Alice")
    assert first.sender_id == "p-alice" and first.status == "pending"
    assert service.find(FixturePlatform.channel, "first", "p-alice").id == first.id
    second = service.request(FixturePlatform.channel, "second", "PERSON::alice", "Alice")
    service.update(first.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
    assert service.resolve(FixturePlatform.channel, "first", "PERSON::alice").id == first.user_id
    with pytest.raises(PermissionError):
        service.resolve(FixturePlatform.channel, "second", "PERSON::alice")
    assert second.user_id != first.user_id
    with pytest.raises(ValueError, match="群地址"):
        service.request(FixturePlatform.channel, "first", "ROOM::alice", "Fake Alice")
    with SessionFactory() as session:
        row = session.get(ChannelAuthorization, first.id)
        row.sender_id = "PERSON::alice"
        session.commit()
    assert service.find(FixturePlatform.channel, "first", "p-alice").id == first.id
    # 两条历史授权都声明同一人时，查询和重新审批必须拒绝，而不是合并权限。
    collision = service.request(FixturePlatform.channel, "first", "PERSON::bob", "Bob")
    with SessionFactory() as session:
        row = session.get(ChannelAuthorization, collision.id)
        row.aliases = ["p-alice"]
        session.commit()
    with pytest.raises(ValueError, match="冲突"):
        service.find(FixturePlatform.channel, "first", "PERSON::alice")
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
    assert len(project_runtime_channels(snapshot)) == 14
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




class BoundaryFixtureModule(_MessageBase):
    """
    独立平台的最小真实模块：模拟外部传输，实际执行认证、解析和宿主消息入口。
    """

    def __init__(self, config):
        """
        绑定该 SDK 实例的连接，不从报文读取 provider 或连接身份。
        """
        super().__init__()
        self.connection = config
        self._service_name = FixturePlatform.provider
        self._channel = NotificationChannel(FixturePlatform.channel)
        self.key = b"fixture-sdk-transport-key"

    @staticmethod
    def get_name():
        """
        返回测试模块名称。
        """
        return "Fixture"

    @staticmethod
    def get_type():
        """
        返回与清单一致的通知模块类型。
        """
        from app.schemas.types import ModuleType

        return ModuleType.Notification

    @staticmethod
    def get_subtype():
        """
        独立传输类型不需要修改内置枚举成员列表。
        """
        return NotificationChannel(FixturePlatform.channel)

    @staticmethod
    def get_priority():
        """
        沿用宿主优先级契约。
        """
        return 0

    def init_module(self):
        """
        不启动任何真实平台连接。
        """
        return None

    def init_setting(self):
        """
        测试使用既有连接选择器。
        """
        return None

    def stop(self):
        """
        测试模块没有后台资源。
        """
        return None

    def test(self):
        """
        仅确认内存传输已装配。
        """
        return True, "fixture"

    def get_configs(self):
        """
        返回本模块实际绑定的连接，不包含其他平台。
        """
        return {self.connection.name: self.connection}

    def message_parser(self, source, body, form, args):
        """
        将已认证平台事件转换为统一入站对象，保留会话和线程。
        """
        import json

        from app.sdk.channel import IncomingMessage

        if source != self.connection.name:
            return None
        payload = json.loads(body)
        return IncomingMessage(
            channel=self._channel,
            source=source,
            userid=payload["sender"],
            text=payload["text"],
            chat_id=payload.get("group"),
            thread_id=payload.get("thread"),
            message_id=payload["event"],
            conversation_type="group" if payload.get("group") else "private",
        )

    def receive(self, payload, signature):
        """
        通过模拟 SDK 的真实签名验证后，调用正式的已认证入站端口。
        """
        import hmac
        import json

        from app.application.messaging.ingress import bind_platform_payload

        content = json.dumps(payload, sort_keys=True).encode()
        if not hmac.compare_digest(hmac.digest(self.key, content, "sha256").hex(), signature):
            return False
        bound = bind_platform_payload(self.connection)
        assert bound is not None and CHANNEL_EXTENSION_API_VERSION == 1
        return bound(payload, submit=lambda operation, *args: operation(*args))


def test_independent_platform_authenticated_lifecycle_with_all_builtins(extension, monkeypatch, db):
    """
    全部内置渠道共存，走实际签名、宿主分派、申请、HTTP 审批、规则执行及拒绝链；只替换外部业务和传输。
    """
    import hmac
    import json
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.endpoints import authorization as endpoint
    from app.application.messaging import ingress
    from app.application.messaging.message import MessageTemplateHelper
    from app.chain.message import MessageChain
    from app.db.adapters.authorization import claim_inbound_event
    from app.db.models.authorization import ChannelInboundReceipt
    from app.db.models.outbox import OutboxMessage
    from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
    from app.schemas.channel import AdmissionRule, InteractionPolicy

    directory, configs, service = extension
    assert len(directory.providers()) == 13
    assert len({entry.provider for entry in directory.providers()}) == 13
    assert FixturePlatform.channel not in {member.value for member in NotificationChannel}
    config = configs[0]
    config.identity_namespace = "fixture-namespace"
    config.interaction_policy = InteractionPolicy(
        connection_id=config.id,
        identity_namespace=config.identity_namespace,
        enabled=True,
        admission="manual",
        revision=1,
    )
    module = BoundaryFixtureModule(config)
    chain = MessageChain()
    spy = Mock(return_value=None)
    # 已认证入口只能调用指定宿主 parser；即使插件和其他 parser 优先级更高也不能接管。
    plugin = SimpleNamespace(get_plugin_modules=lambda: {("rogue", "rogue"): {"message_parser": spy}})
    other = SimpleNamespace(get_priority=lambda: -100, message_parser=spy)
    catalog = SimpleNamespace(get_running_modules=lambda _method: [other, module])
    dispatcher = ModuleInvocationDispatcher(
        module_catalog=catalog,
        plugin_catalog=plugin,
        plugin_error_handler=Mock(),
        system_error_handler=Mock(),
        rate_limit_handler=Mock(),
    )
    monkeypatch.setattr(chain, "_module_dispatcher", dispatcher)
    monkeypatch.setattr(auth, "_configured_service", service)
    monkeypatch.setattr("app.chain.message.get_notification_configs", lambda: configs)
    monkeypatch.setattr(ingress, "_platform_payload_receiver", chain.process_platform_payload)
    monkeypatch.setattr(ingress, "_verified_event_claim", partial(claim_inbound_event, SessionFactory))
    db.watermark(OutboxMessage)
    db.watermark(ChannelInboundReceipt)
    outgoing = []

    def send(message):
        """
        外部发送停止在实际模块安全边界，仍执行私聊校验和统一回复渲染。
        """
        outgoing.append(module.prepare_message(MessageTemplateHelper.render(message)))

    monkeypatch.setattr(chain, "post_message", send)
    effects = []
    proof_facts = []

    def search(channel, source, userid, username, text, **kwargs):
        """
        替代外部媒体搜索，实际执行原业务操作授权与业务归属解析。
        """
        principal = service.resolve_operation(channel.value, source, str(userid), "search.torrents")
        effects.append((principal.id, text, kwargs.get("original_chat_id")))
        proof_facts.append(auth._admission_proof.get())
        return True

    monkeypatch.setattr(
        "app.chain.message._MediaInteractionChain", lambda: SimpleNamespace(handle_text_interaction=search)
    )
    # 通过审批后的传统业务同样在公共执行边界复核权限。
    monkeypatch.setattr(
        chain,
        "_handle_channel_member_message",
        lambda context, text, *_args: search(
            context.channel,
            context.source,
            context.user_id,
            context.username,
            text,
            original_chat_id=context.original_chat_id,
        ),
    )
    serial = 0
    frames = []

    def receive(sender, text, group=None, **extra):
        """
        发送带唯一平台事件 ID 的已签名模拟 SDK 帧。
        """
        nonlocal serial
        serial += 1
        payload = {"event": str(serial), "sender": sender, "text": text, "group": group, **extra}
        signature = hmac.digest(module.key, json.dumps(payload, sort_keys=True).encode(), "sha256").hex()
        frames.append((payload, signature))
        return module.receive(payload, signature)

    assert module.receive({"event": "bad", "sender": "PERSON::alice", "text": "申请使用"}, "bad-signature") is False
    assert not service.list_members()
    receive("PERSON::alice", "搜索 before approval", group="ROOM::red")
    assert not effects and not service.list_members()
    receive(
        "PERSON::alice", "申请使用", group="ROOM::red", thread="thread-red", provider="telegram", connection_id="second"
    )
    member = service.find(FixturePlatform.channel, config.name, "p-alice")
    assert member.status == "pending" and member.notification_id == config.id
    assert service.find(FixturePlatform.channel, configs[1].name, "p-alice") is None
    receive("ROOM::red", "申请使用")
    assert len(service.list_members()) == 1
    monkeypatch.setattr(endpoint, "get_notification_configs", lambda **_: configs)
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[endpoint._administrator] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        response = client.put(f"/{member.id}", json={"status": "approved", "role": "member", "scopes": ["search"]})
        assert response.status_code == 200, response.text
    receive("PERSON::alice", "/noai 搜索 after approval", group="ROOM::red", thread="thread-green")
    assert len(effects) == 1 and effects[0][0] == member.user_id
    assert effects[0][2] == "ROOM::red"
    assert not service.is_admin(FixturePlatform.channel, config.name, "p-alice")
    assert proof_facts[-1].provider == FixturePlatform.provider
    assert proof_facts[-1].identity_namespace == config.identity_namespace
    assert proof_facts[-1].ingress_source == "authenticated_sdk"
    chain.process(json.dumps({
        "event": "legacy-approved", "sender": "PERSON::alice", "text": "/noai 搜索 legacy approved",
        "group": "ROOM::red",
    }).encode(), None, {"source": config.name})
    assert len(effects) == 2 and effects[-1][0] == member.user_id
    assert proof_facts[-1] is None
    with pytest.raises(PermissionError):
        service.resolve(FixturePlatform.channel, configs[1].name, "p-alice")
    service.update(member.id, status="disabled", role="member", scopes=("search",), reviewer_id=1)
    receive("PERSON::alice", "搜索 disabled", group="ROOM::red")
    assert len(effects) == 2
    config.interaction_policy = config.interaction_policy.model_copy(
        update={
            "admission": "rules",
            "revision": 2,
            "admission_rules": (AdmissionRule(kind="group", target="ROOM::red", scopes=("search",)),),
        }
    )
    chain.process(json.dumps({
        "event": "legacy-proof-spoof", "sender": "PERSON::bob", "text": "搜索 forged proof", "group": "ROOM::red",
        "_admission_proof": {"connection_id": config.id, "sender": "p-bob", "conversation": "ROOM::red"},
    }).encode(), None, {"source": config.name, "_authenticated_sdk": True})
    assert len(effects) == 2
    receive("PERSON::bob", "搜索 by rule", group="ROOM::red")
    assert len(effects) == 3
    assert module.receive(*frames[-1]) is True
    assert len(effects) == 3  # 重放帧不能再次执行规则授权的业务。
    bob = service.find(FixturePlatform.channel, config.name, "p-bob")
    assert bob.status == "pending" and effects[-1][0] == bob.user_id
    receive("PERSON::bob", "搜索 wrong group", group="ROOM::blue")
    assert len(effects) == 3
    service.update(bob.id, status="rejected", role="member", scopes=(), reviewer_id=1)
    receive("PERSON::bob", "搜索 rejected", group="ROOM::red")
    assert len(effects) == 3
    assert spy.call_count == 0
    assert auth._admission_proof.get() is None
    assert directory.get("telegram").channel == "Telegram"
    assert len(project_runtime_channels(import_legacy_channels(configs))) == 14


class BoundaryTwinPlatform(FixturePlatform):
    """
    共享传输类型但具有独立 provider 和主体格式的平台，用于拒绝全局 channel 唯一假设。
    """

    provider = "boundary-twin"

    def actor(self, value: str) -> ChannelActor:
        """
        相同原始文本在此平台属于不同规范身份。
        """
        name = value.removeprefix("PERSON::").removeprefix("t-")
        return ChannelActor("t-" + name, aliases=("PERSON::" + name,))


def test_shared_transport_uses_connection_provider_without_global_uniqueness(extension, monkeypatch):
    """
    两个平台共用传输标识也能申请、审批和解析归属；无连接的歧义调用拒绝猜测。
    """
    from dataclasses import replace

    from app.application import configuration
    from app.application.messaging.channel import platform_for_channel
    from app.runtime.extensions.module.adapter import (
        HOST_MODULE_KIND,
        HOST_MODULE_SELECTOR_SCHEMAS,
        build_host_module_registry,
    )

    directory, configs, service = extension
    original = directory.get(FixturePlatform.provider)._spec
    other = replace(
        original,
        id="BoundaryTwinModule",
        metadata=dict(original.metadata) | {"channel_extension": __name__ + ":BoundaryTwinPlatform"},
        selector=SelectorSpec(
            original.selector.kind, dict(original.selector.config) | {"match_value": BoundaryTwinPlatform.provider}
        ),
    )
    specs = {spec.id: spec for spec in build_host_module_registry().list_specs()} | {
        original.id: original,
        other.id: other,
    }
    expanded = ModuleChannelCatalog(
        CapabilityRegistry(specs, kinds={HOST_MODULE_KIND}, selector_schemas=HOST_MODULE_SELECTOR_SCHEMAS)
    )
    monkeypatch.setattr(
        configuration,
        "_runtime_configuration",
        replace(configuration._runtime_configuration, channels=lambda: expanded),
    )
    assert len(expanded.providers()) == 14
    configs.append(NotificationConf(id="twin", name="twin", type=BoundaryTwinPlatform.provider, enabled=True))
    first = service.request(FixturePlatform.channel, "first", "PERSON::alice", "Alice")
    twin = service.request(FixturePlatform.channel, "twin", "PERSON::alice", "Alice")
    assert (first.sender_id, twin.sender_id) == ("p-alice", "t-alice")
    service.update(first.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
    assert service.principal(first.id).id == first.user_id
    with pytest.raises(PermissionError):
        service.resolve(FixturePlatform.channel, "twin", "PERSON::alice")
    with pytest.raises(ValueError, match="不唯一"):
        platform_for_channel(FixturePlatform.channel)
    assert (
        platform_for_channel(FixturePlatform.channel, provider=FixturePlatform.provider).actor("PERSON::alice").id
        == "p-alice"
    )
    with SessionFactory() as session:
        session.execute(delete(ChannelAuthorization).where(ChannelAuthorization.notification_id == "twin"))
        session.commit()


@pytest.mark.parametrize("reserved", ["Web", "WebAgent"])
def test_external_provider_cannot_claim_host_web_identity(extension, reserved):
    """
    模块能力声明不能通过复用宿主网页身份绕过统一授权。
    """
    from dataclasses import replace

    from app.runtime.extensions.module.adapter import (
        HOST_MODULE_KIND,
        HOST_MODULE_SELECTOR_SCHEMAS,
        build_host_module_registry,
    )

    directory, _, _ = extension
    entry = directory.get(FixturePlatform.provider)._spec
    entry = replace(entry, metadata=dict(entry.metadata) | {"subtype": reserved})
    specs = {spec.id: spec for spec in build_host_module_registry().list_specs()} | {entry.id: entry}
    with pytest.raises(ValueError, match="宿主网页身份"):
        ModuleChannelCatalog(
            CapabilityRegistry(specs, kinds={HOST_MODULE_KIND}, selector_schemas=HOST_MODULE_SELECTOR_SCHEMAS)
        )


def test_namespaced_transport_roundtrips_without_granting_trust(extension):
    """
    固定枚举保留全部内置值，扩展传输经过公开 JSON 往返仍没有来源证明。
    """
    from app.schemas.message import IncomingMessage

    _, _, service = extension
    info = IncomingMessage.model_validate(
        {
            "channel": FixturePlatform.channel,
            "userid": "p-alice",
            "text": "申请使用",
            "_admission_proof": {"provider": "telegram", "connection_id": "first"},
        }
    )
    assert IncomingMessage.model_validate_json(info.model_dump_json()).channel == info.channel
    assert info._admission_proof is None
    with pytest.raises(PermissionError):
        service.request("platform:unregistered", "first", "p-alice", "Alice")
    with pytest.raises(ValueError):
        NotificationChannel("typo-channel")


def test_unregistered_transport_batch_does_not_mutate_enum_or_platform_state(extension):
    """
    批量解析合法但未注册的外部标识不得增长全局枚举缓存、平台目录或授权记录。
    """
    import json

    from pydantic import TypeAdapter

    directory, _, service = extension
    values_before = dict(NotificationChannel._value2member_map_)
    members_before = dict(NotificationChannel.__members__)
    names_before = tuple(NotificationChannel._member_names_)
    providers_before = tuple(entry.provider for entry in directory.providers())
    adapter = TypeAdapter(NotificationChannel)
    for index in range(4096):
        identifier = f"platform:unregistered-batch-{index}"
        channel = NotificationChannel(identifier)
        decoded = adapter.validate_json(json.dumps(identifier))
        assert channel == decoded
        assert adapter.dump_json(decoded) == json.dumps(identifier).encode()
        assert directory.get(identifier) is None
    assert NotificationChannel._value2member_map_ == values_before
    assert dict(NotificationChannel.__members__) == members_before
    assert tuple(NotificationChannel._member_names_) == names_before
    assert tuple(entry.provider for entry in directory.providers()) == providers_before
    assert not service.list_members()
    with pytest.raises(PermissionError):
        service.request("platform:unregistered-batch-0", "first", "p-alice", "Alice")
    assert not service.list_members()


def test_uncached_transport_preserves_enum_equality_hashing_and_builtin_identity():
    """
    无缓存扩展仍可比较和充当键，复制与序列化不改变值；内置成员继续保持单例。
    """
    import copy
    import pickle

    from pydantic import TypeAdapter

    first = NotificationChannel("platform:uncached-equality")
    second = NotificationChannel("platform:uncached-equality")
    different = NotificationChannel("platform:uncached-other")
    adapter = TypeAdapter(NotificationChannel)
    assert first == second and first != different
    assert hash(first) == hash(second)
    assert {first: "owner"}[second] == "owner"
    assert len({first, second, different}) == 2
    assert first != first.value and first != object()
    assert first not in {NotificationChannel.Web, NotificationChannel.WebAgent}
    assert copy.copy(first) == first
    assert copy.deepcopy(first) == first
    assert pickle.loads(pickle.dumps(first)) == first
    assert adapter.validate_json(adapter.dump_json(first)) == first
    for member in NotificationChannel:
        assert NotificationChannel(member.value) is member
        assert NotificationChannel[member.name] is member
        assert adapter.validate_json(adapter.dump_json(member)) is member
        assert hash(member) == hash(member.name)
        assert member != member.value


@pytest.mark.parametrize("field,value", [("provider", "telegram"), ("identity_namespace", "recreated-namespace")])
def test_authenticated_origin_mismatch_cannot_use_existing_approval(extension, field, value):
    """
    已批准成员也不能借其他 owner 或重建身份空间的事件证明执行操作。
    """
    import time

    from app.domain.admission import AdmissionProof

    _, configs, service = extension
    first = service.request(FixturePlatform.channel, "first", "PERSON::alice", "Alice")
    service.update(first.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
    facts = {"provider": FixturePlatform.provider, "identity_namespace": configs[0].identity_namespace or configs[0].id}
    facts[field] = value
    proof = AdmissionProof(configs[0].id, 1, "p-alice", None, time.time() + 60, **facts)
    with auth.scoped_admission(proof), pytest.raises(PermissionError):
        service.resolve_operation(FixturePlatform.channel, "first", "PERSON::alice", "search.torrents")


@pytest.mark.parametrize("identifier", ["platform:", "platform:Bad", "platform:" + "a" * 56, "platform:with space"])
def test_extension_transport_identifiers_fit_persistent_contract(identifier):
    """
    扩展传输格式与现有 64 字符持久化限制一致，不悄悄扩大数据库合同。
    """
    with pytest.raises(ValueError):
        NotificationChannel(identifier)


def test_independent_subtype_is_valid_only_for_an_explicit_notification_owner(extension):
    """
    注册适配器接受独立扩展标识，拒绝未声明平台实现、错误模块类型和格式错误。
    """
    from dataclasses import replace

    from app.runtime.extensions.module.adapter import _extended_notification_subtype

    directory, _, _ = extension
    spec = directory.get(FixturePlatform.provider)._spec
    assert _extended_notification_subtype(spec)
    assert not _extended_notification_subtype(replace(spec, metadata=dict(spec.metadata) | {"channel_extension": None}))
    assert not _extended_notification_subtype(replace(spec, metadata=dict(spec.metadata) | {"type": "downloader"}))
    assert not _extended_notification_subtype(replace(spec, metadata=dict(spec.metadata) | {"subtype": "typo-channel"}))


def test_saved_management_resolves_owner_from_connection_with_independent_transport(extension, monkeypatch):
    """
    启动装配不再由 channel.lower 猜测 provider，独立传输的已保存连接也能管理。
    """
    from app.startup.initializers import modules

    _, configs, _ = extension
    monkeypatch.setattr(modules.ServiceConfigHelper, "get_notification_configs", lambda: configs)
    manage = Mock(return_value={"success": True})
    monkeypatch.setattr(ChannelPlatformBase, "manage", manage)
    assert modules._manage_inactive_notification(
        FixturePlatform.channel, "status", {"source": "first", "saved_connection": True}
    ) == {"success": True}
    manage.assert_called_once()
    assert (
        modules._manage_inactive_notification("Telegram", "status", {"source": "first", "saved_connection": True})
        is None
    )
    assert (
        modules._manage_inactive_notification(
            FixturePlatform.channel, "status", {"source": "missing", "saved_connection": True}
        )
        is None
    )


def test_public_transport_schema_preserves_builtin_values_and_namespaced_extensions():
    """
    公开接口的 Schema 同时发布内置值和扩展格式，编码往返不能丢失独立平台身份。
    """
    import re

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.schemas.message import IncomingMessage

    app = FastAPI()

    @app.post("/message", response_model=IncomingMessage)
    def echo_message(message: IncomingMessage) -> IncomingMessage:
        """
        保留真实公开消息 DTO 的编解码，不接入授权或外部传输。
        """
        return message

    schema = app.openapi()["components"]["schemas"]["NotificationChannel"]
    builtins, extension = schema["anyOf"]
    assert set(builtins["enum"]) == {member.value for member in NotificationChannel}
    assert "enum" not in schema
    pattern = extension["pattern"]
    maximum = "platform:" + "a" * 55
    assert len(maximum) == 64 and re.fullmatch(pattern, maximum)
    assert not re.fullmatch(pattern, maximum + "a")
    assert not re.fullmatch(pattern, "platform:Bad")
    with TestClient(app) as client:
        for channel in ("Telegram", FixturePlatform.channel, maximum):
            response = client.post("/message", json={"channel": channel, "userid": "p-alice"})
            assert response.status_code == 200, response.text
            assert response.json()["channel"] == channel
        assert client.post("/message", json={"channel": "unknown", "userid": "p-alice"}).status_code == 422
    assert all(member.value != maximum for member in NotificationChannel)
    with pytest.raises(ValueError):
        NotificationChannel(123)


@pytest.mark.parametrize("version", [2, True, "1", None])
def test_host_platform_registration_rejects_unknown_api_versions(extension, version):
    """
    注册表投影拒绝未知版本，版本值不能通过布尔或字符串的宽松转换获得信任。
    """
    from dataclasses import replace

    from app.runtime.extensions.module.channel import ModuleChannelPlatform

    directory, _, _ = extension
    spec = directory.get(FixturePlatform.provider)._spec
    with pytest.raises(ValueError, match="契约版本"):
        ModuleChannelPlatform(replace(spec, metadata=dict(spec.metadata) | {"channel_api_version": version}))


def test_plugin_capability_cannot_masquerade_as_host_platform_owner(extension):
    """
    即使完整复制宿主渠道元数据，可安装插件也不能取得宿主解析 owner 身份。
    """
    from dataclasses import replace

    from app.runtime.extensions.module.channel import ModuleChannelPlatform

    directory, _, _ = extension
    spec = directory.get(FixturePlatform.provider)._spec
    with pytest.raises(ValueError, match="插件不能冒充"):
        ModuleChannelPlatform(replace(spec, kind="plugin"))


def test_legacy_host_manifest_defaults_to_v1_without_granting_extra_capability(extension):
    """
    未标版本的已发布宿主清单按第一版解释，全部内置平台和独立平台共存。
    """
    from dataclasses import replace

    from app.runtime.extensions.module.channel import ModuleChannelPlatform

    directory, _, _ = extension
    spec = directory.get(FixturePlatform.provider)._spec
    metadata = dict(spec.metadata)
    metadata.pop("channel_api_version")
    legacy = ModuleChannelPlatform(replace(spec, metadata=metadata))
    assert legacy.api_version == CHANNEL_EXTENSION_API_VERSION == 1
    assert len(directory.providers()) == 13
    assert all(entry.api_version == 1 for entry in directory.providers())
