"""
验证旧目标移交、个人范围隔离和可靠投递的规则快照。
"""

import importlib
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.application.messaging.channel.catalog import project_runtime_channels
from app.application.messaging.channel.rules import (
    routed_notification_messages,
    validate_notification_rule,
)
from app.application.messaging.channel.settings import read_channel_resources
from app.application.messaging.delivery import (
    notification_plan_incomplete,
    notification_rules_for_dispatch,
    observe_notification_progress,
    read_or_freeze_notification_rules,
)
from app.modules.telegram.module import TelegramModule
from app.schemas.channel import NotificationRule
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import MessageType


def _rule(kind, target=None, **kwargs):
    """
    构造一条订阅通知规则。
    """
    return NotificationRule(kind=kind, target=target, message_types=(MessageType.Subscribe.value,), **kwargs)


@pytest.mark.parametrize(
    ("provider", "config", "expected"),
    [
        ("telegram", {"TELEGRAM_CHAT_ID": "-123", "TELEGRAM_TOPIC_ID": "09"}, ("conversation", "-123", "9")),
        ("qqbot", {"QQ_OPENID": "alice", "QQ_GROUP_OPENID": "groupid"}, ("conversation", "groupid", None)),
        ("feishu", {"FEISHU_OPEN_ID": "ou_alice", "FEISHU_CHAT_ID": "oc_group"}, ("user", "ou_alice", None)),
        ("wechat", {"WECHAT_MODE": "bot", "WECHAT_BOT_CHAT_ID": "GROUP:room"}, ("conversation", "room", None)),
        ("wechatclawbot", {"WECHATCLAWBOT_DEFAULT_TARGET": "wx_alice"}, ("user", "wx_alice", None)),
        ("discord", {"DISCORD_CHANNEL_ID": "123"}, ("conversation", "123", None)),
        ("slack", {"SLACK_CHANNEL": "#releases"}, ("conversation", "#releases", None)),
        ("vocechat", {"VOCECHAT_CHANNEL_ID": 123}, ("conversation", "123", None)),
    ],
)
def test_migrates_default_and_preserves_target_priority(provider, config, expected):
    """
    优先目标启用，低优先目标留在停用规则中。
    """
    snapshot = read_channel_resources(None, [{
        "id": "bot", "type": provider, "name": "bot", "enabled": True,
        "config": config, "switchs": ["订阅"],
    }])
    assert read_channel_resources(snapshot.model_dump(mode="json"), None) == snapshot
    rules = snapshot.notifications["bot"].rules
    assert rules and [rule for rule in rules if rule.enabled and rule.fallback_only] == [rules[1]]
    assert (rules[1].kind, rules[1].target, rules[1].thread_id) == expected
    assert not (set(config) - {"WECHAT_MODE"}) & set(snapshot.connections["bot"].provider_config)


def test_migration_preserves_dormant_default_and_webpush_filter():
    """
    未勾选事件的配置保留为停用。空格用户名条件不会扩大匹配范围。
    """
    snapshot = read_channel_resources(None, [
        {"id": "tg", "name": "tg", "type": "telegram", "config": {"TELEGRAM_CHAT_ID": "-123"}},
        {"id": "push", "name": "push", "type": "webpush", "switchs": ["订阅"],
         "config": {"WEBPUSH_USERNAME": " alice,bob"}},
    ])
    assert [(rule.enabled, rule.target) for rule in snapshot.notifications["tg"].rules] == [(False, None), (False, "-123")]
    assert snapshot.notifications["push"].rules[0].legacy_usernames == ("bob",)


def test_new_rules_keep_personal_scope_and_deduplicate_same_topic(monkeypatch):
    """
    通过真实模块发送入口按话题分流，个人范围只使用绑定地址。
    """
    rules = (_rule("conversation", "-123", thread_id="7"), _rule("conversation", "-123", thread_id="7"),
             _rule("conversation", "-123", thread_id="8"), _rule("bound"))
    config = NotificationConf(id="tg", name="tg", type="telegram", enabled=True,
                              notification_enabled=True, notification_rules=rules)
    module = TelegramModule()
    client = Mock()
    monkeypatch.setattr(module, "get_config", lambda _name: config)
    monkeypatch.setattr(module, "get_configs", lambda: {"tg": config})
    monkeypatch.setattr(module, "get_instance", lambda _name: client)

    message = Message(mtype=MessageType.Subscribe, title="已订阅")
    message.set_notification_route_scope("admin")
    module.post_message(message)
    assert [(call.kwargs["original_chat_id"], call.kwargs["thread_id"]) for call in client.send_msg.call_args_list] == [
        ("-123", "7"), ("-123", "8"),
    ]

    client.reset_mock()
    message.set_notification_route_scope("user")
    module.post_message(message)
    client.send_msg.assert_not_called()
    message.targets = {"telegram_userid": "42"}
    module.post_message(message)
    assert [call.kwargs["userid"] for call in client.send_msg.call_args_list] == ["42"]


def test_account_scope_and_event_owner_are_independent():
    """
    指定接收账号只筛选已授权受众，消息关联账号是单独条件。
    """
    conf = NotificationConf(type="webpush")
    rule = _rule("account", "recipient", owner_identity="creator")
    event = Message(mtype=MessageType.Subscribe, subscription_owner_identity="creator")
    event.set_notification_route_scope("all")
    event.targets = {"webpush": ("recipient", "other")}
    routed = list(routed_notification_messages(event, conf, (rule, rule), has_bound_target=True))
    assert [item.targets for item in routed] == [{"webpush": "recipient"}]
    event.set_notification_route_scope("user")
    event.targets = {"webpush": "other"}
    assert not list(routed_notification_messages(event, conf, (rule,), has_bound_target=True))


def test_notification_rules_retry_uses_frozen_target():
    """
    规则变更不会扩大在途意图的接收范围。
    """
    original = (_rule("conversation", "-123", thread_id="2"),)
    updated = (_rule("conversation", "-456"),)
    _, snapshot = read_or_freeze_notification_rules({}, {"tg": original})
    assert notification_plan_incomplete(snapshot)
    frozen, _ = read_or_freeze_notification_rules(snapshot, {"tg": updated})
    config = NotificationConf(id="tg", notification_rules=updated)
    with observe_notification_progress(lambda _items: None, rules=frozen):
        assert notification_rules_for_dispatch(config) == original
    assert notification_rules_for_dispatch(config) == updated


def test_migration_moves_discord_command_scope_in_the_config_transaction():
    """
    数据库升级可重复读取，启动投影只把服务器用于命令注册。
    """
    migration = importlib.import_module("database.versions.62c91ad8b057_channel_resources")
    stored = read_channel_resources(None, [{
        "id": "discord", "name": "discord", "type": "discord", "enabled": True,
        "config": {"DISCORD_BOT_TOKEN": "fixture", "DISCORD_GUILD_ID": 23}, "switchs": ["订阅"],
    }]).model_dump(mode="json")
    stored["notifications"]["discord"].pop("rules")
    stored["connections"]["discord"]["provider_config"]["DISCORD_GUILD_ID"] = 23
    stored["interactions"]["discord"].pop("platform_options")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as bind:
        meta = sa.MetaData()
        config = sa.Table("systemconfig", meta, sa.Column("key", sa.String), sa.Column("value", sa.JSON))
        meta.create_all(bind)
        bind.execute(config.insert().values(key="ChannelSettings", value=stored))
        with Operations.context(MigrationContext.configure(bind)):
            migration._migrate_channel_configuration()
            migration._migrate_channel_configuration()
        saved = bind.execute(sa.select(config.c.value)).scalar_one()
    snapshot = read_channel_resources(saved, None)
    assert snapshot.interactions["discord"].platform_options["command_guild_id"] == "23"
    assert snapshot.notifications["discord"].rules[-1].kind == "review"
    assert project_runtime_channels(snapshot)[0].config == {
        "DISCORD_BOT_TOKEN": "fixture", "DISCORD_COMMAND_GUILD_ID": "23",
    }


def test_platform_rule_rejects_ambiguous_and_cross_audience_targets():
    """
    服务器模式和目标类型在服务端独立校验。
    """
    snapshot = read_channel_resources(None, [{"id": "bot", "name": "bot", "type": "telegram"}])
    with pytest.raises(ValueError, match="个人通知"):
        _rule("conversation", "-123", audience="user")
    with pytest.raises(ValueError, match="有效的平台"):
        validate_notification_rule(snapshot.connections["bot"], _rule("user", "-123"))


@pytest.mark.parametrize("start,end,hour,minute,expected", [
    ("08:00", "22:00", 8, 0, True), ("08:00", "22:00", 22, 0, True),
    ("08:00", "22:00", 7, 59, False), ("08:00", "22:00", 22, 1, False),
    ("22:00", "06:00", 23, 0, True), ("22:00", "06:00", 5, 0, True),
    ("22:00", "06:00", 12, 0, False), ("08:00", "08:00", 1, 0, True),
])
def test_rule_sending_windows_include_boundaries_and_support_overnight(start, end, hour, minute, expected):
    """
    时间窗口使用系统本地时间，跨午夜时使用当天两侧的分钟。
    """
    from datetime import datetime

    from app.application.messaging.delivery import notification_rule_time_allows
    from app.schemas.channel import NotificationSendTime

    rule = _rule("user", "123", send_time=NotificationSendTime(start=start, end=end))
    assert notification_rule_time_allows(rule, datetime(2026, 11, 21, hour, minute)) is expected


def test_outside_window_skips_without_submitting_or_retrying(monkeypatch):
    """
    确认未提交的重试分段跨出窗口后终止为跳过，不补发。
    """
    from datetime import datetime

    from app.application.messaging import delivery
    from app.schemas.channel import NotificationSendTime
    from app.schemas.types import NotificationChannel

    class Clock:
        """
        固定规则检查使用的系统本地时间。
        """

        @staticmethod
        def now():
            """
            返回窗口外的本地时间。
            """
            return datetime(2026, 11, 21, 23, 0)

    monkeypatch.setattr(delivery, "datetime", Clock)
    rule = _rule("user", "123", send_time=NotificationSendTime(start="08:00", end="22:00"))
    parts = delivery.NotificationParts(NotificationChannel.Telegram, "connection", "connection")
    snapshot = {}
    submit = Mock(return_value=("confirmed", None))

    def reserve(part, should_submit):
        """
        使用真实分段状态机结算时间窗口外的未发送分段。
        """
        nonlocal snapshot
        result, snapshot = delivery.claim_notification_part(
            snapshot or delivery.merge_notification_progress({}, (part.model_copy(update={"status": "retryable_failure"}),)),
            part, initial_attempt=False, should_submit=should_submit,
        )
        return result

    with delivery.observe_notification_progress(lambda _results: None, reserve=reserve):
        with delivery.notification_sending_windows((rule,)):
            result = parts.send_result("123", 0, "正文", True, submit)
    assert result.status == "skipped" and result.code == "outside_rule_time"
    assert not delivery.pending_notification_parts(snapshot)
    submit.assert_not_called()






@pytest.mark.parametrize("value", ["24:00", "9:00", "12:60", "01:00:00", "junk"])
def test_invalid_send_times_are_rejected(value):
    """
    非法窗口不能通过 API 或导入配置绕过校验。
    """
    from app.schemas.channel import NotificationSendTime

    with pytest.raises(ValueError):
        NotificationSendTime(start=value, end="22:00")


@pytest.mark.parametrize("provider,targets,kind,address", [
    ("telegram", {"telegram_userid": "123"}, "user", "123"),
    ("qqbot", {"qq_group_openid": "room"}, "conversation", "room"),
    ("feishu", {"feishu_chat_id": "oc_room"}, "conversation", "oc_room"),
])
def test_frozen_retry_combines_windows_for_bound_and_same_explicit_recipient(monkeypatch, provider, targets, kind, address):
    """
    相同收件人的规则取并集，其他目标的开放窗口不能放行此收件人。
    """
    from datetime import datetime

    from app.application.messaging import delivery
    from app.schemas.channel import NotificationSendTime

    class Clock:
        """
        窗口外重试的固定时钟。
        """

        @staticmethod
        def now():
            """
            返回晚间时间。
            """
            return datetime(2026, 11, 21, 23, 0)

    monkeypatch.setattr(delivery, "datetime", Clock)
    rules = (
        _rule("bound", send_time=NotificationSendTime(start="08:00", end="22:00")),
        _rule(kind, address),
        _rule("user", "other"),
    )
    event = Message(mtype=MessageType.Subscribe, targets=targets)
    event.set_notification_route_scope("admin")
    observations = []
    with observe_notification_progress(lambda _items: None, rules={"bot": rules}):
        for routed in routed_notification_messages(event, NotificationConf(id="bot", type=provider), rules, has_bound_target=True):
            observations.append((routed.userid, delivery._sending_time_allows()))
    assert len(observations) == 2
    assert all(allowed for _, allowed in observations)
    with observe_notification_progress(lambda _items: None, rules={"bot": rules}):
        observations = []
        for routed in routed_notification_messages(event, NotificationConf(id="bot", type=provider), (rules[0], rules[2]), has_bound_target=True):
            observations.append(delivery._sending_time_allows())
    assert observations == [False, True]


@pytest.mark.parametrize("audiences", [(), ("admin",), ("user",), ("all",), ("admin", "user"), ("admin", "all"), ("user", "all"), ("admin", "user", "all")])
@pytest.mark.parametrize("scope", ["admin", "user", "all"])
def test_multi_audience_rules_match_only_selected_global_route_classes(audiences, scope):
    """
    类别内多选取并集，空集合沿用全局，但不自行生成额外通知路由。
    """
    from app.application.messaging.channel.rules import matches_notification_rule

    rule = _rule("bound", audiences=audiences)
    event = Message(mtype=MessageType.Subscribe)
    event.set_notification_route_scope(scope)
    assert matches_notification_rule(event, rule) is (not audiences or scope in audiences)
    routed = list(routed_notification_messages(event, NotificationConf(type="telegram"), (rule,), has_bound_target=True))
    assert len(routed) == int(not audiences or scope in audiences)


@pytest.mark.parametrize("legacy,expected", [("any", ()), ("admin", ("admin",)), ("user", ("user",)), ("all", ("all",))])
def test_legacy_single_audience_rules_keep_the_same_scope(legacy, expected):
    """
    旧规则和持久化投递快照可以直接读取，不扩大原通知范围。
    """
    rule = _rule("bound", audience=legacy)
    restored = NotificationRule.model_validate_json(rule.model_dump_json())
    assert restored.matched_audiences == expected
    assert restored.audiences is None


@pytest.mark.parametrize("audiences", [[], ["admin"], ["user"], ["all", "admin"], ["all", "user"]])
def test_multi_audience_cannot_expand_platform_all_or_personal_destinations(audiences):
    """
    平台广播只能匹配全体类别，个人类别不可通过多选转发到明确第三方地址。
    """
    with pytest.raises(ValueError, match="所有用户范围|个人通知"):
        _rule("all", audiences=audiences)
    if "user" in audiences:
        with pytest.raises(ValueError, match="个人通知"):
            _rule("user", "123", audiences=audiences)
    assert _rule("all", audiences=["all"]).matched_audiences == ("all",)


def test_multi_audience_snapshot_freezes_selection_and_rejects_unknown_classes():
    """
    分享或在途恢复保留多选类别，不把未知类别或旧 any 哨兵当成选项。
    """
    original = (_rule("bound", audiences=["admin", "user", "admin"]),)
    assert original[0].matched_audiences == ("admin", "user")
    _, raw = read_or_freeze_notification_rules({}, {"tg": original})
    frozen, _ = read_or_freeze_notification_rules(raw, {"tg": (_rule("bound", audiences=[]),)})
    assert frozen == {"tg": original}
    with pytest.raises(ValueError):
        _rule("bound", audiences=["any"])
