"""原生适配器只运行假进程或控制边界，核验生命周期、目录隔离和独立评分真实性。"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tomllib
import urllib.request
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from scripts.evaluation import codex
from scripts.evaluation.models import ModelSettings
from scripts.evaluation.proxy import NATIVE_SHELL_TOOLS, project_tools
from scripts.evaluation.score import _delegation_task_count
from scripts.evaluation.world import EvaluationWorld


def _program(tmp_path: Path, code: str) -> list[str]:
    """把无害 Python 假程序放在测试目录，不调用真实 Codex 或模型。"""
    script = tmp_path / "fake_native.py"
    script.write_text(code, encoding="utf-8")
    return [sys.executable, "-u", str(script)]


def _jsonl(events: list[dict[str, Any]]) -> str:
    """构造与原生客户端 stdout 相同的一行一个事件格式。"""
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"


def _completed_events(final: dict[str, Any]) -> list[dict[str, Any]]:
    """完成证据来自终态事件，最终业务报告仍交给真实 oracle。"""
    return [{"type": "turn.started"}, {"type": "item.completed", "item": {
        "type": "agent_message", "text": json.dumps(final),
    }}, {"type": "turn.completed"}]


def test_new_turn_cannot_reuse_previous_completion_or_final_text() -> None:
    """上一轮成功后开始了新轮但尚未完成，不能继续标成已完成。"""
    events, final, completed = codex._events(_jsonl([*_completed_events({"status": "completed"}), {"type": "turn.started"}]))
    assert len(events) == 4
    assert completed is False
    assert final == ""


def test_invalid_jsonl_preserves_valid_events_without_claiming_completion() -> None:
    """损坏日志用摘要保留诊断，不泄露非事件正文，也不能被后续 completed 抹平。"""
    text = _jsonl(_completed_events({"status": "completed"})) + "private-broken-event\n"
    events, final, completed = codex._events(text)
    assert completed is False
    assert json.loads(final)["status"] == "completed"
    assert events[-1]["type"] == "evaluation.invalid_event"
    assert len(events[-1]["sha256"]) == 64
    assert "private-broken-event" not in json.dumps(events)


def test_normalize_app_server_preserves_collaboration_scope_and_action() -> None:
    """原生协作事件要保留动作、线程和授权提示，便于独立验收子代理行为。"""
    item = codex._normalize_app_server_item({
        "type": "collabAgentToolCall", "id": "collab-1", "tool": "spawnAgent",
        "status": "inProgress", "prompt": '{"terminal_sessions":[{"session_id":"123","actions":["read"]}]}',
        "model": "gpt-test", "reasoningEffort": "high", "senderThreadId": "parent",
        "receiverThreadIds": ["child"], "agentsStates": {
            "child": {"status": "running", "message": "读取父终端"},
        },
    })
    assert item == {
        "type": "collab_tool_call", "id": "collab-1", "tool": "spawn_agent", "status": "inProgress",
        "prompt": '{"terminal_sessions":[{"session_id":"123","actions":["read"]}]}',
        "model": "gpt-test", "reasoning_effort": "high", "sender_thread_id": "parent",
        "receiver_thread_ids": ["child"], "agents_states": {
            "child": {"status": "running", "message": "读取父终端"},
        },
    }
    event = codex._normalize_app_server_notification(
        "item/started", {"threadId": "parent", "turnId": "turn-1", "item": {
            "type": "collabAgentToolCall", "id": "collab-1", "tool": "spawnAgent",
            "status": "inProgress", "agentsStates": {}, "receiverThreadIds": [], "senderThreadId": "parent",
        }},
    )
    assert event == {
        "type": "item.started", "thread_id": "parent", "turn_id": "turn-1", "item": {
            "type": "collab_tool_call", "id": "collab-1", "tool": "spawn_agent", "status": "inProgress",
            "prompt": None, "model": None, "reasoning_effort": None, "sender_thread_id": "parent",
            "receiver_thread_ids": [], "agents_states": {},
        },
    }
    assert _delegation_task_count([event]) == 1


def test_append_thread_history_events_preserves_child_scope() -> None:
    """thread/read 的历史命令应保留子代理线程和轮次身份。"""
    events: list[dict[str, Any]] = []
    codex._append_thread_history_events(events, {
        "id": "child", "turns": [{"id": "turn-child", "items": [{
            "type": "commandExecution", "id": "command-child", "command": "printf READY",
            "status": "completed", "aggregatedOutput": "READY\\n", "exitCode": 0,
        }]}],
    })
    assert events == [{
        "type": "item.completed", "thread_id": "child", "turn_id": "turn-child",
        "item": {
            "type": "command_execution", "id": "command-child", "command": "printf READY",
            "cwd": None, "process_id": None, "status": "completed", "aggregated_output": "READY\\n",
            "exit_code": 0, "command_actions": None,
        }, "thread_history": True,
    }]


@pytest.mark.asyncio
async def test_execute_sends_exact_prompt_and_preserves_nonzero_exit_output(tmp_path: Path) -> None:
    """真实假程序从 stdin 接收公开输入，非零退出与两路输出不得被丢弃。"""
    command = _program(tmp_path, "import sys\ntext=sys.stdin.read()\nprint(text)\nprint('stderr-marker',file=sys.stderr)\nraise SystemExit(7)\n")
    result = await codex._execute(command, "公开任务：保持原样 $HOME `echo no`", codex._worker_environment(), tmp_path, 5)
    assert result["returncode"] == 7
    assert result["error_type"] is None
    assert result["stdout"].strip() == "公开任务：保持原样 $HOME `echo no`"
    assert "stderr-marker" in result["stderr"]


@pytest.mark.asyncio
async def test_execute_app_server_preserves_streamed_terminal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """app-server 的终端增量和同一进程输入事件应归一化为可评分的原生轨迹。"""
    async def hold_stderr_reader(stream: asyncio.StreamReader, result: bytearray) -> None:
        """让测试覆盖正常收尾时主动取消 stderr 读取任务的路径。"""
        del stream, result
        await asyncio.Future()

    monkeypatch.setattr(codex, "_read_output", hold_stderr_reader)
    command = _program(tmp_path, """
import json
import sys

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})
    elif method == 'thread/start':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {'thread': {'id': 'thread-1'}}})
    elif method == 'turn/start':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})
        emit({'jsonrpc': '2.0', 'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1'}}})
        item = {
            'type': 'commandExecution', 'id': 'command-1', 'command': 'printf READY',
            'processId': 'process-1', 'status': 'inProgress',
        }
        emit({'jsonrpc': '2.0', 'method': 'item/started', 'params': {'item': item}})
        emit({'jsonrpc': '2.0', 'method': 'item/commandExecution/outputDelta',
              'params': {'itemId': 'command-1', 'processId': 'process-1', 'delta': 'READY\\n'}})
        emit({'jsonrpc': '2.0', 'method': 'item/commandExecution/terminalInteraction',
              'params': {'itemId': 'command-1', 'processId': 'process-1', 'stdin': 'MOVIEPILOT_TERMINAL_OK\\n'}})
        item.update(status='completed', aggregatedOutput=None, exitCode=0)
        emit({'jsonrpc': '2.0', 'method': 'item/completed', 'params': {'item': item}})
        final = {'status': 'completed', 'terminal_output': 'READY\\nMOVIEPILOT_TERMINAL_OK',
                 'terminal_exit_code': 0, 'completed': ['terminal'], 'unresolved': [],
                 'subscription_ids': [], 'download_ids': [], 'enabled_site_ids': []}
        emit({'jsonrpc': '2.0', 'method': 'item/completed', 'params': {
            'item': {'type': 'agentMessage', 'id': 'message-1', 'phase': 'final_answer',
                     'text': json.dumps(final)}}})
        emit({'jsonrpc': '2.0', 'method': 'turn/completed', 'params': {'threadId': 'thread-1'}})
""")
    result = await codex._execute_app_server(
        command, "公开终端任务", codex._worker_environment(), tmp_path, 5,
        model="gpt-test", reasoning_effort="high",
    )

    assert result["returncode"] == 0
    assert result["error_type"] is None
    events, final_text, completed = codex._events(result["stdout"])
    assert completed is True
    assert json.loads(final_text)["terminal_exit_code"] == 0
    assert any(event["type"] == "item.command_execution.terminal_interaction" for event in events)

    world = EvaluationWorld("terminal_session")
    codex._record_native_command_events(world, events)
    assert world.ledger[0]["observations"][0]["record"]["terminal_input_observed"] is True


@pytest.mark.asyncio
async def test_execute_app_server_steers_after_business_tool_boundary(tmp_path: Path) -> None:
    """中途追加必须在业务工具完成后携带当前轮次 ID 注入，并保留排队到应用的顺序。"""
    command = _program(tmp_path, """
import json
import sys

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})
    elif method == 'thread/start':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {'thread': {'id': 'thread-1'}}})
    elif method == 'turn/start':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})
        emit({'jsonrpc': '2.0', 'method': 'turn/started',
              'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1'}}})
        emit({'jsonrpc': '2.0', 'method': 'item/completed', 'params': {'item': {
            'type': 'mcpToolCall', 'id': 'call-1', 'server': 'evaluation',
            'tool': 'moviepilot_api', 'status': 'completed', 'arguments': {}, 'result': {},
        }}})
    elif method == 'turn/steer':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})
        emit({'jsonrpc': '2.0', 'method': 'item/completed', 'params': {'item': {
            'type': 'userMessage', 'id': 'steering-1', 'text': request['params']['input'][0]['text'],
        }}})
        emit({'jsonrpc': '2.0', 'method': 'turn/completed',
              'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1'}}})
""")
    result = await codex._execute_app_server(
        command, "公开任务", codex._worker_environment(), tmp_path, 5,
        model="gpt-test", reasoning_effort="high", steering_message="补充要求",
    )

    assert result["returncode"] == 0
    assert result["error_type"] is None
    events, _final_text, completed = codex._events(result["stdout"])
    assert completed is True
    queued_index = next(i for i, event in enumerate(events)
                         if event["type"] == "evaluation.steering.queued")
    applied_index = next(i for i, event in enumerate(events)
                         if event["type"] == "evaluation.steering.applied")
    user_index = next(i for i, event in enumerate(events)
                      if event.get("type") == "item.completed"
                      and event.get("item", {}).get("type") == "user_message")
    assert queued_index < applied_index < user_index
    assert events[queued_index]["message_id"] == codex.NATIVE_STEERING_MESSAGE_ID
    assert events[applied_index]["message_id"] == codex.NATIVE_STEERING_MESSAGE_ID


@pytest.mark.asyncio
async def test_execute_app_server_preserves_multiple_steering_boundaries(tmp_path: Path) -> None:
    """原生 app-server 的多条 steering 应按业务回执次数逐条排队和应用。"""
    command = _program(tmp_path, """
import json
import sys

def emit(value):
    print(json.dumps(value), flush=True)

steer_count = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})
    elif method == 'thread/start':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {'thread': {'id': 'thread-1'}}})
    elif method == 'turn/start':
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})
        emit({'jsonrpc': '2.0', 'method': 'turn/started',
              'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1'}}})
        emit({'jsonrpc': '2.0', 'method': 'item/completed', 'params': {'item': {
            'type': 'mcpToolCall', 'id': 'call-1', 'server': 'evaluation',
            'tool': 'moviepilot_api', 'status': 'completed', 'arguments': {}, 'result': {},
        }}})
    elif method == 'turn/steer':
        steer_count += 1
        emit({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})
        emit({'jsonrpc': '2.0', 'method': 'item/completed', 'params': {'item': {
            'type': 'userMessage', 'id': 'steering-%d' % steer_count,
            'text': request['params']['input'][0]['text'],
        }}})
        if steer_count == 1:
            for call_id in ('call-2', 'call-3'):
                emit({'jsonrpc': '2.0', 'method': 'item/completed', 'params': {'item': {
                    'type': 'mcpToolCall', 'id': call_id, 'server': 'evaluation',
                    'tool': 'moviepilot_api', 'status': 'completed', 'arguments': {}, 'result': {},
                }}})
        else:
            emit({'jsonrpc': '2.0', 'method': 'turn/completed',
                  'params': {'threadId': 'thread-1', 'turn': {'id': 'turn-1'}}})
""")
    result = await codex._execute_app_server(
        command,
        "公开任务",
        codex._worker_environment(),
        tmp_path,
        5,
        model="gpt-test",
        reasoning_effort="high",
        steering_plan=((1, "第一条"), (3, "第二条")),
    )

    assert result["returncode"] == 0
    assert result["error_type"] is None
    events, _final_text, completed = codex._events(result["stdout"])
    assert completed is True
    assert [event["message_id"] for event in events if event.get("type") == "evaluation.steering.queued"] == [
        "moviepilot-evaluation-steering-1", "moviepilot-evaluation-steering-2",
    ]
    assert [event["message_id"] for event in events if event.get("type") == "evaluation.steering.applied"] == [
        "moviepilot-evaluation-steering-1", "moviepilot-evaluation-steering-2",
    ]
    assert [
        event["item"]["text"] for event in events
        if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "user_message"
    ] == ["第一条", "第二条"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="继承进程组回收是 POSIX 合同")
@pytest.mark.parametrize(("cancel", "parent_exits"), [(False, False), (False, True), (True, False)])
async def test_execute_timeout_or_cancellation_closes_descendant_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool, parent_exits: bool,
) -> None:
    """继承 stdout 的后代不关闭管道就不会出现 EOF，用真实 EOF 验证整组回收。"""
    command = _program(tmp_path, (
        "import subprocess,sys,time\n"
        "sys.stdin.read()\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],stdin=subprocess.DEVNULL)\n"
        "print('READY',flush=True)\n"
        + ("raise SystemExit(0)\n" if parent_exits else "time.sleep(30)\n")
    ))
    ready = asyncio.Event()
    captured: list[asyncio.subprocess.Process] = []
    original_create = asyncio.create_subprocess_exec

    async def capture(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        """在真实 stdout 读取边界发布握手，取消测试不依靠固定休眠。"""
        process = await original_create(*args, **kwargs)
        captured.append(process)
        original_read = process.stdout.read

        async def read(size: int = -1) -> bytes:
            """确认父进程已创建后代并输出 READY 后才允许测试发起取消。"""
            data = await original_read(size)
            if b"READY" in data:
                ready.set()
            return data

        monkeypatch.setattr(process.stdout, "read", read)
        return process

    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(codex._execute(command, "task", codex._worker_environment(), tmp_path, 10 if cancel else 1))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result["error_type"] == "TimeoutError"
            assert "READY" in result["stdout"]
            assert result["returncode"] == (0 if parent_exits else -9)
        assert captured[0].returncode is not None
        assert await asyncio.wait_for(captured[0].stdout.read(), 2) == b""
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for process in captured:
            if process.returncode is None:
                await codex._stop_process(process)


@pytest.mark.asyncio
async def test_output_limit_preserves_bounded_prefix_and_stops_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超过输出预算时保留已取得的前缀，记录控制器失败并终止假进程。"""
    monkeypatch.setattr(codex, "MAX_PROCESS_OUTPUT_BYTES", 128)
    command = _program(tmp_path, "import sys,time\nsys.stdout.write('x'*4096)\nsys.stdout.flush()\ntime.sleep(30)\n")
    result = await codex._execute(command, "", codex._worker_environment(), tmp_path, 5)
    assert result["error_type"] == "RuntimeError"
    assert result["stdout"] == "x" * 128
    assert result["returncode"] is not None


@pytest.mark.asyncio
async def test_cleanup_failure_still_cancels_and_joins_output_readers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """组终止失败不能跳过其他读取器的 finally 回收。"""
    stdout, stderr = object(), object()
    readers: list[asyncio.Task[Any]] = []
    process = SimpleNamespace(stdin=SimpleNamespace(write=lambda _data: None, drain=AsyncMock(), close=lambda: None),
                              stdout=stdout, stderr=stderr, returncode=None)

    async def read(stream: Any, _buffer: bytearray) -> None:
        """一条读取器失败，另一条保持挂起以验证 gather 不会自动替控制器清理。"""
        readers.append(asyncio.current_task())
        if stream is stdout:
            raise RuntimeError("read failed")
        await asyncio.Event().wait()

    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(codex, "_read_output", read)
    monkeypatch.setattr(codex, "_stop_process", AsyncMock(side_effect=RuntimeError("cleanup failed")))
    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await codex._execute(["fake"], "", {}, tmp_path, 5)
        assert len(readers) == 2 and all(reader.done() for reader in readers)
    finally:
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


def _model_catalog() -> dict[str, Any]:
    """模拟二进制自带的模型记录，保留与工具模式无关的嵌套原生配置。"""
    return {"models": [{"slug": "gpt-test", "tool_mode": "code_mode_only", "base_instructions": "native instructions",
                        "model_messages": {"instructions_template": "native message template"}, "limits": [1, 2]},
                       {"slug": "unrelated", "tool_mode": "direct"}]}


def test_catalog_projection_changes_only_selected_tool_mode_and_is_independent() -> None:
    """控制条件投影不能替换模型提示词，也不能原地改写从二进制取得的目录。"""
    original = _model_catalog()
    before = deepcopy(original)
    projected, metadata = codex._catalog(original, "gpt-test")
    assert projected == {"models": [{**original["models"][0], "tool_mode": "direct"}]}
    assert metadata["original_tool_mode"] == "code_mode_only"
    assert metadata["projected_tool_mode"] == "direct"
    assert metadata["original_model_sha256"] != metadata["projected_model_sha256"]
    projected["models"][0]["model_messages"]["instructions_template"] = "changed copy"
    assert original == before


def test_native_app_server_stream_timeout_follows_evaluation_budget() -> None:
    """原生 app-server 的流式空闲上限必须与真实评测墙钟预算一致。"""
    settings = ModelSettings(
        "gpt-test",
        "https://provider.invalid/v1",
        "private-provider-test-key",
        timeout_seconds=180,
    )
    proxy = SimpleNamespace(endpoint="http://127.0.0.1:1/v1")
    server = SimpleNamespace(endpoint="http://127.0.0.1:2/mcp")
    configuration = codex._configuration(
        settings,
        proxy,
        server,
        Path("/tmp/moviepilot-evaluation-test"),
        scenario_id="terminal_pty_session",
    )
    assert configuration["model_providers.evaluation"]["stream_idle_timeout_ms"] == 180_000


def test_browser_runtime_expands_portable_skill_root_in_private_instruction_copy(tmp_path: Path,
                                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """独立 CLI 没有桌面插件上下文时，浏览器 Skill 的根路径仍必须可执行且不改写安装文件。"""
    codex_home = tmp_path / "codex-home"
    plugin_root = codex_home / ".tmp" / "bundled-marketplaces" / "openai-bundled" / "plugins" / "browser"
    node_repl = tmp_path / "node" / "node_repl"
    node_repl.parent.mkdir(parents=True)
    node_repl.write_text("", encoding="utf-8")
    (node_repl.parent / "node").write_text("", encoding="utf-8")
    (node_repl.parent.parent / "lib" / "node_modules").mkdir(parents=True)
    (plugin_root / "scripts").mkdir(parents=True)
    (plugin_root / "scripts" / "browser-client.mjs").write_text("client", encoding="utf-8")
    skill = plugin_root / "skills" / "control-in-app-browser" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("import('<plugin root>/scripts/browser-client.mjs')", encoding="utf-8")
    service = codex_home / "plugins" / "cache" / "openai-bundled" / "browser" / "26.903.71938" / "scripts" / "browser-service.mjs"
    service.parent.mkdir(parents=True)
    service.write_text("service", encoding="utf-8")
    instruction_dir = tmp_path / "control"
    instruction_dir.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("MOVIEPILOT_EVAL_NODE_REPL", str(node_repl))
    runtime = codex._browser_runtime_configuration("/bin/codex", instruction_dir)
    assert runtime is not None
    expanded = Path(runtime["skill"])
    assert expanded != skill and expanded.is_file()
    assert str(plugin_root) in expanded.read_text(encoding="utf-8")
    assert "<plugin root>" not in expanded.read_text(encoding="utf-8")
    assert runtime["node_repl"]["env"]["NODE_REPL_TRUSTED_SERVICES"]


def test_native_browser_fixture_records_only_loopback_click_callback() -> None:
    """原生浏览器插件绕过评测 MCP 时，回环页面回调仍能作为独立点击证据。"""
    with codex._native_browser_fixture() as (url, state):
        page = urllib.request.urlopen(url, timeout=2).read().decode("utf-8")
        assert "BROWSER_OK" in page and state["clicked"] is False
        urllib.request.urlopen(url.replace("/fixture", "/clicked"), timeout=2).read()
        assert state["clicked"] is True


@pytest.mark.parametrize("catalog", [{}, {"models": []}, {"models": [{"slug": "gpt-test"}, {"slug": "gpt-test"}]}])
def test_missing_or_ambiguous_selected_model_never_falls_back(catalog: dict[str, Any]) -> None:
    """模型元数据缺失或重复时必须中止，不能换另一模型制造成功对照。"""
    with pytest.raises(ValueError):
        codex._catalog(catalog, "gpt-test")


async def _run_case(monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, completed: bool = True,
                    error_type: Optional[str] = None, echo_tokens: bool = False) -> dict[str, Any]:
    """只替换原生进程与模型边界，业务状态和 oracle 仍使用真正内存世界。"""
    worlds = []
    settings = ModelSettings("gpt-test", "https://provider.invalid/v1", "private-provider-test-key")

    class Server:
        """捕获同一世界，等价模拟真实 MCP 已读取的独立业务事实。"""

        endpoint = "http://127.0.0.1:49997/mcp"
        bearer_token = "local-mcp-test-token"
        skill_sha256 = "skill-digest"
        stats: dict[str, Any] = {}

        def __init__(self, world: Any) -> None:
            """保留控制器传入的真实世界，模拟器不能替换 oracle。"""
            worlds.append(world)

        async def __aenter__(self) -> "Server":
            """不启动服务，所有验证都在本进程的模拟边界内。"""
            return self

        async def __aexit__(self, *_args: Any) -> None:
            """没有外部监听或进程需要清理。"""

    class Proxy(Server):
        """模型调用统计由模拟控制边界给出，不产生真实请求。"""

        endpoint = "http://127.0.0.1:49998/v1"
        bearer_token = "local-model-test-token"

        def __init__(self, _settings: ModelSettings, **_kwargs: Any) -> None:
            """接受真实控制器参数，但不把连接凭据送到子进程。"""
            self.closed = False

        async def __aexit__(self, *_args: Any) -> None:
            """模拟客户端断开后的最终资源收敛，统计必须在此之后取得。"""
            self.closed = True

        def snapshot(self) -> dict[str, Any]:
            """返回最终模拟用量，关闭前不能冻结仍可能变化的完成/取消状态。"""
            assert self.closed
            return {"model_calls": 1, "completed_model_calls": 1, "probe_requests": 0, "model_requests": [],
                    "tokens": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}

    async def execute(command: list[str], prompt: str, environment: dict[str, str], work_dir: Path, _timeout: int) -> dict[str, Any]:
        """检查 argv、私有环境与中立目录，然后生成原生 JSONL 事件。"""
        assert settings.api_key not in json.dumps(environment)
        assert "OPENAI_API_KEY" not in environment and "CONFIG_DIR" not in environment
        assert list(work_dir.iterdir()) == []
        if command[1:] == ["debug", "models", "--bundled"]:
            assert not prompt
            return {"returncode": 0, "stdout": json.dumps(_model_catalog()), "stderr": "", "error_type": None}
        assert command[1] == "exec" and command[-1] == "-"
        for flag in ("--ignore-user-config", "--ignore-rules", "--strict-config", "--ephemeral", "--json", "--skip-git-repo-check"):
            assert flag in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[command.index("-C") + 1] == str(work_dir)
        assert 'approval_policy="never"' in command
        mcp = tomllib.loads(next(value for value in command if value.startswith("mcp_servers=")))["mcp_servers"]
        assert set(mcp) == {"evaluation"}
        assert set(mcp["evaluation"]["enabled_tools"]) == {"moviepilot_api", "read_skill", "read_tool_result"}
        assert mcp["evaluation"]["tools"] == {name: {"approval_mode": "approve"}
                                                for name in mcp["evaluation"]["enabled_tools"]}
        assert settings.api_key not in json.dumps(command)
        assert "oracle" not in prompt and "dedup_existing" not in prompt
        world = worlds[0]
        subscription = world.execute("subscription.find", path_params={"media_id": world.scenario.media_id},
                                     query={"media_source": world.scenario.media_source})["data"]
        downloads = world.execute("download.tasks.active")["data"]
        final = {"status": "completed", "subscription_ids": [subscription["id"]],
                 "download_ids": [row["id"] for row in downloads if row["infohash"] == world.scenario.infohash],
                 "enabled_site_ids": [], "completed": ["subscription", "download"], "unresolved": []}
        if echo_tokens:
            final["debug"] = [Server.bearer_token, Proxy.bearer_token]
        events = _completed_events(final)
        if not completed:
            events[-1] = {"type": "turn.failed", "error": {"message": "native failed"}}
        stdout = _jsonl(events)
        if echo_tokens:
            for token in (Server.bearer_token, Proxy.bearer_token):
                stdout = stdout.replace(token, "".join(f"\\u{ord(char):04x}" for char in token))
        return {"returncode": returncode, "stdout": stdout, "stderr": "", "error_type": error_type}

    monkeypatch.setattr(codex, "EvaluationMcpServer", Server)
    monkeypatch.setattr(codex, "EvaluationModelProxy", Proxy)
    monkeypatch.setattr(codex, "_execute", execute)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-private-key")
    return await codex._run_codex("dedup_existing", settings, "fake-codex", probe_only=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(("returncode", "completed", "error_type"), [(7, True, None), (0, False, None), (0, True, "TimeoutError"), (0, True, None)])
async def test_native_success_is_required_even_when_real_oracle_passes(monkeypatch: pytest.MonkeyPatch, returncode: int,
                                                                    completed: bool, error_type: Optional[str]) -> None:
    """真实状态和读取证据正确也不能掩盖原生退出、未完成或控制器异常。"""
    report = await _run_case(monkeypatch, returncode=returncode, completed=completed, error_type=error_type)
    assert report["task_passed"] is True
    assert report["passed"] is (returncode == 0 and completed and error_type is None)
    assert report["native_exit_code"] == returncode
    assert report["native_turn_completed"] is completed


@pytest.mark.asyncio
async def test_local_tokens_are_removed_after_native_json_unicode_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON Unicode 转义不能绕过本地代理和 MCP 令牌的最终报告脱敏。"""
    report = await _run_case(monkeypatch, echo_tokens=True)
    serialized = json.dumps(report)
    assert "local-mcp-test-token" not in serialized
    assert "local-model-test-token" not in serialized


@pytest.mark.parametrize(("names", "calls", "rejected", "world_calls", "ready"), [
    (["functions.update_plan"], 0, [], 0, False),
    (["tool_search"], 0, [], 0, True),
    (["tool_search"], 1, [], 0, False),
    (["tool_search"], 0, [{}], 0, False),
    (["tool_search"], 0, [], 1, False),
])
def test_probe_requires_fixture_discovery_without_model_or_world_execution(names: list[str], calls: int,
                                                                          rejected: list[Any], world_calls: int,
                                                                          ready: bool) -> None:
    """空目录和仅计划目录不能被作为可执行对照入口，探针也不能消耗真实预算。"""
    usage = {"probe_requests": 1, "model_calls": calls, "rejected_requests": rejected,
             "model_requests": [{"retained_tools": names}]}
    assert codex._probe_ready(usage, {"world_calls": world_calls}) is ready


@pytest.mark.parametrize("version", ["codex-cli 0.153.3", codex.SUPPORTED_CLI_VERSION])
def test_cli_version_gate_uses_isolated_environment_before_any_execution(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    """只在已核对版本上进入适配器；版本查询本身也不继承真实模型凭据。"""
    calls = []

    def check(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """模拟真实 --version 响应，不运行原生客户端。"""
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=version + "\n", stderr="")

    execute = AsyncMock(return_value={"checked": True})
    monkeypatch.setattr(codex.shutil, "which", lambda _name: "/fake/codex")
    monkeypatch.setattr(codex.subprocess, "run", check)
    monkeypatch.setattr(codex, "_run_codex", execute)
    monkeypatch.setenv("OPENAI_API_KEY", "private-key-not-for-native")
    settings = ModelSettings("gpt-test", "https://provider.invalid/v1", "provider-key")
    if version != codex.SUPPORTED_CLI_VERSION:
        with pytest.raises(RuntimeError, match="只核对过"):
            codex.run_codex("dedup_existing", settings)
        execute.assert_not_awaited()
    else:
        assert codex.run_codex("dedup_existing", settings) == {"checked": True}
    assert calls[0][0] == ["/fake/codex", "--version"]
    assert calls[0][1]["timeout"] == 10
    assert "OPENAI_API_KEY" not in calls[0][1]["env"]


def test_toml_arguments_preserve_literals_without_shell_interpretation() -> None:
    """带引号、换行和 shell 字符的模型配置仍是单个正确 TOML 值。"""
    value = {"name": "引号\"与换行\n以及$(command)", "enabled": False, "budget": 12}
    assert tomllib.loads("value=" + codex._toml(value))["value"] == value


def test_command_scenario_enables_only_native_shell_controls() -> None:
    """命令场景显式开启原生终端开关，普通 API 场景继续关闭。"""
    settings = ModelSettings("gpt-test", "https://provider.invalid/v1", "provider-key")
    proxy = SimpleNamespace(endpoint="http://model.invalid/v1")
    server = SimpleNamespace(endpoint="http://mcp.invalid/mcp", bearer_token="token")
    control = Path("/tmp/evaluation-control")
    api_config = codex._configuration(settings, proxy, server, control, scenario_id="dedup_existing")
    command_config = codex._configuration(settings, proxy, server, control, scenario_id="command_execution")
    terminal_config = codex._configuration(settings, proxy, server, control, scenario_id="terminal_session")
    shared_terminal_config = codex._configuration(
        settings, proxy, server, control, scenario_id="subagent_terminal_share",
    )
    assert all(api_config[f"features.{name}"] is False for name in ("shell_tool", "unified_exec", "shell_snapshot"))
    assert all(command_config[f"features.{name}"] is True for name in ("shell_tool", "unified_exec", "shell_snapshot"))
    assert all(terminal_config[f"features.{name}"] is True for name in ("shell_tool", "unified_exec", "shell_snapshot"))
    assert all(shared_terminal_config[f"features.{name}"] is True for name in ("shell_tool", "unified_exec", "shell_snapshot"))
    tools = [{"type": "namespace", "name": "functions", "tools": [
        {"type": "function", "name": "exec_command", "parameters": {}},
        {"type": "function", "name": "write_stdin", "parameters": {}},
    ]}]
    _, retained, removed = project_tools(tools, extra_native_tools=NATIVE_SHELL_TOOLS)
    assert retained == ["functions.exec_command", "functions.write_stdin"] and removed == []


def test_native_terminal_output_cannot_fake_stdin_evidence() -> None:
    """原生 command_execution 聚合输出即使包含输入标记，也不能伪造 stdin 证据。"""
    world = EvaluationWorld("terminal_session")
    codex._record_native_command_events(world, [{"type": "item.completed", "item": {
        "id": "cmd-1", "type": "command_execution", "command": "/bin/zsh -lc " + shlex.quote(world.scenario.command),
        "aggregated_output": "READY\r\nMOVIEPILOT_TERMINAL_OK\r\nREPLY=MOVIEPILOT_TERMINAL_OK\r\n", "exit_code": 0,
    }}])
    event = world.ledger[0]
    assert event["request"]["action"] == "start"
    assert event["observations"][0]["record"]["terminal_input_observed"] is False


def test_native_command_events_infer_subagent_scope_from_spawn_thread() -> None:
    """子代理线程中的原生命令回执应进入独立 scope，不能冒充父任务操作。"""
    world = EvaluationWorld("subagent_terminal_share")
    command = world.scenario.command
    events = [
        {"type": "item.started", "thread_id": "parent", "item": {
            "type": "collab_tool_call", "tool": "spawn_agent", "receiver_thread_ids": ["child"],
        }},
        {"type": "item.completed", "thread_id": "child", "item": {
            "id": "child-command", "type": "command_execution", "command": command,
            "aggregated_output": "SHARED_READY\n", "exit_code": 0,
        }},
    ]
    codex._record_native_command_events(world, events)
    assert world.ledger[0]["scope"] == {"kind": "subagent", "task_id": "child"}
