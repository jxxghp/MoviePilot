"""以实际执行边界验证只读子代理不会获得父任务的写权限。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.mcp import AgentMcpToolSpec
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.middleware.subagents import _builtin_subagent_profiles, _SubAgentAgentProvider
from app.agent.policy.api import API_OPERATION_SPECS
from app.agent.policy.contracts import ActionEffect, AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.mcp import McpExternalTool
from app.schemas.agent import AgentMcpServerConfig


class _Model(FakeMessagesListChatModel):
    """脚本模型只用于向生产子图提交不应执行的动作，不作为智能评分。"""

    def bind_tools(self, tools, **kwargs):
        """保留真实工具执行图，模型响应完全离线。"""
        return self


def _context(admin=True, origin=ToolOrigin.SUBAGENT):
    """创建不可由工具参数改写的宿主身份。"""
    return ToolPolicyContext(
        session_id="readonly", user_id="owner", origin=origin,
        principal_type=PrincipalType.SUBAGENT if origin is ToolOrigin.SUBAGENT else PrincipalType.HUMAN,
        auth_source=AuthSource.INTERNAL, agent_context={"is_admin": admin},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("operation_id", [
    spec.operation_id for spec in API_OPERATION_SPECS
    if spec.effect not in {ActionEffect.SAFE_READ, ActionEffect.SENSITIVE_READ}
])
async def test_every_write_operation_is_denied_before_handler(admin, operation_id):
    """全部登记的副作用操作均在 handler 前拒绝，普通角色也不能绕过。"""
    gateway = MoviePilotApiTool(session_id="readonly", user_id="owner")
    handler = AsyncMock(return_value="unexpected write")
    middleware = AgentPolicyMiddleware(context=_context(admin))
    allowed, result = await middleware.execute_tool_call(
        tool=gateway, arguments={"operation_id": operation_id}, handler=handler,
        invocation_id="write", enforce_decision=False,
    )
    assert allowed is False
    assert result.status == "error"
    assert json.loads(result.content)["error"] == "subagent_read_only"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_child_graph_denies_write_then_executes_valid_read():
    """真实默认子图必须零写入，并保留后续正常查询能力。"""
    executor = AsyncMock()
    executor.execute.return_value = '{"success":true,"data":[]}'
    gateway = MoviePilotApiTool(session_id="readonly", user_id="owner", executor=executor)
    gateway.set_agent_context({"is_admin": True})
    model = _Model(responses=[
        AIMessage(content="", tool_calls=[{
            "id": "write", "name": "moviepilot_api", "args": {
                "operation_id": "config.system.update", "body": {"setting_key": "LLM_MAX_ITERATIONS", "value": 128},
            },
        }]),
        AIMessage(content="", tool_calls=[{
            "id": "read", "name": "moviepilot_api", "args": {"operation_id": "site.list"},
        }]),
        AIMessage(content="已读取站点状态，未修改设置"),
    ])
    provider = _SubAgentAgentProvider(model=model, profiles=_builtin_subagent_profiles(), tools=[gateway])
    result = await provider.run_task(description="只读检查站点", subagent_type="general-purpose")
    assert result == "已读取站点状态，未修改设置"
    assert [call.args[0] for call in executor.execute.await_args_list] == ["site.list"]


@pytest.mark.asyncio
async def test_child_invalid_read_returns_operation_contract_for_retry():
    """只读操作参数错位时，回执必须带合同帮助子代理修正。"""
    gateway = MoviePilotApiTool(session_id="readonly", user_id="owner")
    allowed, result = await AgentPolicyMiddleware(context=_context()).execute_tool_call(
        tool=gateway,
        arguments={"operation_id": "subscription.find", "query": {"media_source": "themoviedb"}},
        handler=AsyncMock(),
        invocation_id="invalid-read",
        enforce_decision=False,
    )
    assert allowed is False
    payload = json.loads(result.content)
    assert payload["error"] == "subagent_read_only"
    assert payload["operation_id"] == "subscription.find"
    assert "path_params" in payload["input_contract"]["allowed_arguments"]
    assert "input_contract" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("show_secrets", [True, "true", "1", 1])
async def test_child_secret_read_uses_canonical_query_semantics(show_secrets):
    """不能利用布尔字符串或数字的 HTTP 转换请求原始敏感设置。"""
    gateway = MoviePilotApiTool(session_id="readonly", user_id="owner")
    handler = AsyncMock()
    allowed, result = await AgentPolicyMiddleware(context=_context()).execute_tool_call(
        tool=gateway, arguments={"operation_id": "config.system.get", "query": {"show_secrets": show_secrets}},
        handler=handler, invocation_id="secret", enforce_decision=False,
    )
    assert allowed is False
    assert result.status == "error"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_compatibility_read_tag_cannot_authorize_child_operation():
    """外部 MCP 的固定 Read 标签没有动作级语义，不能给只读子代理授权。"""
    spec = AgentMcpToolSpec(
        server=AgentMcpServerConfig(id="remote", name="Remote", transport="stdio", command="unused"),
        name="erase", agent_tool_name="remote_erase", description="External operation",
        input_schema={"type": "object"},
    )
    tool = McpExternalTool(spec, session_id="readonly", user_id="owner")
    handler = AsyncMock()
    allowed, result = await AgentPolicyMiddleware(context=_context()).execute_tool_call(
        tool=tool, arguments={}, handler=handler, invocation_id="mcp", enforce_decision=False,
    )
    assert allowed is False and result.status == "error"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,action,allowed", [
    ("execute_command", action, action in {"read", "wait"})
    for action in ("start", "run", "read", "wait", "write", "interrupt", "kill")
] + [
    ("browse_webpage", action, action in {"snapshot", "get_content", "screenshot", "wait", "list_tabs"})
    for action in ("goto", "snapshot", "get_content", "screenshot", "click", "click_ref", "fill", "fill_ref",
                   "select", "select_ref", "evaluate", "wait", "list_tabs", "open_tab", "focus_tab", "close_tab", "close_session")
] + [("persona", action, action == "list") for action in ("list", "switch", "update")]
  + [("agent_task", action, action == "list") for action in ("list", "create", "update", "run", "delete")])
async def test_mixed_tools_are_checked_by_action_despite_read_tag(name, action, allowed):
    """终端和浏览器的 Read 标签不能给输入、脚本、导航或关闭动作授权。"""
    tool = SimpleNamespace(name=name, tags=["read"])
    handler = AsyncMock(return_value="read result")
    permitted, result = await AgentPolicyMiddleware(context=_context()).execute_tool_call(
        tool=tool, arguments={"action": action}, handler=handler,
        invocation_id="mixed", enforce_decision=False,
    )
    assert permitted is allowed
    assert handler.await_count == int(allowed)
    if not allowed:
        assert result.status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"cookies": "session=private"}, {"user_agent": "changed"}])
async def test_browser_read_cannot_change_shared_session_identity(extra):
    """截图等读取动作也不能顺带替换共享浏览器的 Cookie 或身份参数。"""
    handler = AsyncMock()
    permitted, _ = await AgentPolicyMiddleware(context=_context()).execute_tool_call(
        tool=SimpleNamespace(name="browse_webpage", tags=["read"]),
        arguments={"action": "snapshot", **extra}, handler=handler,
        invocation_id="browser", enforce_decision=False,
    )
    assert permitted is False
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_child_cannot_close_terminal_input():
    """读取标签不能授权向已知会话写入最终输入或触发 EOF。"""
    handler = AsyncMock()
    permitted, result = await AgentPolicyMiddleware(context=_context()).execute_tool_call(
        tool=SimpleNamespace(name="execute_command", tags=["read"]),
        arguments={"action": "write", "session_id": "known-session", "input_text": "last", "close_stdin": True},
        handler=handler, invocation_id="terminal-eof", enforce_decision=False,
    )
    assert permitted is False and result.status == "error"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [None, {}, [], 1])
async def test_invalid_mixed_action_is_denied_without_interrupting_graph(action):
    """不合法的动作类型返回可恢复拒绝，不让集合查询异常打断子任务。"""
    handler = AsyncMock()
    permitted, result = await AgentPolicyMiddleware(context=_context()).execute_tool_call(
        tool=SimpleNamespace(name="execute_command", tags=["read"]),
        arguments={"action": action}, handler=handler,
        invocation_id="invalid", enforce_decision=False,
    )
    assert permitted is False and result.status == "error"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_parent_graph_keeps_authorized_api_write_path():
    """只读约束限定子代理，主代理已有写入路径继续实际执行。"""
    executor = AsyncMock()
    executor.execute.return_value = '{"success":true}'
    gateway = MoviePilotApiTool(session_id="readonly", user_id="owner", executor=executor)
    gateway.set_agent_context({"is_admin": True})
    model = _Model(responses=[AIMessage(content="", tool_calls=[{
        "id": "write", "name": "moviepilot_api", "args": {
            "operation_id": "config.system.update", "body": {"setting_key": "LLM_MAX_ITERATIONS", "value": 128},
        },
    }]), AIMessage(content="已执行")])
    graph = create_agent(model=model, tools=[gateway], middleware=[
        AgentPolicyMiddleware(context=_context(origin=ToolOrigin.AGENT_INTERACTIVE), tools=[gateway]),
    ])
    result = await graph.ainvoke({"messages": [HumanMessage(content="修改此设置")]})
    assert executor.execute.await_count == 1
    assert any(isinstance(message, ToolMessage) and message.status == "success" for message in result["messages"])
