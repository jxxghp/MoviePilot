"""
真实发送者、群回复地址与统一授权入口之间的隔离回归。
"""

import json
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.application.outbox import CHANNEL_AUTHORIZATION_RECEIPT_TOPIC
from app.application.security import authorization as authorization_module
from app.application.security.authorization import ChannelAuthorizationService
from app.chain.message import MessageChain
from app.db.adapters.authorization import authorization_scope
from app.db.models.authorization import ChannelAuthorization
from app.db.models.outbox import OutboxMessage
from app.db.models.user import User
from app.db.session import SessionFactory
from app.modules.qqbot.module import QQBotModule
from app.modules.vocechat import VoceChatModule
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationChannel


@pytest.fixture
def actor_channels(db, monkeypatch):
    """
    在临时测试数据库装配群平台授权，恢复全局服务和新增记录。

    :param db: 隔离数据库水位清理工具
    :param monkeypatch: 可撤销的依赖替换工具
    :yields: 授权服务、渠道模块与模拟发送客户端
    """
    db.watermark(User)
    configs = [
        NotificationConf(id="actor-qq", name="actor-qq", type="qqbot", enabled=True),
        NotificationConf(id="actor-voce", name="actor-voce", type="vocechat", enabled=True,
                         config={"VOCECHAT_CHANNEL_ID": "2"}),
    ]
    monkeypatch.setattr(authorization_module, "get_notification_configs", lambda **_: configs)
    monkeypatch.setattr("app.chain.message.get_notification_configs", lambda **_: configs)
    previous = authorization_module.get_channel_authorization()
    service = ChannelAuthorizationService(partial(authorization_scope, SessionFactory))
    authorization_module.configure_channel_authorization(service)
    modules = {"qqbot": QQBotModule(), "vocechat": VoceChatModule()}
    clients = {key: Mock() for key in modules}
    for client in clients.values():
        client.send_msg.return_value = True
    for config in configs:
        module = modules[config.type]
        monkeypatch.setattr(module, "get_config", lambda _source, value=config: value)
        monkeypatch.setattr(module, "get_configs", lambda value=config: {value.name: value})
        monkeypatch.setattr(module, "get_instance", lambda _source, key=config.type: clients[key])
        monkeypatch.setattr(module, "check_message", lambda *_: True)
    with SessionFactory() as session:
        before = set(session.scalars(select(ChannelAuthorization.id)))
    try:
        yield service, modules, clients
    finally:
        authorization_module.configure_channel_authorization(previous)
        with SessionFactory() as session:
            statement = delete(ChannelAuthorization)
            if before:
                statement = statement.where(ChannelAuthorization.id.not_in(before))
            session.execute(statement)
            session.commit()


def _parse(module, platform, actor, text="申请使用"):
    """
    使用真实渠道解析器构造来自同一群的不同发送者报文。

    :param module: 待测渠道模块
    :param platform: qqbot 或 vocechat
    :param actor: 平台原始发送者标识
    :param text: 消息正文
    :return: 解析后的统一入站消息
    """
    if platform == "qqbot":
        body = {"type": "GROUP_AT_MESSAGE_CREATE", "author": {"member_openid": actor},
                "group_openid": "same-group", "content": text}
    else:
        body = json.dumps({"from_uid": actor, "target": {"gid": 2},
                           "detail": {"type": "normal", "content_type": "text/plain", "content": text}})
    return module.message_parser(f"actor-{'qq' if platform == 'qqbot' else 'voce'}", body, {}, {})


@pytest.mark.parametrize("platform,actors,expected", [
    ("qqbot", ("alice", "bob"), ("alice", "bob")),
    ("vocechat", (7, 8), ("UID#7", "UID#8")),
])
def test_group_senders_have_independent_authority(actor_channels, monkeypatch, platform, actors, expected):
    """
    同群两人经过真实解析和消息入口后分别申请，批准一人不授权另一人。
    """
    service, modules, _ = actor_channels
    chain = MessageChain()
    monkeypatch.setattr(chain, "post_message", Mock())
    messages = [_parse(modules[platform], platform, actor) for actor in actors]
    assert tuple(str(item.userid) for item in messages) == expected
    assert all(item.conversation_type == "group" and item.chat_id for item in messages)
    for message in messages:
        monkeypatch.setattr(chain, "run_module", lambda _method, value=message, **_: value)
        chain.process(None, {}, {"source": message.source})
    first = service.find(messages[0].channel.value, messages[0].source, expected[0])
    second = service.find(messages[1].channel.value, messages[1].source, expected[1])
    assert first.id != second.id and first.user_id != second.user_id
    service.update(first.id, status="approved", role="admin", scopes=(), reviewer_id=1)
    assert service.is_admin(messages[0].channel.value, messages[0].source, expected[0])
    assert not service.is_admin(messages[1].channel.value, messages[1].source, expected[1])
    with pytest.raises(PermissionError):
        service.resolve(messages[1].channel.value, messages[1].source, expected[1])
    assert _parse(modules[platform], platform, actors[0]).is_channel_admin
    assert not _parse(modules[platform], platform, actors[1]).is_channel_admin
    monkeypatch.setattr(chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=False))
    agent = Mock()
    monkeypatch.setattr(chain, "_handle_ai_message", agent)
    forged = messages[1].model_copy(update={"is_channel_admin": True, "text": "下载电影"})
    monkeypatch.setattr(chain, "run_module", lambda _method, **_: forged)
    chain.process(None, {}, {"source": forged.source})
    agent.assert_not_called()
    assert "授权" in chain.post_message.call_args.args[0].title


@pytest.mark.parametrize("platform,actor,target", [
    ("qqbot", "alice", "group:same-group"), ("vocechat", 7, "GID#2"),
])
@pytest.mark.parametrize("private", [False, True])
def test_reply_address_is_independent_from_actor(actor_channels, platform, actor, target, private):
    """
    普通回复回原群，强制私聊按发送者寻址，不改变授权主体。
    """
    _, modules, clients = actor_channels
    module = modules[platform]
    incoming = _parse(module, platform, actor)
    outgoing = Message(channel=incoming.channel, source=incoming.source, userid=incoming.userid,
                       original_chat_id=incoming.chat_id, title="结果", private_delivery=private)
    module.post_message(outgoing)
    assert clients[platform].send_msg.call_args.kwargs["userid"] == (
        str(incoming.userid) if private else target
    )


@pytest.mark.parametrize("channel,source,sender", [
    (NotificationChannel.QQ, "actor-qq", "group:same-group"),
    (NotificationChannel.QQ, "actor-qq", "GROUP:same-group"),
    (NotificationChannel.VoceChat, "actor-voce", "GID#2"),
])
def test_group_addresses_cannot_be_personal_grants(actor_channels, channel, source, sender):
    """
    手工授权和兼容调用均不能把群地址登记成个人成员。
    """
    service, _, _ = actor_channels
    with pytest.raises(ValueError, match="群"):
        service.request(channel.value, source, sender, "误配置")


def test_qq_group_without_sender_is_rejected(actor_channels):
    """
    缺少实际发送者的 QQ 群事件不能通过群 ID 推断身份。
    """
    _, modules, _ = actor_channels
    assert _parse(modules["qqbot"], "qqbot", "") is None


def test_vocechat_numeric_legacy_grant_remains_same_actor(actor_channels):
    """
    VoceChat 历史数字用户 ID 和 UID 前缀是同一个人的确定性格式，不恢复停用授权。
    """
    service, modules, _ = actor_channels
    member = service.request("VoceChat", "actor-voce", "7", "历史用户")
    service.update(member.id, status="disabled", role="member", scopes=(), reviewer_id=1)
    with SessionFactory() as session:
        session.get(ChannelAuthorization, member.id).sender_id = "7"
        session.commit()
    incoming = _parse(modules["vocechat"], "vocechat", 7)
    same = service.find("VoceChat", "actor-voce", str(incoming.userid))
    assert same.id == member.id and same.status == "disabled"


@pytest.mark.parametrize("channel,source,sender", [
    ("QQ", "actor-qq", "group:same-group"), ("VoceChat", "actor-voce", "GID#2"),
])
def test_preexisting_group_grant_cannot_execute_or_be_reapproved(actor_channels, channel, source, sender):
    """
    即使迁移尚未执行，历史群管理员令牌也无法继续执行或重新批准。
    """
    service, _, _ = actor_channels
    member = service.request(channel, source, "alice", "历史群")
    with SessionFactory() as session:
        row = session.get(ChannelAuthorization, member.id)
        row.sender_id, row.status, row.role = sender, "approved", "admin"
        session.commit()
    with pytest.raises(PermissionError, match="群身份"):
        service.principal(member.id)
    with pytest.raises(ValueError, match="群"):
        service.update(member.id, status="approved", role="admin", scopes=(), reviewer_id=1)
    assert service.update(member.id, status="disabled", role="member", scopes=(), reviewer_id=1).status == "disabled"


def test_manual_group_authorization_returns_client_error(actor_channels, monkeypatch):
    """
    网页端误填群地址返回明确 400，不建立授权或发送成功回执。
    """
    from app.api.endpoints import authorization as endpoint
    from app.schemas.authorization import ChannelMemberCreate

    _, modules, _ = actor_channels
    config = modules["qqbot"].get_config("actor-qq")
    monkeypatch.setattr(endpoint, "get_notification_configs", lambda: [config])
    with SessionFactory() as session:
        members = set(session.scalars(select(ChannelAuthorization.id)))
        receipts = set(session.scalars(select(OutboxMessage.event_key).where(
            OutboxMessage.topic == CHANNEL_AUTHORIZATION_RECEIPT_TOPIC,
        )))
    with pytest.raises(HTTPException) as error:
        endpoint.create_authorization(
            ChannelMemberCreate(notification_id=config.id, sender_id="group:same-group", status="approved"),
            user=SimpleNamespace(id=1),
        )
    assert error.value.status_code == 400 and "群" in error.value.detail
    with SessionFactory() as session:
        assert set(session.scalars(select(ChannelAuthorization.id))) == members
        assert set(session.scalars(select(OutboxMessage.event_key).where(
            OutboxMessage.topic == CHANNEL_AUTHORIZATION_RECEIPT_TOPIC,
        ))) == receipts


@pytest.mark.parametrize("platform,actor,target", [
    ("qqbot", "alice", "group:same-group"), ("vocechat", 7, "GID#2"),
])
def test_application_reply_reaches_original_group(actor_channels, monkeypatch, platform, actor, target):
    """
    真实解析经消息入口生成申请回执后，客户端仍以原群为目标。
    """
    _, modules, clients = actor_channels
    module = modules[platform]
    incoming = _parse(module, platform, actor)
    chain = MessageChain()
    monkeypatch.setattr(chain, "run_module", lambda _method, **_: incoming)

    def deliver(message, **_):
        """
        仅把当前渠道回复交给真实发送模块，忽略管理员广播事件。
        """
        if message.channel == incoming.channel:
            module.post_message(message)

    monkeypatch.setattr(chain, "post_message", deliver)
    chain.process(None, {}, {"source": incoming.source})
    assert clients[platform].send_msg.call_args.kwargs["userid"] == target


@pytest.mark.parametrize("platform,actor,target", [
    ("qqbot", "alice", "group:same-group"), ("vocechat", 7, "GID#2"),
])
def test_group_media_and_pagination_keep_reply_context(actor_channels, monkeypatch, platform, actor, target):
    """
    搜索、翻页及资源列表通过实际媒体渲染到达群目标，不把 actor 当私聊地址。
    """
    from app.application.messaging.media import media_interaction_manager
    from app.chain.interaction import MediaInteractionChain
    from app.chain.media import MediaChain
    from app.domain.context import MediaInfo
    from app.domain.meta.metabase import MetaBase
    from app.schemas.types import MediaType

    service, modules, clients = actor_channels
    module = modules[platform]
    incoming = _parse(module, platform, actor, "搜索 测试电影")
    member = service.request(incoming.channel.value, incoming.source, str(incoming.userid), "成员")
    service.update(member.id, status="approved", role="admin", scopes=(), reviewer_id=1)
    chain = MessageChain()
    monkeypatch.setattr(chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=False))
    monkeypatch.setattr(chain, "_mark_message_processing_finished", Mock())
    monkeypatch.setattr(chain, "_record_user_message", Mock())
    monkeypatch.setattr(chain, "_handle_secret_confirmation_control", Mock(return_value=False))
    monkeypatch.setattr(chain, "run_module", lambda _method, **_: incoming)
    meta = MetaBase("测试电影")
    meta.name = "测试电影"
    medias = [MediaInfo(title=f"电影{index}", type=MediaType.MOVIE, tmdb_id=index) for index in range(1, 11)]
    monkeypatch.setattr(MediaChain, "search", lambda *_, **__: (meta, medias))
    monkeypatch.setattr(MediaInteractionChain, "post_medias_message", lambda _self, message, medias: module.post_medias_message(message, medias))
    monkeypatch.setattr(MediaInteractionChain, "post_torrents_message", lambda _self, message, torrents: module.post_torrents_message(message, torrents))
    monkeypatch.setattr(MediaInteractionChain, "post_message", lambda _self, message: module.post_message(message))
    try:
        chain.process(None, {}, {"source": incoming.source})
        assert clients[platform].send_medias_msg.call_args.kwargs["userid"] == target
        incoming.text = "下一页"
        chain.process(None, {}, {"source": incoming.source})
        assert clients[platform].send_medias_msg.call_args.kwargs["userid"] == target
        request = media_interaction_manager.get_by_user(incoming.userid, incoming.channel, incoming.source, target)
        assert request.original_chat_id == target
    finally:
        if (request := media_interaction_manager.get_by_user(incoming.userid, incoming.channel, incoming.source, target)) is not None:
            media_interaction_manager.remove(request.request_id)


def test_ambiguous_vocechat_grants_reject_existing_principals_and_tasks(actor_channels):
    """
    数字 ID 的拒绝记录和 UID 管理员记录冲突时，旧令牌与任务不能绕过唯一性检查。
    """
    import asyncio

    from app.agent.identity import AgentIdentityError, require_execution_user
    from app.application.security.auth import AuthService
    from app.schemas.token import TokenPayload

    service, _, _ = actor_channels
    approved = service.request("VoceChat", "actor-voce", "7", "管理员")
    service.update(approved.id, status="approved", role="admin", scopes=(), reviewer_id=1)
    rejected = service.request("VoceChat", "actor-voce", "different", "已拒绝")
    with SessionFactory() as session:
        row = session.get(ChannelAuthorization, rejected.id)
        row.sender_id, row.status = "7", "rejected"
        session.commit()
    with pytest.raises(ValueError, match="冲突"):
        service.find("VoceChat", "actor-voce", "UID#7")
    with pytest.raises(ValueError, match="冲突"):
        service.update(approved.id, status="approved", role="admin", scopes=(), reviewer_id=1)
    with pytest.raises(PermissionError, match="冲突"):
        service.principal(approved.id)
    with pytest.raises(AgentIdentityError, match="冲突"):
        asyncio.run(require_execution_user(None, approved.user_id, approved.user_identity, approved.id))
    auth = AuthService(users=Mock(), config=Mock(), passkeys=Mock())
    with pytest.raises(PermissionError, match="冲突"):
        auth.validate_token_identity(TokenPayload(
            sub=approved.user_id, username="old", super_user=True,
            agent_authorization_id=approved.id, agent_operation="subscription.list",
            agent_method="GET", agent_path="/api/v1/subscribe/",
        ))


def test_legacy_command_arity_and_reply_scope_are_preserved(monkeypatch):
    """
    旧三参数命令不改变调用协议，消息冻结原群地址，作用域结束后不污染后续消息。
    """
    from app.application.messaging.message import MessageTemplateHelper
    from app.command import Command

    command = object.__new__(Command)
    rendered = []

    def legacy_handler(channel, userid, source):
        """
        模拟旧命令只接受三个位置参数，并渲染持久化前的通知。
        """
        message = Message(channel=channel, source=source, userid=userid, title="命令结果")
        rendered.append(MessageTemplateHelper.render(message).model_dump(mode="json"))

    monkeypatch.setattr(command, "get", lambda _: {"func": legacy_handler, "description": "测试命令"})
    command.execute("/test", channel=NotificationChannel.QQ, source="qq", userid="alice",
                    original_chat_id="group:room")
    assert rendered[0]["userid"] == "alice" and rendered[0]["original_chat_id"] == "group:room"
    later = MessageTemplateHelper.render(Message(channel=NotificationChannel.QQ, source="qq", userid="alice", title="稍后"))
    assert later.original_chat_id is None


def test_reply_scope_freezes_business_notifications_without_expanding_audience():
    """
    下载/订阅模板在当前交互中冻结地址，其他账号、实例和私聊消息不继承该目标。
    """
    from app.application.messaging.message import MessageTemplateHelper, scoped_message_reply
    from app.schemas.types import MessageType

    @scoped_message_reply
    def business(channel, source, userid, original_chat_id):
        """
        模拟旧业务用例在模板渲染后将独立消息序列化入持久化 intent。
        """
        messages = [
            Message(channel=channel, source=source, userid=userid, title="下载完成", mtype=MessageType.Download),
            Message(channel=channel, source=source, userid=userid, title="订阅完成", mtype=MessageType.Subscribe),
            Message(channel=channel, source=source, userid=userid, title="私聊", private_delivery=True),
            Message(channel=channel, source="other", userid=userid, title="其他实例"),
            Message(channel=channel, source=source, userid="bob", title="其他账号"),
            Message(channel=channel, source=source, title="广播"),
        ]
        return [MessageTemplateHelper.render(message).model_dump(mode="json") for message in messages]

    frozen = business(NotificationChannel.QQ, "qq", "alice", "group:room")
    restored = [Message.model_validate(data) for data in frozen]
    assert [message.original_chat_id for message in restored] == ["group:room", "group:room", None, None, None, None]
