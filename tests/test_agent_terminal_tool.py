"""终端分页必须完整穿过真实 ToolNode、策略和通用工具输出预算。"""

import json
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware.output import ToolOutputMiddleware
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.terminal.manager import _TerminalSessionManager
from app.agent.terminal.ownership import current_terminal_scope
from app.agent.terminal.session import _TerminalSession
from app.agent.tools.base import DEFAULT_TOOL_RESULT_MAX_CHARS
from app.agent.tools.impl import execute_command as command_module
from app.agent.tools.impl.execute_command import ExecuteCommandInput, ExecuteCommandTool

pytestmark = pytest.mark.usefixtures("terminal_scope")


class _PageModel(FakeMessagesListChatModel):
    """只固定两轮工具协议，真实分页和外层预算均由生产实现执行。"""

    def bind_tools(self, _tools: Any, **_kwargs: Any) -> "_PageModel":
        """接收图内工具，不产生真实模型或网络调用。"""
        return self


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [True, False])
async def test_terminal_page_or_error_reaches_model_without_outer_truncation(monkeypatch, partial):
    """大转义正文保留真实消费游标，小页错误也必须保持失败状态及恢复参数。"""
    manager = _TerminalSessionManager()
    session = _TerminalSession(owner=current_terminal_scope(), session_id="term-page-test", command="completed command", cwd=".", pid=123456789, use_pty=False)
    text = "\\\n\"\t" * 20000
    session.append_output("stdout", text.encode("utf-8"))
    session.mark_finished(0)
    session.finish_output()
    manager._sessions[session.session_id] = session
    monkeypatch.setattr(command_module, "get_terminal_session_manager", lambda: manager)
    tool = ExecuteCommandTool(session_id="page-chat", user_id="page-user")
    tool.set_agent_context({"is_admin": True})
    context = ToolPolicyContext(
        session_id="page-chat", user_id="page-user", origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN, auth_source=AuthSource.INTERNAL, agent_context={"is_admin": True},
    )
    arguments = {"action": "read", "session_id": session.session_id, "since_seq": 0,
                 "max_bytes": 65536 if partial else 1}
    if partial:
        arguments["since_offset"] = 0
    model = _PageModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "read-page", "name": tool.name, "args": arguments}]),
        AIMessage(content="已读取本页。"),
    ])
    graph = create_agent(model=model, tools=[tool], middleware=[AgentPolicyMiddleware(context=context), ToolOutputMiddleware(context)])
    try:
        result = await graph.ainvoke({"messages": [HumanMessage(content="读取命令输出")]})
        message = next(item for item in result["messages"] if isinstance(item, ToolMessage))
        payload = json.loads(message.content)
        assert len(message.content) <= DEFAULT_TOOL_RESULT_MAX_CHARS
        assert "tool_result_truncated" not in payload
        if partial:
            assert message.status == "success"
            assert payload["output_until_seq"] == 0
            assert payload["output_until_offset"] > 0
            assert payload["output_truncated"] is True
            assert ("\n[标准输出]\n" + text).startswith(payload["output"])
            assert len(payload["output"].encode("utf-8")) == payload["output_until_offset"]
        else:
            assert message.status == "error"
            assert payload["execution_outcome"] == "failed"
            assert payload["code"] == "read_limit_too_small"
            assert payload["minimum_read_bytes"] > 1
    finally:
        await manager.close()


@pytest.mark.parametrize("field", ["since_seq", "since_offset", "yield_time_ms"])
def test_terminal_cursor_schema_rejects_boolean_numbers(field):
    """模型参数不能在 Pydantic 转换时把布尔值变成游标或等待时间。"""
    with pytest.raises(ValueError):
        ExecuteCommandInput.model_validate({"action": "read", field: True})
