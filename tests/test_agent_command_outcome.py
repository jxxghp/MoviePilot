"""真实短命令通过 Agent 图与直调入口传播退出、超时、未知及取消语义。"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.policy.contracts import (
    AuthSource,
    ExecutionOutcome,
    PrincipalType,
    ToolOrigin,
    ToolPolicyContext,
)
from app.agent.policy.orchestrator import AgentToolPolicyOrchestrator
from app.agent.tools.impl import execute_command as command_module
from app.agent.tools.impl.execute_command import ExecuteCommandTool, _CommandOutput
from app.agent.tools.manager import MoviePilotToolsManager
from app.agent.tools.result import EXECUTION_OUTCOME_KEY, inspect_tool_result

pytestmark = pytest.mark.usefixtures("terminal_scope")


def _command(code: str) -> str:
    """固定使用当前虚拟环境解释器，并对每个 shell 参数独立转义。"""
    arguments = [sys.executable, "-c", code]
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def _tool() -> ExecuteCommandTool:
    """工具只拥有本测试会话的显式管理员上下文，不查询真实用户或渠道。"""
    tool = ExecuteCommandTool(session_id="command-outcome-test", user_id="command-test-owner")
    tool.set_agent_context({"is_admin": True})
    return tool


class _CommandModel(FakeMessagesListChatModel):
    """按固定两轮消息驱动生产 ToolNode，完全不调用真实模型。"""

    def bind_tools(self, _tools: Any, **_kwargs: Any) -> "_CommandModel":
        """接收图内实际工具绑定，命令由真实执行器运行。"""
        return self


class _RecordingPolicy(AgentToolPolicyOrchestrator):
    """保留生产回执生成逻辑，只额外记录测试可核验的终态。"""

    def __init__(self) -> None:
        """为每个测试创建独立回执列表。"""
        super().__init__()
        self.receipts: list[Any] = []

    def finish(self, observation: Any, result: Any) -> Any:
        """通过生产判定生成回执，避免测试自行推断工具状态。"""
        receipt = super().finish(observation, result)
        self.receipts.append(receipt)
        return receipt


@pytest.mark.asyncio
@pytest.mark.parametrize(("exit_code", "timed_out"), [(0, False), (7, False), (0, True)])
async def test_real_graph_command_outcomes_reach_message_receipt_and_log(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, timed_out: bool,
) -> None:
    """真实非零退出和已终止超时必须成为失败工具消息、失败回执及一致日志。"""
    tool = _tool()
    code = "import sys, time; print('stdout-probe', flush=True); print('stderr-probe', file=sys.stderr, flush=True); "
    code += "time.sleep(3)" if timed_out else f"raise SystemExit({exit_code})"
    arguments = {"action": "run", "command": _command(code), "timeout": 1 if timed_out else 5}
    model = _CommandModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "command", "name": tool.name, "args": arguments}]),
        AIMessage(content="命令结果已读取。"),
    ])
    policy = _RecordingPolicy()
    context = ToolPolicyContext(
        session_id="command-outcome-test", user_id="command-test-owner", origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN, auth_source=AuthSource.INTERNAL, agent_context={"is_admin": True},
    )
    logs: list[str] = []
    monkeypatch.setattr(command_module.logger, "info", logs.append)
    graph = create_agent(model=model, tools=[tool], middleware=[AgentPolicyMiddleware(context=context, orchestrator=policy)])
    result = await graph.ainvoke({"messages": [HumanMessage(content="执行测试命令")]})
    message = next(item for item in result["messages"] if isinstance(item, ToolMessage))
    expected = ExecutionOutcome.FAILED if timed_out or exit_code else ExecutionOutcome.SUCCEEDED
    assert message.status == ("error" if expected is ExecutionOutcome.FAILED else "success")
    assert message.additional_kwargs[EXECUTION_OUTCOME_KEY] == expected.value
    assert policy.receipts[0].outcome is expected
    assert any(f"outcome={expected.value}" in line for line in logs)
    payload = json.loads(message.content)
    assert payload["action"] == "run"
    assert payload["success"] is (expected is ExecutionOutcome.SUCCEEDED)
    assert payload["execution_outcome"] == expected.value
    assert payload["status"] == ("timed_out" if timed_out else "exited")
    assert payload["timed_out"] is timed_out
    assert payload["exit_code"] is not None
    if not timed_out:
        assert payload["exit_code"] == exit_code
    assert "stdout-probe" in payload["output"]
    assert "stderr-probe" in payload["output"]
    assert "stdout-probe" not in payload["message"]
    assert "stderr-probe" not in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_direct_tool_manager_keeps_command_outcome(exit_code: int) -> None:
    """HTTP/MCP 使用的直调工具入口也必须把真实退出状态交给同一回执判定。"""
    policy = _RecordingPolicy()
    manager = MoviePilotToolsManager(
        session_id="command-direct-test", user_id="command-test-owner", is_admin=True, policy_orchestrator=policy,
    )
    tool = _tool()
    manager.tools = [tool]
    result = await manager.call_tool(tool.name, {
        "action": "run", "command": _command(f"raise SystemExit({exit_code})"), "timeout": 5,
    })
    expected = ExecutionOutcome.FAILED if exit_code else ExecutionOutcome.SUCCEEDED
    assert inspect_tool_result(result) is expected
    assert policy.receipts[0].outcome is expected
    assert json.loads(result)["exit_code"] == exit_code


@pytest.mark.asyncio
async def test_run_honors_working_directory_and_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一次性命令必须在指定目录看到调用方提供的环境变量，父进程不能被污染。"""
    monkeypatch.delenv("MOVIEPILOT_COMMAND_TEST_VALUE", raising=False)
    result = await _tool().run(
        action="run", command=_command("import os; print(os.getcwd()); print(os.environ.get('MOVIEPILOT_COMMAND_TEST_VALUE', 'MISSING'))"),
        cwd=str(tmp_path), env={"MOVIEPILOT_COMMAND_TEST_VALUE": "command-env-marker"}, timeout=5,
    )
    payload = json.loads(result)
    assert payload["execution_outcome"] == "succeeded"
    assert str(tmp_path.resolve()) in payload["output"]
    assert "command-env-marker" in payload["output"]
    assert "MOVIEPILOT_COMMAND_TEST_VALUE" not in os.environ


@pytest.mark.parametrize("timed_out", [False, True])
def test_missing_exit_status_is_unknown_without_claiming_process_terminated(timed_out: bool) -> None:
    """无法取得真实退出码时必须保留未知，超时标志也不能证明进程已结束。"""
    output = _CommandOutput(preview_limit_bytes=1024)
    output.append("stdout", "partial-command-output")
    result = ExecuteCommandTool._format_run_result(
        exit_code=None, output=output, timeout=1, timed_out=timed_out, timeout_note=None,
    )
    assert inspect_tool_result(result) is ExecutionOutcome.UNKNOWN
    payload = json.loads(result)
    assert payload["success"] is False
    assert payload["execution_outcome"] == "unknown"
    assert payload["status"] == "unknown"
    assert payload["exit_code"] is None
    assert payload["timed_out"] is timed_out
    assert "已终止" not in payload["message"]
    assert "partial-command-output" in payload["output"]


@pytest.mark.asyncio
async def test_cancelled_run_finishes_readers_and_closes_output_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消真实命令必须传播取消，同时回收读取任务并关闭已经创建的输出归档。"""
    captured: list[_CommandOutput] = []
    original_append = _CommandOutput.append
    original_finish = ExecuteCommandTool._finish_reader_tasks
    archive_ready = asyncio.Event()
    reader_groups: list[list[asyncio.Task[Any]]] = []

    def append(output: _CommandOutput, stream: str, text: str) -> None:
        """在真实归档已创建时通知测试发出取消，不以固定休眠猜测子进程进度。"""
        original_append(output, stream, text)
        if output.temp_file_handle and not captured:
            captured.append(output)
            archive_ready.set()

    async def finish(readers: list[asyncio.Task[Any]]) -> None:
        """记录实际读取任务，再执行原有收尾流程。"""
        reader_groups.append(readers)
        await original_finish(readers)

    def temporary_file(**kwargs: Any) -> Any:
        """将命令归档限定在 pytest 临时目录，避免遗留系统临时文件。"""
        from tempfile import NamedTemporaryFile

        return NamedTemporaryFile(dir=tmp_path, **kwargs)

    monkeypatch.setattr(_CommandOutput, "append", append)
    monkeypatch.setattr(ExecuteCommandTool, "_finish_reader_tasks", staticmethod(finish))
    monkeypatch.setattr(command_module, "NamedTemporaryFile", temporary_file)
    task = asyncio.create_task(_tool().run(
        action="run", command=_command("import sys, time; sys.stdout.write('x' * 70000); sys.stdout.flush(); time.sleep(20)"), timeout=30,
    ))
    try:
        await asyncio.wait_for(archive_ready.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reader_groups
        assert all(reader.done() for readers in reader_groups for reader in readers)
        assert captured[0].temp_file_handle is None or captured[0].temp_file_handle.closed
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for output in captured:
            output.close()


@pytest.mark.asyncio
async def test_windows_shell_branch_receives_run_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """无需 Windows 宿主也验证显式 shell 分支不会丢失本次 env 覆盖值。"""
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    original_environment = command_module.build_agent_subprocess_env
    create_process = AsyncMock(return_value=process)

    class Shell:
        """提供与 Windows shell 策略相同的参数构造边界。"""

        executable = sys.executable
        login = False

        @staticmethod
        def build_argv(_command_text: str) -> list[str]:
            """真实子进程已在隔离环境启动，此处只检验启动参数。"""
            return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(command_module, "resolve_agent_shell", lambda **_kwargs: Shell())
    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", create_process)
    result = await _tool().run(action="run", command="unused", env={"MOVIEPILOT_COMMAND_TEST_VALUE": "shell-env-marker"}, timeout=5)
    assert create_process.call_args.kwargs["env"]["MOVIEPILOT_COMMAND_TEST_VALUE"] == "shell-env-marker"
    assert create_process.call_args.kwargs["env"] == original_environment({"MOVIEPILOT_COMMAND_TEST_VALUE": "shell-env-marker"})
    assert inspect_tool_result(result) is ExecutionOutcome.SUCCEEDED
