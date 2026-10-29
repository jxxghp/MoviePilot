"""
验证渠道计划任务的执行身份、旧任务确认绑定与失败结果，不访问真实模型或通知服务。
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.agent.identity import AgentIdentityError, require_execution_user, resolve_task_user
from app.agent.manager import AgentManager
from app.agent.orchestrator import AGENT_EXECUTION_ERROR_PREFIX, MoviePilotAgent
from app.agent.tools.impl.agent_task import AgentTaskTool
from app.agent.tools.impl.api import MoviePilotApiTool
from app.db.adapters.agent import TransactionalAgentTaskRepository
from app.db.adapters.user import TransactionalUserRepository
from app.db.models.agenttask import AgentTask
from app.db.models.agenttaskrun import AgentTaskRun
from app.db.models.user import User
from app.db.session import SessionFactory, async_session_scope
from app.runtime.config import settings


@pytest.fixture
def identity_case(db, monkeypatch):
    """
    装配临时数据库中的真实渠道绑定、任务仓储与受控模型和通知边界。
    """
    db.watermark(AgentTask, AgentTaskRun)
    account = db.add(User(
        name=f"task-owner-{uuid4().hex}", is_active=True, is_superuser=False,
        settings={"telegram_userid": "telegram-sender"},
    ))
    users = TransactionalUserRepository(sync_session=SessionFactory, async_session=async_session_scope)
    data = SimpleNamespace(
        users=users, tasks=TransactionalAgentTaskRepository(SessionFactory),
        chat=SimpleNamespace(get_sync=lambda **_kwargs: None),
    )
    tool = AgentTaskTool(session_id="telegram-session", user_id="telegram-sender", data=data)
    tool.set_message_attr(channel="Telegram", source="test-bot", username="telegram-sender")
    tool.set_agent_context({"is_admin": True})
    manager = AgentManager()
    manager._data = data
    manager.process_message = AsyncMock(return_value="已完成入库重试")
    notify = AsyncMock()
    monkeypatch.setattr(settings, "AI_AGENT_ENABLE", True)
    monkeypatch.setattr("app.agent.tasks.AgentChain.async_post_message", notify)
    monkeypatch.setattr("app.application.scheduling.update_agent_task_job", lambda _task_id: None)
    return SimpleNamespace(
        account=account, users=users, data=data, tool=tool, manager=manager, notify=notify, db=db,
    )


async def _create_task(case):
    """
    从真实渠道入口创建计划，并返回提交后的冻结任务快照。
    """
    result = await case.tool.run(
        action="create", name="失败入库重试", content="查询失败入库历史并重试",
        trigger_type="cron", trigger="0 * * * *",
    )
    return case.data.tasks.get(json.loads(result)["id"])


def _legacy_task(case):
    """
    保存升级前把 Telegram 发送者同时写入 user_id 和 username 的旧任务。
    """
    return case.data.tasks.add(
        name="旧入库重试", content="查询失败入库历史并重试", trigger_type="cron",
        cron_expression="0 * * * *", user_id="telegram-sender", username="telegram-sender",
        session_id="telegram-session", channel="Telegram", source="test-bot", original_chat_id="group",
    )


@pytest.mark.asyncio
async def test_channel_create_pins_real_account_and_preserves_notification_context(identity_case):
    """
    发送者 ID 保留为会话 owner，执行主体使用真实账号，不依赖旧会话昵称。
    """
    case = identity_case
    task = await _create_task(case)
    assert task.user_id == "telegram-sender"
    assert task.username == case.account.name
    assert task.execution_user_id == case.account.id
    assert task.execution_user_identity == case.account.notification_identity
    assert (task.channel, task.source) == ("Telegram", "test-bot")
    assert await case.manager.execute_scheduled_task(task.id) == (True, "已完成入库重试")
    kwargs = case.manager.process_message.await_args.kwargs
    assert kwargs["user_id"] == "telegram-sender"
    assert kwargs["username"] == case.account.name
    assert kwargs["execution_user_id"] == case.account.id
    assert kwargs["execution_user_identity"] == case.account.notification_identity
    assert kwargs["channel"] is None and kwargs["source"] is None
    run = case.data.tasks.list_runs(task.id)[0]
    assert run.execution_user_id == task.execution_user_id
    assert run.execution_user_identity == task.execution_user_identity
    assert run.status == "success"


@pytest.mark.parametrize("state", ["unbound", "ambiguous", "inactive"])
@pytest.mark.asyncio
async def test_invalid_channel_binding_is_rejected_before_creation(identity_case, state):
    """
    未绑定、重复绑定和停用账号均在创建阶段拒绝，也不能借用同名账号。
    """
    case = identity_case
    changes = {"settings": {}} if state == "unbound" else {"is_active": state != "inactive"}
    case.db.session.execute(update(User).where(User.id == case.account.id).values(**changes))
    case.db.session.commit()
    if state == "ambiguous":
        case.db.add(User(name=f"other-{uuid4().hex}", settings={"telegram_userid": "telegram-sender"}))
    case.db.add(User(name="telegram-sender", is_active=True, is_superuser=True))
    result = await case.tool.run(
        action="create", name="不应创建", content="重试", trigger_type="cron", trigger="0 * * * *",
    )
    assert "唯一" in result and "账号绑定" in result
    assert case.data.tasks.list(user_id="telegram-sender") == []


@pytest.mark.parametrize("state", ["inactive", "deleted", "reused_id"])
@pytest.mark.asyncio
async def test_invalid_pinned_account_fails_before_model_and_records_failure(identity_case, state):
    """
    执行账号失效时不运行模型，且计划和运行记录均失败；同名或同主键新账号不能接管。
    """
    case = identity_case
    task = await _create_task(case)
    account_id = case.account.id
    if state == "inactive":
        case.db.session.execute(update(User).where(User.id == account_id).values(is_active=False))
        case.db.session.commit()
    else:
        case.db.session.delete(case.account)
        case.db.session.commit()
        case.db.add(User(
            id=account_id if state == "reused_id" else account_id + 1,
            name=task.username, is_active=True, is_superuser=True,
        ))
    success, result = await case.manager.execute_scheduled_task(task.id)
    assert success is False and "任务执行账号" in result
    case.manager.process_message.assert_not_awaited()
    assert case.data.tasks.get(task.id).last_status == "failed"
    assert case.data.tasks.list_runs(task.id)[0].status == "failed"
    notification = case.notify.await_args.args[0]
    assert notification.username is None
    assert notification.channel.value == "Telegram" and notification.userid == "telegram-sender"


@pytest.mark.asyncio
async def test_legacy_task_requires_explicit_original_session_update(identity_case):
    """
    旧任务保持证据且先记录失败，原会话确认绑定后无需删除即可正常执行。
    """
    case = identity_case
    task = _legacy_task(case)
    success, result = await case.manager.execute_scheduled_task(task.id)
    assert success is False and "agent_task update" in result
    case.manager.process_message.assert_not_awaited()
    assert case.data.tasks.get(task.id).execution_user_id is None
    await case.tool.run(action="update", task_id=task.id, name=task.name)
    rebound = case.data.tasks.get(task.id)
    assert rebound.execution_user_id == case.account.id
    assert rebound.username == case.account.name
    assert rebound.user_id == task.user_id
    assert await case.manager.execute_scheduled_task(task.id) == (True, "已完成入库重试")
    assert [run.status for run in case.data.tasks.list_runs(task.id)] == ["success", "failed"]
    assert case.data.tasks.get(task.id).run_count == 2


@pytest.mark.asyncio
async def test_legacy_rebind_rejects_other_session_and_can_pause_without_binding(identity_case):
    """
    旧任务不能从其他渠道同号会话重绑，未绑定用户仍可暂停自己的旧任务。
    """
    case = identity_case
    task = _legacy_task(case)
    case.tool._session_id = "another-session"
    assert "原会话" in await case.tool.run(action="update", task_id=task.id, name="修改")
    assert case.data.tasks.get(task.id).execution_user_id is None
    result = json.loads(await case.tool.run(action="update", task_id=task.id, enabled=False))
    assert result["enabled"] is False


@pytest.mark.parametrize("require_admin", [False, True])
@pytest.mark.asyncio
async def test_scheduled_api_operations_use_one_pinned_account(identity_case, require_admin, monkeypatch):
    """
    普通与管理员 API 始终解析同一执行账号，后台管理员标记不能提升该账号权限。
    """
    case = identity_case
    task = await _create_task(case)
    gateway = MoviePilotApiTool(session_id=task.session_id, user_id=task.user_id, data=case.data)
    gateway.set_message_attr(channel=None, source=None, username="spoofed-admin")
    context = {
        "is_admin": True, "execution_user_id": task.execution_user_id,
        "execution_user_identity": task.execution_user_identity,
    }
    gateway.set_agent_context(context)
    host_identity = AsyncMock(side_effect=AssertionError("不应借用宿主管理员"))
    monkeypatch.setattr(MoviePilotApiTool, "_resolve_superuser_integration_identity", host_identity)
    assert await gateway._resolve_api_identity(require_system_admin=require_admin) == (
        str(case.account.id), case.account.name, False,
    )
    result = json.loads(await gateway.run("plugin.ratings"))
    assert result["error"] == "permission_denied"
    assert "管理员权限" in context["execution_identity_error"]["message"]
    host_identity.assert_not_awaited()
    executor, is_admin = await gateway._get_executor(public_read_only=True)
    assert executor._context.user_id == str(case.account.id) and is_admin is False


@pytest.mark.asyncio
async def test_scheduled_permission_context_uses_current_role_and_records_host_failure(identity_case, monkeypatch):
    """
    计划任务使用账号实时权限，模型的完成文案不能覆盖宿主身份阻塞。
    """
    case = identity_case
    agent = MoviePilotAgent(session_id="identity-context", user_id="telegram-sender", data=case.data)
    agent.execution_user_id = case.account.id
    agent.execution_user_identity = case.account.notification_identity
    assert await agent._is_system_admin_context() is False
    case.db.session.execute(update(User).where(User.id == case.account.id).values(is_superuser=True))
    case.db.session.commit()
    assert await agent._is_system_admin_context() is True
    monkeypatch.setattr(MoviePilotAgent, "_process", AsyncMock(return_value="模型声称完成"))
    agent._tool_context = {"execution_identity_error": {"message": "身份校验阻塞"}}
    assert (await agent.process("执行")).startswith(AGENT_EXECUTION_ERROR_PREFIX)
    agent.execution_user_id = None
    agent.execution_user_identity = None
    assert await agent.process("继续") == "模型声称完成"


@pytest.mark.parametrize("status_code", [400, 401, 403])
@pytest.mark.asyncio
async def test_scheduled_http_identity_denial_is_recorded(identity_case, monkeypatch, status_code):
    """
    后端返回的鉴权拒绝也作为宿主身份失败保存，普通输入校验失败不误分类。
    """
    case = identity_case
    gateway = MoviePilotApiTool(session_id="http-denial", user_id="telegram-sender", data=case.data)
    context = {
        "execution_user_id": case.account.id,
        "execution_user_identity": case.account.notification_identity,
        "execution_identity_error": {},
    }
    gateway.set_agent_context(context)
    executor = SimpleNamespace(execute=AsyncMock(return_value=json.dumps({
        "success": False, "error": "api_error", "status_code": status_code,
    })))
    monkeypatch.setattr(MoviePilotApiTool, "_get_executor", AsyncMock(return_value=(executor, False)))
    response = json.loads(await gateway.run("download.history.list"))
    assert response["success"] is False
    assert bool(context["execution_identity_error"]) is (status_code in {401, 403})


@pytest.mark.parametrize("entry", ["web", "integration", "background", "nested"])
@pytest.mark.asyncio
async def test_supported_task_entrypoints_pin_real_account(identity_case, entry, monkeypatch):
    """
    Web 持久化 ID、已认证 API、宿主用户名与嵌套任务均转换为同一真实主体。
    """
    case = identity_case
    arguments = {
        "user_id": str(case.account.id), "username": case.account.name,
        "channel": "WebAgent" if entry == "web" else None,
        "source": "api" if entry == "integration" else None, "is_admin": entry == "integration",
    }
    if entry == "integration":
        monkeypatch.setattr("app.application.security.auth.build_superuser_token_payload", lambda: SimpleNamespace(sub=case.account.id))
    if entry == "nested":
        arguments.update(execution_user_id=case.account.id, execution_user_identity=case.account.notification_identity)
    assert (await resolve_task_user(case.users, **arguments)).id == case.account.id


@pytest.mark.asyncio
async def test_missing_context_and_missing_web_account_fail_closed(identity_case):
    """
    缺少宿主数据上下文或 Web 用户 ID 无效时不能利用用户名回退。
    """
    with pytest.raises(AgentIdentityError, match="未装配"):
        await require_execution_user(None, 1)
    with pytest.raises(AgentIdentityError, match="唯一"):
        await resolve_task_user(
            identity_case.users, user_id="not-an-id", username=identity_case.account.name,
            channel="WebAgent", source="web", is_admin=True,
        )
    with pytest.raises(AgentIdentityError, match="未通过管理员身份验证"):
        await resolve_task_user(
            identity_case.users, user_id="api-client", username=identity_case.account.name,
            channel=None, source="api", is_admin=False,
        )


@pytest.mark.asyncio
async def test_scheduled_subagent_inherits_pinned_subject_and_reports_identity_failure(identity_case, monkeypatch):
    """
    子代理使用相同执行账号并共享错误槽，账号中途失效不能被最终模型回复掩盖。
    """
    case = identity_case
    agent = MoviePilotAgent(session_id="subagent-identity", user_id="telegram-sender", data=case.data)
    agent.execution_user_id = case.account.id
    agent.execution_user_identity = case.account.notification_identity
    agent._tool_context = await agent._build_tool_context(False)
    factory = SimpleNamespace(create_tools=Mock(return_value=[]))
    monkeypatch.setattr("app.agent.loader.get_tool_factory", lambda: factory)
    assert agent._initialize_subagent_tools() == []
    context = factory.create_tools.call_args.kwargs["agent_context"]
    assert context["execution_user_id"] == case.account.id
    assert context["execution_user_identity"] == case.account.notification_identity
    assert context["execution_identity_error"] is agent._tool_context["execution_identity_error"]
    gateway = MoviePilotApiTool(session_id=agent.session_id, user_id=agent.user_id, data=case.data)
    gateway.set_agent_context(context)
    case.db.session.execute(update(User).where(User.id == case.account.id).values(is_active=False))
    case.db.session.commit()
    response = json.loads(await gateway.run("download.history.list"))
    assert response["success"] is False and "已停用" in response["message"]
    monkeypatch.setattr(MoviePilotAgent, "_process", AsyncMock(return_value="模型声称完成"))
    assert (await agent.process("继续执行")).startswith(AGENT_EXECUTION_ERROR_PREFIX)


@pytest.mark.asyncio
async def test_scheduled_administrator_tool_denial_is_recorded_in_shared_state(identity_case):
    """
    非 API 管理工具的权限拒绝同样阻止计划任务被最终模型回复标为成功。
    """
    case = identity_case
    context = {
        "is_admin": False, "execution_user_id": case.account.id,
        "execution_user_identity": case.account.notification_identity, "execution_identity_error": {},
    }
    case.tool.set_agent_context(context)
    result = json.loads(await case.tool.ainvoke({"action": "list"}))
    assert result["success"] is False
    assert "系统管理员权限" in context["execution_identity_error"]["message"]


@pytest.mark.asyncio
async def test_confirmed_task_subject_cannot_be_replaced_by_update(identity_case):
    """
    仓储以条件更新拒绝再次确认主体，避免并发重绑覆盖已确定的执行账号。
    """
    case = identity_case
    task = await _create_task(case)
    assert case.data.tasks.update(task.id, {
        "execution_user_id": case.account.id + 1, "execution_user_identity": "another-account",
        "name": "不能提交的修改",
    }) is False
    unchanged = case.data.tasks.get(task.id)
    assert unchanged.execution_user_id == task.execution_user_id and unchanged.name == task.name
