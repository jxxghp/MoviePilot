"""生产 Agent 评测运行器覆盖真实中间件、内存业务 API 和隔离回收。"""

import json
import socket
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from pydantic import Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent import orchestrator
from app.agent.api.executor import MoviePilotApiExecutor
from app.agent.middleware import config as config_middleware
from app.agent.middleware import subagents
from app.agent.middleware.summarization import FinalRequestCompactionMiddleware
from app.agent.terminal.ownership import TerminalScope, bind_terminal_scope, close_terminal_scope
from app.db.adapters.invocation import TransactionalInvocationRepository
from app.db.models.agentinvocation import AgentInvocation
from scripts.evaluation.runtime import _EvaluationExecuteCommandTool, _Transport, run_moviepilot
from scripts.evaluation.score import evaluate
from scripts.evaluation.world import EvaluationWorld


class _ScriptModel(FakeMessagesListChatModel):
    """脚本仅控制模型响应，不替换生产模型请求或工具执行路径。"""

    bound_tools: list[list[Any]] = Field(default_factory=list)
    requests: list[Any] = Field(default_factory=list)

    def bind_tools(self, tools: list[Any], **_kwargs: Any) -> Any:
        """记录真实图每次传给模型的工具定义。"""
        self.bound_tools.append(list(tools))
        return self

    async def _agenerate(self, messages: list[Any], stop=None, run_manager=None, **kwargs: Any) -> Any:
        """记录包含生产系统提示词和中间件动态上下文的最终请求。"""
        self.requests.append(messages)
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _call(name: str, arguments: dict[str, Any], identifier: str) -> AIMessage:
    """构造标准模型工具调用，交给真实 ToolNode 执行。"""
    return AIMessage(content="", tool_calls=[{"id": identifier, "name": name, "args": arguments}])


def _responses(world: EvaluationWorld) -> list[AIMessage]:
    """提供三个明确标记为脚本的正确轨迹，由独立终态判定器核验。"""
    scenario = world.scenario
    responses = [
        _call("update_plan", {"objective": "完成用户任务并核验", "steps": [
            {"step": "查询并核验业务状态", "status": "in_progress"},
        ]}, "plan"),
        _call("read_skill", {"name": "moviepilot-api"}, "skill"),
    ]
    report = {
        "status": "completed", "subscription_ids": [], "download_ids": [scenario.infohash],
        "enabled_site_ids": [], "completed": ["download"], "unresolved": [],
    }
    if scenario.scenario_id == "dedup_existing":
        responses.append(_call("moviepilot_api", {
            "operation_id": "subscription.find", "path_params": {"media_id": scenario.media_id},
            "query": {"media_source": scenario.media_source},
        }, "subscription"))
        report["subscription_ids"] = [72]
        report["completed"] = ["subscription", "download"]
    responses.append(_call("moviepilot_api", {"operation_id": "download.tasks.active"}, "before"))
    if scenario.scenario_id != "dedup_existing":
        responses.extend([
            _call("moviepilot_api", {"operation_id": "download.add", "body": {
                "torrent_in": {"title": scenario.title, "enclosure": scenario.magnet},
                "media_source": scenario.media_source, "media_id": scenario.media_id,
            }}, "submit"),
            _call("moviepilot_api", {"operation_id": "download.tasks.active"}, "after"),
        ])
    if scenario.scenario_id == "honest_unknown":
        responses.append(_call("moviepilot_api", {"operation_id": "site.list", "query": {"status": "active"}}, "sites"))
        report.update(status="blocked", download_ids=[], enabled_site_ids=[11, 13], completed=["sites"], unresolved=["download"])
    responses.append(AIMessage(content=json.dumps(report, ensure_ascii=False)))
    return responses


@pytest.fixture
def invocation_store(tmp_path):
    """提供独立真实回执库，测试结束释放连接。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'invocations.db'}")
    AgentInvocation.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    yield TransactionalInvocationRepository(factory), factory
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", ["dedup_existing", "unknown_download", "honest_unknown"])
async def test_complete_production_agent_scenarios(scenario_id, invocation_store, monkeypatch):
    """真实 process、策略、回执、计划、Skills 和最终压缩共同执行，不连真实业务。"""
    world = EvaluationWorld(scenario_id)
    model = _ScriptModel(responses=_responses(world))
    repository, factory = invocation_store
    original_runtime = orchestrator.agent_runtime_manager
    original_mcp = orchestrator.agent_mcp_manager
    original_headers = MoviePilotApiExecutor._build_headers
    notify = AsyncMock(side_effect=AssertionError("不应调用真实通知"))
    monkeypatch.setattr(orchestrator.AgentChain, "async_post_message", notify)
    compaction_requests = []
    original_compaction = FinalRequestCompactionMiddleware.awrap_model_call

    async def observe_compaction(self, request, handler):
        """继续执行真实最终请求压缩，同时证明评测未绕过该边界。"""
        compaction_requests.append(request)
        return await original_compaction(self, request, handler)

    monkeypatch.setattr(FinalRequestCompactionMiddleware, "awrap_model_call", observe_compaction)

    def refuse_network(*_args: Any, **_kwargs: Any) -> None:
        """任何直接出站均令测试失败，包含模型目录、业务 API 和 MCP 发现。"""
        raise AssertionError("评测不得访问网络")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_network)
    capture = await run_moviepilot(
        world, model, model_name="scripted-test", context_window=128000, max_iterations=48,
        invocation_repository=repository,
    )
    assert capture["execution_success"] is True, capture["final_text"]
    result = evaluate(world, json.loads(capture["final_text"]))
    assert result.passed, result.violations
    assert capture["tool_catalog_scope"] == "controlled_moviepilot_api_and_production_internal_tools"
    assert {"moviepilot_api", "read_file", "read_skill", "update_plan", "get_tool_execution", "task", "subagent_task", "search_tools"} <= set(capture["tool_names"])
    assert not {"execute_command", "write_file", "edit_file", "browser", "send_message"} & set(capture["tool_names"])
    assert capture["child_tool_names"] == ["moviepilot_api", "read_file"]
    assert capture["tool_catalog"]["signature_sha256"]
    assert {entry["name"] for entry in capture["tool_catalog"]["entries"]} == set(capture["tool_names"])
    assert {entry["name"] for entry in capture["child_tool_catalog"]["entries"]} == set(capture["child_tool_names"])
    api_entry = next(entry for entry in capture["tool_catalog"]["entries"] if entry["name"] == "moviepilot_api")
    assert api_entry["revision"]["implementation"].endswith("MoviePilotApiTool")
    assert api_entry["schema_digest"]
    assert capture["task_plan"]["objective"] == "完成用户任务并核验"
    assert any("InvocationMiddleware" in node for node in capture["graph_nodes"])
    assert any("SkillsMiddleware" in node for node in capture["graph_nodes"])
    assert model.requests and "<task_planning>" in str(model.requests[0][0].content)
    assert "<agent_memory>" in str(model.requests[0][0].content)
    assert len(compaction_requests) == len(model.requests)
    assert capture["usage"]["model_call_count"] > 0
    assert capture["display_messages"][0]["role"] == "user"
    assert capture["display_messages"][-1]["role"] == "assistant"
    assert scenario_id not in repr(model.requests)
    assert notify.await_count == 0
    assert orchestrator.agent_runtime_manager is original_runtime
    assert config_middleware.agent_runtime_manager is original_runtime
    assert subagents.agent_runtime_manager is original_runtime
    assert orchestrator.agent_mcp_manager is original_mcp
    assert MoviePilotApiExecutor._build_headers is original_headers
    with factory() as session:
        records = list(session.execute(select(AgentInvocation)).scalars())
    if scenario_id == "dedup_existing":
        assert records == []
    else:
        assert len(records) == 1
        assert records[0].status == "unknown"


@pytest.mark.asyncio
async def test_child_tool_instance_enforces_production_readonly():
    """实际启动生产子图，写操作必须由子图策略拒绝且不触及业务世界。"""
    world = EvaluationWorld("unknown_download")
    model = _ScriptModel(responses=[
        _call("task", {"description": "检查下载，尝试添加任务", "subagent_type": "general-purpose"}, "delegate"),
        _call("moviepilot_api", {"operation_id": "download.add", "body": {
            "torrent_in": {"title": world.scenario.title, "enclosure": world.scenario.magnet},
            "media_source": world.scenario.media_source, "media_id": world.scenario.media_id,
        }}, "child-write"),
        AIMessage(content="子代理仅可读取，写入已拒绝。"),
        AIMessage(content="子代理核验结束。"),
    ])
    capture = await run_moviepilot(world, model, model_name="scripted-test", context_window=128000, max_iterations=20)
    assert capture["execution_success"] is True
    assert world.ledger == []
    api_instances = [tool for tools in model.bound_tools for tool in tools if getattr(tool, "name", None) == "moviepilot_api"]
    assert len({id(tool) for tool in api_instances}) == 2
    assert any("subagent_read_only" in str(message.content) for request in model.requests for message in request)


@pytest.mark.asyncio
async def test_failed_model_restores_runtime_and_profile(monkeypatch):
    """模型失败后的恢复继续走生产逻辑，临时模型 profile 和外部边界必须还原。"""
    original_runtime = orchestrator.agent_runtime_manager
    original_mcp = orchestrator.agent_mcp_manager
    original_headers = MoviePilotApiExecutor._build_headers
    model = _ScriptModel(responses=[AIMessage(content="unused")])
    original_profile = model.profile

    async def fail_model(*_args: Any, **_kwargs: Any) -> None:
        """在真实模型调用边界注入失败，不替换 Agent 的错误恢复路径。"""
        raise RuntimeError("评测模型暂不可用")

    monkeypatch.setattr(model, "_agenerate", fail_model)
    capture = await run_moviepilot(
        EvaluationWorld("dedup_existing"), model, model_name="scripted-test", context_window=128000, max_iterations=10,
    )
    assert capture["execution_success"] is False
    assert capture["final_text"]
    assert "moviepilot_api" in capture["tool_names"]
    assert capture["graph_nodes"]
    assert any(message["type"] == "human" for message in capture["raw_messages"])
    assert orchestrator.agent_runtime_manager is original_runtime
    assert orchestrator.agent_mcp_manager is original_mcp
    assert MoviePilotApiExecutor._build_headers is original_headers
    assert model.profile is original_profile


@pytest.mark.asyncio
async def test_transport_rejects_external_and_unlisted_routes():
    """真实执行器之外直接构造请求也不能让内存传输访问外网或未纳入业务。"""
    world = EvaluationWorld("dedup_existing")
    transport = _Transport(world)
    with pytest.raises(ValueError, match="内存 API"):
        await transport.request(method="GET", url="https://example.com", params=None, json=None, raise_exception=True)
    response = await transport.request(
        method="GET", url="http://evaluation.invalid/api/v1/system/config", params=None, json=None, raise_exception=True,
    )
    assert response.json()["execution_outcome"] == "failed"
    assert world.ledger == []


@pytest.mark.asyncio
async def test_evaluation_command_tool_rejects_wrong_input_with_correction(tmp_path):
    """评测命令工具拒绝越界输入，并把可修正的合同返回给模型。"""
    world = EvaluationWorld("command_execution")
    tool = _EvaluationExecuteCommandTool(world=world, allowed_root=tmp_path, session_id="command-test", user_id="1")
    scope = TerminalScope(user_id="1", task_id="command-test", kind="conversation")
    with bind_terminal_scope(scope):
        result = json.loads(await tool.run(action="run", command="echo outside"))
    assert result["execution_outcome"] == "failed"
    assert result["error"] == "evaluation_command_rejected"
    assert "完全一致" in result["message"]
    assert world.ledger[0]["operation_id"] == "execute_command"
    assert await close_terminal_scope(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize(("scenario_id", "use_pty"), [("terminal_session", False), ("terminal_pty_session", True)])
async def test_evaluation_terminal_tool_runs_session_and_writes_stdin(tmp_path, scenario_id: str, use_pty: bool):
    """评测终端复用生产会话管理器，必须通过真实 session_id 写入并等待退出。"""
    world = EvaluationWorld(scenario_id)
    tool = _EvaluationExecuteCommandTool(world=world, allowed_root=tmp_path, session_id="terminal-test", user_id="1")
    scope = TerminalScope(user_id="1", task_id="terminal-test", kind="conversation")
    with bind_terminal_scope(scope):
        start = json.loads(await tool.run(
            action="start", command=world.scenario.command, use_pty=use_pty, yield_time_ms=1000,
        ))
        assert start["execution_outcome"] == "pending"
        session_id = start["session_id"]
        write = json.loads(await tool.run(
            action="write", session_id=session_id, input_text="MOVIEPILOT_TERMINAL_OK\n", close_stdin=False,
        ))
        assert write["session_id"] == session_id
        waited = write
        for _ in range(5):
            if waited["status"] == "exited":
                break
            waited = json.loads(await tool.run(
                action="wait", session_id=session_id, timeout_ms=1000,
                since_seq=waited["output_until_seq"],
            ))
        assert waited["status"] == "exited"
        assert waited["exit_code"] == 0
    assert await close_terminal_scope(scope)
    report = {
        "status": "completed", "terminal_output": "READY\nREPLY=MOVIEPILOT_TERMINAL_OK\n", "terminal_exit_code": 0,
        "completed": ["terminal"], "unresolved": [],
        "subscription_ids": [], "download_ids": [], "enabled_site_ids": [],
    }
    assert evaluate(world, report).passed is True


@pytest.mark.asyncio
async def test_evaluation_terminal_tool_returns_correction_for_wrong_stdin(tmp_path):
    """交互会话的错误 stdin 被结构化拒绝，并明确告知模型正确输入。"""
    world = EvaluationWorld("terminal_session")
    tool = _EvaluationExecuteCommandTool(world=world, allowed_root=tmp_path, session_id="terminal-test", user_id="1")
    scope = TerminalScope(user_id="1", task_id="terminal-test", kind="conversation")
    with bind_terminal_scope(scope):
        start = json.loads(await tool.run(action="start", command=world.scenario.command, use_pty=False))
        result = json.loads(await tool.run(
            action="write", session_id=start["session_id"], input_text="wrong\n", close_stdin=False,
        ))
        assert result["execution_outcome"] == "failed"
        assert "MOVIEPILOT_TERMINAL_OK" in result["message"]
    assert await close_terminal_scope(scope)


@pytest.mark.asyncio
async def test_runtime_refuses_mismatched_config(monkeypatch, tmp_path):
    """导入后更换 CONFIG_DIR 不能被误认成已隔离后端。"""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="不一致"):
        await run_moviepilot(
            EvaluationWorld("dedup_existing"), _ScriptModel(responses=[AIMessage(content="unused")]),
            model_name="scripted-test", context_window=128000, max_iterations=10,
        )
