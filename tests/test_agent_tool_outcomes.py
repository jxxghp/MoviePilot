"""工具业务结果、异常和异步提交必须通过统一的执行状态进入模型与宿主回执。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from app.agent.api.executor import ApiExecutionContext, MoviePilotApiExecutor
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.policy.contracts import AuthSource, ExecutionOutcome, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.policy.orchestrator import AgentToolPolicyOrchestrator
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.mcp import McpExternalTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.agent.tools.result import EXECUTION_OUTCOME_KEY, ToolExecutionError, inspect_tool_result


def _context() -> ToolPolicyContext:
    """创建不访问外部服务的工具宿主上下文。"""
    return ToolPolicyContext(
        session_id="outcome-test", user_id="owner", origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN, auth_source=AuthSource.INTERNAL,
        agent_context={"is_admin": True},
    )


@pytest.mark.parametrize(("payload", "expected"), [
    ({"success": False, "message": "拒绝"}, ExecutionOutcome.FAILED),
    ({"state": False, "data": None}, ExecutionOutcome.FAILED),
    ({"error": "invalid_arguments"}, ExecutionOutcome.FAILED),
    ({"isError": True, "content": [{"type": "text", "text": "remote failed"}]}, ExecutionOutcome.FAILED),
    ({"state": True, "error": None, "data": {"status": "running"}}, ExecutionOutcome.SUCCEEDED),
    ({"error": None}, ExecutionOutcome.SUCCEEDED),
    ({"success": True, "error": "previous error"}, ExecutionOutcome.SUCCEEDED),
    ({"status": "running"}, ExecutionOutcome.SUCCEEDED),
    ({"name": "torrent", "error": "tracker failed", "status": "running"}, ExecutionOutcome.SUCCEEDED),
    ({"success": True, "data": [{"success": False}]}, ExecutionOutcome.SUCCEEDED),
    ("该日志记录了失败，当前查询正常", ExecutionOutcome.SUCCEEDED),
    ({"success": True, "task_id": "task-1", "status": "queued"}, ExecutionOutcome.PENDING),
    ({"action": "start", "success": True, "tasks": [{"task_id": "task-1", "status": "running"}]}, ExecutionOutcome.PENDING),
    ({"session_id": "term-1", "command": "echo ok", "exit_code": None, "status": "running"}, ExecutionOutcome.PENDING),
    ({"session_id": "term-1", "command": "exit 2", "exit_code": 2, "status": "exited"}, ExecutionOutcome.FAILED),
    ({"execution_outcome": "unknown", "error": "timed out"}, ExecutionOutcome.UNKNOWN),
    ({"task_id": "task-1", "status": {"arbitrary": "data"}}, ExecutionOutcome.SUCCEEDED),
])
def test_explicit_protocol_outcomes_preserve_business_data(payload, expected):
    """仅解析已知协议，业务数据和自由文本不会被错误识别为执行失败。"""
    assert inspect_tool_result(payload) is expected
    if not isinstance(payload, str):
        assert inspect_tool_result(json.dumps(payload)) is expected


@pytest.mark.parametrize("outcome", list(ExecutionOutcome))
def test_receipt_retains_each_explicit_outcome(outcome):
    """回执必须区分所有四类结果，未知状态明确要求核验。"""
    orchestrator = AgentToolPolicyOrchestrator()
    tool = SimpleNamespace(name="plugin_write", args_schema=None)
    observation = orchestrator.start(context=_context(), tool=tool, arguments={})
    receipt = orchestrator.finish(observation, {"execution_outcome": outcome.value})
    assert receipt.outcome is outcome
    assert receipt.needs_reconcile is (outcome is ExecutionOutcome.UNKNOWN)


class _ResultTool(MoviePilotTool):
    """提供成功、业务失败及运行异常三种离线工具行为。"""

    name: str = "outcome_tool"
    description: str = "Validate tool outcomes."

    async def run(self, **kwargs):
        """根据测试输入返回业务载荷或抛出故障。"""
        if kwargs.get("fail"):
            raise RuntimeError("private-provider-payload")
        return {"success": False, "message": "目标记录不存在"}


class _TaskModel(FakeMessagesListChatModel):
    """生成固定工具调用来验证真实图遇到故障后仍会继续。"""

    def bind_tools(self, _tools, **_kwargs):
        """接受工具绑定，无需真实模型调用。"""
        return self


@pytest.mark.asyncio
async def test_base_exception_becomes_typed_safe_failure():
    """基类异常不能以普通成功字符串返回，也不能暴露供应商正文。"""
    tool = _ResultTool(session_id="outcome-test", user_id="owner")
    with pytest.raises(ToolExecutionError) as failure:
        await tool._arun(fail=True)
    assert "RuntimeError" in str(failure.value)
    assert "private-provider-payload" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_real_graph_continues_after_business_failure(monkeypatch, raises):
    """业务失败和执行异常都成为 error 工具消息，真实图继续完成下一模型回合。"""
    tool = _ResultTool(session_id="outcome-test", user_id="owner")
    if raises:
        monkeypatch.setattr(_ResultTool, "run", AsyncMock(side_effect=RuntimeError("private-provider-payload")))
    model = _TaskModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "failure", "name": tool.name, "args": {}}]),
        AIMessage(content="记录不存在，继续检查记录编号。"),
    ])
    graph = create_agent(model=model, tools=[tool], middleware=[AgentPolicyMiddleware(context=_context())])
    result = await graph.ainvoke({"messages": [HumanMessage(content="检查记录")]})
    message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert message.status == "error"
    assert message.additional_kwargs[EXECUTION_OUTCOME_KEY] == "failed"
    if raises:
        assert "RuntimeError" in message.content
        assert "private-provider-payload" not in message.content
    else:
        assert json.loads(message.content) == {"success": False, "message": "目标记录不存在"}
    assert result["messages"][-1].content == "记录不存在，继续检查记录编号。"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("private-provider-payload"), TimeoutError("private-timeout-body")])
async def test_external_errors_are_safe_and_write_timeouts_unknown(error):
    """故障不会崩溃整张图，外部写超时必须保留未知状态。"""
    middleware = AgentPolicyMiddleware(context=_context())
    request = SimpleNamespace(tool=SimpleNamespace(name="plugin_write", args_schema=None),
                              tool_call={"id": "write", "args": {}})
    message = await middleware.awrap_tool_call(request, AsyncMock(side_effect=error))
    assert message.status == "error"
    expected = ExecutionOutcome.UNKNOWN if isinstance(error, TimeoutError) else ExecutionOutcome.FAILED
    assert inspect_tool_result(message) is expected
    assert "private-" not in message.content


@pytest.mark.asyncio
async def test_cancellation_propagates_without_converting_to_tool_success():
    """用户取消必须继续传播给会话生命周期，不被故障转换吞掉。"""
    middleware = AgentPolicyMiddleware(context=_context())
    request = SimpleNamespace(tool=SimpleNamespace(name="plugin_write", args_schema=None),
                              tool_call={"id": "write", "args": {}})
    with pytest.raises(asyncio.CancelledError):
        await middleware.awrap_tool_call(request, AsyncMock(side_effect=asyncio.CancelledError()))


@pytest.mark.asyncio
async def test_command_receipt_and_direct_manager_keep_original_payload():
    """Command 和 direct manager 复用解析规则，正常业务载荷无需增加包装。"""
    message = ToolMessage(content='{"success":false}', tool_call_id="plan", status="error")
    command = Command(update={"messages": [message], "custom_state": "unchanged"})
    assert inspect_tool_result(command) is ExecutionOutcome.FAILED
    tool = _ResultTool(session_id="outcome-test", user_id="owner")
    manager = MoviePilotToolsManager(is_admin=True)
    manager.tools = [tool]
    assert json.loads(await manager.call_tool(tool.name, {})) == {"success": False, "message": "目标记录不存在"}


def test_mcp_error_with_text_content_does_not_lose_error_flag():
    """外部 MCP 的文本内容不能覆盖 isError 标识，正常文本格式保持兼容。"""
    payload = {"isError": True, "content": [{"type": "text", "text": "operation rejected"}]}
    result = McpExternalTool._format_mcp_result(payload)
    assert json.loads(result) == payload
    assert inspect_tool_result(result) is ExecutionOutcome.FAILED
    assert McpExternalTool._format_mcp_result({"content": payload["content"]}) == "operation rejected"


def _api_tool(request: AsyncMock) -> MoviePilotApiTool:
    """把内存 HTTP 请求替身接到真实 API 工具，覆盖异常映射全链路。"""
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(user_id="1", username="admin", is_admin=True),
        request_factory=lambda **_kwargs: SimpleNamespace(request=request),
    )
    tool = MoviePilotApiTool(session_id="api-outcome-test", user_id="1", executor=executor)
    tool.set_agent_context({"is_admin": True})
    return tool


def _valid_api_arguments(operation: str) -> dict:
    """为传输层测试提供每个 operation 的最小合法输入。"""
    return {
        "download.add": {"body": {"torrent_in": {
            "title": "Test Movie", "enclosure": "https://example.invalid/test.torrent",
        }}},
        "subscription.update": {"body": {"id": 1}},
        "system.restart": {},
        "subscription.list": {},
        "storage.list": {"body": {"path": "/"}},
    }[operation]


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "expected"), [
    ("download.add", ExecutionOutcome.UNKNOWN),
    ("subscription.update", ExecutionOutcome.UNKNOWN),
    ("system.restart", ExecutionOutcome.UNKNOWN),
    ("subscription.list", ExecutionOutcome.FAILED),
    ("storage.list", ExecutionOutcome.FAILED),
])
async def test_api_transport_failure_preserves_unknown_mutations(monkeypatch, operation, expected):
    """API 传输异常对写操作保留未知结果，包含副作用 GET 及只读 POST 的策略区分。"""
    monkeypatch.setattr("app.agent.api.executor.create_access_token", lambda **_kwargs: "test-token")
    request = AsyncMock(side_effect=httpx2.ReadError("private-api-response-body"))
    result = await _api_tool(request).run(
        operation_id=operation,
        **_valid_api_arguments(operation),
    )
    assert inspect_tool_result(result) is expected
    assert "private-api-response-body" not in result
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_preflight_validation_and_http_rejection_are_definite_failures(monkeypatch):
    """未发送的参数错误和明确 HTTP 4xx 拒绝不能被误记为未知外部写。"""
    monkeypatch.setattr("app.agent.api.executor.create_access_token", lambda **_kwargs: "test-token")
    response = SimpleNamespace(status_code=403, headers={}, json=lambda: {"detail": "denied"}, aclose=AsyncMock())
    request = AsyncMock(return_value=response)
    tool = _api_tool(request)
    missing_path = await tool.run(operation_id="subscription.delete")
    assert inspect_tool_result(missing_path) is ExecutionOutcome.FAILED
    request.assert_not_awaited()
    rejected = await tool.run(
        operation_id="download.add",
        **_valid_api_arguments("download.add"),
    )
    assert inspect_tool_result(rejected) is ExecutionOutcome.FAILED
    assert json.loads(rejected)["status_code"] == 403
    response.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "expected"), [(200, ExecutionOutcome.UNKNOWN), (403, ExecutionOutcome.FAILED)])
async def test_api_unreadable_response_retains_observed_http_status(monkeypatch, status_code, expected):
    """成功 HTTP 状态但结果不可读时禁止重放写操作，明确拒绝则保持失败。"""
    monkeypatch.setattr("app.agent.api.executor.create_access_token", lambda **_kwargs: "test-token")

    def unreadable():
        """模拟 HTTP 请求完成但正文无法解码。"""
        raise ValueError("private-response-body")

    response = SimpleNamespace(status_code=status_code, headers={}, json=unreadable, aclose=AsyncMock())
    result = await _api_tool(AsyncMock(return_value=response)).run(
        operation_id="download.add",
        **_valid_api_arguments("download.add"),
    )
    assert inspect_tool_result(result) is expected
    assert "private-response-body" not in result
    response.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_endpoint_submission_remains_pending_in_agent(monkeypatch):
    """真实 API 端点返回已提交时，Agent 必须保留 pending，不能声称后台服务完成。"""
    from app.api.endpoints import system

    finished = asyncio.Event()
    release = asyncio.Event()
    jobs = []

    async def execute_job():
        """模拟已接受但仍等待外部结果的定时服务。"""
        await release.wait()
        finished.set()

    def start(job_id):
        """复现 Scheduler.start 的异步提交合同，不等待任务结束。"""
        assert job_id == "test-job"
        jobs.append(asyncio.create_task(execute_job()))
        return True

    async def request(**kwargs):
        """用实际 runscheduler 端点响应接通真实 Agent API 执行器。"""
        assert kwargs["method"] == "GET"
        response = system.run_scheduler(kwargs["params"]["jobid"], _=None)
        payload = response.model_dump()
        assert payload["success"] is True
        assert "execution_outcome" not in payload
        return SimpleNamespace(status_code=200, headers={}, json=lambda: payload, aclose=AsyncMock())

    monkeypatch.setattr(system, "get_scheduler", lambda: SimpleNamespace(start=start))
    monkeypatch.setattr("app.agent.api.executor.create_access_token", lambda **_kwargs: "test-token")
    try:
        result = await _api_tool(AsyncMock(side_effect=request)).run(operation_id="scheduler.run", query={"jobid": "test-job"})
        assert inspect_tool_result(result) is ExecutionOutcome.PENDING
        assert json.loads(result)["success"] is True
        assert finished.is_set() is False
    finally:
        release.set()
        await asyncio.gather(*jobs)


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "success", "expected"), [
    ("scheduler.run", False, ExecutionOutcome.FAILED),
    ("workflow.run", True, ExecutionOutcome.SUCCEEDED),
])
async def test_api_submission_annotation_preserves_failure_and_synchronous_workflow(monkeypatch, operation, success, expected):
    """明确服务拒绝仍为失败，同步完成的工作流保留原返回载荷。"""
    monkeypatch.setattr("app.agent.api.executor.create_access_token", lambda **_kwargs: "test-token")
    payload = {"success": success, "message": "original", "data": None}
    response = SimpleNamespace(status_code=200, headers={}, json=lambda: payload, aclose=AsyncMock())
    result = await _api_tool(AsyncMock(return_value=response)).run(
        operation_id=operation,
        path_params={"workflow_id": 1} if operation == "workflow.run" else {},
        query={"jobid": "test-job"} if operation == "scheduler.run" else None,
    )
    assert inspect_tool_result(result) is expected
    assert json.loads(result) == payload
