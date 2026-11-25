"""
统一渠道授权的身份隔离、实时撤销、API 令牌约束与审批交互回归。
"""

import asyncio
import json
import time
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.agent.api.executor import ApiExecutionContext, ApiExecutionError, MoviePilotApiExecutor
from app.agent.identity import AgentIdentityError, require_execution_user, resolve_task_user
from app.application.messaging.plugin import plugin_input_interaction_manager
from app.application.security import authorization as authorization_module
from app.application.security.auth import AuthService
from app.application.security.authorization import ChannelAuthorizationService, scoped_admission
from app.chain.message import MessageChain
from app.db.adapters.agent import TransactionalAgentTaskRepository
from app.db.adapters.authorization import authorization_scope
from app.db.adapters.user import TransactionalUserRepository
from app.db.models.agenttask import AgentTask
from app.db.models.agenttaskrun import AgentTaskRun
from app.db.models.authorization import ChannelAuthorization, ChannelBinding, ChannelRuleTaskGrant
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.message import Message as MessageHistory
from app.db.models.outbox import OutboxMessage
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.user import User
from app.db.session import SessionFactory, async_session_scope
from app.domain.admission import AdmissionProof
from app.schemas.channel import AdmissionRule, InteractionPolicy
from app.schemas.message import IncomingMessage, Message
from app.schemas.system import NotificationConf
from app.schemas.token import TokenPayload
from app.schemas.types import EventType, NotificationChannel


@pytest.fixture
def channel_authorization(db, monkeypatch):
    """
    装配真实临时数据库授权服务，恢复进程服务并清理新增成员、内部账号及消息历史。

    :param db: 共享隔离库的水位清理工具
    :param monkeypatch: 当前用例的配置函数替换工具
    :yields: 授权服务、启用渠道配置及真实用户仓储
    """
    db.watermark(User, OutboxMessage, MessageHistory)
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
            new_member_ids = select(ChannelAuthorization.id)
            if before:
                new_member_ids = new_member_ids.where(ChannelAuthorization.id.not_in(before))
            session.execute(delete(ChannelRuleTaskGrant).where(ChannelRuleTaskGrant.member_id.in_(new_member_ids)))
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


@pytest.mark.parametrize("entry", ["selection", "automatic", "api", "trusted"])
def test_download_revoked_during_preparation_is_never_submitted(channel_authorization, monkeypatch, entry):
    """
    使用真实成员仓储，在准备资源后、提交下载器前停用发起者。
    """
    from app.api.endpoints import download as download_api
    from app.application.messaging.media import media_interaction_manager
    from app.chain.download import DownloadChain
    from app.chain.interaction import MediaInteractionChain
    from app.domain.admission import AdmissionProof
    from app.schemas.channel import InteractionPolicy
    from app.schemas.context import TorrentInfo
    from tests.test_media_interaction import _build_context

    service, configs, _ = channel_authorization
    channel, source, chat_id = NotificationChannel.Telegram, "机器人A", "-100"
    member = None
    proof = None
    now = 1000.0
    if entry == "trusted":
        channel, source, chat_id = NotificationChannel.Wechat, "可信群", "group:test"
        configs.append(NotificationConf(id="wechat-trusted", name=source, type="wechat", enabled=True,
                                        interaction_policy=InteractionPolicy(
                                            connection_id="wechat-trusted", identity_namespace="wechat-trusted",
                                            enabled=True, revision=2,
                                            admission="manual_rules",
                                            admission_rules=(AdmissionRule(kind="group", target="test", scopes=("download",)),),
                                        )))
        proof = AdmissionProof("wechat-trusted", 2, "42", "test", 1060.0)
        monkeypatch.setattr(authorization_module, "time", SimpleNamespace(time=lambda: now))
    else:
        member = _approve(service)
    sample = _build_context()
    assert sample.media_info and sample.torrent_info and sample.meta_info
    submit = Mock(return_value=(None, None, None, "测试下载器"))
    monkeypatch.setattr(DownloadChain, "download", submit)
    monkeypatch.setattr(DownloadChain, "_record_rejected_download", Mock())
    monkeypatch.setattr(DownloadChain, "_prepare_batch_download_contexts", lambda _, contexts, **kwargs: (contexts, {}))
    monkeypatch.setattr(DownloadChain, "_build_download_failure_fingerprint", lambda *_: None)
    monkeypatch.setattr(DownloadChain, "_has_completed_music_download", lambda *_: False)

    def prepare(_chain, **_kwargs):
        """
        资源已经准备完成，发起者此时被停用或事件证明刚好过期。
        """
        nonlocal now
        if member is None:
            now = 1060.0
        else:
            service.update(member.id, status="disabled", role="member", scopes=(), reviewer_id=1)
        return SimpleNamespace(media=sample.media_info, torrent=sample.torrent_info,
                               torrent_content="magnet:fixture", download_dir=None, site_downloader=None), None

    monkeypatch.setattr(DownloadChain, "_prepare_download_single", prepare)
    if entry == "api":
        assert member is not None
        monkeypatch.setattr(download_api, "_resolve_add_media", lambda *_: (sample.meta_info, sample.media_info, None))
        with pytest.raises(HTTPException, match="停用") as response:
            download_api.add(
                TorrentInfo.model_validate(sample.torrent_info.to_dict()),
                current_user=service.authorize_operation(member.id, "download.add"),
            )
        assert response.value.status_code == 403
    else:
        chain = MediaInteractionChain()
        replies = Mock()
        monkeypatch.setattr(chain, "post_message", replies)
        monkeypatch.setattr(chain, "_prompt_download_dir_selection", Mock(return_value=False))
        request = media_interaction_manager.create_or_replace(
            user_id="42", channel=channel, source=source, username="测试成员",
            original_chat_id=chat_id, action="Search", keyword="星际穿越", title="星际穿越",
            meta=sample.meta_info, items=[sample],
        )
        request.phase = "torrent"
        request.current_media = sample.media_info
        try:
            with authorization_module.scoped_admission(proof):
                if entry == "selection":
                    chain._handle_torrent_selection(request, 1, channel, source, "42", "测试成员")
                else:
                    chain._auto_download(request, [sample], channel, source, "42", "测试成员", no_exists={})
            reply = replies.call_args.args[0]
            assert "授权" in reply.title and reply.original_chat_id == chat_id
        finally:
            media_interaction_manager.clear()
    saved = service.find(channel.value, source, "42")
    assert saved is not None and saved.status == ("pending" if entry == "trusted" else "disabled")
    submit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["traditional", "fallback", "agent_api"])
async def test_subscription_revoked_during_recognition_is_not_written(channel_authorization, monkeypatch, entry):
    """
    在识别媒体时停用实际成员，覆盖传统订阅、自动择优补订阅及渠道 Agent API。
    """
    from app.api.endpoints.subscribe import create_subscribe
    from app.chain.download import DownloadChain
    from app.chain.interaction import MediaInteractionChain
    from app.chain.subscribe import create
    from app.chain.subscribe.facade import SubscribeChain
    from app.db.worker import DatabaseWorker
    from app.schemas.subscribe import Subscribe as SubscribeRequest
    from tests.test_media_interaction import _build_context

    service, _, _ = channel_authorization
    member = _approve(service)
    user = service.authorize_operation(member.id, "subscription.add")
    sample = _build_context()
    media = sample.media_info
    assert media is not None and sample.meta_info is not None
    chain = MediaInteractionChain()
    reply = Mock()
    monkeypatch.setattr(chain, "post_message", reply)

    def recognize(_owner, context):
        """
        媒体识别完成时模拟审批状态的并发变化。
        """
        context.mediainfo = media
        service.update(member.id, status="disabled", role="member", scopes=(), reviewer_id=1)

    async def async_recognize(owner, context):
        """
        在异步 API 的媒体识别阶段触发同一状态变化。
        """
        await asyncio.to_thread(recognize, owner, context)

    write = Mock(return_value=(987, ""))
    async_write = AsyncMock(return_value=(987, ""))
    monkeypatch.setattr(create, "find_existing_subscription", Mock(return_value=None))
    monkeypatch.setattr(create, "async_find_existing_subscription", AsyncMock(return_value=None))
    monkeypatch.setattr(create, "add_subscribe", write)
    monkeypatch.setattr(create, "async_add_subscribe", async_write)
    monkeypatch.setattr(SubscribeChain, "_SubscribeChain__build_subscribe_notification", Mock(return_value=None))
    monkeypatch.setattr(SubscribeChain, "_SubscribeChain__recognize_subscribe_media", recognize)
    monkeypatch.setattr(SubscribeChain, "_SubscribeChain__async_recognize_subscribe_media", async_recognize)
    monkeypatch.setattr(SubscribeChain, "_SubscribeChain__prepare_subscribe_episodes", Mock(return_value=None))
    monkeypatch.setattr(SubscribeChain, "_SubscribeChain__async_prepare_subscribe_episodes", AsyncMock(return_value=None))
    monkeypatch.setattr(SubscribeChain, "_SubscribeChain__finalize_subscribe_create_context", Mock())
    monkeypatch.setattr(SubscribeChain, "_SubscribeChain__async_finalize_subscribe_create_context", AsyncMock())
    monkeypatch.setattr(DownloadChain, "get_no_exists_info", Mock(return_value=(False, {})))
    monkeypatch.setattr(DownloadChain, "batch_download", Mock(return_value=([], {})))
    if entry == "agent_api":
        worker = DatabaseWorker(max_workers=1, capacity=1, reserve_interactive=False)
        await worker.start()
        monkeypatch.setattr(service, "_executor", worker)
        try:
            with pytest.raises(HTTPException, match="停用") as response:
                await create_subscribe(subscribe_in=SubscribeRequest(name="星际穿越", year="2014", type="电影"),
                                       current_user=user)
        finally:
            await worker.shutdown()
        assert response.value.status_code == 403
    else:
        from app.chain.music_interaction.subscribe import subscribe_media

        channel = NotificationChannel.Telegram
        request = SimpleNamespace(action="Subscribe", current_media=media, meta=sample.meta_info,
                                  original_chat_id="-100", keyword="星际穿越")
        if entry == "fallback":
            chain._auto_download(request, [sample], channel, "机器人A", "42", "测试成员", no_exists={})
        else:
            subscribe_media(chain, request, media, channel, "机器人A", "42", "测试成员")
        assert reply.call_args.args[0].original_chat_id == "-100"
    assert service.find("Telegram", "机器人A", "42").status == "disabled"
    write.assert_not_called()
    async_write.assert_not_awaited()


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
    dispatch = Mock(return_value=False)
    monkeypatch.setattr(chain, "post_message", post)
    monkeypatch.setattr(chain, "_handle_message_core", dispatch)
    assert chain._handle_channel_authorization(
        NotificationChannel.Telegram, "机器人A", "42", "成员", "订阅某部电影", None
    )
    assert post.call_args.args[0].buttons[0][0]["callback_data"] == "authorization:apply"
    assert chain._handle_channel_authorization(
        NotificationChannel.Telegram, "机器人A", "42", "成员", "", "authorization:apply"
    )
    member = service.find("Telegram", "机器人A", "42")
    assert member.status == "pending"
    assert post.call_count == 3
    service.update(member.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
    dispatch.assert_not_called()
    assert post.call_count == 3


def test_channel_authorization_requires_web_login(monkeypatch):
    """
    固定 API 凭据和已关联的渠道令牌不能管理授权或领取网页账号关联码。
    """
    from app.api.dependencies.auth import get_current_user
    from app.api.endpoints import authorization as endpoint

    app = FastAPI()
    app.include_router(endpoint.router, prefix="/authorization")
    principal = SimpleNamespace(id=17, is_active=True, is_superuser=True, account_type="local", authorization_id=None)
    app.dependency_overrides[get_current_user] = lambda: principal
    service = Mock()
    service.list_members.return_value = []
    service.create_binding.return_value = ("fixture-code", 123456)
    monkeypatch.setattr(endpoint, "_service", lambda: service)
    monkeypatch.setattr(endpoint, "get_notification_configs", lambda **_: [])

    with TestClient(app) as client:
        web_login = {"Authorization": "Bearer fixture"}
        assert client.get("/authorization/", headers={"X-API-Key": "fixture"}).status_code == 403
        assert client.post("/authorization/binding", params={"token": "fixture"}).status_code == 403
        principal.authorization_id = "linked-channel"
        assert client.get("/authorization/", headers=web_login).status_code == 403
        assert client.post("/authorization/binding", headers=web_login).status_code == 403
        service.list_members.assert_not_called()
        service.create_binding.assert_not_called()

        principal.authorization_id = None
        assert client.get("/authorization/", headers=web_login).status_code == 200
        principal.is_superuser = False
        assert client.get("/authorization/", headers=web_login).status_code == 403
        assert client.post("/authorization/binding", headers=web_login).status_code == 200
        service.create_binding.assert_called_once_with(principal.id)
        service.create_binding.side_effect = ValueError("关联账号已失效，请重新登录后生成关联码")
        assert client.post("/authorization/binding", headers=web_login).status_code == 403


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_channel_member_text_cannot_reenter_legacy_admin_sessions(channel_authorization, monkeypatch, status):
    """
    普通及待审批成员显式请求 Agent 时绕过旧插件输入会话。
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
    chain.handle_message(NotificationChannel.Telegram, "机器人A", "42", "昵称", "/ai 继续操作", is_channel_admin=True)
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


@pytest.mark.parametrize(("text", "callback"), [
    ("1", None), ("", "sites:fixture:select:1"), ("", "subscribes:fixture:select:1"),
    ("", "skills:fixture:select:1"), ("", "update:fixture:confirm"), ("", "[PLUGIN]fixture:select"),
])
def test_manual_revocation_stops_existing_admin_interactions_before_dispatch(
    channel_authorization, monkeypatch, text, callback,
):
    """
    实际授权仓储停用成员后，原群的旧数字输入和按钮不能进入传统交互或插件入口。
    """
    service, _, _ = channel_authorization
    member = _approve(service, role="admin", scopes=("search",))
    chain = MessageChain()
    dispatch = Mock(return_value=False)
    reply = Mock()
    monkeypatch.setattr(chain, "_handle_message_core", dispatch)
    monkeypatch.setattr(chain, "_mark_message_processing_started", Mock(return_value=None))
    monkeypatch.setattr(chain, "_mark_message_processing_finished", Mock())
    monkeypatch.setattr(chain, "post_message", reply)

    def send_old_interaction():
        """
        保持原消息和调用上下文，只改变数据库中的当前授权。
        """
        chain.handle_message(NotificationChannel.Telegram, "机器人A", "42", "成员", text,
                             callback_data=callback, original_chat_id="-100", is_channel_admin=True)

    with SessionFactory() as session:
        history_before = set(session.scalars(select(MessageHistory.id)))
    send_old_interaction()
    dispatch.assert_called_once()
    with SessionFactory() as session:
        accepted_history = set(session.scalars(select(MessageHistory.id))) - history_before
    assert len(accepted_history) == (1 if callback is None else 0)
    service.update(member.id, status="disabled", role="admin", scopes=(), reviewer_id=1)
    send_old_interaction()
    dispatch.assert_called_once()
    assert reply.call_args.args[0].original_chat_id == "-100"
    with SessionFactory() as session:
        assert set(session.scalars(select(MessageHistory.id))) - history_before == accepted_history


def test_member_can_search_select_download_and_subscribe_without_ai(channel_authorization, monkeypatch):
    """
    已批准的普通成员可从原群进入传统业务，最终写入以实际授权主体为准。
    """
    from dataclasses import replace

    from app.application.messaging.media import media_interaction_manager
    from app.chain.interaction import MediaInteractionChain
    from tests.test_media_interaction import _build_context

    service, _, _ = channel_authorization
    member = _approve(service)
    owner = service.principal(member.id)
    chain = MessageChain()
    media = MediaInteractionChain()
    monkeypatch.setattr(chain, "runtime_config", replace(chain.runtime_config, ai_agent_enable=False))
    monkeypatch.setattr(chain, "_record_user_message", Mock())
    monkeypatch.setattr(chain, "_mark_message_processing_started", Mock(return_value=None))
    monkeypatch.setattr(chain, "_mark_message_processing_finished", Mock())
    agent = Mock()
    monkeypatch.setattr(chain, "_handle_ai_message", agent)
    monkeypatch.setattr(chain, "post_message", Mock())
    monkeypatch.setattr(MediaInteractionChain, "post_message", Mock())
    medias = Mock()
    torrents = Mock()
    monkeypatch.setattr(MediaInteractionChain, "post_medias_message", medias)
    monkeypatch.setattr(MediaInteractionChain, "post_torrents_message", torrents)
    sample = _build_context()
    monkeypatch.setattr("app.chain.media.MediaChain.search", Mock(return_value=(sample.meta_info, [sample.media_info])))
    monkeypatch.setattr("app.chain.interaction.SearchChain.process", Mock(return_value=[sample]))
    monkeypatch.setattr("app.chain.interaction.TorrentHelper.sort_torrents", lambda _, results: results)
    monkeypatch.setattr("app.chain.interaction.DownloadChain.get_no_exists_info", Mock(return_value=(False, {})))
    monkeypatch.setattr(MediaInteractionChain, "_get_download_dirs", Mock(return_value=[]))
    download = Mock()
    subscribe = Mock()
    monkeypatch.setattr("app.chain.interaction.DownloadChain.download_single", download)
    monkeypatch.setattr("app.chain.subscribe.facade.SubscribeChain.add", subscribe)
    channel = NotificationChannel.Telegram

    def send(text="", *, callback=None):
        """
        从普通成员的原群提交文本或由界面生成的回调。
        """
        chain.handle_message(channel, "机器人A", "42", "群昵称", text, original_chat_id="-100", callback_data=callback)

    try:
        send("搜索 星际穿越")
        medias.assert_called_once()
        assert medias.call_args.args[0].original_chat_id == "-100"
        send("1")
        torrents.assert_called_once()
        callback = next(button["callback_data"] for row in torrents.call_args.args[0].buttons for button in row
                        if media.parse_callback(button["callback_data"])[1:] == ("download", 1))
        send(callback=callback)
        download.assert_called_once()
        assert download.call_args.kwargs["username"] == owner.name
        send("订阅 星际穿越")
        send("1")
        subscribe.assert_called_once()
        assert subscribe.call_args.kwargs["owner_identity"] == owner.notification_identity
        assert subscribe.call_args.kwargs["username"] == owner.name
        agent.assert_not_called()
        assert not owner.is_superuser
    finally:
        request = media_interaction_manager.get_by_user("42", channel, "机器人A", "-100")
        if request is not None:
            media_interaction_manager.remove(request.request_id)


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


@pytest.mark.parametrize("conflict", [False, True])
def test_binding_moves_exact_business_identity_atomically(channel_authorization, db, conflict):
    """
    关联只迁移该成员自己的稳定归属。目标已有相同订阅时保留旧记录和关联码。
    """
    service, _, users = channel_authorization
    local = db.add(User(name="binding-owner-target", is_active=True))
    member = service.request("Telegram", "机器人A", "42", "成员")
    previous = users.get_by_id(member.user_id)
    assert previous is not None
    media = {"name": "旧订阅", "type": "电影", "media_source": "themoviedb", "media_id": "123"}
    active = db.add(Subscribe(**media, username="更名后的显示名", owner_identity=previous.notification_identity))
    history = db.add(SubscribeHistory(name="历史", username=previous.name, owner_identity=previous.notification_identity))
    download = db.add(DownloadHistory(path="/synthetic/file", type="电影", title="下载", owner_identity=previous.notification_identity))
    legacy = db.add(Subscribe(name="待人工核对", username=previous.name))
    if conflict:
        duplicate = db.add(Subscribe(**media, username=local.name, owner_identity=local.notification_identity))
    code, _ = service.create_binding(local.id)

    if conflict:
        with pytest.raises(ValueError, match="业务归属冲突"):
            service.bind("Telegram", "机器人A", "42", "成员", code)
        with SessionFactory() as session:
            assert session.get(ChannelAuthorization, member.id).user_identity == previous.notification_identity
            assert session.get(Subscribe, active.id).owner_identity == previous.notification_identity
            assert session.get(SubscribeHistory, history.id).owner_identity == previous.notification_identity
            assert session.get(DownloadHistory, download.id).owner_identity == previous.notification_identity
        with SessionFactory() as session, session.begin():
            session.execute(delete(Subscribe).where(Subscribe.id == duplicate.id))

    linked = service.bind("Telegram", "机器人A", "42", "成员", code)
    assert linked.user_identity == local.notification_identity and linked.status == "pending"
    with SessionFactory() as session:
        for model, record in ((Subscribe, active), (SubscribeHistory, history), (DownloadHistory, download)):
            row = session.get(model, record.id)
            assert row.owner_identity == local.notification_identity and row.username == local.name
        legacy_row = session.get(Subscribe, legacy.id)
        assert legacy_row.username == previous.name and legacy_row.owner_identity is None


def test_two_accounts_cannot_bind_the_same_member_concurrently(channel_authorization, db):
    """
    不同网页账号提交不同关联码时，只能有一个账号取得成员的数据归属。
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, _, _ = channel_authorization
    first, second = db.add(User(name="bind-race-first", is_active=True), User(name="bind-race-second", is_active=True))
    member = service.request("Telegram", "机器人A", "42", "成员")
    codes = [service.create_binding(user.id)[0] for user in (first, second)]
    barrier = Barrier(2)

    def bind(code):
        """
        同时提交两个不同账号各自的一次性码。
        """
        barrier.wait(timeout=5)
        try:
            return service.bind("Telegram", "机器人A", "42", "成员", code).user_id
        except ValueError as error:
            assert "已关联其它账号" in str(error)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(bind, code) for code in codes]
        results = [job.result(timeout=10) for job in jobs]
    assert results.count(None) == 1
    linked = service.find("Telegram", "机器人A", "42")
    assert linked.id == member.id
    assert linked.linked_user_id == linked.user_id == next(result for result in results if result is not None)


def test_concurrent_binding_code_generation_leaves_one_valid_code(channel_authorization, db):
    """
    同一账号并行发起两次生成，数据库中只保留一个有效码。
    """
    from concurrent.futures import ThreadPoolExecutor
    from hashlib import sha256
    from threading import Barrier

    service, _, _ = channel_authorization
    user = db.add(User(name="binding-code-race", is_active=True))
    user_id = user.id
    barrier = Barrier(2)

    def create():
        """
        从两个独立事务同时生成网页关联码。
        """
        barrier.wait(timeout=5)
        return service.create_binding(user_id)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(create) for _ in range(2)]
        codes = [job.result(timeout=10) for job in jobs]
    with SessionFactory() as session:
        stored = session.scalars(select(ChannelBinding.id).where(ChannelBinding.user_id == user_id)).one()
    valid, revoked = sorted(codes, key=lambda code: sha256(code.encode()).hexdigest() != stored)
    with pytest.raises(ValueError, match="已使用或已过期"):
        service.bind("Telegram", "机器人A", "42", "成员", revoked)
    assert service.bind("Telegram", "机器人A", "42", "成员", valid).user_id == user_id


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
    from app.schemas.legacychannel import LEGACY_CHANNELS

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
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/authorization")
    app.dependency_overrides[get_current_active_superuser] = lambda: SimpleNamespace(
        id=1, account_type="local", authorization_id=None
    )
    with TestClient(app, headers={"Authorization": "Bearer fixture"}) as client:
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
    下载历史按不可变账号在分页前过滤，改名后仍归本人，同名记录和旧无归属记录不会混入。
    """
    from starlette.responses import Response

    from app.api.endpoints.history import download_history
    from app.application.history import HistoryQueryService
    from app.db.adapters.history.download import TransactionalDownloadHistoryRepository
    from app.db.models.downloadhistory import DownloadHistory

    service, _, _ = channel_authorization
    member = _approve(service)
    first = service.principal(member.id)
    second = service.principal(_approve(service, source="机器人B").id)
    db.add(
        DownloadHistory(
            path="/synthetic/first.mkv",
            type="电影",
            title="自己的下载",
            username="旧用户名",
            owner_identity=first.notification_identity,
            task_reference="mp-task-" + "a" * 32,
            date="2026-11-16 10:00:00",
        ),
        DownloadHistory(
            path="/synthetic/second.mkv",
            type="电影",
            title="别人的下载",
            username=first.name,
            owner_identity=second.notification_identity,
            date="2026-11-17 10:00:00",
        ),
        DownloadHistory(
            path="/synthetic/legacy.mkv",
            type="电影",
            title="旧版记录",
            username=first.name,
            date="2026-11-18 10:00:00",
        ),
    )
    repository = TransactionalDownloadHistoryRepository(sync_session=SessionFactory, async_session=async_session_scope)
    response = Response()
    records = asyncio.run(download_history(
        page=1, count=1,
        query=HistoryQueryService(download_repository=repository, transfer_repository=Mock()),
        response=response,
        _=TokenPayload(agent_authorization_id=member.id, agent_operation="download.history.list"),
    ))
    assert [record.title for record in records] == ["自己的下载"]
    assert response.headers["X-Total-Count"] == "1"
    references = ["mp-task-" + "a" * 32]
    assert list(repository.get_owned_tasks(first.notification_identity, references)) == references
    assert repository.get_owned_tasks(second.notification_identity, references) == {}


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
    notify = AsyncMock()
    monkeypatch.setattr(AgentChain, "async_post_message", notify)
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
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduled_final_message_rechecks_current_grant_and_uses_private_target(channel_authorization, monkeypatch):
    """
    定时任务完成时重新核对权限，结果只私发给当前连接中的原执行者。
    """
    from app.agent.orchestrator import MoviePilotAgent
    from app.agent.terminal.ownership import TerminalScope, bind_terminal_scope
    from app.chain.agent import AgentChain

    service, configs, _ = channel_authorization
    member = _approve(service, scopes=("search", "schedule"))
    agent = MoviePilotAgent(
        session_id="scheduled-authorization", user_id=member.sender_id,
        channel=member.channel, source="机器人A", original_chat_id="原群聊",
    )
    agent.authorization_id = member.id
    agent.execution_user_id = member.user_id
    agent.execution_user_identity = member.user_identity
    notify = AsyncMock()
    monkeypatch.setattr(AgentChain, "async_post_message", notify)
    config = configs[0]
    config.name = "新名称"
    with bind_terminal_scope(TerminalScope(member.sender_id, "run", "scheduled")):
        await agent.send_agent_message("任务完成")
        outgoing = notify.await_args.args[0]
        assert outgoing.delivery_kind == "task_result" and outgoing.private_delivery
        assert outgoing.original_chat_id is None and outgoing.userid == member.sender_id
        assert outgoing.source == config.name and outgoing.delivery_connection == (config.id, config.id)
        notify.reset_mock()
        service.update(member.id, status="approved", role="member", scopes=("search",), reviewer_id=1)
        await agent.send_agent_message("撤销后的任务结果")
    notify.assert_not_awaited()
    assert agent._tool_context["execution_identity_error"]["message"]


def test_queued_task_result_rechecks_current_grant_and_connection(channel_authorization):
    """
    队列等待期间改名仍可向原账号发送，换连接身份或撤权则丢弃已排队结果。
    """
    from app.application.messaging.message import MessageQueueManager
    from app.schemas.message import Message

    service, configs, _ = channel_authorization
    member = _approve(service, scopes=("search", "schedule"))
    result = service.prepare_task_result(Message(text="任务结果"), member.id, member.user_id, member.user_identity)
    deliver = Mock()
    configs[0].name = "新名称"
    MessageQueueManager._send_with_callback(deliver, "post_message", message=result)
    forwarded = deliver.call_args.kwargs["message"]
    assert forwarded.source == "新名称" and forwarded.private_delivery
    assert forwarded.userid == member.sender_id and forwarded.delivery_connection == result.delivery_connection
    deliver.reset_mock()

    configs[0].identity_namespace = "replacement-account"
    MessageQueueManager._send_with_callback(deliver, "post_message", message=result)
    deliver.assert_not_called()
    configs[0].identity_namespace = None
    serialized = Message.model_validate_json(result.model_dump_json())
    MessageQueueManager._send_with_callback(deliver, "post_message", message=serialized)
    deliver.assert_not_called()
    service.update(member.id, status="disabled", role="member", scopes=(), reviewer_id=1)
    MessageQueueManager._send_with_callback(deliver, "post_message", message=result)
    deliver.assert_not_called()


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


def test_approval_receipt_retries_without_reapplying_the_grant(channel_authorization, monkeypatch):
    """
    真实审批提交后只重试通知。成员停用后仍收到私聊回执，旧任务结果不发送。
    """
    from datetime import datetime, timedelta, timezone

    from app.api.endpoints import authorization as endpoint
    from app.application.messaging.message import MessageQueueManager
    from app.application.outbox import CHANNEL_AUTHORIZATION_RECEIPT_TOPIC, OutboxDispatcher
    from app.chain._messaging import NotificationMixin
    from app.db.adapters.outbox import SqlAlchemyOutboxDispatchStore
    from app.modules.telegram.botapi import TelegramApiError
    from app.modules.telegram.module import TelegramModule
    from app.modules.telegram.telegram import Telegram
    from app.schemas.message import Message
    from app.startup.composition import outbox

    service, configs, _ = channel_authorization
    config = configs[0]
    config.identity_namespace = config.id
    monkeypatch.setattr(endpoint, "get_notification_configs", lambda **_: configs)
    api = Mock()
    api.get_me.return_value = {"username": "fixture"}
    api.send_message.side_effect = [TelegramApiError("sendMessage", "limited", 429), {"message_id": 1}, {"message_id": 2}]
    monkeypatch.setattr("app.modules.telegram.telegram.TelegramBotApi", Mock(return_value=api))
    telegram = Telegram(TELEGRAM_TOKEN="fixture", TELEGRAM_CHAT_ID="-100", auto_start_polling=False)
    monkeypatch.setattr(telegram, "_mark_rate_limit", Mock())
    module = TelegramModule()
    module._channel = NotificationChannel.Telegram
    monkeypatch.setattr(module, "get_configs", lambda: {config.name: config})
    monkeypatch.setattr(module, "get_config", lambda _: config)
    monkeypatch.setattr(module, "get_instance", lambda _: telegram)

    def dispatch(_method, *, message):
        """
        使用实际通知模块处理原收件人及分段回执。
        """
        return module.post_message(message)

    def strict(message, **_kwargs):
        """
        使用宿主严格派发的实际回执结算。
        """
        host = SimpleNamespace(run_module_strict=dispatch)
        NotificationMixin._deliver_notification(host, message, strict_delivery=True, immediately=True, kwargs={})

    monkeypatch.setattr(outbox, "CommandChain", lambda: SimpleNamespace(post_message_strict=strict))
    original = service.request("Telegram", config.name, "42", "成员")
    app = _authorization_app(SimpleNamespace(id=1, account_type="local", authorization_id=None))
    store = SqlAlchemyOutboxDispatchStore(SessionFactory)
    handler = outbox.build_outbox_handlers()[CHANNEL_AUTHORIZATION_RECEIPT_TOPIC]
    wall_time = datetime.now(timezone.utc) + timedelta(seconds=1)
    dispatcher = OutboxDispatcher(store, {CHANNEL_AUTHORIZATION_RECEIPT_TOPIC: handler}, clock=lambda: wall_time)

    def dispatch_receipt(event_key):
        """
        只领取本次审批产生的意图，保持其他测试中的记录不变。
        """
        claimed = store.claim_by_event_key(event_key, wall_time, wall_time + timedelta(seconds=30))
        assert claimed is not None
        dispatcher.dispatch_claimed(claimed)

    with TestClient(app, headers={"Authorization": "Bearer fixture"}) as browser:
        decision = {"status": "approved", "role": "member", "scopes": ["search", "schedule"]}
        assert browser.put(f"/authorization/{original.id}", json=decision).json()["success"]
        approved = service.find("Telegram", config.name, "42")
        assert approved.status == "approved"
        key = f"{CHANNEL_AUTHORIZATION_RECEIPT_TOPIC}:{approved.id}:{approved.updated_at.hex()}"
        dispatch_receipt(key)
        api.send_message.assert_called_once()
        assert service.find("Telegram", config.name, "42") == approved
        assert browser.put(f"/authorization/{original.id}", json=decision).json()["success"]
        assert service.find("Telegram", config.name, "42") == approved
        wall_time += timedelta(minutes=5)
        dispatch_receipt(key)
        task_result = service.prepare_task_result(Message(text="旧任务结果"), approved.id, approved.user_id, approved.user_identity)

        assert browser.put(f"/authorization/{original.id}", json={**decision, "status": "disabled"}).json()["success"]
        disabled = service.find("Telegram", config.name, "42")
        assert disabled.status == "disabled"
        disabled_key = f"{CHANNEL_AUTHORIZATION_RECEIPT_TOPIC}:{disabled.id}:{disabled.updated_at.hex()}"
        dispatch_receipt(disabled_key)
        submit_task = Mock()
        MessageQueueManager._send_with_callback(submit_task, "post_message", message=task_result)
        submit_task.assert_not_called()

    with SessionFactory() as session:
        rows = session.scalars(select(OutboxMessage).where(OutboxMessage.event_key.in_((key, disabled_key)))).all()
        assert {row.event_key: (row.status, row.attempt) for row in rows} == {key: ("completed", 2), disabled_key: ("completed", 1)}
    assert [call.kwargs["chat_id"] for call in api.send_message.call_args_list] == ["42", "42", "42"]


def test_direct_authorization_api_commits_then_replies_privately(channel_authorization, monkeypatch):
    """
    直接授权同时写入回执，后台按原连接身份私发；重复审批不增加投递。
    """
    from fastapi.testclient import TestClient

    from app.api.endpoints import authorization as endpoint
    from app.application.outbox import CHANNEL_AUTHORIZATION_RECEIPT_TOPIC, ClaimedOutboxMessage
    from app.startup.composition import outbox

    service, configs, _ = channel_authorization
    monkeypatch.setattr(
        endpoint, "get_notification_configs",
        lambda **options: [item for item in configs if options.get("include_disabled") or item.enabled],
    )
    sent = Mock()
    monkeypatch.setattr(outbox, "_deliver_outbox_notification", sent)
    app = _authorization_app(SimpleNamespace(id=1, account_type="local", authorization_id=None))
    payload = {
        "notification_id": "telegram-a", "sender_id": " 42 ", "display_name": " 成员 ",
        "status": "approved", "role": "member", "scopes": ["search"],
    }
    with TestClient(app, headers={"Authorization": "Bearer fixture"}) as client:
        response = client.post("/authorization/", json=payload)
        assert response.status_code == 200
        result = response.json()["data"]
        member = service.find("Telegram", "机器人A", "42")
        assert result["id"] == member.id and result["display_name"] == "成员"
        assert service.principal(member.id).permissions["search"] is True
        assert "回执" in response.json()["message"]
        sent.assert_not_called()
        assert client.post("/authorization/", json=payload).status_code == 200
        with SessionFactory() as session:
            row = session.scalars(select(OutboxMessage).where(
                OutboxMessage.topic == CHANNEL_AUTHORIZATION_RECEIPT_TOPIC,
                OutboxMessage.event_key.startswith(f"{CHANNEL_AUTHORIZATION_RECEIPT_TOPIC}:{member.id}:"),
            )).one()
            intent = ClaimedOutboxMessage(
                row.id, row.event_key, row.topic, dict(row.payload), row.payload_version, 1,
            )
        handler = outbox.build_outbox_handlers()[CHANNEL_AUTHORIZATION_RECEIPT_TOPIC]
        configs[0].name = "改名后的机器人"
        handler(intent)
        message = sent.call_args.args[1]
        assert message.channel == NotificationChannel.Telegram and message.source == "改名后的机器人"
        assert message.delivery_connection == ("telegram-a", "telegram-a")
        assert message.userid == "42" and message.private_delivery is True and message.save_history is False
        assert "重新发送" in message.title
        assert client.put(
            "/authorization/missing-member", json={"status": "approved", "scopes": []},
        ).status_code == 400
        sent.reset_mock()
        configs[0].identity_namespace = "new-identity"
        handler(intent)
        sent.assert_not_called()
        configs[0].identity_namespace = None
        configs[0].interaction_enabled = False
        # 主动投递继续工作时，审批界面也应报告交互已关闭。
        listing = client.get("/authorization/").json()["data"]
        assert next(item for item in listing["channels"] if item["id"] == "telegram-a")["enabled"] is False
        assert next(item for item in listing["members"] if item["id"] == member.id)["channel_enabled"] is False
        assert client.post("/authorization/", json=payload).status_code == 400
        configs[0].enabled = False
        assert client.post("/authorization/", json=payload).status_code == 400
        assert client.post(
            "/authorization/", json={**payload, "notification_id": "missing-instance"},
        ).status_code == 400
        assert client.put(
            f"/authorization/{member.id}", json={"status": "disabled", "scopes": []},
        ).status_code == 200
        configs[0].enabled = True
        handler(intent)
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
    with TestClient(_authorization_app(user), headers={"Authorization": "Bearer fixture"}) as client:
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


@pytest.mark.asyncio
async def test_rule_approval_schedules_only_in_the_verified_scope(channel_authorization, db, monkeypatch):
    """
    创建后的任务复用受限身份，策略、成员和连接撤权约束执行与投递。
    """
    from app.agent.manager import AgentManager
    from app.agent.tools.impl.agent_task import AgentTaskTool
    from app.runtime.config import settings

    db.watermark(AgentTask, AgentTaskRun)
    service, configs, users = channel_authorization
    policy = InteractionPolicy(
        connection_id="telegram-a", identity_namespace="telegram-a", enabled=True, revision=5,
        admission="rules", admission_rules=(AdmissionRule(kind="group", target="-100", scopes=("search", "schedule")),),
    )
    configs[0].interaction_policy = policy
    proof = AdmissionProof("telegram-a", 5, "42", "-100", time.time() + 30)
    assert service.rule_task_principal("Telegram", "机器人A", "42") is None
    with scoped_admission(AdmissionProof("telegram-a", 5, "42", "-200", proof.expires_at)):
        assert service.rule_task_principal("Telegram", "机器人A", "42") is None
    with scoped_admission(proof):
        user = service.rule_task_principal("Telegram", "机器人A", "42")
        assert user is not None and user.authorization_id
        assert service.rule_task_principal("Telegram", "机器人A", "42").authorization_id == user.authorization_id
    member = service.find("Telegram", "机器人A", "42")
    assert member.status == "pending" and not user.is_superuser
    assert user.permissions["schedule"] and not user.permissions["download"]
    with pytest.raises(PermissionError):
        service.authorize_operation(user.authorization_id, "download.add")
    assert service.authorize_operation(user.authorization_id, "search.torrents").id == user.id
    monkeypatch.setattr(settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr("app.application.scheduling.update_agent_task_job", lambda _task_id: None)
    data = SimpleNamespace(
        users=users, tasks=TransactionalAgentTaskRepository(SessionFactory),
        chat=SimpleNamespace(get_sync=lambda **_kwargs: None),
    )
    tool = AgentTaskTool(session_id="rule-task-session", user_id="42", data=data)
    tool.set_message_attr(channel="Telegram", source="机器人A", username="42")
    tool.set_agent_context({
        "authorization_id": user.authorization_id, "execution_user_id": user.id,
        "execution_user_identity": user.notification_identity,
    })
    result = await tool.run(
        action="create", name="搜索新资源", content="搜索影片", trigger_type="cron", trigger="0 * * * *",
    )
    task_id = json.loads(result)["id"]
    assert service.connection_reference_counts("telegram-a") == (1, 1)
    assert data.tasks.get(task_id).execution_authorization_id == user.authorization_id
    manager = AgentManager()
    manager._data = data
    manager.process_message = AsyncMock(return_value="完成")
    assert await manager.execute_scheduled_task(task_id) == (True, "完成")
    assert manager.process_message.await_args.kwargs["authorization_id"] == user.authorization_id

    private = service.prepare_task_result(Message(title="完成"), user.authorization_id, user.id, user.notification_identity)
    assert private.private_delivery and private.userid == "42" and private.original_chat_id is None
    configs[0].interaction_policy = policy.model_copy(update={"revision": 6})
    success, error = await manager.execute_scheduled_task(task_id)
    assert not success and "规则或成员授权已变更" in error
    manager.process_message.assert_awaited_once()
    with pytest.raises(PermissionError):
        service.revalidate_task_result(private)

    configs[0].interaction_policy = policy
    service.update(member.id, status="disabled", role="member", scopes=(), reviewer_id=1)
    with pytest.raises(PermissionError):
        service.principal(user.authorization_id, capability="schedule")


@pytest.mark.parametrize("status", ["pending", "approved", "rejected", "disabled"])
def test_delete_authorization_requires_confirmation_and_rotates_reapplication_identity(channel_authorization, status):
    """
    全部状态可删，已授权必须确认；旧身份立即失效，重申请不继承授权和账号关联。
    """
    from app.application.security.authorization import ChannelAuthorizationConflict

    service, _, _ = channel_authorization
    member = service.request("Telegram", "机器人A", "delete-42", "待删除成员")
    if status != "pending":
        member = service.update(member.id, status=status, role="member", scopes=("search",), reviewer_id=1)
    if status == "approved":
        with pytest.raises(ValueError, match="再次确认"):
            service.manage_members("delete", ((member.id, member.updated_at),), reviewer_id=1)
        assert service.find("Telegram", "机器人A", "delete-42") is not None
    service.manage_members("delete", ((member.id, member.updated_at),), reviewer_id=1, confirm_approved=status == "approved")
    assert service.find("Telegram", "机器人A", "delete-42") is None
    with pytest.raises(PermissionError):
        service.principal(member.id)
    with SessionFactory() as session:
        assert session.get(User, member.user_id) is not None
    renewed = service.request("Telegram", "机器人A", "delete-42", "重新申请")
    assert renewed.status == "pending" and renewed.id != member.id
    assert renewed.user_identity != member.user_identity and renewed.linked_user_id is None
    with pytest.raises(ChannelAuthorizationConflict):
        service.manage_members("delete", ((member.id, member.updated_at),), reviewer_id=1, confirm_approved=True)
    assert service.find("Telegram", "机器人A", "delete-42").id == renewed.id


@pytest.mark.parametrize("action,status,result", [("approve", "pending", "approved"), ("reject", "pending", "rejected"), ("disable", "approved", "disabled")])
def test_batch_authorization_uses_selected_versions_and_explicit_member_scopes(channel_authorization, action, status, result):
    """
    批量通过仅授予普通成员及明确权限，拒绝和停用保留原角色权限且不影响未选择成员。
    """
    service, _, _ = channel_authorization
    records = [service.request("Telegram", "机器人A", f"batch-{i}", f"成员{i}") for i in range(3)]
    if status == "approved":
        records = [service.update(m.id, status=status, role="admin", scopes=("search",), reviewer_id=1) for m in records]
    changed = service.manage_members(action, tuple((m.id, m.updated_at) for m in records[:2]), reviewer_id=1, scopes=("search", "schedule"))
    assert {member.status for member in changed} == {result}
    for member in changed:
        assert member.role == ("admin" if action == "disable" else "member")
        assert set(member.scopes) == ({"search", "schedule"} if action == "approve" else {"search"} if action == "disable" else {"search", "subscribe", "download"})
    assert service.find("Telegram", "机器人A", "batch-2").status == status


def test_batch_delete_stale_or_unconfirmed_record_rolls_back_all(channel_authorization):
    """
    批次包含过期版本或未确认的已授权成员时，不删除任何记录。
    """
    from app.application.security.authorization import ChannelAuthorizationConflict

    service, _, _ = channel_authorization
    pending = service.request("Telegram", "机器人A", "pending-delete", "待审批")
    approved = _approve(service, sender_id="approved-delete")
    with pytest.raises(ValueError, match="再次确认"):
        service.manage_members("delete", ((pending.id, pending.updated_at), (approved.id, approved.updated_at)), reviewer_id=1)
    changed = service.update(approved.id, status="disabled", role="member", scopes=("search",), reviewer_id=1)
    with pytest.raises(ChannelAuthorizationConflict):
        service.manage_members("delete", ((pending.id, pending.updated_at), (approved.id, approved.updated_at)), reviewer_id=1, confirm_approved=True)
    assert service.find("Telegram", "机器人A", "pending-delete").id == pending.id
    assert service.find("Telegram", "机器人A", "approved-delete").updated_at == changed.updated_at


def test_batch_mutation_cas_conflict_rolls_back_prior_changes_and_receipts(channel_authorization, monkeypatch):
    """
    校验后的第二条记录在提交时发生版本冲突，前一条修改和回执均回滚。
    """
    from app.application.security.authorization import ChannelAuthorizationConflict
    from app.db.oper.authorization import ChannelAuthorizationOper

    service, _, _ = channel_authorization
    records = sorted([service.request("Telegram", "机器人A", f"cas-{i}", "成员") for i in range(2)], key=lambda m: m.id)
    original = ChannelAuthorizationOper.update
    with SessionFactory() as session:
        before = set(session.scalars(select(OutboxMessage.id)))

    def race(oper, member_id, **kwargs):
        """
        在第二次比较更新前制造一次版本变化。
        """
        if member_id == records[1].id:
            oper._session.execute(update(ChannelAuthorization).where(ChannelAuthorization.id == member_id).values(updated_at=records[1].updated_at + 1))
        return original(oper, member_id, **kwargs)

    monkeypatch.setattr(ChannelAuthorizationOper, "update", race)
    with pytest.raises(ChannelAuthorizationConflict):
        service.manage_members("approve", tuple((m.id, m.updated_at) for m in records), reviewer_id=1)
    assert all(service.find("Telegram", "机器人A", f"cas-{i}").status == "pending" for i in range(2))
    with SessionFactory() as session:
        assert set(session.scalars(select(OutboxMessage.id))) == before


def test_delete_removes_rule_grants_and_keeps_historical_business_owner(channel_authorization):
    """
    删除同时移除规则派生授权，旧任务主体停止生效，业务账号仍保留。
    """
    service, _, _ = channel_authorization
    member = _approve(service, sender_id="rule-delete")
    with SessionFactory() as session:
        session.add(ChannelRuleTaskGrant(id="gdeleted", member_id=member.id, member_version=member.updated_at, policy_revision=1, scopes=["schedule"]))
        session.commit()
    service.manage_members("delete", ((member.id, member.updated_at),), reviewer_id=1, confirm_approved=True)
    with SessionFactory() as session:
        assert session.get(ChannelRuleTaskGrant, "gdeleted") is None
        assert session.get(User, member.user_id) is not None
    with pytest.raises(PermissionError):
        service.principal("gdeleted", capability="schedule")


def test_batch_and_delete_authorization_http_validate_versions_and_web_administrator(channel_authorization):
    """
    HTTP 强制网页系统管理员、唯一选中身份、合法动作及已授权删除确认。
    """
    from fastapi.testclient import TestClient

    service, _, _ = channel_authorization
    member = service.request("Telegram", "机器人A", "api-batch", "HTTP成员")
    user = SimpleNamespace(id=1, account_type="local", authorization_id=None)
    app = _authorization_app(user)
    target = {"id": member.id, "expected_updated_at": member.updated_at}
    with TestClient(app) as client:
        assert client.post("/authorization/batch", json={"action": "approve", "members": [target]}).status_code == 403
        client.headers["Authorization"] = "Bearer admin-fixture"
        assert client.post("/authorization/batch", json={"action": "approve", "members": [target, target]}).status_code == 422
        assert client.post("/authorization/batch", json={"action": "approve", "members": [target], "role": "admin"}).status_code == 422
        response = client.post("/authorization/batch", json={"action": "approve", "members": [target], "scopes": ["search"]})
        assert response.status_code == 200
        current = response.json()["data"]["members"][0]
        assert current["scopes"] == ["search"] and current["role"] == "member"
        assert "user_identity" not in current
        assert client.request("DELETE", f"/authorization/{member.id}", json={"expected_updated_at": current["updated_at"]}).status_code == 400
        assert client.request("DELETE", f"/authorization/{member.id}", json={"expected_updated_at": member.updated_at, "confirm_approved": True}).status_code == 409
        assert client.request("DELETE", f"/authorization/{member.id}", json={"expected_updated_at": current["updated_at"], "confirm_approved": True}).status_code == 200
        user.account_type = "channel"
        assert client.post("/authorization/batch", json={"action": "delete", "members": [target]}).status_code == 403
