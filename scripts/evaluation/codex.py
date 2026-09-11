"""让原生 Codex CLI 操作同一假世界，模型循环与独立评分器不共用状态。"""

import asyncio
import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from scripts.evaluation.live import _parse_final, _validate_report, _worker_environment
from scripts.evaluation.models import ModelSettings
from scripts.evaluation.proxy import NATIVE_SHELL_TOOLS, EvaluationModelProxy
from scripts.evaluation.score import evaluate
from scripts.evaluation.server import EvaluationMcpServer
from scripts.evaluation.world import EvaluationWorld

SUPPORTED_CLI_VERSION = "codex-cli 0.153.4"
MAX_PROCESS_OUTPUT_BYTES = 8 * 1024 * 1024
NATIVE_TERMINAL_SCENARIOS = frozenset({"terminal_session", "terminal_pty_session"})


def _catalog(payload: Any, model: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """仅修改实际二进制自带模型的工具模式，原生提示词及其他模型字段逐字保留。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("原生模型目录结构无效")
    matches = [row for row in payload["models"] if isinstance(row, dict) and row.get("slug") == model]
    if len(matches) != 1:
        raise ValueError("原生二进制没有唯一的指定模型元数据，不能替换为其他模型")
    original = matches[0]
    projected = copy.deepcopy(original)
    projected["tool_mode"] = "direct"
    def digest(value: Any) -> str:
        """对相同序列化规则取指纹，便于复验单字段投影。"""
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    metadata = {"source": "codex_debug_models_bundled", "model": model,
                "original_tool_mode": original.get("tool_mode"), "projected_tool_mode": "direct",
                "original_model_sha256": digest(original), "projected_model_sha256": digest(projected),
                "base_instructions_sha256": digest(original.get("base_instructions")),
                "model_messages_sha256": digest(original.get("model_messages"))}
    return {"models": [projected]}, metadata


def _toml(value: Any) -> str:
    """按 TOML 表达配置值，直接传 argv，不经过 shell 或其变量展开。"""
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(key)} = {_toml(item)}" for key, item in value.items()) + " }"
    return json.dumps(value, ensure_ascii=False)


def _redact(value: Any, tokens: tuple[str, ...]) -> Any:
    """在全部事件和最终 JSON 解码后移除局部令牌，Unicode 转义不能绕过。"""
    if isinstance(value, str):
        for token in tokens:
            value = value.replace(token, "[local evaluation token]")
        return value
    if isinstance(value, dict):
        return {_redact(key, tokens): _redact(item, tokens) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, tokens) for item in value]
    return value


def _configuration(settings: ModelSettings, proxy: EvaluationModelProxy, server: EvaluationMcpServer,
                   control_dir: Path, *, scenario_id: str = "") -> dict[str, Any]:
    """显式隔离原生客户端的外部能力，仍保留其默认提示词、计划及子代理循环。"""
    disabled = [
        "apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access", "computer_use",
        "in_app_browser", "in_app_chat", "image_generation", "view_image", "hooks", "memories", "plugins",
        "remote_plugin", "plugin_sharing", "skill_search",
        "skill_mcp_dependency_install", "workspace_dependencies", "goals", "sleep_tool", "code_mode",
        "code_mode_host", "enable_request_compression", "unbounded_connection_retries", "tool_suggest",
    ]
    if scenario_id not in {"command_execution", "terminal_session", "terminal_pty_session"}:
        disabled.extend(("shell_tool", "unified_exec", "shell_snapshot"))
    if scenario_id == "browser_navigation":
        for feature in ("browser_use", "browser_use_external", "browser_use_full_cdp_access", "computer_use", "in_app_browser"):
            disabled.remove(feature)
    configuration = {
        "model": settings.model, "model_provider": "evaluation", "model_reasoning_effort": settings.reasoning_effort,
        "model_context_window": settings.context_window, "approval_policy": "never", "web_search": "disabled",
        "tools.update_plan.enabled": True,
        "project_doc_max_bytes": 0, "skills.bundled.enabled": False, "skills.include_instructions": False,
        "sqlite_home": str(control_dir / "state"), "log_dir": str(control_dir / "logs"),
        "model_catalog_json": str(control_dir / "models.json"),
        "features.skip_host_skill_discovery": True, **{f"features.{name}": False for name in disabled},
        "features.code_mode_only": False,
        "features.code_mode_host": {"enabled": False, "disable_in_process_fallback": False},
        "model_providers.evaluation": {
            "name": "Controlled evaluation proxy", "base_url": proxy.endpoint,
            "env_key": "MOVIEPILOT_EVAL_MODEL_TOKEN", "wire_api": "responses", "requires_openai_auth": False,
            "request_max_retries": 0, "stream_max_retries": 0,
            "stream_idle_timeout_ms": min(settings.timeout_seconds, 120) * 1000,
        },
        "mcp_servers": {"evaluation": {
            "url": server.endpoint, "bearer_token_env_var": "MOVIEPILOT_EVAL_MCP_TOKEN",
            "enabled": True, "required": True, "startup_timeout_sec": 10, "tool_timeout_sec": 15,
            "enabled_tools": ["moviepilot_api", "read_skill", "read_tool_result"],
            "tools": {name: {"approval_mode": "approve"} for name in ("moviepilot_api", "read_skill", "read_tool_result")},
        }},
    }
    if scenario_id in {"command_execution", "terminal_session", "terminal_pty_session"}:
        # 命令场景只开放 CLI 已核对的两个终端动作；仍使用只读沙箱和 never 审批。
        configuration.update({f"features.{name}": True for name in ("shell_tool", "unified_exec", "shell_snapshot")})
    elif scenario_id == "browser_navigation":
        # 让探针核对 CLI 自身是否广告浏览器能力；当前版本未广告时必须留在差异证据中。
        configuration.update({f"features.{name}": True for name in (
            "browser_use", "browser_use_external", "browser_use_full_cdp_access", "computer_use", "in_app_browser",
        )})
    return configuration


async def _read_output(stream: asyncio.StreamReader, result: bytearray) -> None:
    """持续排空子进程管道，输出超限时让控制器终止整组进程。"""
    while chunk := await stream.read(65536):
        remaining = MAX_PROCESS_OUTPUT_BYTES - len(result)
        result.extend(chunk[:remaining])
        if len(chunk) > remaining:
            raise RuntimeError("原生评测进程输出超过 8 MiB")


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """父任务取消或超时时回收原生客户端及继承进程组，不遗留后台评测。"""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        process.kill()
    await asyncio.wait_for(process.wait(), timeout=10)


async def _execute(command: list[str], prompt: str, environment: dict[str, str],
                   work_dir: Path, timeout: int) -> dict[str, Any]:
    """只向原生 stdin 发送公开任务，结构化事件和退出状态由控制器采集。"""
    process = await asyncio.create_subprocess_exec(
        *command, cwd=work_dir, env=environment, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
    )
    readers: list[asyncio.Task[None]] = []
    stdout, stderr = bytearray(), bytearray()
    failure = None
    try:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        readers = [asyncio.create_task(_read_output(process.stdout, stdout)),
                   asyncio.create_task(_read_output(process.stderr, stderr))]
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        async with asyncio.timeout(timeout):
            await asyncio.gather(*readers)
            await process.wait()
    except (RuntimeError, TimeoutError, OSError) as error:
        failure = type(error).__name__
    finally:
        try:
            await _stop_process(process)
        finally:
            for task in readers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
    return {"returncode": process.returncode, "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"), "error_type": failure}


def _normalize_app_server_item(item: Any) -> dict[str, Any]:
    """把 app-server 的 camelCase item 转成评测器共用的安全事件字段。"""
    if not isinstance(item, dict):
        return {"type": "unknown"}
    item_type = item.get("type")
    normalized_type = {
        "agentMessage": "agent_message",
        "commandExecution": "command_execution",
        "fileChange": "file_change",
        "userMessage": "user_message",
    }.get(item_type, item_type if isinstance(item_type, str) else "unknown")
    result: dict[str, Any] = {"type": normalized_type, "id": item.get("id")}
    if normalized_type == "agent_message":
        result.update(text=item.get("text", ""), phase=item.get("phase"))
    elif normalized_type == "command_execution":
        result.update(
            command=item.get("command", ""), cwd=item.get("cwd"), process_id=item.get("processId"),
            status=item.get("status"), aggregated_output=item.get("aggregatedOutput"),
            exit_code=item.get("exitCode"), command_actions=item.get("commandActions"),
        )
    return result


def _normalize_app_server_notification(method: str, params: Any) -> dict[str, Any] | None:
    """保留终端、消息和轮次终态，把其他 app-server 通知降为可审计摘要。"""
    payload = params if isinstance(params, dict) else {}
    if method == "turn/started":
        return {"type": "turn.started"}
    if method == "turn/completed":
        return {"type": "turn.completed"}
    if method == "item/started":
        return {"type": "item.started", "item": _normalize_app_server_item(payload.get("item"))}
    if method == "item/completed":
        return {"type": "item.completed", "item": _normalize_app_server_item(payload.get("item"))}
    if method == "item/agentMessage/delta":
        return {
            "type": "item.agent_message.delta", "item_id": payload.get("itemId"),
            "delta": payload.get("delta", ""),
        }
    if method == "item/commandExecution/outputDelta":
        return {
            "type": "item.command_execution.output_delta", "item_id": payload.get("itemId"),
            "delta": payload.get("delta", ""),
        }
    if method == "item/commandExecution/terminalInteraction":
        return {
            "type": "item.command_execution.terminal_interaction", "item_id": payload.get("itemId"),
            "process_id": payload.get("processId"), "stdin": payload.get("stdin", ""),
        }
    if method == "error":
        return {"type": "error", "error": payload}
    if method == "warning":
        return {"type": "warning", "warning": payload}
    return {"type": "native.notification", "method": method} if method else None


async def _execute_app_server(
    command: list[str], prompt: str, environment: dict[str, str], work_dir: Path, timeout: int,
    *, model: str, reasoning_effort: str,
) -> dict[str, Any]:
    """通过 Codex app-server 的 JSON-RPC 驱动可写 stdin 的原生终端会话。"""
    process = await asyncio.create_subprocess_exec(
        *command, cwd=work_dir, env=environment, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stderr = bytearray()
    stderr_reader = asyncio.create_task(_read_output(process.stderr, stderr))
    events: list[dict[str, Any]] = []
    stdout_bytes = 0
    pending: dict[int, str] = {}
    next_request_id = 1
    thread_id: str | None = None
    turn_completed = False
    failure: str | None = None

    async def send_request(method: str, params: dict[str, Any]) -> int:
        """发送带序号的 JSON-RPC 请求，并记录响应所属方法。"""
        nonlocal next_request_id
        request_id = next_request_id
        next_request_id += 1
        pending[request_id] = method
        process.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n").encode()
        )
        await process.stdin.drain()
        return request_id

    async def send_notification(method: str) -> None:
        """发送不等待响应的 JSON-RPC 通知。"""
        process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
        await process.stdin.drain()

    async def send_server_error(request_id: Any) -> None:
        """拒绝意外的原生服务端请求，避免评测控制器代替用户授权。"""
        process.stdin.write(
            (json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32000, "message": "评测适配器不接受原生交互授权请求"},
            }) + "\n").encode()
        )
        await process.stdin.drain()

    async def handle_response(payload: dict[str, Any]) -> None:
        """按 initialize、thread/start、turn/start 的顺序推进原生轮次。"""
        nonlocal thread_id, failure
        request_id = payload.get("id")
        method = pending.pop(request_id, None) if type(request_id) is int else None
        if method is None:
            await send_server_error(request_id)
            failure = failure or "unexpected_server_request"
            return
        if "error" in payload:
            failure = failure or f"app_server_{method.replace('/', '_')}_error"
            return
        if method == "initialize":
            await send_notification("initialized")
            await send_request(
                "thread/start",
                {"cwd": str(work_dir), "model": model, "sandbox": "read-only", "approvalPolicy": "never", "ephemeral": True},
            )
        elif method == "thread/start":
            result = payload.get("result")
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                failure = failure or "app_server_thread_id_missing"
                return
            await send_request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "model": model,
                    "effort": reasoning_effort,
                },
            )

    try:
        async with asyncio.timeout(timeout):
            await send_request(
                "initialize",
                {"clientInfo": {"name": "moviepilot-evaluation", "version": "1"},
                 "capabilities": {"experimentalApi": True}},
            )
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                stdout_bytes += len(line)
                if stdout_bytes + len(stderr) > MAX_PROCESS_OUTPUT_BYTES:
                    raise RuntimeError("原生 app-server 输出超过 8 MiB")
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    events.append({"type": "evaluation.invalid_event", "sha256": hashlib.sha256(line).hexdigest()})
                    failure = failure or "invalid_native_events"
                    continue
                if not isinstance(payload, dict):
                    failure = failure or "invalid_native_events"
                    continue
                if "id" in payload and "method" not in payload:
                    await handle_response(payload)
                    continue
                method = payload.get("method")
                if not isinstance(method, str):
                    failure = failure or "invalid_native_events"
                    continue
                event = _normalize_app_server_notification(method, payload.get("params"))
                if event is not None:
                    events.append(event)
                if method == "turn/completed":
                    turn_completed = True
                    break
                if "id" in payload:
                    await send_server_error(payload["id"])
                    failure = failure or "unexpected_server_request"
    except (RuntimeError, TimeoutError, OSError) as error:
        failure = failure or type(error).__name__
    finally:
        if process.returncode is None:
            if turn_completed and failure is None:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    failure = failure or "app_server_shutdown_timeout"
                    await _stop_process(process)
            else:
                await _stop_process(process)
        if not stderr_reader.done():
            stderr_reader.cancel()
        reader_result = (await asyncio.gather(stderr_reader, return_exceptions=True))[0]
        if isinstance(reader_result, BaseException):
            failure = failure or type(reader_result).__name__
    return {
        "returncode": process.returncode,
        "stdout": "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + ("\n" if events else ""),
        "stderr": stderr.decode("utf-8", errors="replace"), "error_type": failure,
    }


def _events(text: str) -> tuple[list[dict[str, Any]], str, bool]:
    """保留 JSONL 原生事件；只有完成事件之后的有效最终报告才由 oracle 判断。"""
    events, final_text, completed, malformed = [], "", False, False
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("原生客户端输出非事件对象")
        except (ValueError, TypeError):
            malformed = True
            events.append({"type": "evaluation.invalid_event", "sha256": hashlib.sha256(line.encode()).hexdigest()})
            continue
        events.append(event)
        if event.get("type") == "turn.started":
            completed, final_text = False, ""
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            final_text = str(item.get("text") or "")
        if event.get("type") == "turn.completed":
            completed = True
        elif event.get("type") in {"turn.failed", "error"}:
            completed = False
    return events, final_text, completed and not malformed


def _record_native_command_events(world: EvaluationWorld, events: list[dict[str, Any]]) -> None:
    """把 CLI 的已完成命令事件映射为同一场景账本，避免只看模型文字。"""
    seen: set[str] = set()
    streamed_output: dict[str, str] = {}
    streamed_input: set[str] = set()
    for event in events:
        event_type = event.get("type")
        if event_type == "item.command_execution.output_delta":
            item_id = event.get("item_id")
            delta = event.get("delta")
            if isinstance(item_id, str) and isinstance(delta, str):
                streamed_output[item_id] = streamed_output.get(item_id, "") + delta
            continue
        if event_type == "item.command_execution.terminal_interaction":
            item_id = event.get("item_id")
            stdin = event.get("stdin")
            if isinstance(item_id, str) and isinstance(stdin, str) and "MOVIEPILOT_TERMINAL_OK" in stdin:
                streamed_input.add(item_id)
            continue
        if event_type != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") not in {"command_execution", "command_execution_output"}:
            continue
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        identity = str(item.get("id") or command)
        if identity in seen:
            continue
        seen.add(identity)
        output = item.get("aggregated_output", item.get("output", ""))
        if not isinstance(output, str):
            output = str(output or "")
        if not output:
            output = streamed_output.get(identity, "")
        exit_code = item.get("exit_code", item.get("exitCode"))
        if type(exit_code) is not int:
            exit_code = None
        outcome = "succeeded" if exit_code == 0 else ("unknown" if exit_code is None else "failed")
        terminal_input_observed = world.scenario.kind == "terminal" and (
            identity in streamed_input or "MOVIEPILOT_TERMINAL_OK" in output.replace("\r", "")
        )
        world.record_command(command.strip(), {
            "action": "start" if world.scenario.kind == "terminal" else "run",
            "success": outcome == "succeeded", "execution_outcome": outcome,
            "status": "exited" if exit_code is not None else "unknown", "exit_code": exit_code,
            "timed_out": False, "output": output, "terminal_input_observed": terminal_input_observed,
        }, action="start" if world.scenario.kind == "terminal" else "run")


def _probe_ready(usage: dict[str, Any], server_stats: dict[str, Any]) -> bool:
    """探针必须到达实际业务工具目录且没有真实模型或业务调用，仅有计划工具不算就绪。"""
    retained = {name for record in usage.get("model_requests", []) for name in record.get("retained_tools", [])}
    fixture = {f"mcp__evaluation.{name}" for name in ("moviepilot_api", "read_skill", "read_tool_result")}
    return bool(usage.get("probe_requests") == 1 and not usage.get("model_calls")
                and not usage.get("rejected_requests") and not server_stats.get("world_calls")
                and ("tool_search" in retained or fixture <= retained))


async def _run_codex(scenario_id: str, settings: ModelSettings, executable: str, *, probe_only: bool) -> dict[str, Any]:
    """每次运行创建新目录、新模型预算和新世界，源码与 oracle 从不进入模型工作目录。"""
    from scripts.evaluation.__main__ import _provenance

    world = EvaluationWorld(scenario_id)
    if world.scenario.kind == "browser" and not world.browser_url:
        # CLI 当前没有浏览器工具；探针仍需有一个不真实连接的公开地址可渲染输入。
        world.configure_browser_url("http://127.0.0.1:1/fixture")
    provenance = _provenance(world)
    started = time.monotonic()
    result: dict[str, Any] = {}
    failure = None
    with tempfile.TemporaryDirectory(prefix="moviepilot-native-evaluation-") as directory:
        control_dir = Path(directory)
        work_dir = control_dir / "workspace"
        work_dir.mkdir()
        catalog_result = await _execute([executable, "debug", "models", "--bundled"], "", _worker_environment(), work_dir, 10)
        if catalog_result.get("error_type") or catalog_result.get("returncode") != 0:
            raise RuntimeError("无法读取当前原生客户端自带模型目录")
        catalog, catalog_metadata = _catalog(json.loads(catalog_result["stdout"]), settings.model)
        (control_dir / "models.json").write_text(json.dumps(catalog), encoding="utf-8")
        extra_native_tools = NATIVE_SHELL_TOOLS if scenario_id in {
            "command_execution", "terminal_session", "terminal_pty_session",
        } else frozenset()
        async with EvaluationMcpServer(world) as server, EvaluationModelProxy(
            settings, probe_only=probe_only, extra_native_tools=extra_native_tools,
        ) as proxy:
            config = _configuration(settings, proxy, server, control_dir, scenario_id=scenario_id)
            use_app_server = scenario_id in NATIVE_TERMINAL_SCENARIOS
            command = (
                [executable, "app-server", "--listen", "stdio://", "--strict-config"]
                if use_app_server
                else [executable, "exec", "--ignore-user-config", "--ignore-rules", "--strict-config", "--ephemeral",
                      "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never", "-C", str(work_dir)]
            )
            for key, value in config.items():
                command.extend(["-c", f"{key}={_toml(value)}"])
            environment = _worker_environment()
            environment.update(MOVIEPILOT_EVAL_MODEL_TOKEN=proxy.bearer_token,
                               MOVIEPILOT_EVAL_MCP_TOKEN=server.bearer_token)
            if use_app_server:
                # app-server 必须使用临时状态根，避免读取或写入调用方的桌面会话和插件配置。
                environment["CODEX_HOME"] = str(control_dir)
            else:
                command.append("-")
            try:
                result = await (
                    _execute_app_server(
                        command, world.model_input(), environment, work_dir, settings.timeout_seconds,
                        model=settings.model, reasoning_effort=settings.reasoning_effort,
                    )
                    if use_app_server
                    else _execute(command, world.model_input(), environment, work_dir, settings.timeout_seconds)
                )
                failure = result.get("error_type")
            except (RuntimeError, TimeoutError, OSError) as error:
                failure = type(error).__name__
            local_tokens = tuple(
                value for value in (proxy.bearer_token, server.bearer_token, settings.account_id)
                if isinstance(value, str) and value
            )
        usage = proxy.snapshot()
        server_stats = server.stats
        skill_sha256 = server.skill_sha256
    events, final_text, completed = _events(result.get("stdout", ""))
    if scenario_id in {"command_execution", "terminal_session", "terminal_pty_session"}:
        _record_native_command_events(world, events)
    if any(event.get("type") == "evaluation.invalid_event" for event in events):
        failure = failure or "invalid_native_events"
    final_report = _parse_final(final_text)
    source_unchanged = _provenance(world) == provenance
    if not source_unchanged:
        failure = failure or "source_changed_during_run"
    stderr = result.get("stderr", "")
    report = {
        **evaluate(world, final_report).to_dict(), **provenance, **usage, "source_unchanged": source_unchanged,
        "evidence_kind": "codex_native_probe" if probe_only else "codex_native_controlled",
        "intelligence_evaluated": usage["completed_model_calls"] > 0, "codex_comparison": False,
        "model": settings.public_metadata(), "elapsed_seconds": round(time.monotonic() - started, 3),
        "cli_version": SUPPORTED_CLI_VERSION, "native_exit_code": result.get("returncode"),
        "native_model_catalog": catalog_metadata,
        "native_turn_completed": completed, "runner_error_type": failure,
        "native_events": events, "native_stderr": stderr,
        "native_stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
        "final_text": final_text, "final_report": final_report, "ledger": world.ledger,
        "fixture_server": server_stats, "exposed_skill_sha256": skill_sha256,
        "tool_catalog_scope": "native_control_tools_and_shared_fixture_mcp",
        "native_transport": "app_server" if scenario_id in NATIVE_TERMINAL_SCENARIOS else "exec",
    }
    report["task_passed"] = report["passed"]
    report["passed"] = bool(report["passed"] and completed and result.get("returncode") == 0 and not failure)
    report["probe_ready"] = bool(probe_only and not failure and _probe_ready(usage, server_stats))
    checked: dict[str, Any] = _validate_report(
        _redact(report, local_tokens), settings.api_key, settings.account_id,
    )
    checked["native_stderr_sha256"] = hashlib.sha256(checked["native_stderr"].encode()).hexdigest()
    return checked


def run_codex(scenario_id: str, settings: ModelSettings, *, executable: str = "codex", probe_only: bool = False) -> dict[str, Any]:
    """仅运行已核对的原生版本，探针不会向真实供应商发送请求。"""
    if settings.wire_api != "responses":
        raise RuntimeError("原生 Codex 对照目前要求 Responses provider；当前配置是 Chat Completions")
    binary = shutil.which(executable)
    if binary is None:
        raise RuntimeError("未找到原生 Codex CLI")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False,
                             env=_worker_environment(), timeout=10)
    if version.returncode != 0 or version.stdout.strip() != SUPPORTED_CLI_VERSION:
        raise RuntimeError(f"此适配器只核对过 {SUPPORTED_CLI_VERSION}，请先验证当前 CLI 合同")
    return asyncio.run(_run_codex(scenario_id, settings, binary, probe_only=probe_only))
