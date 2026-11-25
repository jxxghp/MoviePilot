"""
渠道公共类型 SDK、宿主绑定、生命周期封口和旧插件显式入口的安全合同。
"""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.messaging import ingress
from app.application.security import authorization
from app.schemas.channel import InteractionPolicy
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel
from app.sdk import channel as sdk
from scripts.architecture.channel_platforms import (
    CHANNEL_SDK_EXPORTS,
    legacy_ingress_violations,
    sdk_source_violations,
)


@pytest.fixture
def ready_ingress(monkeypatch):
    """
    只替换队列边界；真实授权与规则调用链由独立平台共存回归另行覆盖。
    """
    receiver = Mock()
    monkeypatch.setattr(ingress, "_platform_payload_receiver", receiver)
    monkeypatch.setattr(authorization, "get_channel_authorization", lambda: object())
    monkeypatch.setattr(ingress, "runtime_stop_state", SimpleNamespace(is_system_stopped=False))
    return receiver


def _identity():
    """
    返回宿主已装配的合成连接元数据，不含平台凭据。
    """
    return dict(
        source="channel", channel=NotificationChannel.Telegram, connection_id="connection",
        provider="telegram", identity_namespace="namespace", parser_owner="TelegramModule", policy_revision=4,
    )


def test_public_channel_sdk_is_versioned_type_only_and_cannot_inject_proofs():
    """
    类型门面不暴露注册、可信装配或授权引擎；公开 JSON 不能提交私有证明。
    """
    from app.domain.authorization import ChannelActor
    from app.schemas.message import IncomingMessage

    assert sdk.CHANNEL_EXTENSION_API_VERSION == 1
    assert set(sdk.__all__) == CHANNEL_SDK_EXPORTS
    assert sdk.ChannelActor is ChannelActor and sdk.IncomingMessage is IncomingMessage
    for forbidden in (
        "AdmissionProof", "bind_platform_payload", "submit_platform_payload", "configure_platform_payload_receiver",
        "configure_verified_event_claim", "ChannelAuthorizationService", "ChannelPlatformBase", "CapabilityRegistry",
    ):
        assert not hasattr(sdk, forbidden)
    incoming = sdk.IncomingMessage.model_validate({
        "channel": "platform:public-fixture", "userid": "alice", "text": "申请使用",
        "_admission_proof": {"provider": "telegram", "connection_id": "connection"},
    })
    assert incoming._admission_proof is None
    assert sdk.IncomingMessage.model_validate_json(incoming.model_dump_json()).channel == incoming.channel


@pytest.mark.parametrize("missing", ["connection_id", "provider", "identity_namespace", "parser_owner", "policy_revision"])
def test_incomplete_bound_identity_is_rejected_without_submission(ready_ingress, monkeypatch, missing):
    """
    所有必要身份字段都必须来自装配，不从报文补齐，也不走备用路径。
    """
    values = _identity()
    values[missing] = None
    submit = Mock()
    log = Mock()
    monkeypatch.setattr(ingress.logger, "warning", log)
    assert ingress.submit_platform_payload({"provider": "telegram", "connection_id": "connection"}, submit=submit, **values) is False
    submit.assert_not_called()
    ready_ingress.assert_not_called()
    assert "reason=identity_missing," in log.call_args.args[0]


@pytest.mark.parametrize("unavailable", ["receiver", "authorization", "stopping"])
def test_unready_or_stopping_ingress_fails_closed(ready_ingress, monkeypatch, unavailable):
    """
    解析器装配、通用授权及进程生命周期均须就绪，才能接收 SDK 事件。
    """
    if unavailable == "receiver":
        monkeypatch.setattr(ingress, "_platform_payload_receiver", None)
    elif unavailable == "authorization":
        monkeypatch.setattr(authorization, "get_channel_authorization", lambda: None)
    else:
        monkeypatch.setattr(ingress, "runtime_stop_state", SimpleNamespace(is_system_stopped=True))
    submit = Mock()
    assert ingress.submit_platform_payload({}, submit=submit, **_identity()) is False
    submit.assert_not_called()
    ready_ingress.assert_not_called()


@pytest.mark.parametrize("reserved", [NotificationChannel.Web, NotificationChannel.WebAgent])
def test_host_web_identity_cannot_be_used_by_sdk_ingress(ready_ingress, reserved):
    """
    即使提供其它元数据，宿主网页身份也不能用于外部 SDK 的事件入口。
    """
    values = _identity() | {"channel": reserved}
    submit = Mock()
    assert ingress.submit_platform_payload({}, submit=submit, **values) is False
    submit.assert_not_called()


@pytest.mark.parametrize("change", ["stopping", "reset", "replacement"])
def test_queued_event_cannot_outlive_receiver_lifecycle(ready_ingress, monkeypatch, change):
    """
    接受后的排队任务在执行前仍须检查封口及接收器代际，不能切换传输补发。
    """
    submit = Mock(return_value=True)
    assert ingress.submit_platform_payload({"event": "one"}, submit=submit, **_identity()) is True
    call = submit.call_args.args
    if change == "stopping":
        monkeypatch.setattr(ingress, "runtime_stop_state", SimpleNamespace(is_system_stopped=True))
    else:
        monkeypatch.setattr(ingress, "_platform_payload_receiver", None if change == "reset" else Mock())
    call[0](*call[1:])
    ready_ingress.assert_not_called()
    assert submit.call_count == 1


def test_binding_freezes_connection_owner_namespace_revision_and_ignores_payload_spoofing(ready_ingress):
    """
    宿主冻结的连接事实不受配置对象后续修改或报文伪造影响，实际处理仍复核当前配置。
    """
    config = NotificationConf(
        id="connection", name="channel", type="telegram", identity_namespace="namespace", enabled=True,
        interaction_policy=InteractionPolicy(
            connection_id="connection", identity_namespace="namespace", revision=4, enabled=True,
        ),
    )
    bound = ingress.bind_platform_payload(config)
    assert bound is not None
    config.id, config.name, config.type, config.identity_namespace = "other", "renamed", "discord", "other"
    submit = Mock(return_value=True)
    payload = {"provider": "discord", "namespace": "other", "connection_id": "other"}
    assert bound(payload, submit=submit)
    call = submit.call_args.args
    assert call[1] is ready_ingress and json.loads(call[2]) == payload
    assert call[3:] == ("channel", NotificationChannel.Telegram, "connection", "telegram", "namespace", "TelegramModule", 4)


@pytest.mark.parametrize("outcome", [False, RuntimeError("closed"), ValueError("failed")])
def test_submit_rejection_or_exception_never_invokes_receiver(ready_ingress, outcome):
    """
    执行器拒绝和异常都只返回失败，不能同步重试或切换到其它传输。
    """
    submit = Mock(return_value=outcome if not isinstance(outcome, Exception) else None)
    if isinstance(outcome, Exception):
        submit.side_effect = outcome
    assert ingress.submit_platform_payload({}, submit=submit, **_identity()) is False
    submit.assert_called_once()
    ready_ingress.assert_not_called()


def test_bound_entry_exception_is_diagnosed_without_retry_or_sensitive_details(ready_ingress, monkeypatch):
    """
    绑定入口异常只提交一次并记录错误类型，不回环或把可能含凭据的异常正文写入日志。
    """
    bound = Mock(side_effect=RuntimeError("fixture-private-token"))
    submit = Mock()
    legacy = Mock()
    log = Mock()
    monkeypatch.setattr(ingress, "forward_message_to_host", legacy)
    monkeypatch.setattr(ingress.logger, "warning", log)
    assert ingress.submit_bound_payload({"text": "申请使用"}, bound, "fixture", submit=submit) is False
    bound.assert_called_once_with({"text": "申请使用"}, submit=submit)
    submit.assert_not_called()
    legacy.assert_not_called()
    ready_ingress.assert_not_called()
    assert "reason=submit_failed:RuntimeError," in log.call_args.args[0]
    assert "fixture-private-token" not in str(log.call_args)


def test_offline_claim_is_connection_scoped_bounded_and_expires(monkeypatch):
    """
    未装配共享认领服务的隔离入口按连接防重，缓存满时拒绝新事件，过期后才释放容量。
    """
    from cachetools import TTLCache

    from app.domain.admission import AdmissionProof

    clock = [100.0]
    cache = TTLCache(maxsize=2, ttl=120, timer=lambda: clock[0])
    monkeypatch.setattr(ingress, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(ingress, "_verified_events", cache)
    monkeypatch.setattr(ingress, "_verified_event_claim", None)
    first = AdmissionProof("first", 1, "alice", None, 1000)
    second = AdmissionProof("second", 1, "alice", None, 1000)
    assert ingress.claim_verified_event(first, "event-one") is True
    assert ingress.claim_verified_event(first, "event-one") is False
    assert ingress.claim_verified_event(second, "event-one") is True
    assert ingress.claim_verified_event(first, "event-two") is False
    assert len(cache) == cache.maxsize
    clock[0] = 221
    assert ingress.claim_verified_event(first, "event-two") is True
    assert len(cache) == 1


@pytest.mark.parametrize(
    "module,symbol",
    [
        ("telegram.telegram", "forward_message_to_host"), ("slack.slack", "forward_message_to_host"),
        ("discord.discord", "async_forward_message_to_host"), ("feishu.feishu", "submit_message_to_host"),
        ("qqbot.qqbot", "submit_message_to_host"), ("wechat.wechatbot", "submit_message_to_host"),
        ("wechatclawbot.wechatclawbot", "forward_message_to_host"),
    ],
)
def test_old_module_helper_imports_resolve_to_explicit_compatibility(module, symbol):
    """
    旧插件导入仍精确解析至单一兼容实现，宿主模块的物理公开表不恢复旧 helper。
    """
    owner = importlib.import_module("app.modules." + module)
    assert symbol not in owner.__dict__
    assert getattr(owner, symbol) is getattr(ingress, symbol)
    assert symbol not in owner.__all__


@pytest.mark.parametrize("source", [
    "from app.application.messaging.ingress import submit_message_to_host as old\nold({}, 'source', submit=pool)",
    "import app.application.messaging.ingress as entry\nalias = entry\nalias.forward_message_to_host({})",
    "from app.application.messaging import ingress as entry\nalias = entry\nalias.async_forward_message_to_host({})",
    "from app.modules.telegram.telegram import forward_message_to_host as relay\nrelay({})",
    "import app.modules.telegram.telegram as client\nalias = client\nalias.forward_message_to_host({})",
    "from app.modules.telegram import telegram as client\nrelay = client.forward_message_to_host\nrelay({})",
    "import app.application.messaging.ingress\napp.application.messaging.ingress.forward_message_to_host({})",
    "import app.application.messaging.ingress as entry\nrelay: object = entry\nrelay.submit_message_to_host({})",
    "from app.sdk._legacy.ingress import forward_message_to_host as relay\nrelay({})",
])
def test_architecture_guard_rejects_host_legacy_ingress_aliases(source):
    """
    旧 helper 的模块和局部别名不能重新成为宿主的入站旁路。
    """
    assert legacy_ingress_violations(source)


@pytest.mark.parametrize("source", [
    "from app.domain.admission import AdmissionProof as Proof",
    "from app.application.messaging.ingress import configure_platform_payload_receiver as register",
    "import app.application.messaging.ingress as entry",
    "from app.application.messaging.ingress import *",
])
def test_architecture_guard_rejects_trusted_sdk_exports(source):
    """
    公共 SDK 不能通过直接、整体或星号导入扩大可信装配面。
    """
    assert sdk_source_violations(source)


def test_architecture_guard_allows_canonical_calls_and_unrelated_names():
    """
    约束按真实来源识别兼容与信任符号，普通业务同名函数不被当作宿主入口。
    """
    assert not legacy_ingress_violations("client.submit_bound_payload(payload)")
    assert not sdk_source_violations("from example import AdmissionProof")


@pytest.mark.parametrize("field,value", [
    ("provider", "discord"), ("identity_namespace", "rebuilt"), ("parser_owner", "OtherModule"),
    ("policy_revision", 3), ("connection_id", "other-connection"),
])
def test_processing_rechecks_the_bound_owner_before_parsing(monkeypatch, field, value):
    """
    排队后处理仍复核当前连接、owner 和 revision，错误身份不能先调用解析器。
    """
    from app.chain.message import MessageChain

    config = NotificationConf(
        id="connection", name="channel", type="telegram", identity_namespace="namespace", enabled=True,
        interaction_policy=InteractionPolicy(
            connection_id="connection", identity_namespace="namespace", revision=4, enabled=True,
        ),
    )
    monkeypatch.setattr(authorization, "get_notification_configs", lambda: [config])
    monkeypatch.setattr("app.chain.message.get_channel_authorization", lambda: object())
    chain = MessageChain()
    parser = Mock()
    monkeypatch.setattr(chain, "run_module", parser)
    identity = _identity() | {field: value}
    chain.process_platform_payload(body=b"{}", **identity)
    parser.assert_not_called()


def test_legacy_plugin_parser_cannot_inject_proof_or_reserved_web_identity(monkeypatch):
    """
    旧公开解析 Hook 保留基础消息适配，私有证明会被丢弃，网页身份只能走登录入口。
    """
    import time

    from app.chain.message import MessageChain
    from app.domain.admission import AdmissionProof

    monkeypatch.setattr("app.chain.message.get_channel_authorization", lambda: object())
    monkeypatch.setattr("app.chain.message.get_notification_configs", lambda: [])
    chain = MessageChain()
    incoming = sdk.IncomingMessage(channel=NotificationChannel.Telegram, source="legacy", userid="alice", text="申请使用")
    incoming._admission_proof = AdmissionProof("connection", 4, "alice", None, time.time() + 60)
    monkeypatch.setattr(chain, "run_module", Mock(return_value=incoming))
    process = Mock()
    monkeypatch.setattr(chain, "process_incoming", process)
    chain.process(b"{}", None, {"source": "legacy", "_authenticated_sdk": True})
    assert incoming._admission_proof is None
    process.assert_called_once()
    assert "_authenticated_sdk" not in chain.run_module.call_args.kwargs["args"]
    process.reset_mock()
    incoming.channel = NotificationChannel.WebAgent
    chain.process(b"{}", None, {"source": "legacy"})
    process.assert_not_called()


def test_sdk_v1_keeps_the_existing_platform_method_contract():
    """
    第一版固定既有协议方法的参数形状；实现仍是规范端口，而非另一份接口模型。
    """
    import inspect

    expected = {
        "actor": ("self", "value"), "capabilities": ("self", "connection"), "mode": ("self", "config"),
        "editable_fields": ("self", "connection"), "form_values": ("self", "connection"),
        "patch_config": ("self", "connection", "patch"), "credential_key": ("self", "connection"),
        "inbound_token_key": ("self",), "validate_target": ("self", "connection", "rule"),
        "legacy_rules": ("self", "connection", "route"), "target_fields": ("self",),
        "normalize_interaction": ("self", "value"), "runtime_config": ("self", "connection", "policy"),
        "validate_interaction": ("self", "options"), "private_target": ("self", "value"),
        "bound_address": ("self", "message"), "address_message": ("self", "message", "rule"),
        "conversation": ("self", "address"), "attachment_reference": ("self", "kind", "reference"),
        "split_notification": ("self", "parts"), "probe_identity": ("self", "connection"),
        "reconcile": ("self", "previous", "current"), "export_interaction": ("self", "policy"),
        "import_options": ("self", "connection", "policy"), "manage": ("self", "action", "params"),
    }
    methods = {name: tuple(inspect.signature(value).parameters)
               for name, value in vars(sdk.ChannelPlatform).items() if not name.startswith("_") and callable(value)}
    assert methods == expected


def test_notification_only_connection_has_no_sdk_binding():
    """
    仅投递连接不装配可信入站能力，公共类型和构造参数不能自动启用交互。
    """
    config = NotificationConf(id="only-notify", name="notify", type="telegram", enabled=True, interaction_enabled=False)
    assert ingress.bind_platform_payload(config) is None


def test_wechat_sdk_authentication_and_stop_gate_do_not_use_http(monkeypatch):
    """
    智能机器人只接受本连接认证后发给本机器人的事件，停机后不提交或切换路径。
    """
    from app.modules.wechat.wechatbot import WeChatBot

    monkeypatch.setattr(WeChatBot, "_start_gateway", lambda self: None)
    monkeypatch.setattr(WeChatBot, "_load_known_targets", lambda self: None)
    bound = Mock(return_value=True)
    bot = WeChatBot(WECHAT_BOT_ID="bot", WECHAT_BOT_SECRET="fixture", message_ingress=bound)
    payload = {"body": {"aibotid": "bot", "chattype": "single", "from": {"userid": "alice"},
                        "msgtype": "text", "text": {"content": "申请使用"}}}
    assert bot._forward_to_message_chain(payload) is False
    bot._authenticated.set()
    assert bot._forward_to_message_chain({"body": payload["body"] | {"aibotid": "other"}}) is False
    assert bot._forward_to_message_chain({"body": payload["body"] | {"compat_info": {}}}) is False
    assert bot._forward_to_message_chain(payload) is True
    bot._stop_event.set()
    assert bot._forward_to_message_chain(payload) is False
    bound.assert_called_once()


@pytest.mark.parametrize("entry", ["http", "sdk"])
def test_stopped_message_chain_does_not_parse_or_process(entry, monkeypatch):
    """
    即使旧排队调用绕过提交边界，HTTP 和 SDK 消费者也在停机时封口。
    """
    from app.chain.message import MessageChain

    chain = MessageChain()
    monkeypatch.setattr(chain, "stop_state", SimpleNamespace(is_system_stopped=True))
    monkeypatch.setattr("app.chain.message.get_channel_authorization", lambda: object())
    parser = Mock()
    monkeypatch.setattr(chain, "run_module", parser)
    if entry == "http":
        chain.process(b"{}", None, {"source": "channel"})
    else:
        chain.process_platform_payload(body=b"{}", **_identity())
    parser.assert_not_called()


@pytest.mark.parametrize("status", ["already_cancelled", "cancel_later", "failed_later"])
def test_async_submission_failures_are_diagnosed_without_retry(ready_ingress, monkeypatch, status):
    """
    受管 Future 的取消和异步失败也须可诊断，不能触发 HTTP 或第二次提交。
    """
    from concurrent.futures import Future

    future = Future()
    log = Mock()
    monkeypatch.setattr(ingress.logger, "warning", log)
    if status == "already_cancelled":
        future.cancel()
    submit = Mock(return_value=future)
    accepted = ingress.submit_platform_payload({}, submit=submit, **_identity())
    assert accepted is (status != "already_cancelled")
    if status == "cancel_later":
        future.cancel()
    elif status == "failed_later":
        future.set_exception(RuntimeError("worker failed"))
    assert any(f"reason={reason}," in log.call_args.args[0] for reason in (
        "submit_rejected", "submission_cancelled", "processing_failed:RuntimeError",
    ))
    submit.assert_called_once()
    ready_ingress.assert_not_called()


@pytest.mark.parametrize("module", ["telegram.telegram", "qqbot.qqbot", "wechatclawbot.wechatclawbot"])
def test_old_incidental_channel_type_import_has_an_explicit_compatibility_mapping(module):
    """
    内部不再使用的枚举导入名仍可供旧插件精确解析，且不恢复物理别名。
    """
    owner = importlib.import_module("app.modules." + module)
    assert "NotificationChannel" not in owner.__dict__
    assert owner.NotificationChannel is sdk.NotificationChannel


@pytest.mark.parametrize("source", [
    "from app.application.messaging import ingress as entry",
    "from app.modules.wechat import _AdmissionProof as Proof",
    "from app.startup.initializers.modules import configure_runtime_data_providers",
])
def test_public_sdk_cannot_reexport_internal_trust_modules(source):
    """
    禁止经模块对象、私有导入别名或启动装配公开可信能力。
    """
    assert sdk_source_violations(source)


def test_explicit_legacy_sdk_http_facade_does_not_use_the_trusted_receiver(ready_ingress, monkeypatch):
    """
    旧 SDK 门面保留显式 HTTP 调用与认证头，不借当前可信接收器传递证明。
    """
    legacy = importlib.import_module("app.sdk._legacy.ingress")
    port = SimpleNamespace(post=Mock(return_value=200))
    monkeypatch.setattr(ingress, "_message_ingress_port", port)
    monkeypatch.setattr(ingress, "get_runtime_setting", lambda key: {"PORT": 3000, "API_TOKEN": "fixture-token"}[key])
    assert set(legacy.__all__) == {"forward_message_to_host", "async_forward_message_to_host", "submit_message_to_host"}
    assert not hasattr(legacy, "bind_platform_payload")
    assert legacy.forward_message_to_host({"text": "申请使用"}, "legacy") is True
    port.post.assert_called_once()
    assert port.post.call_args.kwargs["headers"] == {"X-API-KEY": "fixture-token"}
    ready_ingress.assert_not_called()


def test_disabled_http_connection_does_not_fall_back_to_plugin_parser(monkeypatch):
    """
    已知连接关闭交互后，兼容 HTTP 入口不能经其它解析器重新处理。
    """
    from app.chain.message import MessageChain

    chain = MessageChain()
    monkeypatch.setattr("app.chain.message.get_channel_authorization", lambda: object())
    monkeypatch.setattr("app.chain.message.get_notification_configs", lambda: [NotificationConf(
        name="disabled", type="telegram", enabled=True, interaction_enabled=False,
    )])
    parser = Mock()
    monkeypatch.setattr(chain, "run_module", parser)
    chain.process(b"{}", None, {"source": "disabled"})
    parser.assert_not_called()


@pytest.mark.parametrize("accepted", [True, False])
def test_wechat_private_rule_text_is_submitted_once_even_when_rejected(monkeypatch, accepted):
    """
    私聊规则文本提交失败也已消费本次入口选择，不能落入普通私聊分支重复投递。
    """
    from app.modules.wechat.wechatbot import WeChatBot
    from app.schemas.channel import AdmissionRule

    monkeypatch.setattr(WeChatBot, "_start_gateway", lambda self: None)
    monkeypatch.setattr(WeChatBot, "_load_known_targets", lambda self: None)
    policy = InteractionPolicy(
        connection_id="fixture", identity_namespace="fixture", enabled=True, revision=1,
        admission_rules=(AdmissionRule(kind="user", target="alice", scopes=("search",)),),
    )
    bound = Mock(return_value=accepted)
    bot = WeChatBot(
        WECHAT_BOT_ID="bot", WECHAT_BOT_SECRET="fixture", interaction_policy=policy, message_ingress=bound,
    )
    bot._authenticated.set()
    monkeypatch.setattr(bot, "_remember_target", Mock())
    bot._handle_callback_message({"body": {
        "aibotid": "bot", "chattype": "single", "msgid": "event-one", "from": {"userid": "alice"},
        "msgtype": "text", "text": {"content": "申请使用"},
    }})
    bound.assert_called_once()
