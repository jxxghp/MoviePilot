"""用真实 Agent 图和 Python RPC 验证宿主策略、Skill 范围及完整结果聚合。"""

import asyncio
import json

import pytest
import pytest_asyncio
from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.code import manager as code_manager
from app.agent.code.manager import CodeSessionManager
from app.agent.middleware.code import CodeCaptureMiddleware, CodeExecutionMiddleware
from app.agent.middleware.guardrails import ToolGuardrailsMiddleware
from app.agent.middleware.output import ToolOutputMiddleware
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.middleware.skills import SkillsMiddleware
from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.policy.orchestrator import AgentToolPolicyOrchestrator
from app.agent.terminal.ownership import TerminalScope, bind_terminal_scope
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.execute_code import ExecuteCodeTool


class CodeModel(FakeMessagesListChatModel):
    """只有模型输出固定，真实工具节点、Python 和 RPC 都参与执行。"""

    def bind_tools(self, _tools, **_kwargs):
        """不访问外部模型供应商。"""
        return self


class RecordedPolicy(AgentToolPolicyOrchestrator):
    """保留真实宿主策略，只额外记录每次独立工具回执。"""

    def __init__(self):
        """为当前测试建立独立记录。"""
        super().__init__()
        self.receipts = []

    def finish(self, observation, result):
        """确认内部调用走完整策略结束路径。"""
        receipt = super().finish(observation, result)
        self.receipts.append(receipt)
        return receipt


class PageExecutor:
    """回放分页列表，每页正文超过模型工具预览上限，最后一条仍须参与 Python 聚合。"""

    def __init__(self):
        """记录真实 API 网关交给执行边界的参数。"""
        self.calls = []

    async def execute(self, operation_id, **kwargs):
        """返回确定性的分页数据，不访问主库、HTTP 或下载器。"""
        self.calls.append((operation_id, kwargs))
        page = kwargs['query']['page']
        return json.dumps({'success': True, 'data': [
            {'id': page * 1000 + index, 'name': 'row-' + 'x' * 1000, 'value': index}
            for index in range(100)
        ]})


@pytest_asyncio.fixture
async def environment(tmp_path, monkeypatch):
    """只替换模型和外部 API 执行边界，管理器及工具实例使用真实实现。"""
    manager = CodeSessionManager(tmp_path / 'code')
    monkeypatch.setattr(code_manager, 'code_session_manager', manager)
    context = ToolPolicyContext(session_id='code', user_id='alice', origin=ToolOrigin.AGENT_INTERACTIVE,
                               principal_type=PrincipalType.HUMAN, auth_source=AuthSource.WEB_SESSION,
                               agent_context={'is_admin': True})
    scope = TerminalScope('alice', 'code', 'interactive')
    executor = PageExecutor()
    api = MoviePilotApiTool(session_id='code', user_id='alice', executor=executor)
    tool = ExecuteCodeTool(session_id='code', user_id='alice')
    for item in (api, tool):
        item.set_agent_context(context.agent_context)
        object.__setattr__(item, '_agent_tool_source', 'builtin')
    skills_dir = tmp_path / 'skills' / 'pages'
    skills_dir.mkdir(parents=True)
    (skills_dir / 'SKILL.md').write_text('---\nname: pages\ndescription: Test pagination\n'
                                      'allowed-tools: moviepilot_api\n'
                                      'allowed-api-operations: subscription.list\n---\nRead subscriptions.\n')
    skills = SkillsMiddleware(sources=[str(skills_dir.parent)])
    guard = ToolGuardrailsMiddleware(context)
    recorded = RecordedPolicy()
    policy = AgentPolicyMiddleware(context=context, tools=[api, tool], guardrails=guard, orchestrator=recorded)
    code = CodeExecutionMiddleware(policy)
    middlewares = [policy, code, ToolOutputMiddleware(context), skills, guard, CodeCaptureMiddleware(code)]
    try:
        yield scope, executor, api, tool, skills, guard, recorded, middlewares
    finally:
        await manager.close_owner(scope)
        if manager._reaper is not None:
            manager._reaper.cancel()
            await asyncio.gather(manager._reaper, return_exceptions=True)


def _call(identifier, name, arguments):
    """每个模型调用使用唯一 ID，内部 RPC 也必须独立生成 ID。"""
    return AIMessage(content='', tool_calls=[{'name': name, 'args': arguments, 'id': identifier}])


def _program():
    """三页完整数据在 Python 内聚合，只打印最终数字。"""
    return ('from moviepilot_tools import moviepilot_api\n'
            'rows = []\nfor page in range(1, 4):\n'
            '    rows += moviepilot_api("subscription.list", query={"page": page, "count": 100})["data"]\n'
            'print({"count": len(rows), "total": sum(row["value"] for row in rows), "last": rows[-1]["id"]})')


@pytest.mark.asyncio
async def test_real_graph_pages_full_results_and_uses_each_policy_receipt(environment):
    """大结果没有被模型预览截断，三次 API 均有真实回执，内层调用不污染循环检测器。"""
    scope, executor, api, tool, _, guard, recorded, middlewares = environment
    model = CodeModel(responses=[_call('skill', 'read_skill', {'name': 'pages'}),
                                _call('code', 'execute_code', {'code': _program()}), AIMessage(content='分析完成')])
    graph = create_agent(model=model, tools=[api, tool], middleware=middlewares)
    with bind_terminal_scope(scope):
        result = await graph.ainvoke({'messages': [HumanMessage(content='统计三个分页')]},
                                     config={'configurable': {'thread_id': 'code'}})
    message = next(item for item in result['messages'] if isinstance(item, ToolMessage) and item.name == 'execute_code')
    payload = json.loads(message.content)
    assert payload['success'], payload
    assert "'count': 300" in payload['output'] and "'total': 14850" in payload['output'] and "'last': 3099" in payload['output']
    assert payload['tool_calls_made'] == len(executor.calls) == 3
    receipts = [receipt for receipt in recorded.receipts if receipt.tool_name == 'moviepilot_api']
    assert len(receipts) == len({receipt.invocation_id for receipt in receipts}) == 3
    assert not guard._runs
    assert not any('CodeExecutionMiddleware.' in node or 'CodeCaptureMiddleware.' in node for node in graph.get_graph().nodes)


@pytest.mark.asyncio
async def test_code_cannot_bypass_unloaded_skill(environment):
    """直接在 Python 调用 API 仍被真实 Skill 中间件拒绝，不能靠代码工具隐式加载权限。"""
    scope, executor, api, tool, _, _, _, middlewares = environment
    model = CodeModel(responses=[_call('code', 'execute_code', {'code': _program()}), AIMessage(content='范围未加载')])
    graph = create_agent(model=model, tools=[api, tool], middleware=middlewares)
    with bind_terminal_scope(scope):
        result = await graph.ainvoke({'messages': [HumanMessage(content='直接统计')]},
                                     config={'configurable': {'thread_id': 'code'}})
    message = next(item for item in result['messages'] if isinstance(item, ToolMessage) and item.name == 'execute_code')
    payload = json.loads(message.content)
    assert not payload['success'] and payload['tool_errors']
    assert 'skill_operation_denied' in str(payload['tool_errors'])
    assert executor.calls == []


class DropApiMiddleware(AgentMiddleware):
    """模拟真实工具筛选，证明 RPC 集合取自最终请求而非完整目录。"""

    async def awrap_model_call(self, request, handler):
        """移除 API 后的 Python 环境不能继续导入 API 存根。"""
        return await handler(request.override(tools=[tool for tool in request.tools if tool.name != 'moviepilot_api']))


@pytest.mark.asyncio
async def test_selected_tools_intersection_is_enforced(environment):
    """目录中存在但本次请求被筛掉的 API 不可从 Python 使用。"""
    scope, executor, api, tool, _, _, _, middlewares = environment
    middlewares.insert(-1, DropApiMiddleware())
    model = CodeModel(responses=[_call('code', 'execute_code', {'code': _program()}), AIMessage(content='工具不可用')])
    graph = create_agent(model=model, tools=[api, tool], middleware=middlewares)
    with bind_terminal_scope(scope):
        result = await graph.ainvoke({'messages': [HumanMessage(content='统计')]},
                                     config={'configurable': {'thread_id': 'code'}})
    message = next(item for item in result['messages'] if isinstance(item, ToolMessage) and item.name == 'execute_code')
    payload = json.loads(message.content)
    assert not payload['success'] and 'ImportError' in payload['error'] and executor.calls == []


@pytest.mark.asyncio
async def test_direct_tool_invocation_cannot_create_host_authority(environment):
    """仅传入管理员标志不足以建立内部执行窗口，外部工具直接调用保持拒绝。"""
    _, _, _, tool, _, _, _, _ = environment
    result = json.loads(await tool.ainvoke({'code': 'print("must not execute")'}))
    assert result['success'] is False and not code_manager.code_session_manager._entries


def test_code_tool_not_published_to_unbound_external_or_nonadmin_catalog(monkeypatch):
    """外部工具目录和普通用户不暴露任意主机 Python；管理员会话默认可用。"""
    monkeypatch.setattr('app.agent.tools.factory._get_plugin_agent_tools', lambda: [])
    for context, external, expected in (({}, False, False), ({'is_admin': True}, True, False), ({'is_admin': True}, False, True)):
        tools = MoviePilotToolFactory.create_tools(session_id='code', user_id='alice', agent_context=context,
                                                  include_external_service_tools=external)
        assert any(tool.name == 'execute_code' for tool in tools) is expected
