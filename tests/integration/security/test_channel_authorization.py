"""
统一渠道授权的身份隔离、实时撤销、API 令牌约束与审批交互回归。
"""

import asyncio
import json
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.agent.api.executor import ApiExecutionContext, ApiExecutionError, MoviePilotApiExecutor
from app.agent.identity import AgentIdentityError, require_execution_user, resolve_task_user
from app.application.messaging.plugin import plugin_input_interaction_manager
from app.application.security import authorization as authorization_module
from app.application.security.auth import AuthService
from app.application.security.authorization import ChannelAuthorizationService
from app.chain.message import MessageChain
from app.db.adapters.authorization import authorization_scope
from app.db.adapters.user import TransactionalUserRepository
from app.db.models.authorization import ChannelAuthorization, ChannelBinding
from app.db.models.user import User
from app.db.session import SessionFactory, async_session_scope
from app.schemas.message import IncomingMessage
from app.schemas.system import NotificationConf
from app.schemas.token import TokenPayload
from app.schemas.types import EventType, NotificationChannel


@pytest.fixture
def channel_authorization(db, monkeypatch):
    """
    装配真实临时数据库授权服务，恢复进程服务并清理新增成员和内部账号。

    :param db: 共享隔离库的水位清理工具
    :param monkeypatch: 当前用例的配置函数替换工具
    :yields: 授权服务、启用渠道配置及真实用户仓储
    """
    db.watermark(User)
    configs = [
        NotificationConf(id="telegram-a", name="机器人A", type="telegram", enabled=True),
        NotificationConf(id="telegram-b", name="机器人B", type="telegram", enabled=True),
        NotificationConf(id="wechat-a", name="微信A", type="wechatclawbot", enabled=True),
    ]
    monkeypatch.setattr(
        authorization_module, "get_notification_configs", lambda **_: [item for item in configs if item.enabled]
    )
    service = ChannelAuthorizationService(partial(authorization_scope, SessionFactory))
    previous = authorization_module.get_channel_authorization()
    authorization_module.configure_channel_authorization(service)
    with SessionFactory() as session:
        before = set(session.scalars(select(ChannelAuthorization.id)))
    users = TransactionalUserRepository(sync_session=SessionFactory, async_session=async_session_scope)
    try:
        yield service, configs, users
    finally:
        authorization_module.configure_channel_authorization(previous)
        with SessionFactory() as session:
            session.execute(delete(ChannelBinding))
            statement = delete(ChannelAuthorization)
            if before:
                statement = statement.where(ChannelAuthorization.id.not_in(before))
            session.execute(statement)
            session.commit()


def _approve(service, source="机器人A", sender_id="42", role="member", scopes=("search", "subscribe", "download")):
    """
    通过真实审批事务批准合成渠道发送者。
    """
    member = service.request(NotificationChannel.Telegram.value, source, sender_id, "测试成员")
    return service.update(member.id, status="approved", role=role, scopes=scopes, reviewer_id=1)


def test_member_request_needs_no_login_and_repeated_request_cannot_restore_access(channel_authorization):
    """
    普通申请没有登录凭据，重复请求幂等且不能覆盖停用和拒绝决定。
    """
    service, _, users = channel_authorization
    member = service.request("Telegram", "机器人A", "42", "普通成员")
    assert member.status == "pending"
    user = asyncio.run(users.async_get_by_id(member.user_id))
    assert user.account_type == "channel"
    assert users.get_auth_by_name(user.name) is None
    with pytest.raises(PermissionError, match="尚未授权"):
        service.principal(member.id)
    service.update(member.id, status="disabled", role="member", scopes=("search",), reviewer_id=1)
    repeated = service.request("Telegram", "机器人A", "42", "伪装管理员")
    assert repeated.id == member.id and repeated.status == "disabled"
    assert repeated.display_name == "普通成员"


def test_same_sender_has_separate_instance_authority_and_rename_preserves_identity(channel_authorization):
    """
    相同平台 ID 的不同机器人分别授权，改名保留授权，删停和类型替换立即失权。
    """
    service, configs, _ = channel_authorization
    admin = _approve(service, role="admin")
    member = _approve(service, source="机器人B")
    assert admin.user_id != member.user_id
    assert service.is_admin("Telegram", "机器人A", "42")
    assert not service.is_admin("Telegram", "机器人B", "42")
    configs[0].name = "已改名"
    assert service.resolve("Telegram", "已改名", "42").authorization_id == admin.id
    configs[0].enabled = False
    with pytest.raises(PermissionError, match="渠道"):
        service.principal(admin.id)
    assert service.principal(member.id).authorization_id == member.id


def test_role_and_scopes_are_rechecked_for_existing_api_executors(channel_authorization):
    """
    已构造的管理员执行器不能在降权或停用后继续使用旧权限。
    """
    service, _, _ = channel_authorization
    member = _approve(service, role="admin")
    user = service.principal(member.id)
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(user_id=str(user.id), username=user.name, is_admin=True, authorization_id=member.id)
    )
    assert executor._build_headers("site.list", "GET", "/api/v1/site/")["Authorization"].startswith("Bearer ")
    service.update(member.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
    with pytest.raises(ApiExecutionError, match="没有此操作"):
        executor._build_headers("site.list", "GET", "/api/v1/site/")
    with pytest.raises(PermissionError):
        service.authorize_operation(member.id, "subscription.add")
    assert service.authorize_operation(member.id, "media.detail").authorization_id == member.id
    service.update(member.id, status="disabled", role="member", scopes=("search",), reviewer_id=1)
    with pytest.raises(ApiExecutionError, match="停用"):
        executor._build_headers("search.torrents", "GET", "/api/v1/search/media/1")


def test_channel_token_has_fixed_route_and_cannot_issue_web_login(channel_authorization):
    """
    渠道令牌带固定路由声明，账号不能取得网页登录令牌，撤销后旧签名也被拒绝。
    """
    from app.application.security.token import decode_access_token

    service, _, users = channel_authorization
    member = _approve(service)
    user = service.principal(member.id)
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(
            user_id=str(user.id), username=user.name, is_admin=False, authorization_id=member.id
        )
    )
    token = executor._build_headers("subscription.list", "GET", "/api/v1/subscribe/")["Authorization"].split(" ", 1)[1]
    payload = decode_access_token(token)
    assert payload.agent_authorization_id == member.id
    assert payload.agent_method == "GET" and payload.agent_path == "/api/v1/subscribe/"
    auth = AuthService(users=users, config=Mock(), passkeys=Mock())
    auth.validate_token_identity(payload)
    with pytest.raises(PermissionError, match="不可网页登录"):
        auth.build_token_response(user)
    with pytest.raises(PermissionError, match="网页登录"):
        auth.validate_token_identity(TokenPayload(sub=user.id, username=user.name, super_user=False))
    service.update(member.id, status="disabled", role="member", scopes=(), reviewer_id=1)
    with pytest.raises(PermissionError):
        auth.validate_token_identity(payload)


def test_scheduled_tasks_preserve_authorization_and_recheck_schedule_permission(channel_authorization):
    """
    渠道定时任务固定执行主体，撤销调度能力后不再允许后台执行。
    """
    service, _, users = channel_authorization
    member = _approve(service, scopes=("search", "schedule"))
    user = asyncio.run(
        resolve_task_user(
            users, user_id="42", username="管理员同名", channel="Telegram", source="机器人A", is_admin=False
        )
    )
    assert user.authorization_id == member.id
    assert (
        asyncio.run(require_execution_user(users, user.id, user.notification_identity, member.id, "schedule")).id
        == user.id
    )
    service.update(member.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
    with pytest.raises(AgentIdentityError, match="schedule"):
        asyncio.run(require_execution_user(users, user.id, user.notification_identity, member.id, "schedule"))


def test_deleted_account_id_reuse_cannot_inherit_channel_authorization(channel_authorization):
    """
    账号主键被重用时，不可变身份阻止旧授权被新账号继承。
    """
    service, _, _ = channel_authorization
    member = _approve(service)
    with SessionFactory() as session:
        user = session.get(User, member.user_id)
        user.notification_identity = "replaced-identity"
        session.commit()
    with pytest.raises(PermissionError, match="账号"):
        service.principal(member.id)


def test_apply_control_is_handled_without_model_and_does_not_run_old_request(channel_authorization, monkeypatch):
    """
    申请按钮和文本共用确定性入口，不经过模型或自动执行先前写入。
    """
    service, _, _ = channel_authorization
    chain = MessageChain()
    post = Mock()
    monkeypatch.setattr(chain, "post_message", post)
    assert chain._handle_channel_authorization(
        NotificationChannel.Telegram, "机器人A", "42", "成员", "订阅某部电影", None
    )
    assert post.call_args.args[0].buttons[0][0]["callback_data"] == "authorization:apply"
    assert chain._handle_channel_authorization(
        NotificationChannel.Telegram, "机器人A", "42", "成员", "", "authorization:apply"
    )
    assert service.find("Telegram", "机器人A", "42").status == "pending"
    assert post.call_count == 3


def test_channel_admin_cannot_manage_public_authorizations():
    """
    渠道管理员不等于网页登录系统管理员，不能从新管理端点继续转授权限。
    """
    from app.api.endpoints.authorization import _administrator

    with pytest.raises(HTTPException) as error:
        _administrator(SimpleNamespace(is_superuser=True, account_type="channel", authorization_id="member"))
    assert error.value.status_code == 403


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_channel_member_text_cannot_reenter_legacy_admin_sessions(channel_authorization, monkeypatch, status):
    """
    普通及待审批成员绕过旧插件输入会话，只把业务请求送到受控 Agent。
    """
    from dataclasses import replace

    service, _, _ = channel_authorization
    if status == "approved":
        _approve(service, scopes=("search",))
    else:
        service.request("Telegram", "机器人A", "42", "成员")
    chain = MessageChain()
    monkeypatch.setattr(
        chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=True, ai_agent_global=True)
    )
    agent = Mock(return_value=False)
    plugin = Mock()
    monkeypatch.setattr(chain, "_handle_ai_message", agent)
    monkeypatch.setattr(chain, "_plugin_input_interaction_handler", plugin)
    monkeypatch.setattr(chain, "_handle_secret_confirmation_control", Mock(return_value=False))
    monkeypatch.setattr(chain, "_mark_message_processing_finished", Mock())
    chain.handle_message(NotificationChannel.Telegram, "机器人A", "42", "昵称", "继续操作", is_channel_admin=True)
    assert agent.call_args.kwargs["is_channel_admin"] is False
    plugin.assert_not_called()
    if status == "approved":
        post = Mock()
        monkeypatch.setattr(chain, "post_message", post)
        assert chain._handle_channel_authorization(
            NotificationChannel.Telegram, "机器人A", "42", "昵称", "", "system:restart"
        )
        assert "管理员" in post.call_args.args[0].title


@pytest.mark.parametrize("text", ["#123456", "?930441", "？930441", "🔥123456", " \n＃123456"])
@pytest.mark.parametrize(("ai_enabled", "ai_global"), [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("member_status", ["missing", "pending", "member", "admin", "unmanaged"])
@pytest.mark.parametrize(("channel", "source"), [
    (NotificationChannel.Telegram, "机器人A"), (NotificationChannel.Wechat, "企微A"),
])
def test_special_prefix_text_is_routed_to_plugins_without_agent_or_history(
        channel_authorization, monkeypatch, text, ai_enabled, ai_global, member_status, channel, source,
):
    """
    企微和 Telegram 插件验证码在不同 Agent 模式和成员身份下均交给插件且不保存普通历史。
    """
    from dataclasses import replace

    service, configs, _ = channel_authorization
    if channel == NotificationChannel.Wechat:
        configs.append(NotificationConf(id="wechat-enterprise", name=source, type="wechat", enabled=True))
    if member_status in {"pending", "member", "admin"}:
        member = service.request(channel.value, source, "42", "成员")
        if member_status != "pending":
            service.update(member.id, status="approved", role=member_status, scopes=(), reviewer_id=1)
    if member_status == "unmanaged":
        authorization_module.configure_channel_authorization(None)

    chain = MessageChain()
    monkeypatch.setattr(
        chain,
        "runtime_config",
        replace(chain.runtime_config, ai_agent_enable=ai_enabled, ai_agent_global=ai_global),
    )
    event_handler = Mock()
    event_handler.handle_text.return_value = False
    agent_handler = Mock()
    post_message = Mock()
    record_message = Mock()
    send_event = Mock()
    monkeypatch.setattr(chain, "_plugin_input_interaction_handler", lambda: event_handler)
    monkeypatch.setattr(chain, "_handle_ai_message", agent_handler)
    monkeypatch.setattr(chain, "post_message", post_message)
    monkeypatch.setattr(chain, "_record_user_message", record_message)
    monkeypatch.setattr(chain.eventmanager, "send_event", send_event)

    chain.handle_message(
        channel=channel,
        source=source,
        userid="42",
        username="成员",
        text=text,
        original_chat_id="chat-7479",
        reply_to_message_id="prompt-7479",
    )

    event_handler.handle_text.assert_called_once()
    agent_handler.assert_not_called()
    post_message.assert_not_called()
    record_message.assert_not_called()
    send_event.assert_called_once_with(
        EventType.UserMessage,
        {
            "text": text,
            "userid": "42",
            "channel": channel,
            "source": source,
            "chat_id": "chat-7479",
            "reply_to_message_id": "prompt-7479",
        },
    )


@pytest.mark.parametrize("status", ["disabled", "rejected"])
def test_special_prefix_text_remains_blocked_for_disabled_channel_member(channel_authorization, monkeypatch, status):
    """
    标点开头的插件输入不能绕过已停用或拒绝成员的渠道授权。
    """
    from dataclasses import replace

    service, _, _ = channel_authorization
    member = service.request("Telegram", "机器人A", "42", "成员")
    service.update(member.id, status=status, role="member", scopes=(), reviewer_id=1)

    chain = MessageChain()
    monkeypatch.setattr(
        chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=False, ai_agent_global=False)
    )
    post_message = Mock()
    send_event = Mock()
    monkeypatch.setattr(chain, "post_message", post_message)
    monkeypatch.setattr(chain.eventmanager, "send_event", send_event)

    chain.handle_message(
        channel=NotificationChannel.Telegram,
        source="机器人A",
        userid="42",
        username="成员",
        text="#123456",
    )

    post_message.assert_called_once()
    assert not any(call.args[0] == EventType.UserMessage for call in send_event.call_args_list)


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_special_prefix_text_consumes_plugin_session_before_broadcast(channel_authorization, monkeypatch, status):
    """
    插件活动输入会话先收到验证码，并保留回复定位且不重复广播或写入消息历史。
    """
    service, _, _ = channel_authorization
    member = service.request("Telegram", "机器人A", "42", "成员")
    if status == "approved":
        service.update(member.id, status=status, role="member", scopes=(), reviewer_id=1)
    chain = MessageChain()
    post = Mock()
    record = Mock()
    agent = Mock()
    send_event = Mock()
    monkeypatch.setattr(chain, "post_message", post)
    monkeypatch.setattr(chain, "_record_user_message", record)
    monkeypatch.setattr(chain, "_handle_ai_message", agent)
    monkeypatch.setattr(chain.eventmanager, "send_event", send_event)
    request = plugin_input_interaction_manager.create_or_replace(
        user_id="42", plugin_id="example_plugin", channel=NotificationChannel.Telegram,
        source="机器人A", username="成员", chat_id="chat-7479", prompt_message_id="prompt-7479",
    )
    try:
        chain.handle_message(
            NotificationChannel.Telegram, "机器人A", "42", "成员", "#123456",
            original_chat_id="chat-7479", reply_to_message_id="prompt-7479",
        )
        send_event.assert_called_once()
        event_type, payload = send_event.call_args.args
        assert event_type == EventType.MessageAction
        assert payload["input_text"] == "#123456"
        assert payload["input_session_id"] == request.request_id
        assert payload["__mp_target_plugin_id"] == "example_plugin"
        assert payload["chat_id"] == "chat-7479"
        assert payload["reply_to_message_id"] == "prompt-7479"
        assert plugin_input_interaction_manager.get_by_user(
            "42", NotificationChannel.Telegram, "机器人A", "chat-7479",
        ) is None
        post.assert_not_called()
        record.assert_not_called()
        agent.assert_not_called()
    finally:
        plugin_input_interaction_manager.remove(request.request_id)


@pytest.mark.parametrize(("text", "attachments"), [
    ("/help", {}), ("/ai 查询", {}), ("123456", {}), ("普通文本", {}), ("", {}),
    ("#123456", {"images": [{"ref": "synthetic-image"}]}),
    ("#123456", {"files": [IncomingMessage.MessageAttachment(ref="synthetic-file")]}),
    ("#123456", {"audio_refs": ["synthetic-audio"]}),
    ("#123456", {"callback_data": "plugin:callback"}),
])
def test_plugin_input_exception_does_not_bypass_other_authorization(
        channel_authorization, monkeypatch, text, attachments,
):
    """
    插件纯文本豁免不放开普通文本、斜杠命令、附件或按钮回调的授权要求。
    """
    from dataclasses import replace

    chain = MessageChain()
    monkeypatch.setattr(chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=False))
    post = Mock()
    plugin = Mock()
    monkeypatch.setattr(chain, "post_message", post)
    monkeypatch.setattr(chain, "_plugin_input_interaction_handler", plugin)
    chain.handle_message(NotificationChannel.Telegram, "机器人A", "42", "成员", text, **attachments)
    post.assert_called_once()
    plugin.assert_not_called()
    assert "授权" in post.call_args.args[0].title


@pytest.mark.parametrize(("channel", "text", "attachments"), [
    (NotificationChannel.Web, "#123456", {}),
    (NotificationChannel.WebAgent, "#123456", {}),
    (NotificationChannel.Telegram, "/help", {}),
    (NotificationChannel.Telegram, "/ai 查询", {}),
    (NotificationChannel.Telegram, "普通文本", {}),
    (NotificationChannel.Telegram, "#123456", {"callback_data": "plugin:callback"}),
    (NotificationChannel.Telegram, "#123456", {"images": [{"ref": "synthetic-image"}]}),
    (NotificationChannel.Telegram, "#123456", {"audio_refs": ["synthetic-audio"]}),
])
def test_special_plugin_route_preserves_web_commands_and_attachments(
        channel_authorization, monkeypatch, channel, text, attachments,
):
    """
    网页会话、命令、回调及带附件消息仍进入既有核心路由。
    """
    _approve(channel_authorization[0], role="admin")
    chain = MessageChain()
    core = Mock(return_value=False)
    plugin = Mock()
    plugin.handle_text.return_value = False
    dispatch = Mock()
    monkeypatch.setattr(chain, "_handle_message_core", core)
    monkeypatch.setattr(chain, "_plugin_input_interaction_handler", lambda: plugin)
    monkeypatch.setattr(chain, "_dispatch_plugin_text", dispatch)
    monkeypatch.setattr(chain, "_transcribe_audio_refs", Mock(return_value="语音转写"))
    monkeypatch.setattr(chain, "_record_user_message", Mock())
    monkeypatch.setattr(chain, "_mark_message_processing_started", Mock(return_value=None))
    chain.handle_message(channel, "机器人A", "42", "成员", text, **attachments)
    dispatch.assert_not_called()
    core.assert_called_once()


def test_account_binding_requires_both_sides_and_never_approves_pending_member(channel_authorization, db):
    """
    网页账号生成的码只能消费一次，关联不会替代渠道审批或解除管理员停用。
    """
    service, _, _ = channel_authorization
    local = db.add(
        User(
            name="bound-web-account",
            is_active=True,
            is_superuser=False,
            settings={},
            permissions={"search": True, "subscribe": True, "manage": True},
        )
    )
    code, _ = service.create_binding(local.id)
    member = service.bind("Telegram", "机器人A", "42", "成员", code)
    assert member.user_id == local.id and member.linked_user_id == local.id
    assert member.status == "pending"
    with pytest.raises(ValueError, match="无效"):
        service.bind("Telegram", "机器人B", "43", "其它成员", code)
    member = service.update(member.id, status="approved", role="member", scopes=("search", "subscribe"), reviewer_id=1)
    assert service.principal(member.id).name == local.name
    assert service.principal(member.id).permissions["download"] is False
    service.update(member.id, status="disabled", role="member", scopes=("search",), reviewer_id=1)
    new_code, _ = service.create_binding(local.id)
    assert service.bind("Telegram", "机器人A", "42", "成员", new_code).status == "disabled"


def test_binding_expiry_and_replaced_account_fail_without_changing_member(channel_authorization, db, monkeypatch):
    """
    到期和网页账号被替换时，关联失败不改变渠道成员业务归属。
    """
    service, _, _ = channel_authorization
    local = db.add(User(name="binding-expiry", is_active=True, is_superuser=False))
    member = service.request("Telegram", "机器人A", "42", "成员")
    code, expires_at = service.create_binding(local.id)
    monkeypatch.setattr(authorization_module.time, "time", lambda: expires_at + 1)
    with pytest.raises(ValueError, match="过期"):
        service.bind("Telegram", "机器人A", "42", "成员", code)
    assert service.find("Telegram", "机器人A", "42").user_id == member.user_id


def test_channel_tokens_cannot_be_reused_for_other_paths(channel_authorization, monkeypatch):
    """
    同一签名令牌不能从只读订阅路径转用到其它 API，且不会下发网页登录 Cookie。
    """
    from starlette.requests import Request
    from starlette.responses import Response

    from app.adapters.web.security import access

    service, _, _ = channel_authorization
    member = _approve(service)
    user = service.principal(member.id)
    payload = TokenPayload(
        sub=user.id,
        username=user.name,
        super_user=False,
        agent_authorization_id=member.id,
        agent_operation="subscription.list",
        agent_method="GET",
        agent_path="/api/v1/subscribe/",
    )
    monkeypatch.setattr(access, "_decode_or_http_error", lambda *_: payload)
    monkeypatch.setattr(access, "_token_identity_validator", lambda _: None)
    cookie = Mock()
    monkeypatch.setattr(access, "set_or_refresh_resource_token_cookie", cookie)
    valid = Request({"type": "http", "method": "GET", "path": "/api/v1/subscribe/", "headers": []})
    assert access.verify_token(valid, Response(), "token", None, None) == payload
    cookie.assert_not_called()
    invalid = Request({"type": "http", "method": "POST", "path": "/api/v1/user/", "headers": []})
    with pytest.raises(HTTPException) as error:
        access.verify_token(invalid, Response(), "token", None, None)
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "channel_type",
    ["telegram", "wechat", "wechatclawbot", "feishu", "qqbot", "discord", "slack", "vocechat", "synologychat"],
)
def test_every_channel_uses_common_grants_instead_of_legacy_keys(channel_authorization, channel_type):
    """
    各平台管理员都读取公共授权，旧名单和默认投递接收人不能恢复已撤销的权限。
    """
    from app.application.messaging.channel.admin import matches_channel_admin
    from app.domain.authorization import LEGACY_CHANNELS

    service, configs, _ = channel_authorization
    channel, _, admin_key, primary_key = LEGACY_CHANNELS[channel_type]
    config = NotificationConf(
        id=f"legacy-{channel_type}",
        name=f"{channel_type}-bot",
        type=channel_type,
        enabled=True,
        config={admin_key: "sender", primary_key: "sender"},
    )
    configs.append(config)
    assert not matches_channel_admin(channel, config.config, "sender", source=config.name)
    member = service.request(channel, config.name, "sender", "管理员")
    service.update(member.id, status="approved", role="admin", scopes=(), reviewer_id=1)
    assert matches_channel_admin(channel, config.config, "sender", source=config.name)
    service.update(member.id, status="disabled", role="admin", scopes=(), reviewer_id=1)
    assert not matches_channel_admin(channel, config.config, "sender", source=config.name)


def test_concurrent_duplicate_requests_create_one_member_and_one_internal_user(channel_authorization):
    """
    两个真实线程同时申请时，数据库锁保证身份唯一且没有孤立内部账号。
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, _, _ = channel_authorization
    barrier = Barrier(2)
    with SessionFactory() as session:
        before = set(session.scalars(select(User.id)))

    def request():
        """
        同步释放两个并发请求，使查重与创建发生真实竞争。
        """
        barrier.wait(timeout=5)
        return service.request("Telegram", "机器人A", "42", "成员")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        members = [future.result(timeout=10) for future in futures]
    assert members[0].id == members[1].id
    with SessionFactory() as session:
        assert len(set(session.scalars(select(User.id))) - before) == 1


def test_approval_api_enforces_administrator_and_returns_sanitized_contract(channel_authorization, monkeypatch):
    """
    通过真实 ASGI 端点验证批准、停用、非法范围拒绝及内部凭据不出现在响应中。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_active_superuser
    from app.api.endpoints import authorization as endpoint

    service, configs, _ = channel_authorization
    member = service.request("Telegram", "机器人A", "42", "成员")
    monkeypatch.setattr(endpoint, "get_notification_configs", lambda **_: configs)
    monkeypatch.setattr(endpoint, "_notify", Mock())
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/authorization")
    app.dependency_overrides[get_current_active_superuser] = lambda: SimpleNamespace(
        id=1, account_type="local", authorization_id=None
    )
    with TestClient(app) as client:
        response = client.get("/authorization/")
        assert response.status_code == 200
        assert response.json()["data"]["members"][0]["status"] == "pending"
        assert "user_identity" not in response.text and "user_id" not in response.text
        assert (
            client.put(
                f"/authorization/{member.id}",
                json={"status": "approved", "role": "member", "scopes": ["search", "subscribe"]},
            ).status_code
            == 200
        )
        assert service.principal(member.id).permissions["subscribe"] is True
        assert (
            client.put(f"/authorization/{member.id}", json={"status": "approved", "scopes": ["unknown"]}).status_code
            == 422
        )
        assert client.put(f"/authorization/{member.id}", json={"status": "disabled", "scopes": []}).status_code == 200
        with pytest.raises(PermissionError):
            service.principal(member.id)
        app.dependency_overrides[get_current_active_superuser] = lambda: SimpleNamespace(
            id=1, account_type="channel", authorization_id=member.id
        )
        assert client.get("/authorization/").status_code == 403


def test_disabled_member_cannot_run_nonbase_tools_or_subagent_tools(channel_authorization):
    """
    已构建的策略中间件在撤销后阻止非基础记忆工具和子代理继续执行。
    """
    from app.agent.middleware.policy import AgentPolicyMiddleware
    from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext

    service, _, _ = channel_authorization
    member = _approve(service)
    context = {"authorization_id": member.id, "is_admin": True}
    middleware = AgentPolicyMiddleware(
        context=ToolPolicyContext(
            session_id="session",
            user_id="42",
            origin=ToolOrigin.SUBAGENT,
            principal_type=PrincipalType.SUBAGENT,
            auth_source=AuthSource.CHANNEL,
            agent_context=context,
        )
    )
    handler = AsyncMock(return_value="不应执行")
    service.update(member.id, status="disabled", role="member", scopes=(), reviewer_id=1)
    allowed, result = asyncio.run(
        middleware.execute_tool_call(tool=SimpleNamespace(name="memory_read"), arguments={}, handler=handler)
    )
    assert allowed is False and result.status == "error"
    handler.assert_not_awaited()


def test_channel_binding_code_is_redacted_from_all_host_log_outputs(monkeypatch):
    """
    SDK 记录入站正文时，一次性关联码在文件与控制台出口前被隐藏。
    """
    from app.runtime.log import LoggerManager

    manager = LoggerManager()
    writer = Mock()
    console = Mock()
    monkeypatch.setattr(manager, "_write_file_log", writer)
    monkeypatch.setattr(manager, "_get_console_logger", lambda _: console)
    manager.info("收到消息：%s", "绑定账号 synthetic_binding_credential_12345")
    assert "synthetic_binding_credential_12345" not in str(writer.call_args)
    assert "synthetic_binding_credential_12345" not in str(console.info.call_args)
    assert "已隐藏" in str(writer.call_args)


def test_channel_task_tool_accepts_schedule_grant_and_keeps_instance_ownership(channel_authorization, db, monkeypatch):
    """
    获调度能力的普通成员能创建任务，任务与运行快照固定同一授权且跨实例不可见。
    """
    from app.agent.tools.impl.agent_task import AgentTaskTool
    from app.application import scheduling
    from app.db.adapters.agent import TransactionalAgentTaskRepository
    from app.db.models.agenttask import AgentTask
    from app.db.models.agenttaskrun import AgentTaskRun

    service, _, users = channel_authorization
    db.watermark(AgentTask, AgentTaskRun)
    first = _approve(service, scopes=("search", "schedule"))
    second = _approve(service, source="机器人B", scopes=("search", "schedule"))
    tasks = TransactionalAgentTaskRepository(SessionFactory)
    data = SimpleNamespace(users=users, tasks=tasks, chat=SimpleNamespace(get_sync=Mock(return_value=None)))
    tool = AgentTaskTool(session_id="channel-task", user_id="42", data=data)
    tool.set_message_attr(channel="Telegram", source="机器人A", username="成员")
    tool.set_agent_context({"authorization_id": first.id, "is_admin": False})
    monkeypatch.setattr(scheduling, "update_agent_task_job", lambda _: None)
    monkeypatch.setattr(
        "app.agent.tools.impl.agent_task.get_runtime_setting",
        lambda name: True if name == "AI_AGENT_ENABLE" else "Asia/Shanghai",
    )
    result = json.loads(
        asyncio.run(
            tool.run(action="create", name="渠道测试", content="查询公共资料", trigger_type="date", delay_minutes=10)
        )
    )
    task = tasks.get(task_id=result["id"], user_id=f"channel:{first.id}")
    assert task.execution_authorization_id == first.id and task.execution_user_id == first.user_id
    assert tasks.get(task_id=result["id"], user_id=f"channel:{second.id}") is None
    run = tasks.begin_run(task_id=task.id, trigger_source="manual")
    assert run.execution_authorization_id == first.id


def test_channel_download_pagination_filters_before_count_and_page(channel_authorization, db):
    """
    下载历史在数据库分页前按归属收窄，总数和页面都不泄露其它成员的记录。
    """
    from app.db.adapters.history.download import TransactionalDownloadHistoryRepository
    from app.db.models.downloadhistory import DownloadHistory

    service, _, _ = channel_authorization
    first = service.principal(_approve(service).id)
    second = service.principal(_approve(service, source="机器人B").id)
    db.add(
        DownloadHistory(
            path="/synthetic/first.mkv",
            type="电影",
            title="自己的下载",
            username=first.name,
            date="2026-11-16 10:00:00",
        ),
        DownloadHistory(
            path="/synthetic/second.mkv",
            type="电影",
            title="别人的下载",
            username=second.name,
            date="2026-11-17 10:00:00",
        ),
    )
    repository = TransactionalDownloadHistoryRepository(sync_session=SessionFactory, async_session=async_session_scope)
    records = asyncio.run(repository.async_list_by_page(1, 1, username=first.name))
    assert [record.title for record in records] == ["自己的下载"]
    assert asyncio.run(repository.async_count(username=first.name)) == 1


def test_scheduled_execution_uses_current_delivery_and_stops_after_revocation(channel_authorization, db, monkeypatch):
    """
    真实任务执行在渠道改名后仍私聊原成员，撤销能力和未确认旧身份均终结为失败。
    """
    from app.agent.manager import AgentManager
    from app.chain.agent import AgentChain
    from app.db.models.agenttask import AgentTask
    from app.db.models.agenttaskrun import AgentTaskRun
    from app.db.oper.agenttask import AgentTaskOper
    from app.runtime.config import settings

    service, configs, users = channel_authorization
    db.watermark(AgentTask, AgentTaskRun)
    member = _approve(service, scopes=("search", "schedule"))
    task = AgentTaskOper().add(
        name="已授权渠道任务",
        content="查询公共资料",
        trigger_type="cron",
        cron_expression="0 * * * *",
        user_id=f"channel:{member.id}",
        username="成员",
        session_id="channel-scheduled-test",
        channel="Telegram",
        source="机器人A",
        original_chat_id="旧群聊",
        execution_user_id=member.user_id,
        execution_user_identity=member.user_identity,
        execution_authorization_id=member.id,
    )
    process = AsyncMock(return_value="执行完成")
    manager = SimpleNamespace(_accepting_tasks=True, _data=SimpleNamespace(users=users), process_message=process)
    monkeypatch.setattr(settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr(AgentChain, "async_post_message", AsyncMock())
    configs[0].name = "已改名"
    assert asyncio.run(AgentManager.execute_scheduled_task(manager, task.id)) == (True, "执行完成")
    context = process.await_args.kwargs
    assert context["user_id"] == "42" and context["source"] == "已改名"
    assert context["channel"] == "Telegram" and context["original_chat_id"] is None
    assert context["authorization_id"] == member.id and context["execution_user_id"] == member.user_id
    process.reset_mock()
    service.update(member.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
    success, result = asyncio.run(AgentManager.execute_scheduled_task(manager, task.id))
    assert success is False and "schedule" in result
    process.assert_not_awaited()
    assert AgentTaskOper().get(task.id).last_status == "failed"
    AgentTaskOper().update(task.id, user_id=task.user_id, payload={"execution_authorization_id": None})
    success, result = asyncio.run(AgentManager.execute_scheduled_task(manager, task.id))
    assert success is False and "旧任务尚未确认渠道授权" in result
    process.assert_not_awaited()


def test_authorization_public_operations_match_the_audited_gateway_contract():
    """
    渠道权限的公共清单与 API 已审计清单精确一致，不能误将只读私密数据放行。
    """
    from app.agent.policy.api import API_UNBOUND_READ_OPERATIONS
    from app.domain.authorization import CHANNEL_PUBLIC_OPERATIONS

    assert CHANNEL_PUBLIC_OPERATIONS == API_UNBOUND_READ_OPERATIONS


def _authorization_app(user):
    """
    创建仅挂载真实授权路由的隔离应用，以合成用户替代网页登录依赖。
    """
    from fastapi import FastAPI

    from app.api.dependencies.auth import get_current_active_superuser, get_current_active_user
    from app.api.endpoints.authorization import router

    app = FastAPI()
    app.include_router(router, prefix="/authorization")
    app.dependency_overrides[get_current_active_superuser] = lambda: user
    app.dependency_overrides[get_current_active_user] = lambda: user
    return app


def test_direct_authorization_api_commits_then_replies_privately(channel_authorization, monkeypatch):
    """
    直接授权写入真实数据库后私发回执；渠道停用与成员不存在时不误发成功通知。
    """
    from fastapi.testclient import TestClient

    from app.api.endpoints import authorization as endpoint

    service, configs, _ = channel_authorization
    monkeypatch.setattr(
        endpoint, "get_notification_configs",
        lambda **options: [item for item in configs if options.get("include_disabled") or item.enabled],
    )
    sent = Mock()
    monkeypatch.setattr("app.chain.message.MessageChain", lambda: SimpleNamespace(post_message=sent))
    app = _authorization_app(SimpleNamespace(id=1, account_type="local", authorization_id=None))
    payload = {
        "notification_id": "telegram-a", "sender_id": " 42 ", "display_name": " 成员 ",
        "status": "approved", "role": "member", "scopes": ["search"],
    }
    with TestClient(app) as client:
        response = client.post("/authorization/", json=payload)
        assert response.status_code == 200
        result = response.json()["data"]
        member = service.find("Telegram", "机器人A", "42")
        assert result["id"] == member.id and result["display_name"] == "成员"
        assert service.principal(member.id).permissions["search"] is True
        message = sent.call_args.args[0]
        assert message.channel == NotificationChannel.Telegram and message.source == "机器人A"
        assert message.userid == "42" and message.private_delivery is True and message.save_history is False
        assert "重新发送" in message.title
        assert client.put(
            "/authorization/missing-member", json={"status": "approved", "scopes": []},
        ).status_code == 400
        sent.reset_mock()
        configs[0].enabled = False
        assert client.post("/authorization/", json=payload).status_code == 400
        assert client.post(
            "/authorization/", json={**payload, "notification_id": "missing-instance"},
        ).status_code == 400
        assert client.put(
            f"/authorization/{member.id}", json={"status": "disabled", "scopes": []},
        ).status_code == 200
        sent.assert_not_called()
        monkeypatch.setattr(endpoint, "get_channel_authorization", lambda: None)
        assert client.get("/authorization/").status_code == 503


def test_binding_api_requires_local_account_and_does_not_approve_member(channel_authorization, db):
    """
    关联端点仅为当前网页账号生成短时码，渠道主体不能生成码或借关联取得审批。
    """
    from fastapi.testclient import TestClient

    service, _, _ = channel_authorization
    local = db.add(User(name="binding-api-account", is_active=True, is_superuser=False))
    user = SimpleNamespace(id=local.id, account_type="local", authorization_id=None)
    with TestClient(_authorization_app(user)) as client:
        response = client.post("/authorization/binding")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["expires_at"] > 0 and len(data["code"]) >= 20
        member = service.bind("Telegram", "机器人A", "42", "成员", data["code"])
        assert member.status == "pending" and member.linked_user_id == local.id
        user.authorization_id = member.id
        assert client.post("/authorization/binding").status_code == 403
        user.authorization_id = None
        user.account_type = "channel"
        assert client.post("/authorization/binding").status_code == 403


@pytest.mark.parametrize("async_request", [False, True])
@pytest.mark.parametrize("state", ["approved", "disabled", "missing-operation", "unavailable"])
def test_current_user_rechecks_channel_claims_without_querying_web_user(
    channel_authorization, monkeypatch, async_request, state,
):
    """
    同步和异步依赖实时复核渠道签名，缺失服务或操作声明时拒绝且不回退网页登录用户。
    """
    from app.api.dependencies import auth as dependencies

    service, _, _ = channel_authorization
    member = _approve(service)
    user = service.principal(member.id)
    payload = TokenPayload(
        sub=user.id, username=user.name, super_user=False,
        agent_authorization_id=member.id, agent_operation="subscription.list",
    )
    repository = Mock()
    runtime = SimpleNamespace(authentication=SimpleNamespace(user_repository=repository))
    if state == "disabled":
        service.update(member.id, status="disabled", role="member", scopes=(), reviewer_id=1)
    elif state == "missing-operation":
        payload.agent_operation = None
    elif state == "unavailable":
        monkeypatch.setattr(dependencies, "get_channel_authorization", lambda: None)

    def current_user():
        """
        经被测同步或异步依赖解析当前签名主体。
        """
        if async_request:
            return asyncio.run(dependencies.get_current_user_async(db=None, token_data=payload, runtime=runtime))
        return dependencies.get_current_user(db=None, token_data=payload, runtime=runtime)

    if state == "approved":
        resolved = current_user()
        assert resolved.id == user.id and resolved.authorization_id == member.id
    else:
        with pytest.raises(HTTPException) as error:
            current_user()
        assert error.value.status_code == (403 if state == "disabled" else 401)
    repository.assert_not_called()


def test_channel_tokens_reject_missing_service_and_changed_identity(channel_authorization, monkeypatch):
    """
    已签发令牌不能在角色或账号声明改变后继续使用，服务未装配时也不能放行。
    """
    service, _, users = channel_authorization
    member = _approve(service)
    user = service.principal(member.id)
    auth = AuthService(users=users, config=Mock(), passkeys=Mock())
    payload = TokenPayload(
        sub=user.id, username=user.name, super_user=False,
        agent_authorization_id=member.id, agent_operation="subscription.list",
    )
    for values in ({"sub": user.id + 1}, {"username": "other-account"}, {"super_user": True}):
        with pytest.raises(PermissionError, match="身份或权限已改变"):
            auth.validate_token_identity(payload.model_copy(update=values))
    with pytest.raises(PermissionError, match="服务不可用"):
        auth.validate_token_identity(payload.model_copy(update={"agent_operation": None}))
    monkeypatch.setattr(authorization_module, "get_channel_authorization", lambda: None)
    with pytest.raises(PermissionError, match="服务不可用"):
        auth.validate_token_identity(payload)


def test_implicit_channel_source_must_resolve_one_instance(channel_authorization, monkeypatch):
    """
    兼容调用缺少来源名时只接受唯一配置，重复配置不能猜测实例或借旧名单恢复权限。
    """
    from app.application import notification
    from app.application.messaging.channel.admin import matches_channel_admin

    service, configs, _ = channel_authorization
    member = _approve(service, role="admin")
    config = {"TELEGRAM_ADMINS": "42", "TELEGRAM_CHAT_ID": "42"}
    configs[0].config = config
    monkeypatch.setattr(notification, "get_notification_configs", lambda: configs)
    assert matches_channel_admin(NotificationChannel.Telegram, config, None, "42")
    configs[1].config = config.copy()
    assert not matches_channel_admin(NotificationChannel.Telegram, config, "42")
    configs[1].config = {}
    service.update(member.id, status="disabled", role="admin", scopes=(), reviewer_id=1)
    assert not matches_channel_admin(NotificationChannel.Telegram, config, "42")


def test_channel_connection_probe_reports_first_unready_instance(monkeypatch):
    """
    消息渠道连接探测聚合客户端就绪状态，无实例不误报失败且保留首个故障说明。
    """
    from app.modules.telegram.module import TelegramModule

    module = TelegramModule()
    monkeypatch.setattr(module, "get_instances", lambda: {})
    assert module.test() is None
    ready = SimpleNamespace(get_state=lambda: True)
    unavailable = SimpleNamespace(get_state=lambda: False)
    monkeypatch.setattr(module, "get_instances", lambda: {"就绪实例": ready})
    assert module.test() == (True, "")
    monkeypatch.setattr(module, "get_instances", lambda: {"就绪实例": ready, "故障实例": unavailable})
    state, message = module.test()
    assert state is False and "故障实例" in message
    monkeypatch.setattr(module, "_test_connection", lambda _: (False, "连接失败"))
    assert "连接失败" in module.test()[1]


@pytest.mark.parametrize(
    ("tool_name", "operation", "allowed"),
    [
        ("moviepilot_api", "media.search", True),
        ("moviepilot_api", "site.list", False),
        ("send_message", None, True),
        ("send_voice_message", None, True),
        ("memory_read", None, False),
    ],
)
def test_unapproved_channel_tools_allow_only_public_reads_and_replies(tool_name, operation, allowed):
    """
    未批准渠道不能借普通工具或非公开 API 执行业务，仅公开资料与消息回执例外。
    """
    from app.agent.middleware.policy import AgentPolicyMiddleware
    from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
    from app.agent.tools.impl.api import MoviePilotApiTool

    tool = (
        MoviePilotApiTool(session_id="unapproved", user_id="42")
        if tool_name == "moviepilot_api" else SimpleNamespace(name=tool_name)
    )
    middleware = AgentPolicyMiddleware(context=ToolPolicyContext(
        session_id="unapproved", user_id="42", origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN, auth_source=AuthSource.CHANNEL,
        agent_context={"channel_authorization_required": True},
    ))
    handler = AsyncMock(return_value="公开结果")
    accepted, result = asyncio.run(middleware.execute_tool_call(
        tool=tool, arguments={"operation_id": operation} if operation else {},
        handler=handler, enforce_decision=False,
    ))
    assert accepted is allowed
    if allowed:
        assert result == "公开结果"
        handler.assert_awaited_once()
    else:
        assert result.status == "error" and "申请使用" in result.content
        handler.assert_not_awaited()


@pytest.mark.parametrize("extension", ["plugin", "mcp"])
def test_channel_middleware_rechecks_role_before_extension_tools(channel_authorization, extension):
    """
    扩展工具执行前重读渠道角色，普通成员不可调用，即使中间件保留过管理员快照。
    """
    from app.agent.mcp import AgentMcpServerConfig, AgentMcpToolSpec
    from app.agent.middleware.policy import AgentPolicyMiddleware
    from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
    from app.agent.tools.impl.mcp import McpExternalTool

    service, _, _ = channel_authorization
    member = _approve(service, role="admin")
    context = {"authorization_id": member.id, "is_admin": True}
    middleware = AgentPolicyMiddleware(context=ToolPolicyContext(
        session_id="extension", user_id="42", origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN, auth_source=AuthSource.CHANNEL, agent_context=context,
    ))
    tool = SimpleNamespace(name="extension-tool", _agent_tool_source="plugin:example")
    if extension == "mcp":
        tool = McpExternalTool(
            AgentMcpToolSpec(
                server=AgentMcpServerConfig(id="offline", name="测试服务"),
                name="read", agent_tool_name="offline_read", description="离线工具", input_schema={"type": "object"},
            ),
            session_id="extension", user_id="42",
        )
    handler = AsyncMock(return_value="授权结果")
    accepted, result = asyncio.run(middleware.execute_tool_call(
        tool=tool, arguments={}, handler=handler, enforce_decision=False,
    ))
    assert accepted and result == "授权结果"
    handler.reset_mock()
    service.update(member.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
    accepted, result = asyncio.run(middleware.execute_tool_call(
        tool=tool, arguments={}, handler=handler, enforce_decision=False,
    ))
    assert not accepted and "渠道管理员" in result.content and context["is_admin"] is False
    handler.assert_not_awaited()


def test_channel_middleware_fails_closed_without_service(channel_authorization, monkeypatch):
    """
    工具中间件在服务未装配时拒绝执行，不因已有授权 ID 和管理员快照而放行。
    """
    from app.agent.middleware.policy import AgentPolicyMiddleware
    from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext

    service, _, _ = channel_authorization
    member = _approve(service)
    middleware = AgentPolicyMiddleware(context=ToolPolicyContext(
        session_id="unavailable", user_id="42", origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN, auth_source=AuthSource.CHANNEL,
        agent_context={"authorization_id": member.id, "is_admin": True},
    ))
    monkeypatch.setattr(authorization_module, "get_channel_authorization", lambda: None)
    handler = AsyncMock()
    accepted, result = asyncio.run(middleware.execute_tool_call(
        tool=SimpleNamespace(name="memory_read"), arguments={}, handler=handler,
    ))
    assert not accepted and "服务不可用" in result.content
    handler.assert_not_awaited()


@pytest.mark.parametrize("text", ["你好 李焕英", "搜索 你好李焕英"])
def test_approved_member_can_use_traditional_media_interaction_when_agent_disabled(
    channel_authorization, monkeypatch, text,
):
    """
    未启用 AI Agent 时，已批准普通成员发传统媒体文本应进入受控媒体交互，不报错 Agent 未启用且隔离旧管理会话。
    """
    from dataclasses import replace

    from app.application.messaging.site import site_interaction_manager

    service, configs, _ = channel_authorization
    configs.append(NotificationConf(id="wechat-app", name="企微应用", type="wechat", enabled=True))
    member = service.request(NotificationChannel.Wechat.value, "企微应用", "wx_member", "微信成员")
    service.update(member.id, status="approved", role="member", scopes=("search", "subscribe", "download"), reviewer_id=1)
    principal = service.principal(member.id)

    chain = MessageChain()
    monkeypatch.setattr(
        chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=False, ai_agent_global=False)
    )
    media_handle = Mock(return_value=True)
    site_handle = Mock(return_value=True)
    plugin_handler = Mock()
    post = Mock()
    monkeypatch.setattr("app.chain.interaction.MediaInteractionChain.handle_text_interaction", media_handle)
    monkeypatch.setattr("app.chain.site.SiteChain.handle_text_interaction", site_handle)
    monkeypatch.setattr(chain, "_plugin_input_interaction_handler", lambda: plugin_handler)
    monkeypatch.setattr(chain, "post_message", post)
    monkeypatch.setattr(chain, "_mark_message_processing_started", Mock(return_value=None))
    monkeypatch.setattr(chain, "_mark_message_processing_finished", Mock())

    # 模拟该成员被降权前遗留的站点管理会话，普通成员回复不得再进入该管理会话。
    site_request = site_interaction_manager.create_or_replace(
        user_id="wx_member", command="/sites", channel=NotificationChannel.Wechat, source="企微应用", username="微信成员",
    )
    try:
        chain.handle_message(NotificationChannel.Wechat, "企微应用", "wx_member", "微信成员", text)
        media_handle.assert_called_once()
        kwargs = media_handle.call_args.kwargs
        assert kwargs["text"] == text
        assert kwargs["owner_username"] == principal.name
        assert kwargs["allowed_scopes"]["search"] is True
        assert kwargs["allowed_scopes"]["subscribe"] is True
        assert kwargs["allowed_scopes"]["download"] is True
        site_handle.assert_not_called()
        plugin_handler.handle_text.assert_not_called()
        post.assert_not_called()

        # 显式 /ai 仍提示 AI Agent 未启用，而普通非媒体文本不误报未启用。
        chain.handle_message(NotificationChannel.Wechat, "企微应用", "wx_member", "微信成员", "/ai 查一下")
        assert post.call_count == 1
        assert "AI Agent 未启用" in post.call_args.args[0].title

        post.reset_mock()
        media_handle.reset_mock()
        media_handle.return_value = False
        chain.handle_message(NotificationChannel.Wechat, "企微应用", "wx_member", "微信成员", "帮我推荐一部电影")
        media_handle.assert_called_once()
        post.assert_not_called()
    finally:
        site_interaction_manager.remove(site_request.request_id)


def test_approved_member_media_callback_and_noai_routing_preserve_admin_boundary(
    channel_authorization, monkeypatch,
):
    """
    普通成员可使用传统媒体按钮回调、非全局模式及 /noai 传统交互，但管理回调与斜杠命令仍被拦截。
    """
    from dataclasses import replace

    from app.application.messaging.media import media_interaction_manager

    service, _, _ = channel_authorization
    member = _approve(service, scopes=("search", "download"))
    principal = service.principal(member.id)

    chain = MessageChain()
    monkeypatch.setattr(
        chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=False, ai_agent_global=False)
    )
    media_callback = Mock(return_value=True)
    media_text = Mock(return_value=True)
    agent = Mock(return_value=False)
    post = Mock()
    monkeypatch.setattr("app.chain.interaction.MediaInteractionChain.handle_callback_interaction", media_callback)
    monkeypatch.setattr("app.chain.interaction.MediaInteractionChain.handle_text_interaction", media_text)
    monkeypatch.setattr(chain, "_handle_ai_message", agent)
    monkeypatch.setattr(chain, "post_message", post)
    monkeypatch.setattr(chain, "_mark_message_processing_started", Mock(return_value=None))
    monkeypatch.setattr(chain, "_mark_message_processing_finished", Mock())

    # 传统媒体按钮回调放行并携带实时权限和业务归属。
    chain.handle_message(
        NotificationChannel.Telegram, "机器人A", "42", "昵称", "", callback_data="media:req1:select:1"
    )
    media_callback.assert_called_once()
    assert media_callback.call_args.kwargs["callback_data"] == "media:req1:select:1"
    assert media_callback.call_args.kwargs["owner_username"] == principal.name
    assert media_callback.call_args.kwargs["allowed_scopes"]["download"] is True
    assert media_callback.call_args.kwargs["allowed_scopes"]["subscribe"] is False

    # 管理类回调和传统斜杠命令仍仅管理员可用。
    chain.handle_message(
        NotificationChannel.Telegram, "机器人A", "42", "昵称", "", callback_data="sites:req1:refresh"
    )
    assert "管理员" in post.call_args.args[0].title
    post.reset_mock()
    chain.handle_message(NotificationChannel.Telegram, "机器人A", "42", "昵称", "/sites")
    assert "管理员" in post.call_args.args[0].title

    # 全局 Agent 开启时，/noai 与已有活跃媒体会话的后续选择走受控传统媒体交互。
    monkeypatch.setattr(
        chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=True, ai_agent_global=True)
    )
    chain.handle_message(NotificationChannel.Telegram, "机器人A", "42", "昵称", "/noai 搜索 流浪地球")
    media_text.assert_called_once()
    assert media_text.call_args.kwargs["text"] == "搜索 流浪地球"
    agent.assert_not_called()

    media_text.reset_mock()
    pending = media_interaction_manager.create_or_replace(
        user_id="42", channel=NotificationChannel.Telegram, source="机器人A", username="昵称",
        action="Search", keyword="流浪地球", title="流浪地球",
    )
    try:
        chain.handle_message(NotificationChannel.Telegram, "机器人A", "42", "昵称", "1")
        media_text.assert_called_once()
        assert media_text.call_args.kwargs["text"] == "1"
        agent.assert_not_called()
    finally:
        media_interaction_manager.remove(pending.request_id)

    # 启用 Agent 但未开全局模式时，未命中媒体交互的普通闲聊提示使用 /ai。
    post.reset_mock()
    media_text.reset_mock()
    media_text.return_value = False
    monkeypatch.setattr(
        chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=True, ai_agent_global=False)
    )
    chain.handle_message(NotificationChannel.Telegram, "机器人A", "42", "昵称", "帮我推荐一部科幻片")
    assert "请使用 /ai" in post.call_args.args[0].title


def test_traditional_media_interaction_enforces_member_scopes_and_owner_username(monkeypatch):
    """
    受控传统媒体交互严格校验搜索、订阅、下载能力，并按授权归属记录下载与订阅用户名。
    """
    from app.application.messaging.media import media_interaction_manager
    from app.chain.interaction import MediaInteractionChain
    from app.domain.context import Context, MediaInfo
    from app.domain.meta.metavideo import MetaVideo

    interaction = MediaInteractionChain()
    post = Mock()
    media_search = Mock(return_value=(MetaVideo("你好李焕英"), [MediaInfo(title="你好，李焕英", year="2021")]))
    download_single = Mock()
    subscribe_add = Mock()
    monkeypatch.setattr(interaction, "post_message", post)
    monkeypatch.setattr("app.chain.interaction.MediaChain.search", media_search)
    monkeypatch.setattr("app.chain.interaction.DownloadChain.download_single", download_single)
    monkeypatch.setattr("app.chain.interaction.DownloadChain.get_no_exists_info", Mock(return_value=(False, {})))
    monkeypatch.setattr("app.chain.music_interaction.subscribe.SubscribeChain.add", subscribe_add)

    # 缺少 search 能力时拒绝搜索。
    assert interaction.handle_text_interaction(
        channel=NotificationChannel.Wechat, source="企微应用", userid="u1", username="昵称",
        text="搜索 你好李焕英", allowed_scopes={"search": False, "subscribe": True, "download": False},
        owner_username="channel_owner",
    ) is True
    media_search.assert_not_called()
    assert "搜索" in post.call_args.args[0].title

    # 缺少 subscribe 能力时拒绝订阅。
    post.reset_mock()
    assert interaction.handle_text_interaction(
        channel=NotificationChannel.Wechat, source="企微应用", userid="u1", username="昵称",
        text="订阅 你好李焕英", allowed_scopes={"search": True, "subscribe": False, "download": False},
        owner_username="channel_owner",
    ) is True
    media_search.assert_not_called()
    assert "订阅" in post.call_args.args[0].title

    # 缺少 download 能力时阻止下载，具备 download 能力时使用授权归属用户名下载。
    post.reset_mock()
    torrent_ctx = Context(media_info=MediaInfo(title="你好，李焕英", year="2021"))
    request = media_interaction_manager.create_or_replace(
        user_id="u1", channel=NotificationChannel.Wechat, source="企微应用", username="昵称",
        action="Search", keyword="你好李焕英", title="你好，李焕英",
        meta=MetaVideo("你好李焕英"), items=[torrent_ctx],
    )
    request.phase = "torrent"
    monkeypatch.setattr(interaction, "_get_download_dirs", Mock(return_value=[]))
    try:
        assert interaction.handle_text_interaction(
            channel=NotificationChannel.Wechat, source="企微应用", userid="u1", username="昵称",
            text="1", allowed_scopes={"search": True, "subscribe": False, "download": False},
            owner_username="channel_owner",
        ) is True
        download_single.assert_not_called()
        assert "下载" in post.call_args.args[0].title

        assert interaction.handle_text_interaction(
            channel=NotificationChannel.Wechat, source="企微应用", userid="u1", username="昵称",
            text="1", allowed_scopes={"search": True, "subscribe": False, "download": True},
            owner_username="channel_owner",
        ) is True
        download_single.assert_called_once()
        assert download_single.call_args.kwargs["username"] == "channel_owner"
    finally:
        media_interaction_manager.remove(request.request_id)

    # 具备 subscribe 能力时，订阅归属记录为授权用户名而非平台昵称。
    media_item = MediaInfo(title="你好，李焕英", year="2021")
    sub_request = media_interaction_manager.create_or_replace(
        user_id="u1", channel=NotificationChannel.Wechat, source="企微应用", username="昵称",
        action="Subscribe", keyword="你好李焕英", title="你好，李焕英",
        meta=MetaVideo("你好李焕英"), items=[media_item],
    )
    try:
        assert interaction.handle_text_interaction(
            channel=NotificationChannel.Wechat, source="企微应用", userid="u1", username="昵称",
            text="1", allowed_scopes={"search": False, "subscribe": True, "download": False},
            owner_username="channel_owner",
        ) is True
        subscribe_add.assert_called_once()
        assert subscribe_add.call_args.kwargs["username"] == "channel_owner"
    finally:
        media_interaction_manager.remove(sub_request.request_id)
