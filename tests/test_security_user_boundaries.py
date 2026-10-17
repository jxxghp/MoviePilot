"""
下载管理与 Web Agent 会话的用户权限回归。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, messages_to_dict
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.web.security.access import verify_token
from app.agent.memory import MemoryManager
from app.agent.orchestrator import MoviePilotAgent
from app.agent.web import _get_web_agent_type
from app.api.dependencies.agent import get_agent_chat_service
from app.api.dependencies.auth import get_current_active_user
from app.api.endpoints import agent as agent_endpoint
from app.api.endpoints import download as download_endpoint
from app.application.messaging import agent as web_agent
from app.application.messaging.chat import AgentChatService
from app.db.models.agentchat import AgentChat


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/", None),
    ("GET", "/start/" + "a" * 40, None),
    ("GET", "/stop/" + "a" * 40, None),
    ("PATCH", "/" + "a" * 40, {"action": "stop"}),
    ("DELETE", "/" + "a" * 40, None),
])
def test_download_management_rejects_user_without_manage(monkeypatch, method, path, payload):
    """
    没有管理权限的登录用户不能读取或控制全局下载器任务。
    """
    principal = SimpleNamespace(id=2, name="ordinary", is_superuser=False, permissions={})
    chain = Mock()
    chain.return_value.downloading.return_value = []
    chain.return_value.set_downloading.return_value = True
    chain.return_value.remove_downloading.return_value = True
    monkeypatch.setattr(download_endpoint, "DownloadChain", chain)
    app = FastAPI()
    app.include_router(download_endpoint.router, prefix="/download")
    app.dependency_overrides[get_current_active_user] = lambda: principal
    app.dependency_overrides[verify_token] = lambda: SimpleNamespace(
        sub=principal.id, username=principal.name, super_user=False,
    )
    with TestClient(app) as client:
        response = client.request(method, f"/download{path}", json=payload)
    assert response.status_code == 403
    chain.assert_not_called()


@pytest.mark.parametrize("principal", [
    SimpleNamespace(id=2, name="manager", is_superuser=False, permissions={"manage": True}),
    SimpleNamespace(id=1, name="admin", is_superuser=True, permissions={}),
])
def test_download_management_preserves_manage_and_superuser_access(monkeypatch, principal):
    """
    管理用户和超级管理员仍可读取并删除下载任务。
    """
    chain = Mock()
    chain.downloading.return_value = []
    chain.remove_downloading.return_value = True
    monkeypatch.setattr(download_endpoint, "DownloadChain", lambda: chain)
    app = FastAPI()
    app.include_router(download_endpoint.router, prefix="/download")
    app.dependency_overrides[get_current_active_user] = lambda: principal
    with TestClient(app) as client:
        listed = client.get("/download/")
        deleted = client.delete("/download/" + "a" * 40)
    assert listed.status_code == deleted.status_code == 200
    chain.downloading.assert_called_once_with(None)
    chain.remove_downloading.assert_called_once_with("a" * 40, name=None)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_prefixed_web_session_rejects_foreign_owner(monkeypatch, asynchronous):
    """
    已分配的 Web 会话 ID 也必须检查当前用户，不能由前缀跳过鉴权。
    """
    principal = SimpleNamespace(id=2, name="attacker", is_superuser=False)
    record = SimpleNamespace(user_id="1", username="victim")
    service = SimpleNamespace(get=AsyncMock(return_value=record), get_sync=Mock(return_value=record))
    monkeypatch.setattr(web_agent, "get_configured_agent_chat_service", lambda: service)
    with pytest.raises(PermissionError):
        if asynchronous:
            asyncio.run(web_agent.build_web_agent_session_id_async(principal, "web-agent:victim", service))
        else:
            web_agent.build_web_agent_session_id(principal, "web-agent:victim")


def test_stream_denies_foreign_session_before_starting_agent(monkeypatch):
    """
    流入口在调度 Agent 前拒绝其他用户已有的会话。
    """
    principal = SimpleNamespace(id=2, name="attacker", is_superuser=False)
    service = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(user_id="1", username="victim")))
    manager_lookup = Mock()
    monkeypatch.setattr(web_agent.agent_application, "get_running_agent_manager", manager_lookup)

    async def scenario():
        """
        消费拒绝事件，证明不会启动后台模型或任务。
        """
        result = await web_agent.build_web_agent_stream(
            web_agent.WebAgentStreamCommand(text="继续", session_id="web-agent:victim"),
            current_user=principal,
            is_disconnected=AsyncMock(return_value=False),
            protected_transport_supported=False,
            service=service,
            persistence=Mock(),
        )
        return [event async for event in result.events]

    assert asyncio.run(scenario()) == [{"type": "error", "message": "会话不存在或无权访问"}]
    manager_lookup.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("owner,allowed", [("1", False), (None, True), ("", True)])
def test_memory_fallback_only_restores_unowned_legacy_sessions(asynchronous, owner, allowed):
    """
    旧记录兜底只恢复无归属会话，不能恢复其他明确归属用户的上下文。
    """
    record = SimpleNamespace(user_id=owner, agent_messages=messages_to_dict([HumanMessage(content="private")]))
    service = SimpleNamespace(get=AsyncMock(side_effect=[None, record]), get_sync=Mock(side_effect=[None, record]))
    memory = MemoryManager(chat=service)
    messages = (
        asyncio.run(memory.async_get_agent_messages("session", "2"))
        if asynchronous else memory.get_agent_messages("session", "2")
    )
    assert [message.content for message in messages] == (["private"] if allowed else [])


def test_display_save_rechecks_owner_before_writing():
    """
    即使调用方绕过流入口，展示快照写回也不能覆盖其他用户记录。
    """
    principal = SimpleNamespace(id=2, name="attacker", is_superuser=False)
    service = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(user_id="1", username="victim")))
    persistence = SimpleNamespace(async_save_display_messages=AsyncMock())
    with pytest.raises(PermissionError):
        asyncio.run(web_agent.save_web_agent_display_snapshot(
            session_id="web-agent:victim", current_user=principal, messages=[],
            service=service, persistence=persistence,
        ))
    persistence.async_save_display_messages.assert_not_awaited()


def test_web_session_does_not_reuse_other_owner_with_recycled_name(monkeypatch):
    """
    已有持久化用户 ID 时，重复用户名不能授予另一个账号会话访问权。
    """
    principal = SimpleNamespace(id=2, name="same-name", is_superuser=False)
    record = SimpleNamespace(user_id="1", username="same-name")
    service = SimpleNamespace(get=AsyncMock(return_value=record))
    monkeypatch.setattr(web_agent, "get_configured_agent_chat_service", lambda: service)
    with pytest.raises(PermissionError):
        asyncio.run(web_agent.build_web_agent_session_id_async(principal, "web-agent:victim", service))


def test_new_web_session_uses_stable_user_id(monkeypatch):
    """
    账号改名保持会话身份，复用旧用户名的新账号不能生成同一个会话 ID。
    """
    service = SimpleNamespace(get_sync=Mock(return_value=None))
    monkeypatch.setattr(web_agent, "get_configured_agent_chat_service", lambda: service)
    before = SimpleNamespace(id=1, name="old", is_superuser=False)
    renamed = SimpleNamespace(id=1, name="new", is_superuser=False)
    replacement = SimpleNamespace(id=2, name="old", is_superuser=False)
    session_id = web_agent.build_web_agent_session_id(before, "browser-session")
    assert web_agent.build_web_agent_session_id(renamed, "browser-session") == session_id
    assert web_agent.build_web_agent_session_id(replacement, "browser-session") != session_id


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unpersisted_web_session_can_only_be_reused_in_its_user_namespace(monkeypatch, asynchronous):
    """
    附件上传后尚未落库的会话可继续使用，其他用户不能占用其活动队列。
    """
    service = SimpleNamespace(get_sync=Mock(return_value=None), get=AsyncMock(return_value=None))
    monkeypatch.setattr(web_agent, "get_configured_agent_chat_service", lambda: service)
    owner = SimpleNamespace(id=1, name="owner", is_superuser=False)
    other = SimpleNamespace(id=2, name="other", is_superuser=False)
    session_id = web_agent.build_web_agent_session_id(owner, "browser-session")
    if asynchronous:
        assert asyncio.run(web_agent.build_web_agent_session_id_async(owner, session_id, service)) == session_id
        with pytest.raises(PermissionError):
            asyncio.run(web_agent.build_web_agent_session_id_async(other, session_id, service))
    else:
        assert web_agent.build_web_agent_session_id(owner, session_id) == session_id
        with pytest.raises(PermissionError):
            web_agent.build_web_agent_session_id(other, session_id)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("username,expected", [(None, []), ("other", []), ("legacy-owner", ["private"])])
def test_memory_legacy_username_requires_authorized_identity(asynchronous, username, expected):
    """
    有用户名的旧记录也有归属，不能当作无主会话直接恢复或复用缓存。
    """
    record = SimpleNamespace(user_id=None, username="legacy-owner",
                             agent_messages=messages_to_dict([HumanMessage(content="private")]))
    service = SimpleNamespace(get=AsyncMock(side_effect=[None, record]), get_sync=Mock(side_effect=[None, record]))
    memory = MemoryManager(chat=service)
    if asynchronous:
        messages = asyncio.run(memory.async_get_agent_messages("session", "2", legacy_username=username))
        if expected:
            assert asyncio.run(memory.async_get_agent_messages("session", "2")) == []
    else:
        messages = memory.get_agent_messages("session", "2", legacy_username=username)
        if expected:
            assert memory.get_agent_messages("session", "2") == []
    assert [message.content for message in messages] == expected


def test_history_access_does_not_accept_recycled_username():
    """
    详情和删除共用的授权规则只对无 ID 旧记录使用用户名。
    """
    principal = SimpleNamespace(id=2, name="same-name", is_superuser=False)
    assert not AgentChatService.can_access(SimpleNamespace(user_id="1", username="same-name"), principal)
    assert AgentChatService.can_access(SimpleNamespace(user_id=None, username="same-name"), principal)


def test_upload_denies_foreign_session_without_saving_file(monkeypatch):
    """
    上传外部用户会话附件返回统一 404，且不读取或保存上传文件。
    """
    principal = SimpleNamespace(id=2, name="attacker", is_superuser=False)
    service = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(user_id="1", username="victim")))
    save = AsyncMock()
    monkeypatch.setattr(web_agent, "save_web_agent_upload", save)
    app = FastAPI()
    app.include_router(agent_endpoint.router, prefix="/agent")
    app.dependency_overrides[get_current_active_user] = lambda: principal
    app.dependency_overrides[get_agent_chat_service] = lambda: service
    with TestClient(app) as client:
        response = client.post("/agent/upload", data={"session_id": "web-agent:victim"},
                               files={"file": ("test.txt", b"test", "text/plain")})
    assert response.status_code == 404
    assert response.json()["detail"] == "会话不存在或无权访问"
    save.assert_not_awaited()


@pytest.mark.parametrize("method,path", [
    ("GET", "/agent/sessions/web-agent:victim"),
    ("POST", "/agent/sessions/web-agent:victim/stop"),
])
def test_session_endpoints_deny_foreign_session_without_server_error(monkeypatch, method, path):
    """
    读取或停止他人会话返回统一拒绝结果，不能抛出 500 或触达运行中的 Agent。
    """
    principal = SimpleNamespace(id=2, name="attacker", is_superuser=False)
    service = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(user_id="1", username="victim")),
        get_accessible=AsyncMock(return_value=None),
    )
    manager = Mock()
    manager.stop_current_task = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_endpoint.agent_application, "get_running_agent_manager", lambda: manager)
    app = FastAPI()
    app.include_router(agent_endpoint.router, prefix="/agent")
    app.dependency_overrides[get_current_active_user] = lambda: principal
    app.dependency_overrides[get_agent_chat_service] = lambda: service
    with TestClient(app) as client:
        response = client.request(method, path)
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["message"] == "会话不存在或无权访问"
    manager.stop_current_task.assert_not_awaited()
    manager.is_session_busy.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_admin_memory_restore_requires_explicit_authorization_on_cache_reuse(asynchronous):
    """
    管理员恢复其他归属后，未授权调用也不能通过该管理员的缓存读取。
    """
    record = SimpleNamespace(user_id="1", agent_messages=messages_to_dict([HumanMessage(content="private")]))
    service = SimpleNamespace(get=AsyncMock(side_effect=[None, record]), get_sync=Mock(side_effect=[None, record]))
    memory = MemoryManager(chat=service)
    if asynchronous:
        messages = asyncio.run(memory.async_get_agent_messages("session", "2", allow_cross_owner=True))
        denied = asyncio.run(memory.async_get_agent_messages("session", "2"))
    else:
        messages = memory.get_agent_messages("session", "2", allow_cross_owner=True)
        denied = memory.get_agent_messages("session", "2")
    assert [message.content for message in messages] == ["private"]
    assert denied == []
    assert memory.get_memory("session", "2").user_id == "2"


@pytest.mark.parametrize("admin,expected", [(True, ["private"]), (False, [])])
def test_web_agent_admin_continuation_keeps_acting_identity(monkeypatch, admin, expected):
    """
    Web 续聊按真实管理员身份授权历史，执行人始终是当前用户。
    """
    record = SimpleNamespace(user_id="1", agent_messages=messages_to_dict([HumanMessage(content="private")]))
    memory = MemoryManager(chat=SimpleNamespace(get=AsyncMock(side_effect=[None, record])))
    agent = _get_web_agent_type()(session_id="web-agent:foreign", user_id="2", username="acting-user",
                                  channel="WebAgent", source="web-agent", memory=memory)
    monkeypatch.setattr("app.agent.web.get_configured_user_id_lookup",
                        lambda: lambda user_id: SimpleNamespace(is_superuser=admin))
    monkeypatch.setattr(agent, "prepare_chat_title", AsyncMock())
    monkeypatch.setattr(agent, "_save_display_history_messages", AsyncMock())
    execute = AsyncMock(return_value="ok")
    monkeypatch.setattr(agent, "_execute_agent", execute)
    assert asyncio.run(agent._process("continue")) == "ok"
    assert [message.content for message in execute.await_args.args[0][:-1]] == expected
    assert (agent.user_id, agent.username) == ("2", "acting-user")


@pytest.mark.parametrize("channel,source", [(None, None), ("Web", "openai"), ("Telegram", "bot")])
def test_implicit_admin_context_does_not_authorize_foreign_history(channel, source):
    """
    后台和协议入口的工具管理员语义不能扩散成任意会话历史授权。
    """
    agent = MoviePilotAgent(session_id="foreign", user_id="2", channel=channel, source=source,
                           is_channel_admin=True)
    assert asyncio.run(agent._can_restore_cross_owner_history()) is False


@pytest.mark.parametrize("asynchronous", [False, True])
def test_history_pagination_excludes_foreign_owner_with_same_name(asynchronous):
    """
    分页 SQL 在限制条数前排除同名异主记录，并兼容无 ID 的旧历史。
    """
    engine = create_engine("sqlite://")
    AgentChat.__table__.create(engine)
    try:
        with Session(engine) as session:
            session.add_all([
                AgentChat(session_id="foreign", user_id="1", username="same", updated_at="3"),
                AgentChat(session_id="own", user_id="2", username="renamed", updated_at="2"),
                AgentChat(session_id="legacy", user_id=None, username="same", updated_at="1"),
            ])
            session.commit()
            if asynchronous:
                records = asyncio.run(AgentChat.async_list_by_page(
                    SimpleNamespace(execute=AsyncMock(side_effect=session.execute)),
                    count=2, user_id="2", username="same",
                ))
            else:
                records = AgentChat.list_by_page(session, count=2, user_id="2", username="same")
            assert [record.session_id for record in records] == ["own", "legacy"]
    finally:
        engine.dispose()


def test_admin_display_save_preserves_original_owner():
    """
    管理员续聊可写回原会话展示历史，但不会把原归属替换成管理员。
    """
    principal = SimpleNamespace(id=2, name="acting-admin", is_superuser=True)
    record = SimpleNamespace(user_id="1", username="victim", channel="Telegram", source="bot",
                             original_chat_id="chat", client_session_id="client")
    service = SimpleNamespace(get=AsyncMock(return_value=record))
    persistence = SimpleNamespace(async_save_display_messages=AsyncMock())
    asyncio.run(web_agent.save_web_agent_display_snapshot(
        session_id="foreign", current_user=principal, messages=[], service=service, persistence=persistence,
    ))
    assert persistence.async_save_display_messages.await_args.kwargs["user_id"] == "1"
    assert persistence.async_save_display_messages.await_args.kwargs["username"] == "victim"


def test_admin_title_generation_preserves_cross_channel_session_owner(monkeypatch):
    """
    续聊首次生成标题时也必须保留原归属与渠道，不能通过 ensure_session 变更所有者。
    """
    record = SimpleNamespace(user_id="1", username="victim", channel="Telegram", source="bot",
                             original_chat_id="chat", title="新会话")
    service = SimpleNamespace(get=AsyncMock(side_effect=[None, record]))
    persistence = SimpleNamespace(async_update_title_if_empty=AsyncMock())
    monkeypatch.setattr("app.agent.orchestrator.get_configured_agent_chat_service", lambda: service)
    monkeypatch.setattr("app.agent.orchestrator.get_configured_agent_chat_persistence", lambda: persistence)
    monkeypatch.setattr("app.agent.web.get_configured_user_id_lookup",
                        lambda: lambda _: SimpleNamespace(is_superuser=True))
    agent = _get_web_agent_type()(session_id="foreign", user_id="2", username="acting-admin",
                                  channel="WebAgent", source="web-agent")
    monkeypatch.setattr(agent, "_generate_chat_title", AsyncMock(return_value="新的标题"))
    monkeypatch.setattr("app.agent.orchestrator.has_custom_agent_chat_title", lambda _: False)
    asyncio.run(agent.prepare_chat_title("continue"))
    assert persistence.async_update_title_if_empty.await_args.kwargs == {
        "session_id": "foreign", "user_id": "1", "title": "新的标题", "username": "victim",
        "channel": "Telegram", "source": "bot", "original_chat_id": "chat",
    }
