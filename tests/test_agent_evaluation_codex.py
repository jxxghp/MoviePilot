"""原生适配器只运行假进程或控制边界，核验生命周期、目录隔离和独立评分真实性。"""

import asyncio
import json
import os
import subprocess
import sys
import tomllib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from scripts.evaluation import codex
from scripts.evaluation.models import ModelSettings


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
