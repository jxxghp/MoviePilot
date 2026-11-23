"""
验证 Web Agent 与网页智能助手严格按当前登录用户的实时权限运行。
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

import app.agent.orchestrator as orchestrator_module
from app.agent.middleware.jobs import JobsMiddleware
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.policy.contracts import (
    AuthSource,
    PrincipalRole,
    PrincipalType,
    ToolOrigin,
    ToolPolicyContext,
)
from app.agent.prompt import PromptManager
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.read_file import ReadFileTool
from app.agent.tools.impl.write_file import WriteFileTool
from app.agent.web import _get_web_agent_type
from app.api.dependencies.auth import get_current_download_viewer
from app.api.endpoints import download as download_endpoint
from app.api.endpoints import history as history_endpoint
from app.api.endpoints import message as message_endpoint
from app.chain.message import MessageChain
from app.domain.authorization import (
    authorize_web_operation,
    describe_web_permissions,
    has_web_permission,
)
from app.schemas.token import TokenPayload
from app.schemas.types import NotificationChannel


class _DummyTool(MoviePilotTool):
    """
    用于验证权限过滤与策略拦截的最小测试工具。
    """

    name: str = "dummy_tool"
    description: str = "dummy"

    async def run(self, **kwargs: object) -> str:
        """
        返回固定执行成功结果。
        """
        return "ok"


class _DummyAdminTool(MoviePilotTool):
    """
    模拟需要系统管理员权限的内置工具。
    """

    name: str = "dummy_admin_tool"
    description: str = "admin only"
    require_admin: bool = True

    async def run(self, **kwargs: object) -> str:
        """
        返回固定执行成功结果。
        """
        return "admin-ok"


def _build_json_request(body: dict[str, object]) -> Request:
    """
    构造携带 JSON 请求体的测试 HTTP 请求。
    """
    raw = json.dumps(body).encode("utf-8")

    async def _receive() -> dict[str, object]:
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/message/web",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        },
        receive=_receive,
    )


def test_web_permission_rules_enforce_categories_and_features() -> None:
    """
    网页用户权限规则应严格区分分类、功能开关与超级管理员专属能力。
    """
    assert has_web_permission(False, {"admin": True}, "manage") is False
    assert has_web_permission(True, {}, "manage", "manage.site") is True

    # 仅开通发现但关闭推荐子功能
    discovery_only = {"discovery": True, "features": {"discovery.recommend": False}}
    assert authorize_web_operation(False, discovery_only, "music.explore")[0] is True
    assert authorize_web_operation(False, discovery_only, "recommendation.list")[0] is False
    assert authorize_web_operation(False, discovery_only, "search.torrents")[0] is False
    assert authorize_web_operation(False, discovery_only, "config.system.get")[0] is False

    # 订阅功能级细粒度控制
    tv_only_sub = {"subscribe": True, "features": {"subscribe.movie": False, "subscribe.tv": True}}
    assert authorize_web_operation(False, tv_only_sub, "subscription.add", media_type="电视剧")[0] is True
    assert authorize_web_operation(False, tv_only_sub, "subscription.add", media_type="电影")[0] is False

    # 受托管理权限按子功能开放，同时禁止超级管理员专属操作
    site_manager = {
        "manage": True,
        "features": {"manage.site": True, "manage.filemanager": False},
    }
    assert authorize_web_operation(False, site_manager, "site.update")[0] is True
    assert authorize_web_operation(False, site_manager, "storage.mkdir")[0] is False
    assert authorize_web_operation(False, site_manager, "plugin.install")[0] is False
    assert authorize_web_operation(False, site_manager, "config.system.update")[0] is False
    assert authorize_web_operation(False, site_manager, "site.reset")[0] is False

    # 零权限用户仍可读取基础用户/公开配置，但不能访问发现或业务操作
    assert authorize_web_operation(False, {}, "config.user.get")[0] is True
    assert authorize_web_operation(False, {}, "media.search")[0] is False

    summary = describe_web_permissions(False, site_manager)
    assert "普通网页登录用户" in summary
    assert "文件管理" in summary
    assert describe_web_permissions(True, site_manager) == ""


def test_web_agent_api_never_borrows_superuser_for_public_operations() -> None:
    """
    直接网页会话调用公共资料操作时不得借用超级管理员身份，且按用户实时权限校验。
    """
    zero_perm_user = SimpleNamespace(
        id=9,
        name="alice",
        is_active=True,
        is_superuser=False,
        permissions={"discovery": False, "search": False, "subscribe": False, "manage": False},
    )
    users = SimpleNamespace(async_get_by_id=AsyncMock(return_value=zero_perm_user))
    gateway = MoviePilotApiTool(session_id="web-agent:9:s1", user_id="9", data=SimpleNamespace(users=users))
    gateway.set_message_attr(channel=NotificationChannel.WebAgent.value, source="web-agent", username="alice")

    with patch("app.application.security.auth.build_superuser_token_payload") as borrow_admin:
        raw = asyncio.run(gateway.run("recommendation.list", query={"source": "tmdb_movies"}))
        payload = json.loads(raw)
        assert payload["success"] is False
        assert payload["error"] == "permission_denied"
        borrow_admin.assert_not_called()

    # 具备 manage.site 的受托管理用户可执行站点更新，无需 superuser
    site_manager = SimpleNamespace(
        id=10,
        name="bob",
        is_active=True,
        is_superuser=False,
        permissions={"manage": True, "features": {"manage.site": True}},
    )
    users_mgr = SimpleNamespace(async_get_by_id=AsyncMock(return_value=site_manager))
    mgr_gateway = MoviePilotApiTool(session_id="web-agent:10:s1", user_id="10", data=SimpleNamespace(users=users_mgr))
    mgr_gateway.set_message_attr(channel=NotificationChannel.WebAgent.value, source="web-agent", username="bob")

    with patch("app.agent.tools.impl.api.MoviePilotApiExecutor.execute", new=AsyncMock(return_value='{"success":true}')) as execute_mock:
        allowed_raw = asyncio.run(mgr_gateway.run("site.test", path_params={"site_id": 1}))
        assert json.loads(allowed_raw)["success"] is True
        execute_mock.assert_awaited_once()

        denied_raw = asyncio.run(mgr_gateway.run("config.system.get", query={"setting_key": "LLM_API_KEY"}))
        denied_payload = json.loads(denied_raw)
        assert denied_payload["success"] is False
        assert denied_payload["error"] == "permission_denied"


def test_web_agent_hides_admin_and_plugin_tools_jobs_and_host_paths() -> None:
    """
    普通网页用户会话不应向模型暴露管理员工具、插件工具、Jobs 中间件或宿主机路径信息。
    """
    normal_user = SimpleNamespace(
        id=12,
        name="viewer",
        is_active=True,
        is_superuser=False,
        permissions={"discovery": True, "search": False, "subscribe": False, "manage": False},
    )
    agent = _get_web_agent_type()(
        session_id="web-agent:12:chat",
        user_id="12",
        channel=NotificationChannel.WebAgent.value,
        source="web-agent",
        username="viewer",
    )
    normal_tool = _DummyTool(session_id="web-agent:12:chat", user_id="12")
    admin_tool = _DummyAdminTool(session_id="web-agent:12:chat", user_id="12")
    plugin_tool = _DummyTool(session_id="web-agent:12:chat", user_id="12", name="plugin_helper")
    object.__setattr__(plugin_tool, "_agent_tool_source", "plugin:DemoPlugin")

    captured: dict[str, object] = {}

    def _capture_create_agent(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    fake_llm = SimpleNamespace(
        _llm_type="fake",
        model="fake",
        profile={"max_input_tokens": 64000},
    )
    fake_llm.with_retry = lambda: fake_llm

    with (
        patch("app.agent.web.get_configured_user_id_lookup", return_value=lambda _uid: normal_user),
        patch.object(agent, "_initialize_tools", return_value=[normal_tool, admin_tool, plugin_tool]),
        patch.object(agent, "_initialize_subagent_tools", return_value=[normal_tool, admin_tool, plugin_tool]),
        patch.object(agent, "_initialize_mcp_tools", new=AsyncMock(return_value=[])),
        patch.object(agent, "_initialize_subagent_mcp_tools", new=AsyncMock(return_value=[])),
        patch.object(agent, "_initialize_llm", new=AsyncMock(return_value=fake_llm)),
        patch.object(orchestrator_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(orchestrator_module, "create_agent", side_effect=_capture_create_agent),
        patch.object(orchestrator_module.settings, "LLM_MAX_TOOLS", 0),
    ):
        tool_ctx = asyncio.run(agent._build_tool_context(False))
        agent._refresh_tool_context(tool_ctx)
        assert tool_ctx["is_admin"] is False
        assert tool_ctx["web_user_active"] is True
        assert tool_ctx["web_user_permissions"]["discovery"] is True

        sig_before = asyncio.run(agent._agent_bundle_signature(streaming=False))
        asyncio.run(agent._create_agent(streaming=False))

        # 权限修改后缓存签名必须变化，触发重建
        normal_user.permissions["search"] = True
        updated_ctx = asyncio.run(agent._build_tool_context(False))
        agent._refresh_tool_context(updated_ctx)
        sig_after = asyncio.run(agent._agent_bundle_signature(streaming=False))
        assert sig_after != sig_before

    exposed_names = [getattr(tool, "name", "") for tool in captured["tools"]]  # type: ignore[index]
    assert "dummy_tool" in exposed_names
    assert "dummy_admin_tool" not in exposed_names
    assert "plugin_helper" not in exposed_names
    assert not any(isinstance(mw, JobsMiddleware) for mw in captured["middleware"])  # type: ignore[index]

    system_prompt = str(captured["system_prompt"])
    assert "<user_permissions>" in system_prompt
    assert "普通网页登录用户" in system_prompt
    assert "关键运行路径" not in system_prompt
    assert "已安装的常用系统命令" not in system_prompt


def test_non_admin_prompt_omits_host_details_and_subagent_principal_stays_user() -> None:
    """
    非管理员提示词不得暴露宿主机路径，非管理员子代理不得自动提升为 SYSTEM_INTERNAL。
    """
    prompt_mgr = PromptManager()
    non_admin_prompt = prompt_mgr.get_agent_prompt(
        channel=NotificationChannel.WebAgent.value,
        is_admin=False,
        user_permissions_prompt="- 当前会话身份：普通网页登录用户",
    )
    assert "关键运行路径" not in non_admin_prompt
    assert "<user_permissions>" in non_admin_prompt

    context = ToolPolicyContext(
        session_id="web-agent:12:chat",
        user_id="12",
        origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN,
        auth_source=AuthSource.WEB_SESSION,
        agent_context={"is_admin": False, "web_user_active": True},
        channel=NotificationChannel.WebAgent.value,
        source="web-agent",
    )
    subagent_context = context.for_subagent()
    assert subagent_context.principal.role is PrincipalRole.USER


def test_policy_middleware_blocks_plugin_and_inactive_web_user() -> None:
    """
    策略中间件必须拦截普通网页用户调用插件工具，并拦截已停用网页用户的全部工具调用。
    """
    plugin_tool = _DummyTool(session_id="web-agent:12:chat", user_id="12", name="plugin_action")
    object.__setattr__(plugin_tool, "_agent_tool_source", "plugin:SecretPlugin")
    handler = AsyncMock(return_value="should-not-run")

    context = ToolPolicyContext(
        session_id="web-agent:12:chat",
        user_id="12",
        origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN,
        auth_source=AuthSource.WEB_SESSION,
        agent_context={"is_admin": False, "web_user_active": True},
        channel=NotificationChannel.WebAgent.value,
        source="web-agent",
    )
    middleware = AgentPolicyMiddleware(context=context)
    allowed, result = asyncio.run(
        middleware.execute_tool_call(tool=plugin_tool, arguments={}, handler=handler, invocation_id="p1")
    )
    assert allowed is False
    assert "插件工具仅允许系统管理员" in str(result.content)
    handler.assert_not_awaited()

    # 停用用户连普通工具也被拦截
    inactive_context = ToolPolicyContext(
        session_id="web-agent:12:chat",
        user_id="12",
        origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN,
        auth_source=AuthSource.WEB_SESSION,
        agent_context={"is_admin": False, "web_user_active": False},
        channel=NotificationChannel.WebAgent.value,
        source="web-agent",
    )
    normal_tool = _DummyTool(session_id="web-agent:12:chat", user_id="12")
    allowed2, result2 = asyncio.run(
        AgentPolicyMiddleware(context=inactive_context).execute_tool_call(
            tool=normal_tool, arguments={}, handler=handler, invocation_id="p2"
        )
    )
    assert allowed2 is False
    assert "已停用" in str(result2.content)
    handler.assert_not_awaited()


def test_non_admin_file_access_blocks_jobs_and_allows_own_uploads(tmp_path, monkeypatch) -> None:
    """
    普通用户禁止读写 Agent 后台 jobs 目录，但允许只读访问自己会话上传的临时附件。
    """
    config_path = tmp_path / "config"
    temp_path = tmp_path / "temp"
    monkeypatch.setattr(
        "app.agent.tools.base.get_runtime_setting",
        lambda name: config_path if name == "CONFIG_PATH" else (temp_path if name == "TEMP_PATH" else None),
    )

    jobs_file = config_path / "agent" / "jobs" / "daily" / "JOB.md"
    jobs_file.parent.mkdir(parents=True)
    jobs_file.write_text("admin job", encoding="utf-8")

    own_upload = temp_path / "agent_uploads" / "web-agent_12_chat" / "note.txt"
    own_upload.parent.mkdir(parents=True)
    own_upload.write_text("用户上传内容", encoding="utf-8")

    other_upload = temp_path / "agent_uploads" / "web-agent_99_other" / "secret.txt"
    other_upload.parent.mkdir(parents=True)
    other_upload.write_text("他人上传内容", encoding="utf-8")

    reader = ReadFileTool(session_id="web-agent:12:chat", user_id="12")
    reader.set_agent_context({"is_admin": False})
    writer = WriteFileTool(session_id="web-agent:12:chat", user_id="12")
    writer.set_agent_context({"is_admin": False})

    assert "后台任务配置仅允许系统管理员" in asyncio.run(reader.run(str(jobs_file)))
    assert "后台任务配置仅允许系统管理员" in asyncio.run(writer.run(str(jobs_file), "hijack", overwrite=True))
    assert "用户上传内容" in asyncio.run(reader.run(str(own_upload)))
    assert "只能写入" in asyncio.run(writer.run(str(own_upload), "mutate", overwrite=True))
    assert "只能读取" in asyncio.run(reader.run(str(other_upload)))


def test_download_and_download_history_isolate_regular_web_users(monkeypatch) -> None:
    """
    普通搜索/订阅网页用户只能查看自己的下载任务和下载历史，管理用户和管理员可查看全局。
    """
    alice = SimpleNamespace(
        id=5,
        name="alice",
        is_active=True,
        is_superuser=False,
        permissions={"search": True, "manage": False},
    )
    no_perm = SimpleNamespace(
        id=6,
        name="guest",
        is_active=True,
        is_superuser=False,
        permissions={"discovery": True, "search": False, "subscribe": False, "manage": False},
    )
    manager = SimpleNamespace(
        id=7,
        name="ops",
        is_active=True,
        is_superuser=False,
        permissions={"manage": True, "features": {"manage.downloading": True}},
    )

    assert get_current_download_viewer(alice) is alice
    assert get_current_download_viewer(manager) is manager
    with pytest.raises(HTTPException) as exc_info:
        get_current_download_viewer(no_perm)
    assert exc_info.value.status_code == 403

    fake_tasks = [
        SimpleNamespace(username="alice", title="A"),
        SimpleNamespace(username="bob", title="B"),
    ]
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: SimpleNamespace(downloading=lambda _name: fake_tasks))
    assert [t.title for t in download_endpoint.current(_=alice)] == ["A"]
    assert [t.title for t in download_endpoint.current(_=manager)] == ["A", "B"]

    # 下载历史接口同样按用户隔离
    query = SimpleNamespace(
        list_download=AsyncMock(return_value=["alice-record"]),
        count_download=AsyncMock(return_value=1),
    )
    monkeypatch.setattr("app.application.security.user.get_configured_user_id_lookup", lambda: lambda uid: alice if uid == 5 else manager)

    alice_token = TokenPayload(sub=5, username="alice", super_user=False)
    resp = Response()
    records = asyncio.run(history_endpoint.download_history(page=1, count=10, query=query, _=alice_token, response=resp))
    assert records == ["alice-record"]
    query.list_download.assert_awaited_once_with(page=1, count=10, username="alice")
    query.count_download.assert_awaited_once_with(username="alice")

    query.list_download.reset_mock()
    query.count_download.reset_mock()
    manager_token = TokenPayload(sub=7, username="ops", super_user=False)
    asyncio.run(history_endpoint.download_history(page=1, count=10, query=query, _=manager_token, response=Response()))
    query.list_download.assert_awaited_once_with(page=1, count=10, username=None)


def test_web_message_endpoint_binds_user_id_and_blocks_non_admin_commands(monkeypatch) -> None:
    """
    POST /message/web 允许普通激活用户发送消息并传递真实 user_id，同时拦截非管理员传统斜杠命令。
    """
    recorded_calls: list[dict[str, object]] = []
    fake_chain = SimpleNamespace(handle_message=lambda **kwargs: recorded_calls.append(kwargs))
    monkeypatch.setattr(message_endpoint, "MessageChain", lambda: fake_chain)

    regular_user = SimpleNamespace(id=15, name="alice", is_superuser=False, is_active=True)
    result = asyncio.run(
        message_endpoint.web_message(
            request=_build_json_request({"text": "帮我找电影"}),
            current_user=regular_user,
        )
    )
    assert result.success is True
    assert recorded_calls == [
        {
            "channel": NotificationChannel.Web,
            "source": "alice",
            "userid": "15",
            "username": "alice",
            "text": "帮我找电影",
            "images": None,
            "is_channel_admin": False,
        }
    ]

    # 普通用户在 Web 渠道发送传统斜杠命令时应被拦截
    chain = MessageChain()
    posted: list[object] = []
    send_event = MagicMock()
    monkeypatch.setattr(chain, "post_message", lambda message: posted.append(message))
    monkeypatch.setattr(chain, "eventmanager", SimpleNamespace(send_event=send_event))
    monkeypatch.setattr(chain, "_plugin_input_interaction_handler", lambda: SimpleNamespace(handle_text=lambda **_kw: False))

    chain._handle_message_core(
        channel=NotificationChannel.Web,
        source="alice",
        userid="15",
        username="alice",
        text="/restart",
        is_channel_admin=False,
    )
    send_event.assert_not_called()
    assert posted and "只有管理员才有权限执行此命令" in str(getattr(posted[0], "title", ""))
