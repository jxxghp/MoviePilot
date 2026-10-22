"""
Agent 公共资料访问、真实身份解析和授权范围的离线回归。
"""

import asyncio
import json
import re
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.agent.api.executor import ApiExecutionContext, ApiExecutionError, MoviePilotApiExecutor
from app.agent.policy.api import (
    API_OPERATION_ROUTES,
    API_OPERATION_SPECS,
    API_UNBOUND_READ_OPERATIONS,
    ApiIdentityScope,
    resolve_api_operation,
)
from app.agent.policy.contracts import ActionEffect, ConfirmationMode, PrincipalRole, ResultSensitivity
from app.agent.tools.impl.api import MoviePilotApiTool
from app.db.adapters.user import TransactionalUserRepository
from app.db.models.user import User
from app.db.session import SessionFactory, async_session_scope
from app.schemas.types import NotificationChannel

_MUSIC_ID = "00000000-0000-0000-0000-000000000001"
# 参数独立于生产注册表：每项都经真实输入校验和 HTTP 执行器，不以 safe_read 推断公共性。
PUBLIC_CASES = {
    "media.search": {"query": {"title": "示例电影"}},
    "media.person.search": {"query": {"title": "示例人物", "type": "person"}},
    "media.person.credits": {"path_params": {"source": "tmdb", "person_id": 1}},
    "media.episode_schedule": {"path_params": {"tmdbid": 1, "season": 1}},
    "media.detail": {"path_params": {"media_id": "1"}, "query": {"media_source": "tmdb", "type_name": "电影"}},
    "media.episode_groups": {"path_params": {"tmdbid": 1}},
    "media.episode_group.seasons": {"path_params": {"episode_group": "group"}},
    "media.seasons": {},
    "music.recognize": {"body": {"media_source": "musicbrainz", "media_id": _MUSIC_ID}},
    "music.explore": {},
    "music.album.get": {"path_params": {"album_id": _MUSIC_ID}},
    "music.album.related": {"path_params": {"album_id": _MUSIC_ID}},
    "music.artist.get": {"path_params": {"artist_id": _MUSIC_ID}},
    "music.artist.albums": {"path_params": {"artist_id": _MUSIC_ID}},
    "music.artist.related": {"path_params": {"artist_id": _MUSIC_ID}},
    "recommendation.list": {},
    "subscription.shares": {},
    "subscription.popular": {"query": {"stype": "movie"}},
    "subscription.share.statistics": {},
    "plugin.statistics": {},
}


@pytest.mark.parametrize("operation_id,arguments", PUBLIC_CASES.items())
@pytest.mark.parametrize("channel", [NotificationChannel.WechatClawBot, NotificationChannel.Telegram])
@pytest.mark.parametrize("is_admin", [False, True])
@pytest.mark.parametrize("user_state", ["unbound", "inactive", "active"])
def test_public_queries_do_not_require_channel_binding(operation_id, arguments, channel, is_admin, user_state):
    """
    所有公共操作通过真实宿主认证执行，绑定缺失或停用均不阻断公共结果。
    """
    user = None if user_state == "unbound" else SimpleNamespace(is_active=user_state == "active")
    users = SimpleNamespace(async_get_by_name=AsyncMock(return_value=user), find_name_by_bindings=Mock())
    gateway = MoviePilotApiTool(session_id="session", user_id="channel-user", data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=channel.value, source="main-bot", username="admin")
    gateway.set_agent_context({"is_admin": is_admin})
    response = SimpleNamespace(
        status_code=200, headers={}, aclose=AsyncMock(),
        json=lambda: {"success": True, "data": [{"title": "公共资料"}]},
    )
    with (
        patch("app.application.security.auth.build_superuser_token_payload", return_value=SimpleNamespace(
            sub=7, username="admin", super_user=True,
        )),
        patch("app.agent.api.executor.create_access_token", return_value="host-token") as token,
        patch("app.agent.api.executor.AsyncRequestUtils.request", new=AsyncMock(return_value=response)) as request,
    ):
        result = asyncio.run(gateway.run(operation_id, **arguments))

    assert json.loads(result) == {"success": True, "data": [{"title": "公共资料"}]}
    request.assert_awaited_once()
    assert request.call_args.kwargs["method"] == API_OPERATION_ROUTES[operation_id].method
    assert "/api/v1/" in request.call_args.kwargs["url"]
    token.assert_called_once_with(userid=7, username="admin", super_user=True)
    users.async_get_by_name.assert_not_awaited()
    users.find_name_by_bindings.assert_not_called()
    response.aclose.assert_awaited_once()


@pytest.mark.parametrize("operation_id,arguments", [
    ("subscription.list", {}),
    ("download.history.list", {}),
    ("subscription.add", {"body": {"name": "测试电影", "type": "movie"}}),
    ("library.latest", {"query": {"server": "server"}}),
    ("media.recognize", {"query": {"title": "测试电影"}}),
    ("media.recognize_file", {"query": {"path": "/media/file.mkv"}}),
    ("search.results", {}),
    ("storage.list", {"body": {"path": "/", "storage": "local", "type": "dir"}}),
    ("config.user.get", {}),
    ("config.public.get", {"path_params": {"key": "Storages"}}),
    ("subscription.follow.list", {}),
])
@pytest.mark.parametrize("user_state", ["unbound", "inactive"])
def test_instance_and_user_operations_still_require_binding(operation_id, arguments, user_state):
    """
    个人和实例数据不能借用公共查询身份，渠道管理员也保留绑定要求。
    """
    user = None if user_state == "unbound" else SimpleNamespace(is_active=False)
    users = SimpleNamespace(
        async_get_by_name=AsyncMock(return_value=user),
        find_name_by_bindings=Mock(return_value="disabled" if user else None),
    )
    gateway = MoviePilotApiTool(session_id="session", user_id="channel-user", data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=NotificationChannel.WechatClawBot.value, source="main-bot", username="admin")
    gateway.set_agent_context({"is_admin": True})
    with (
        patch("app.application.security.auth.build_superuser_token_payload") as identity,
        patch("app.agent.api.executor.AsyncRequestUtils.request", new=AsyncMock()) as request,
    ):
        result = asyncio.run(gateway.run(operation_id, **arguments))
    assert json.loads(result)["error"] == "operation_unavailable"
    identity.assert_not_called()
    request.assert_not_awaited()


@pytest.mark.parametrize("operation_id", sorted(set(API_OPERATION_ROUTES) - set(PUBLIC_CASES)))
def test_public_executor_rejects_every_nonpublic_operation(operation_id):
    """
    持有宿主身份的公共执行器在发送前拒绝其余全部个人、实例和管理员操作。
    """
    executor = MoviePilotApiExecutor(context=ApiExecutionContext(
        user_id="7", username="admin", is_admin=True, public_read_only=True,
    ))
    with patch("app.agent.api.executor.AsyncRequestUtils.request", new=AsyncMock()) as request:
        with pytest.raises(ApiExecutionError, match="公共查询执行器"):
            asyncio.run(executor.execute(operation_id))
    request.assert_not_awaited()


def test_public_allowlist_only_contains_audited_metadata_queries():
    """
    公共清单独立锁定，写入、私密结果和管理数据不能因只读标签被自动放行。
    """
    assert API_UNBOUND_READ_OPERATIONS == set(PUBLIC_CASES)
    for operation_id in PUBLIC_CASES:
        spec = resolve_api_operation(operation_id)
        assert spec.identity_scope is ApiIdentityScope.PUBLIC
        assert spec.effect is ActionEffect.SAFE_READ
        assert spec.required_role is PrincipalRole.USER
        assert spec.result_sensitivity is ResultSensitivity.NORMAL
        assert spec.confirmation is ConfirmationMode.NONE


@pytest.mark.parametrize("changes", [
    {"effect": ActionEffect.REVERSIBLE_WRITE},
    {"effect": ActionEffect.SENSITIVE_READ},
    {"required_role": PrincipalRole.SYSTEM_ADMIN},
    {"result_sensitivity": ResultSensitivity.PRIVATE},
    {"confirmation": ConfirmationMode.REQUIRED},
])
def test_public_identity_cannot_be_declared_for_privileged_operations(changes):
    """
    错误公共身份声明应在注册时失败，不能带着管理员或私密能力进入运行时。
    """
    with pytest.raises(ValueError, match="公共 API 身份"):
        replace(resolve_api_operation("recommendation.list"), **changes)


@pytest.mark.parametrize("channel", [NotificationChannel.Telegram, NotificationChannel.WechatClawBot])
@pytest.mark.parametrize("bound", [False, True])
def test_channel_nickname_cannot_impersonate_moviepilot_user(db, channel, bound):
    """
    渠道展示名与超级管理员同名时，仍只能使用稳定发送者 ID 的实际绑定。
    """
    db.add(User(name="admin", is_active=True, is_superuser=True))
    user = db.add(User(
        name="alice", is_active=True, is_superuser=False,
        settings={f"{channel.name.lower()}_userid": "sender"} if bound else {},
    ))
    users = TransactionalUserRepository(sync_session=SessionFactory, async_session=async_session_scope)
    gateway = MoviePilotApiTool(session_id="session", user_id="sender", data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=channel.value, source="bot", username="admin")
    if bound:
        assert asyncio.run(gateway._resolve_api_identity()) == (str(user.id), "alice", False)
    else:
        with pytest.raises(ApiExecutionError, match="未绑定"):
            asyncio.run(gateway._resolve_api_identity())


@pytest.mark.parametrize("channel", [NotificationChannel.Web, NotificationChannel.WebAgent])
def test_web_identity_uses_authenticated_id_instead_of_stale_name(db, channel):
    """
    旧会话用户名与另一个账号同名时，不能替换当前已登录账号身份。
    """
    db.add(User(name="old-name", is_active=True, is_superuser=True))
    user = db.add(User(name="new-name", is_active=True, is_superuser=False))
    users = TransactionalUserRepository(sync_session=SessionFactory, async_session=async_session_scope)
    gateway = MoviePilotApiTool(session_id="session", user_id=str(user.id), data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=channel.value, source="web", username="old-name")
    assert asyncio.run(gateway._resolve_api_identity()) == (str(user.id), "new-name", False)


@pytest.mark.parametrize("state", ["inactive", "missing"])
def test_web_identity_cannot_fall_back_after_account_disappears(db, state):
    """
    停用或删除的 Web 用户不能依靠数字 ID 回退继续访问个人数据。
    """
    user = db.add(User(name="disabled", is_active=False, is_superuser=False))
    users = TransactionalUserRepository(sync_session=SessionFactory, async_session=async_session_scope)
    user_id = str(user.id) if state == "inactive" else "999999"
    gateway = MoviePilotApiTool(session_id="session", user_id=user_id, data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=NotificationChannel.WebAgent.value, source="web", username=user.name)
    with pytest.raises(ApiExecutionError, match="已停用" if state == "inactive" else "重新登录"):
        asyncio.run(gateway._resolve_api_identity())


@pytest.mark.parametrize("source", ["openai", "openai.responses", "anthropic"])
@pytest.mark.parametrize("is_admin", [False, True])
def test_api_token_integration_uses_host_identity_only_after_authentication(source, is_admin):
    """
    兼容入口的会话 ID 不能当用户 ID，且仅可信管理员上下文可使用宿主身份。
    """
    users = SimpleNamespace(async_get_by_id=AsyncMock(), async_get_by_name=AsyncMock())
    gateway = MoviePilotApiTool(session_id="session", user_id="client-session", data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=NotificationChannel.Web.value, source=source, username="admin")
    gateway.set_agent_context({"is_admin": is_admin})
    with patch("app.application.security.auth.build_superuser_token_payload", return_value=SimpleNamespace(
        sub=7, username="admin", super_user=True,
    )) as identity:
        if is_admin:
            assert asyncio.run(gateway._resolve_api_identity()) == ("7", "admin", True)
            identity.assert_called_once()
        else:
            with pytest.raises(ApiExecutionError, match="重新登录"):
                asyncio.run(gateway._resolve_api_identity())
            identity.assert_not_called()
    users.async_get_by_id.assert_not_awaited()
    users.async_get_by_name.assert_not_awaited()


@pytest.mark.parametrize("channel,label", [
    (NotificationChannel.WechatClawBot, "微信 ClawBot 用户"),
    (NotificationChannel.Wechat, "企业微信用户"),
    (NotificationChannel.Telegram, "Telegram用户"),
    (NotificationChannel.Discord, "Discord用户"),
    (NotificationChannel.Feishu, "飞书用户"),
    (NotificationChannel.QQ, "QQ 用户 ID 或 QQ 用户 OpenID"),
    (NotificationChannel.Slack, "Slack用户"),
    (NotificationChannel.VoceChat, "VoceChat用户"),
    (NotificationChannel.SynologyChat, "SynologyChat用户"),
])
def test_binding_failure_contains_correct_profile_field_and_sender_id(channel, label):
    """
    各通知渠道给出实际前端字段、当前发送者 ID 和唯一启用绑定要求。
    """
    users = SimpleNamespace(find_name_by_bindings=Mock(return_value=None))
    gateway = MoviePilotApiTool(session_id="session", user_id="actual-sender", data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=channel.value, source="bot", username="display-name")
    result = json.loads(asyncio.run(gateway.run("subscription.list")))
    assert result["error"] == "operation_unavailable"
    assert "右上角头像 → 个人信息 → 账号绑定" in result["message"]
    assert label in result["message"]
    assert '"actual-sender"' in result["message"]
    assert "保存并重试" in result["message"]
    assert "重复绑定" in result["message"]
    assert "不要填写昵称、群 ID 或机器人 ID" in result["message"]
    assert "display-name" not in result["message"]


def test_background_task_keeps_host_persisted_identity(db):
    """
    无渠道的可信后台任务仍按宿主持久化用户名执行，不误用渠道绑定规则。
    """
    user = db.add(User(name="task-owner", is_active=True, is_superuser=False))
    users = TransactionalUserRepository(sync_session=SessionFactory, async_session=async_session_scope)
    gateway = MoviePilotApiTool(session_id="session", user_id="system", data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=None, source=None, username=user.name)
    assert asyncio.run(gateway._resolve_api_identity()) == (str(user.id), user.name, False)


@pytest.mark.parametrize("operation_id,arguments", [
    ("plugin.ratings", {}),
    ("plugin.rating", {"path_params": {"plugin_id": "Example"}}),
    ("plugin.rating.submit", {"path_params": {"plugin_id": "Example"}, "body": {"rating": 4}}),
])
@pytest.mark.parametrize("is_admin", [False, True])
def test_plugin_rating_gateway_matches_rest_administrator_requirement(operation_id, arguments, is_admin):
    """
    插件评分包含实例自己的记录，不能按普通公共统计放行。
    """
    user = SimpleNamespace(id=11, name="alice", is_active=True, is_superuser=False)
    users = SimpleNamespace(find_name_by_bindings=Mock(return_value=user.name), async_get_by_name=AsyncMock(return_value=user))
    gateway = MoviePilotApiTool(session_id="session", user_id="sender", data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=NotificationChannel.Telegram.value, source="bot", username="admin")
    gateway.set_agent_context({"is_admin": is_admin})
    with (
        patch("app.application.security.auth.build_superuser_token_payload", return_value=SimpleNamespace(
            sub=7, username="admin", super_user=True,
        )) as identity,
        patch("app.agent.tools.impl.api.MoviePilotApiExecutor") as executor_type,
    ):
        executor_type.return_value.execute = AsyncMock(return_value='{"success": true}')
        result = json.loads(asyncio.run(gateway.run(operation_id, **arguments)))
    assert resolve_api_operation(operation_id).identity_scope is ApiIdentityScope.ADMIN
    if is_admin:
        assert result["success"] is True
        identity.assert_called_once()
        assert executor_type.call_args.kwargs["context"].public_read_only is False
    else:
        assert result["error"] == "permission_denied"
        identity.assert_not_called()
        executor_type.return_value.execute.assert_not_awaited()


def test_identity_scopes_cover_every_operation_and_match_rest_dependencies():
    """
    全部操作均有身份范围，公共端点不能依赖当前用户，超级用户端点不能声明普通角色。
    """
    from app.api.routers import API_V1_ROUTER_SPECS

    endpoints = {}
    for router_spec in API_V1_ROUTER_SPECS:
        for route in router_spec.router.routes:
            if not hasattr(route, "dependant"):
                continue
            path = re.sub(r"\{[^}]+\}", "{}", "/api/v1" + router_spec.prefix + route.path)
            for method in route.methods:
                endpoints[method, path] = route
    assert len(API_OPERATION_SPECS) == len(API_OPERATION_ROUTES) == 232
    for spec in API_OPERATION_SPECS:
        assert isinstance(spec.identity_scope, ApiIdentityScope)
        if spec.identity_scope is not ApiIdentityScope.PUBLIC:
            assert spec.identity_scope is (
                ApiIdentityScope.ADMIN if spec.required_role is PrincipalRole.SYSTEM_ADMIN else ApiIdentityScope.BOUND_USER
            )
        registered = API_OPERATION_ROUTES[spec.operation_id]
        paths = [registered.path]
        if "{source}" in registered.path:
            paths = [registered.path.replace("{source}", source) for source in ("tmdb", "douban", "bangumi", "anilist")]
        for path in paths:
            endpoint = endpoints[registered.method, re.sub(r"\{[^}]+\}", "{}", path)]
            dependencies = {dep.call.__name__: dep.name for dep in endpoint.dependant.dependencies}
            if any("superuser" in name for name in dependencies):
                assert spec.required_role is PrincipalRole.SYSTEM_ADMIN, spec.operation_id
            if spec.identity_scope is ApiIdentityScope.PUBLIC:
                assert dependencies.get("verify_token") == "_", spec.operation_id
                assert not any("current_user" in name or "active_" in name for name in dependencies), spec.operation_id
