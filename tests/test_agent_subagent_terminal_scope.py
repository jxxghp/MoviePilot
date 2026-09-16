"""子任务终端授权必须按真实调用隔离，缓存图和已知句柄不产生隐式权限。"""

import asyncio
import builtins
import json
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError

from app.agent.middleware import subagents as subagent_module
from app.agent.middleware.subagents import (
    MoviePilotSubAgentMiddleware,
    SubAgentTaskControlMiddleware,
    _builtin_subagent_profiles,
    _SubAgentAgentProvider,
)
from app.agent.middleware.terminal import SubAgentTerminalGrant, subagent_terminal_scope
from app.agent.policy.contracts import AuthSource, PrincipalType, ToolOrigin, ToolPolicyContext
from app.agent.terminal import manager as terminal_module
from app.agent.terminal.manager import _TerminalSessionManager
from app.agent.terminal.ownership import (
    TerminalAccessError,
    TerminalScope,
    bind_terminal_scope,
    current_terminal_scope,
    require_terminal_scope,
)
from app.agent.tools.impl.execute_command import ExecuteCommandTool


class _TerminalReadModel(FakeMessagesListChatModel):
    """只固定工具协议，终端动作经过真实子图 ToolNode、策略和管理器。"""

    terminal_handle: str

    def bind_tools(self, _tools: Any, **_kwargs: Any) -> "_TerminalReadModel":
        """使用提供器装配的真实工具列表，无供应商或网络调用。"""
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **_kwargs: Any) -> ChatResult:
        """按本次图状态生成确定性工具调用，不用共享响应序号串扰并发子图。"""
        previous = next((item for item in reversed(messages) if isinstance(item, ToolMessage)), None)
        message = AIMessage(content=str(previous.content)) if previous else AIMessage(
            content="", tool_calls=[{"id": "read-terminal", "name": "execute_command", "args": {
                "action": "read", "session_id": self.terminal_handle,
            }}],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _policy() -> ToolPolicyContext:
    """使用固定宿主用户，测试输入不能覆盖该身份。"""
    return ToolPolicyContext(
        session_id="conversation", user_id="owner", origin=ToolOrigin.SUBAGENT,
        principal_type=PrincipalType.SUBAGENT, auth_source=AuthSource.INTERNAL,
        agent_context={"is_admin": True},
    )


def _provider() -> _SubAgentAgentProvider:
    """保留真实缓存提供器，以离线图边界观察调用上下文。"""
    return _SubAgentAgentProvider(
        model=FakeListChatModel(responses=["unused"]), profiles=_builtin_subagent_profiles(),
        tools=[], policy_context=_policy(),
    )


def _command() -> str:
    """测试进程用 stdout 发布证据，并等待父任务输入后才退出。"""
    code = "import sys\nprint('PARENT_ONLY',flush=True)\ndata=sys.stdin.buffer.read()\nprint('BYTES:'+str(len(data)),flush=True)"
    args = [sys.executable, "-u", "-c", code]
    return subprocess.list2cmdline(args) if os.name == "nt" else "exec " + shlex.join(args)


@pytest_asyncio.fixture
async def terminal_parent(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[_TerminalSessionManager, TerminalScope, str]]:
    """测试独占管理器和父进程，失败时也回收自身创建的子进程。"""
    manager = _TerminalSessionManager()
    monkeypatch.setattr(terminal_module, "terminal_session_manager", manager)
    parent = TerminalScope(user_id="owner", task_id="parent-task", kind="interactive")
    options = {"shell": "/bin/sh", "login": False} if os.name == "posix" else {}
    try:
        with bind_terminal_scope(parent):
            payload = await asyncio.wait_for(manager.start(
                command=_command(), use_pty=False, yield_time_ms=10000, **options,
            ), 5)
        assert "PARENT_ONLY" in payload["output"]
        yield manager, parent, payload["session_id"]
    finally:
        await asyncio.wait_for(manager.close(), 10)


@pytest.mark.asyncio
async def test_cached_profile_keeps_parallel_child_grants_separate(
    terminal_parent: tuple[_TerminalSessionManager, TerminalScope, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一缓存图并发执行，只有显式获授权的真实子任务能读父终端。"""
    manager, parent, session_id = terminal_parent
    arrived = asyncio.Event()
    observations: dict[str, tuple[TerminalScope, str]] = {}
    calls = 0

    async def invoke(state: dict[str, Any], **_kwargs: Any) -> dict[str, list[AIMessage]]:
        """在两个子调用重叠期间读取真实管理器，不用假权限替代隔离检查。"""
        scope = require_terminal_scope()
        assert scope is not parent and scope.user_id == parent.user_id
        assert scope.task_id in {"shared-child", "sibling-child"}
        if scope.task_id == "shared-child":
            assert session_id in state["messages"][0].content
            result = (await manager.read(session_id=session_id))["output"]
        else:
            with pytest.raises(TerminalAccessError):
                await manager.read(session_id=session_id)
            result = "denied"
        observations[scope.task_id] = (scope, result)
        if len(observations) == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), 5)
        assert require_terminal_scope() is scope
        return {"messages": [AIMessage(content=result)]}

    graph = SimpleNamespace(ainvoke=invoke)

    def create_graph(**_kwargs: Any) -> Any:
        """记录缓存图创建次数，防止通过每次新建图掩盖共享工具问题。"""
        nonlocal calls
        calls += 1
        return graph

    monkeypatch.setattr(subagent_module, "create_agent", create_graph)
    provider = _provider()
    with bind_terminal_scope(parent):
        results = await asyncio.gather(
            provider.run_task(description="读取父证据", subagent_type="general-purpose", task_id="shared-child",
                              terminal_sessions=[SubAgentTerminalGrant(session_id=session_id)]),
            provider.run_task(description=f"尝试读取已知句柄 {session_id}", subagent_type="general-purpose", task_id="sibling-child"),
        )
        assert current_terminal_scope() is parent
        assert (await manager.read(session_id=session_id))["status"] == "running"
    assert calls == 1 and provider.get_agent("general-purpose")[1] is graph
    assert "PARENT_ONLY" in results[0] and results[1] == "denied"
    assert observations["shared-child"][0] is not observations["sibling-child"][0]
    for scope, _result in observations.values():
        assert scope.closed
        with bind_terminal_scope(scope), pytest.raises(TerminalAccessError):
            await manager.read(session_id=session_id)


@pytest.mark.asyncio
async def test_cancel_child_revokes_grants_and_preserves_parent_input(
    terminal_parent: tuple[_TerminalSessionManager, TerminalScope, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消获授权的子任务只关闭子作用域，父进程仍能收到唯一完整输入。"""
    manager, parent, session_id = terminal_parent
    entered = asyncio.Event()
    child_scopes: list[TerminalScope] = []

    async def invoke(_state: Any, **_kwargs: Any) -> Any:
        """真实读取授权生效后保持调用运行，让外部取消触发 finally 清理。"""
        child_scopes.append(require_terminal_scope())
        assert "PARENT_ONLY" in (await manager.read(session_id=session_id))["output"]
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(subagent_module, "create_agent", lambda **_kwargs: SimpleNamespace(ainvoke=invoke))
    task = None
    try:
        with bind_terminal_scope(parent):
            task = asyncio.create_task(_provider().run_task(
                description="只读等待", subagent_type="general-purpose", task_id="cancel-child",
                terminal_sessions=[SubAgentTerminalGrant(session_id=session_id)],
            ))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert current_terminal_scope() is parent and not parent.closed
            payload = await manager.write(session_id=session_id, input_text="parent", close_stdin=True)
            output = payload["output"]
            async with asyncio.timeout(5):
                while not payload["output_complete"]:
                    payload = await manager.wait(session_id=session_id, timeout_ms=1000,
                                                 since_seq=payload["output_until_seq"], since_offset=payload["output_until_offset"])
                    output += payload["output"]
            assert "BYTES:6" in output and payload["exit_code"] == 0
        assert child_scopes[0].closed
        with bind_terminal_scope(child_scopes[0]), pytest.raises(TerminalAccessError):
            await manager.read(session_id=session_id)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_kind", ["missing", "unrelated"])
async def test_share_failure_never_invokes_child_model(
    terminal_parent: tuple[_TerminalSessionManager, TerminalScope, str], monkeypatch: pytest.MonkeyPatch, parent_kind: str,
) -> None:
    """无父上下文或不拥有该终端的父任务不能通过委派执行模型。"""
    _manager, _parent, session_id = terminal_parent
    invoked = False

    async def invoke(*_args: Any, **_kwargs: Any) -> Any:
        """本边界不可触达，调用就表示授权失败仍执行了模型。"""
        nonlocal invoked
        invoked = True
        return {"messages": [AIMessage(content="unexpected")]}

    monkeypatch.setattr(subagent_module, "create_agent", lambda **_kwargs: SimpleNamespace(ainvoke=invoke))
    provider = _provider()
    arguments = {"description": "读取", "subagent_type": "general-purpose",
                 "terminal_sessions": [SubAgentTerminalGrant(session_id=session_id)]}
    if parent_kind == "missing":
        assert current_terminal_scope() is None
        with pytest.raises(TerminalAccessError):
            await provider.run_task(**arguments)
    else:
        unrelated = TerminalScope(user_id="owner", task_id="unrelated", kind="interactive")
        with bind_terminal_scope(unrelated), pytest.raises(TerminalAccessError):
            await provider.run_task(**arguments)
    assert not invoked


@pytest.mark.asyncio
async def test_nonterminal_children_use_local_identity_without_loading_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """无终端委派仍有独立子身份，但进入和退出都不物化终端单例。"""
    module_name = "app.agent.terminal.manager"
    monkeypatch.delitem(sys.modules, module_name)
    original_import = builtins.__import__
    scopes: list[TerminalScope] = []

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        """未启用终端的测试禁止隐式导入终端管理器。"""
        assert name != module_name
        return original_import(name, *args, **kwargs)

    async def invoke(_state: Any, **_kwargs: Any) -> dict[str, list[AIMessage]]:
        """记录每次调用真实 scope，确保缓存图不缓存任务身份。"""
        scopes.append(require_terminal_scope())
        return {"messages": [AIMessage(content="只读分析完成")]}

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(subagent_module, "create_agent", lambda **_kwargs: SimpleNamespace(ainvoke=invoke))
    provider = _provider()
    for task_id in ("analysis-one", "analysis-two"):
        await provider.run_task(description="分析", subagent_type="general-purpose", task_id=task_id)
        assert current_terminal_scope() is None
    assert [scope.task_id for scope in scopes] == ["analysis-one", "analysis-two"]
    assert all(scope.user_id == "owner" and scope.closed for scope in scopes)
    assert scopes[0] is not scopes[1] and module_name not in sys.modules


@pytest.mark.asyncio
async def test_nested_scope_restores_parent_and_rejects_redelegating_grant(
    terminal_parent: tuple[_TerminalSessionManager, TerminalScope, str],
) -> None:
    """嵌套普通子调用结束还原上层，获授只读句柄的子任务不能再转授。"""
    manager, parent, session_id = terminal_parent
    grants = [SubAgentTerminalGrant(session_id=session_id)]
    with bind_terminal_scope(parent):
        async with subagent_terminal_scope(task_id="child", user_id="owner", terminal_sessions=grants):
            child = require_terminal_scope()
            async with subagent_terminal_scope(task_id="nested-analysis", user_id="owner"):
                assert require_terminal_scope() is not child
                with pytest.raises(TerminalAccessError):
                    await manager.read(session_id=session_id)
            assert current_terminal_scope() is child
            with pytest.raises(TerminalAccessError):
                async with subagent_terminal_scope(task_id="redelegate", user_id="owner", terminal_sessions=grants):
                    pytest.fail("子代理不能转授父任务终端")
            assert current_terminal_scope() is child
            assert "PARENT_ONLY" in (await manager.read(session_id=session_id))["output"]
        assert current_terminal_scope() is parent


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["task", "start", "run", "batch", "pipeline"])
async def test_registered_task_entries_forward_per_task_terminal_grants(
    terminal_parent: tuple[_TerminalSessionManager, TerminalScope, str], monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    """阻塞、异步、批量和管道的真实工具入口都按条目转发且分配独立 task_id。"""
    manager, parent, session_id = terminal_parent
    scopes: list[TerminalScope] = []
    output: list[str] = []

    async def invoke(state: dict[str, Any], **_kwargs: Any) -> dict[str, list[AIMessage]]:
        """直接读取真实管理器，授权随任务输入而非同缓存 profile 传播。"""
        scopes.append(require_terminal_scope())
        description = state["messages"][0].content
        if description.startswith("shared"):
            text = (await manager.read(session_id=session_id))["output"]
        else:
            with pytest.raises(TerminalAccessError):
                await manager.read(session_id=session_id)
            text = "denied"
        output.append(text)
        return {"messages": [AIMessage(content=text)]}

    monkeypatch.setattr(subagent_module, "create_agent", lambda **_kwargs: SimpleNamespace(ainvoke=invoke))
    middleware_type = MoviePilotSubAgentMiddleware if entry == "task" else SubAgentTaskControlMiddleware
    middleware = middleware_type(model=FakeListChatModel(responses=["unused"]), profiles=_builtin_subagent_profiles(),
                                 tools=[], policy_context=_policy())
    shared = {"description": "shared", "subagent_type": "general-purpose",
              "terminal_sessions": [{"session_id": session_id}]}
    arguments: dict[str, Any] = dict(shared)
    if entry in {"batch", "pipeline"}:
        arguments = {"action": "run" if entry == "batch" else "pipeline", "timeout_ms": 1000,
                     "tasks": [shared, {"description": "sibling"}]}
    elif entry != "task":
        arguments.update(action=entry, timeout_ms=1000)
    try:
        with bind_terminal_scope(parent):
            result = await middleware.tools[0].ainvoke(arguments)
            if entry == "start":
                response = json.loads(result)
                ids = [task["task_id"] for task in response["tasks"]]
                result = await middleware.tools[0].ainvoke({"action": "wait", "task_ids": ids, "timeout_ms": 1000})
            if entry != "task":
                response = json.loads(result)
                assert response["success"]
                assert {scope.task_id for scope in scopes} == {task["task_id"] for task in response["tasks"]}
            assert current_terminal_scope() is parent and not parent.closed
            assert "PARENT_ONLY" in (await manager.read(session_id=session_id))["output"]
        assert "PARENT_ONLY" in output[0]
        assert len(scopes) == (2 if entry in {"batch", "pipeline"} else 1)
        assert len({id(scope) for scope in scopes}) == len(scopes)
        assert all(scope.closed for scope in scopes)
        if len(scopes) == 2:
            assert output[1] == "denied"
    finally:
        if isinstance(middleware, SubAgentTaskControlMiddleware):
            assert await middleware.close()


@pytest.mark.parametrize("actions", [["write"], ["interrupt"], ["kill"], ["read", "write"], []])
def test_terminal_grant_schema_rejects_process_control(actions: list[str]) -> None:
    """只读委派不因声明终端共享而增加写入、终止或其他控制能力。"""
    with pytest.raises(ValidationError):
        SubAgentTerminalGrant.model_validate({"session_id": "parent-terminal", "actions": actions})


@pytest.mark.asyncio
async def test_batch_rejects_ambiguous_top_level_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量顶层共享不能变成隐式广播，调用者须在每个任务明确声明。"""
    def create_graph(**_kwargs: Any) -> Any:
        """输入被拒绝时不能构造或执行子图。"""
        pytest.fail("批量输入校验失败不应构造子图")

    monkeypatch.setattr(subagent_module, "create_agent", create_graph)
    middleware = SubAgentTaskControlMiddleware(
        model=FakeListChatModel(responses=["unused"]), profiles=_builtin_subagent_profiles(),
        tools=[], policy_context=_policy(),
    )
    response = json.loads(await middleware.tools[0].ainvoke({
        "action": "run", "tasks": [{"description": "first"}, {"description": "second"}],
        "terminal_sessions": [{"session_id": "parent-terminal"}],
    }))
    assert response["success"] is False and "单独声明" in response["error"]
    assert middleware._tasks == {}
    assert await middleware.close()


@pytest.mark.asyncio
async def test_real_cached_graph_carries_each_child_scope_to_tool_node(terminal_parent) -> None:
    """同一实际缓存图并发调用时，ToolNode 只给获授权的子任务返回父终端证据。"""
    manager, parent, handle = terminal_parent
    provider = _SubAgentAgentProvider(
        model=_TerminalReadModel(responses=[AIMessage(content="unused")], terminal_handle=handle),
        profiles=_builtin_subagent_profiles(),
        tools=[ExecuteCommandTool(session_id="conversation", user_id="owner")],
        policy_context=_policy(),
    )
    _, graph = provider.get_agent("general-purpose")
    with bind_terminal_scope(parent):
        allowed, denied = await asyncio.gather(
            provider.run_task(description="读取已分享的证据", subagent_type="general-purpose", task_id="allowed",
                              terminal_sessions=[SubAgentTerminalGrant(session_id=handle)]),
            provider.run_task(description="尝试读取任务中给出的句柄", subagent_type="general-purpose", task_id="denied"),
        )
    assert provider.get_agent("general-purpose")[1] is graph
    assert "PARENT_ONLY" in allowed
    assert "terminal_access_denied" in denied and "PARENT_ONLY" not in denied
    assert manager._sessions[handle].process.returncode is None
    assert not manager._grants
