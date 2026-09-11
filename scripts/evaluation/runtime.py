"""用完整生产 Agent 驱动隔离业务世界；工具目录受控，不代表真实部署配置。"""

import json
import os
import re
import sys
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Iterator, Optional
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pydantic import PrivateAttr

from app.agent.tools.impl.browse_webpage import BrowseWebpageTool
from app.agent.tools.impl.execute_command import ExecuteCommandTool
from app.agent.tools.impl.read_file import ReadFileTool
from scripts.evaluation.world import EvaluationWorld

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

_RUN_LOCK = Lock()
_OPERATIONS = (
    "subscription.list", "subscription.find", "subscription.get", "subscription.add", "subscription.delete",
    "download.tasks.active", "download.history.list", "download.clients", "download.paths", "download.add", "site.list",
    "library.exists",
)


def _config_path() -> Path:
    """拒绝未隔离或导入后才换目录的调用，不主动导入可能读取真实配置的模块。"""
    configured = os.environ.get("CONFIG_DIR")
    bootstrap = sys.modules.get("app.testing.bootstrap")
    config = sys.modules.get("app.runtime.config")
    isolated = getattr(bootstrap, "_isolated_config_dir", None)
    if not configured or not isolated or config is None:
        raise RuntimeError("评测必须先设置独立 CONFIG_DIR 并调用 prepare_backend")
    expected = Path(configured).resolve()
    actual = Path(config.settings.CONFIG_PATH).resolve()
    if expected != actual or Path(isolated).resolve() != expected:
        raise RuntimeError("评测 CONFIG_DIR 与已初始化后端目录不一致")
    return expected


class _Response:
    """内存响应保留执行器真实的读取和关闭协议，不产生网络请求。"""

    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: dict[str, Any]) -> None:
        """保存世界返回的成功、失败或未知结果，不提升为成功。"""
        self._payload = payload

    def json(self) -> dict[str, Any]:
        """仅返回此次 API 执行得到的可见响应。"""
        return self._payload

    async def aclose(self) -> None:
        """满足生产响应的显式关闭协议。"""


class _Transport:
    """按生产固定路由反向匹配 operation，无法向任意主机发送请求。"""

    def __init__(self, world: EvaluationWorld, **_kwargs: Any) -> None:
        """绑定当前世界；执行器传来的认证头不记录、不使用。"""
        from app.agent.policy.api import resolve_api_route

        self._world = world
        self._routes = []
        for operation in _OPERATIONS:
            route = resolve_api_route(operation)
            if route is None:
                raise RuntimeError(f"评测 operation 已不在生产路由注册表：{operation}")
            pattern = re.escape(route.path)
            pattern = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", pattern)
            self._routes.append((operation, route.method, re.compile(f"^{pattern}$")))

    async def request(
        self, *, method: str, url: str, params: Optional[dict[str, Any]], json: Any, raise_exception: bool,
    ) -> _Response:
        """解释执行器生成的 HTTP 请求，未纳入场景的真实 API 同样拒绝。"""
        del raise_exception
        parsed = urlsplit(url)
        if parsed.netloc != "evaluation.invalid" or parsed.scheme != "http" or parsed.query or parsed.fragment:
            raise ValueError("评测传输只接受内存 API 地址")
        for operation, route_method, pattern in self._routes:
            match = pattern.fullmatch(parsed.path)
            if route_method != method or match is None:
                continue
            path_params: dict[str, Any] = {name: unquote(value) for name, value in match.groupdict().items()}
            if "subscribe_id" in path_params:
                try:
                    path_params["subscribe_id"] = int(path_params["subscribe_id"])
                except ValueError:
                    pass
            return _Response(self._world.execute(operation, path_params=path_params, query=params, body=json))
        return _Response({"success": False, "execution_outcome": "failed", "message": "该 API 不属于受控评测目录"})


class _MemoryPort:
    """为真实 MemoryManager 提供一次运行独占的会话读取与持久化边界。"""

    def __init__(self) -> None:
        """存储仅属于当前运行的消息快照。"""
        self.messages: list[dict[str, Any]] = []

    async def get(self, **_kwargs: Any) -> None:
        """新评测会话没有历史消息，不读取宿主会话表。"""

    async def async_save_agent_messages(self, *, messages: list[dict[str, Any]], **_kwargs: Any) -> None:
        """接收 MemoryManager 的序列化结果，不访问宿主全局持久化服务。"""
        self.messages = messages


class _EvaluationReadFileTool(ReadFileTool):
    """评测专用文件读取工具，只允许访问本次运行的临时 Agent 根目录。"""

    _evaluation_allowed_root: Path = PrivateAttr()

    def __init__(self, *, allowed_root: Path, **kwargs: Any) -> None:
        """绑定隔离根目录后复用生产文件读取、大小限制和行范围合同。"""
        super().__init__(**kwargs)
        self._evaluation_allowed_root = allowed_root.resolve()

    def _get_non_admin_local_file_roots(self) -> list[Path]:
        """返回评测临时 Agent 根，避免回退到宿主 CONFIG_PATH/agent。"""
        return [self._evaluation_allowed_root]


class _EvaluationExecuteCommandTool(ExecuteCommandTool):
    """在临时工作目录执行命令或交互终端并保留生产工具的真实回执合同。"""

    _evaluation_world: Any = PrivateAttr()
    _evaluation_allowed_root: Path = PrivateAttr()
    _evaluation_terminal_session_id: Optional[str] = PrivateAttr(default=None)

    def __init__(self, *, world: EvaluationWorld, allowed_root: Path, **kwargs: Any) -> None:
        """绑定假世界与临时目录，拒绝环境覆盖和目录逃逸。"""
        super().__init__(**kwargs)
        self._evaluation_world = world
        self._evaluation_allowed_root = allowed_root.resolve()

    @staticmethod
    def _failure(
        message: str,
        command: str = "",
        *,
        action: str = "run",
        session_id: Optional[str] = None,
        input_text: Optional[str] = None,
    ) -> str:
        """返回带纠正提示的受控失败，供模型按评测合同修正输入。"""
        payload: dict[str, Any] = {
            "action": action, "success": False, "execution_outcome": "failed", "status": "error",
            "exit_code": None, "timed_out": False, "command": command,
            "error": "evaluation_command_rejected", "message": message,
        }
        if session_id is not None:
            payload["session_id"] = session_id
        if input_text is not None:
            payload["input_text"] = input_text
        return ExecuteCommandTool._dump(payload)

    @staticmethod
    def _payload(result: str) -> dict[str, Any]:
        """解码生产命令工具的 JSON 回执，异常文本仍作为失败证据保留。"""
        try:
            decoded = json.loads(result)
        except (TypeError, ValueError):
            return {"execution_outcome": "failed", "raw": result}
        return decoded if isinstance(decoded, dict) else {"execution_outcome": "failed", "raw": result}

    async def _run_terminal(
        self,
        action: str,
        *,
        command: Optional[str],
        cwd: Optional[str],
        env: Optional[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> str:
        """按固定顺序代理启动、读取、写入和等待动作，验证真实会话句柄不会被替换。"""
        command_text = (command or "").strip()
        session_id = kwargs.get("session_id")
        input_text = kwargs.get("input_text")
        close_stdin = kwargs.get("close_stdin", False)
        if action not in {"start", "read", "wait", "write"}:
            result = self._failure(
                "终端场景只接受 action=start、read、wait、write；不要使用 action=run",
                command_text, action=action, session_id=session_id,
            )
            self._evaluation_world.record_command(command_text, self._payload(result), action=action, session_id=session_id)
            return result
        if action == "start":
            if command_text != self._evaluation_world.scenario.command:
                result = self._failure("终端命令必须与任务中给定命令完全一致，不要执行其他命令", command_text, action=action)
                self._evaluation_world.record_command(command_text, self._payload(result), action=action)
                return result
            if env:
                result = self._failure("终端启动不接受 env；请删除 env 后重试", command_text, action=action)
                self._evaluation_world.record_command(command_text, self._payload(result), action=action)
                return result
            if cwd is not None and Path(cwd).expanduser().resolve() != self._evaluation_allowed_root:
                result = self._failure("cwd 必须省略或使用当前评测工作目录", command_text, action=action)
                self._evaluation_world.record_command(command_text, self._payload(result), action=action)
                return result
            expected_use_pty = self._evaluation_world.scenario.terminal_use_pty
            if expected_use_pty is None:
                expected_use_pty = False
            if kwargs.get("use_pty", True) is not expected_use_pty:
                mode = "PTY" if expected_use_pty else "pipe"
                result = self._failure(
                    f"终端场景必须使用 use_pty={str(expected_use_pty).lower()} 的 {mode} 模式",
                    command_text, action=action,
                )
                self._evaluation_world.record_command(command_text, self._payload(result), action=action)
                return result
            if session_id:
                result = self._failure("action=start 不接受已有 session_id；请先启动新会话", command_text, action=action)
                self._evaluation_world.record_command(command_text, self._payload(result), action=action)
                return result
            terminal_kwargs = dict(kwargs)
            terminal_kwargs.update({
                "action": "start", "command": command_text, "session_id": None, "input_text": None,
                "close_stdin": False, "cwd": str(self._evaluation_allowed_root), "env": None,
                "use_pty": expected_use_pty,
            })
            result = await super().run(**terminal_kwargs)
            payload = self._payload(result)
            returned_session = payload.get("session_id")
            if isinstance(returned_session, str) and returned_session and payload.get("execution_outcome") in {"pending", "succeeded"}:
                self._evaluation_terminal_session_id = returned_session
            self._evaluation_world.record_command(command_text, payload, action=action)
            return result

        if not isinstance(session_id, str) or session_id != self._evaluation_terminal_session_id:
            result = self._failure(
                "请使用 action=start 返回的同一 session_id，再执行 read、wait 或 write",
                action=action, session_id=session_id,
            )
            self._evaluation_world.record_command("", self._payload(result), action=action, session_id=session_id)
            return result
        if action == "write":
            if input_text != "MOVIEPILOT_TERMINAL_OK\n":
                result = self._failure(
                    "write 必须向当前 session_id 写入 MOVIEPILOT_TERMINAL_OK 并保留结尾换行",
                    action=action, session_id=session_id, input_text=input_text,
                )
                self._evaluation_world.record_command(
                    "", self._payload(result), action=action, session_id=session_id, input_text=input_text,
                )
                return result
            if close_stdin is not False:
                result = self._failure(
                    "pipe 场景的 write 必须使用 close_stdin=false；写入后再读取或等待退出",
                    action=action, session_id=session_id, input_text=input_text,
                )
                self._evaluation_world.record_command(
                    "", self._payload(result), action=action, session_id=session_id, input_text=input_text,
                )
                return result

        terminal_kwargs = dict(kwargs)
        terminal_kwargs.update({
            "action": action, "command": None, "session_id": session_id, "cwd": None, "env": None,
            "use_pty": None,
        })
        if action != "write":
            terminal_kwargs["input_text"] = None
        result = await super().run(**terminal_kwargs)
        payload = self._payload(result)
        self._evaluation_world.record_command(
            "", payload, action=action, session_id=session_id,
            input_text=input_text if action == "write" else None,
        )
        return result

    async def run(self, action: Optional[str] = "run", command: Optional[str] = None, cwd: Optional[str] = None,
                  env: Optional[dict[str, Any]] = None, **kwargs: Any) -> str:
        """仅允许场景给定的短命令，执行路径仍经过生产命令安全、超时和作用域清理。"""
        scenario = self._evaluation_world.scenario
        normalized_action = (action or ("start" if scenario.kind == "terminal" else "run")).strip().lower()
        if scenario.kind == "terminal":
            return await self._run_terminal(
                normalized_action, command=command, cwd=cwd, env=env, kwargs=kwargs,
            )
        command_text = (command or "").strip()
        if normalized_action != "run":
            result = self._failure("命令场景只接受 action=run；请直接提交给定命令", command_text)
            self._evaluation_world.record_command(command_text, json.loads(result))
            return result
        if command_text != self._evaluation_world.scenario.command:
            result = self._failure("命令必须与任务中给定命令完全一致，不要执行其他命令", command_text)
            self._evaluation_world.record_command(command_text, json.loads(result))
            return result
        if env:
            result = self._failure("命令场景不接受 env；请删除 env 后重试", command_text)
            self._evaluation_world.record_command(command_text, json.loads(result))
            return result
        if cwd is not None and Path(cwd).expanduser().resolve() != self._evaluation_allowed_root:
            result = self._failure("cwd 必须省略或使用当前评测工作目录", command_text)
            self._evaluation_world.record_command(command_text, json.loads(result))
            return result
        result = await super().run(
            action="run", command=command_text, cwd=str(self._evaluation_allowed_root), env=None, **kwargs,
        )
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            payload = {"execution_outcome": "failed", "raw": result}
        self._evaluation_world.record_command(command_text, payload)
        return result


class _EvaluationBrowseWebpageTool(BrowseWebpageTool):
    """在本地固定页面上复用生产 Playwright 工具，拒绝外部 URL 和任意脚本。"""

    _evaluation_world: Any = PrivateAttr()

    def __init__(self, *, world: EvaluationWorld, **kwargs: Any) -> None:
        """绑定本轮世界；浏览器会话归属仍由生产 TerminalScope 管理。"""
        super().__init__(**kwargs)
        self._evaluation_world = world

    @staticmethod
    def _failure(action: str, message: str) -> str:
        """返回告诉模型正确动作或输入的结构化失败。"""
        return json.dumps({
            "action": action, "success": False, "execution_outcome": "failed",
            "error": "evaluation_browser_rejected", "message": message,
        }, ensure_ascii=False)

    async def run(self, action: str, url: Optional[str] = None, selector: Optional[str] = None,
                  ref: Optional[str] = None, value: Optional[str] = None, script: Optional[str] = None,
                  content_type: Optional[str] = "text", timeout: Optional[int] = 30,
                  cookies: Optional[str] = None, user_agent: Optional[str] = None,
                  session_key: Optional[str] = None, tab_index: Optional[int] = None,
                  allow_private_network: bool = False, **kwargs: Any) -> Any:
        """仅允许页面读取与按快照 ref 点击，执行仍走生产浏览器实现。"""
        allowed_actions = {"goto", "snapshot", "click_ref", "get_content", "close_session"}
        normalized_action = str(action or "").strip().lower()
        browser_url = self._evaluation_world.browser_url
        if normalized_action not in allowed_actions:
            result = self._failure(normalized_action, "评测浏览器只接受 goto、snapshot、click_ref、get_content、close_session")
            self._evaluation_world.record_browser(normalized_action, json.loads(result))
            return result
        if normalized_action == "goto" and (url != browser_url or not allow_private_network):
            result = self._failure(normalized_action, "goto 必须使用任务给定的本地页面，并设置 allow_private_network=true")
            self._evaluation_world.record_browser(normalized_action, json.loads(result))
            return result
        if normalized_action == "click_ref" and not ref:
            result = self._failure(normalized_action, "click_ref 必须使用 snapshot 返回的 ref")
            self._evaluation_world.record_browser(normalized_action, json.loads(result))
            return result
        if normalized_action == "get_content" and content_type not in (None, "text"):
            result = self._failure(normalized_action, "get_content 只接受 content_type=text")
            self._evaluation_world.record_browser(normalized_action, json.loads(result))
            return result
        if session_key is not None and session_key != self._session_id:
            result = self._failure(normalized_action, "session_key 必须省略或使用当前 Agent 会话")
            self._evaluation_world.record_browser(normalized_action, json.loads(result))
            return result
        result = await super().run(
            action=normalized_action, url=url, selector=selector, ref=ref, value=value, script=script,
            content_type=content_type, timeout=timeout, cookies=cookies, user_agent=user_agent,
            session_key=session_key, tab_index=tab_index, allow_private_network=allow_private_network, **kwargs,
        )
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except (TypeError, ValueError):
            payload = {"execution_outcome": "failed", "raw": result}
        if isinstance(payload, dict) and "execution_outcome" not in payload:
            payload["execution_outcome"] = "failed" if payload.get("success") is False else "succeeded"
        self._evaluation_world.record_browser(normalized_action, payload)
        return result


@contextmanager
def _browser_fixture() -> Iterator[str]:
    """启动只绑定回环地址的动态测试页，并在评测结束后等待线程退出。"""
    class _Handler(BaseHTTPRequestHandler):
        """返回固定页面，不记录请求正文或客户端环境。"""

        def do_GET(self) -> None:  # noqa: N802 - 标准库处理器方法名
            """仅提供固定路径和最小 HTML，其他请求返回 404。"""
            if self.path != "/fixture":
                self.send_error(404)
                return
            body = (
                "<!doctype html><html><head><meta charset='utf-8'><title>MoviePilot Browser Fixture</title></head>"
                "<body><main><h1>MoviePilot Browser Fixture</h1>"
                "<button id='reveal' type='button'>显示结果</button><p id='result'>PENDING</p>"
                "<script>document.getElementById('reveal').addEventListener('click', () => "
                "document.getElementById('result').textContent = 'BROWSER_OK');</script>"
                "</main></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            """禁止标准库把本地测试请求写入 stderr。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, name="moviepilot-evaluation-browser", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _McpDirectory:
    """受控目录不配置任何外部 MCP，禁止发现阶段启动服务或联网。"""

    @staticmethod
    def config_signature() -> str:
        """提供明确区别于真实部署的空目录签名。"""
        return "evaluation-empty-mcp"

    @staticmethod
    async def list_enabled_tool_specs() -> list[Any]:
        """无须读取 SystemConfig 即可返回空外部工具目录。"""
        return []


def _agent_type() -> type:
    """延迟加载宿主；只替换模型、目录和展示边界，继承完整生产执行图。"""
    from app.agent.middleware.selection import ToolSelectorMiddleware
    from app.agent.orchestrator import MoviePilotAgent
    from app.agent.tools.catalog import ToolCatalogSnapshot

    # 脚本单独严格检查时 follow_imports=skip 不展开宿主类型；运行合同由生产图测试验证。
    class EvaluationAgent(MoviePilotAgent):  # type: ignore[misc]
        """在生产策略、中间件和生命周期内运行受控工具及外部注入模型。"""

        def __init__(self, *, model: Any, model_name: str, context_window: int, max_iterations: int, **kwargs: Any) -> None:
            """隔离模型身份和预算，不解析宿主 LLM 设置或供应商选择事件。"""
            super().__init__(**kwargs)
            self.evaluation_model = model
            self.evaluation_model_name = model_name
            self.evaluation_context_window = context_window
            self.evaluation_max_iterations = max_iterations
            self.evaluation_tools: list[Any] = []
            self.evaluation_child_tools: list[Any] = []
            self.display_messages: list[dict[str, Any]] = []
            self.execution_success = False
            self.evaluation_bundle: Any = None
            self.evaluation_request_budgets: list[dict[str, Any]] = []

        async def _create_agent(self, streaming: bool = False) -> Any:
            """保留原始执行图的观察引用，生产失败恢复清缓存后仍可导出失败轨迹。"""
            graph = await super()._create_agent(streaming=streaming)
            self.evaluation_bundle = self._compiled_agent_bundle
            return graph

        async def _initialize_llm(self, streaming: bool = False) -> Any:
            """直接使用调用方模型；内部总结和子代理仍由生产代码调用同一模型。"""
            del streaming
            return self.evaluation_model

        async def _resolve_llm_runtime_config(self) -> dict[str, Any]:
            """返回不含凭据的稳定模型描述，不加载供应商配置。"""
            return {"provider": "openai", "model": self.evaluation_model_name, "web_search_mode": "disabled"}

        def _sync_model_profile(self, model: Any) -> None:
            """记录显式传入的模型预算，避免脚本模型缺失 profile 时丢失上下文上限。"""
            super()._sync_model_profile(model)
            self._session_usage.model = self.evaluation_model_name
            self._session_usage.context_window_tokens = self.evaluation_context_window

        def _get_recursion_limit(self) -> int:
            """将显式评测迭代预算交给真实 LangGraph 执行器。"""
            return self.evaluation_max_iterations

        def _record_request_budget(self, budget: dict[str, Any]) -> None:
            """保留最终请求预算快照，供真实长上下文评测核对压缩触发边界。"""
            super()._record_request_budget(budget)
            self.evaluation_request_budgets.append({
                key: budget.get(key) for key in (
                    "request_sequence", "estimated_input_tokens", "context_window_tokens",
                    "estimated_input_ratio", "estimated_over_input_limit", "message_count",
                    "tool_count",
                )
            })

        def _should_stream(self) -> bool:
            """使用生产非流式分支，模型输出只进入评测捕获。"""
            return False

        def _initialize_local_tool_catalogs(self) -> tuple[Any, Any]:
            """主图和子图使用不同工具实例，保持完整生产目录校验和子图只读策略。"""
            return tuple(ToolCatalogSnapshot.from_tools(
                tools, plugin_revision=0, factory_revision="evaluation-controlled-api",
            ).require_unique() for tools in (self.evaluation_tools, self.evaluation_child_tools))

        @staticmethod
        def _initialize_tool_selector(tools: list[Any], internal_tools: list[Any], model: Any) -> Any:
            """保留生产发现中间件，同时避免加载实际插件工具工厂。"""
            return ToolSelectorMiddleware(
                model=model, selection_tools=[*tools, *internal_tools], max_tools=20,
                always_include=[tool.name for tool in [*tools, *internal_tools]], enable_discovery=True,
            )

        async def prepare_chat_title(self, message: str) -> None:
            """评测不额外生成会话标题，业务推理与总结调用仍按生产路径计费。"""

        async def _save_display_history_messages(self, messages: list[dict[str, Any]]) -> None:
            """捕获真实 process 的展示历史，不注册通知或宿主会话服务。"""
            self.display_messages.extend(messages)

        async def send_agent_message(self, message: str, title: str = "") -> None:
            """意外进入发送边界时直接拒绝，不能把评测内容发送到真实渠道。"""
            raise RuntimeError("评测禁止发送通知")

        def _send_agent_tokens_usage_event(self, *, success: bool, error: Optional[str] = None) -> None:
            """只记录成功标志，不广播可被插件消费的真实运行事件。"""
            self.execution_success = success

    return EvaluationAgent


@contextmanager
def _runtime_scope(directory: Path, world: EvaluationWorld) -> Iterator[Any]:
    """在独立 worker 中临时替换明确的外部边界，退出时恢复全部全局引用。"""
    from app.agent.api.executor import MoviePilotApiExecutor
    from app.agent.runtime import AgentRuntimeManager

    manager = AgentRuntimeManager(agent_root_dir=directory / "agent")
    manager.ensure_layout()
    (manager.memory_dir / "MEMORY.md").write_text("用户偏好简洁、准确地完成当前任务。\n", encoding="utf-8")
    with ExitStack() as stack:
        for module in ("orchestrator", "middleware.config", "middleware.subagents"):
            stack.enter_context(patch(f"app.agent.{module}.agent_runtime_manager", manager))
        stack.enter_context(patch("app.agent.orchestrator._get_plugin_tools_revision", return_value=0))
        stack.enter_context(patch("app.agent.orchestrator.agent_mcp_manager", _McpDirectory()))
        stack.enter_context(patch.object(MoviePilotApiExecutor, "_resolve_base_url", return_value="http://evaluation.invalid"))
        stack.enter_context(patch.object(MoviePilotApiExecutor, "_build_headers", return_value={"Accept": "application/json"}))
        yield lambda **kwargs: _Transport(world, **kwargs)


async def run_moviepilot(
    world: EvaluationWorld, model: "BaseChatModel", *, model_name: str, context_window: int, max_iterations: int,
    invocation_repository: Optional[Any] = None,
) -> dict[str, Any]:
    """调用真实 process 并捕获最终图；每个 live trial 应使用独立进程和 CONFIG_DIR。"""
    config_path = _config_path()
    if context_window < 1 or max_iterations < 1:
        raise ValueError("context_window 和 max_iterations 必须为正整数")
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("评测全局隔离边界不能在同一进程并行使用")
    try:
        with TemporaryDirectory(prefix="agent-evaluation-", dir=config_path) as directory:
            return await _run_isolated(
                world, model, directory=Path(directory), model_name=model_name, context_window=context_window,
                max_iterations=max_iterations, invocation_repository=invocation_repository,
            )
    finally:
        _RUN_LOCK.release()


async def _run_isolated(
    world: EvaluationWorld, model: Any, *, directory: Path, model_name: str, context_window: int,
    max_iterations: int, invocation_repository: Optional[Any],
) -> dict[str, Any]:
    """装配真实 API 执行器、独立回执库和会话端口，并在回收前保存执行证据。"""
    from langchain_core.messages import messages_to_dict
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.agent.api.executor import ApiExecutionContext, MoviePilotApiExecutor
    from app.agent.contracts import ReplyMode
    from app.agent.memory import MemoryManager
    from app.agent.tools.impl.api import MoviePilotApiTool
    from app.db.adapters.invocation import TransactionalInvocationRepository
    from app.db.models.agentinvocation import AgentInvocation
    from app.schemas.types import NotificationChannel

    with ExitStack() as stack:
        stack.enter_context(patch.object(model, "profile", {
            **(getattr(model, "profile", None) or {}), "max_input_tokens": context_window,
        }))
        if invocation_repository is None:
            engine = create_engine(f"sqlite:///{directory / 'invocations.db'}")
            stack.callback(engine.dispose)
            AgentInvocation.__table__.create(engine)
            invocation_repository = TransactionalInvocationRepository(sessionmaker(bind=engine))
        factory = stack.enter_context(_runtime_scope(directory, world))
        if world.scenario.kind == "browser":
            world.configure_browser_url(stack.enter_context(_browser_fixture()))
        memory_port = _MemoryPort()
        output: list[str] = []
        command_root = directory / "command-workspace"
        command_root.mkdir()
        agent = _agent_type()(
            session_id=uuid4().hex, user_id="1", username="evaluation", channel=NotificationChannel.WebAgent.value,
            source="evaluation", is_channel_admin=True, replay_mode=ReplyMode.CAPTURE_ONLY, allow_message_tools=False,
            output_callback=output.append, data=SimpleNamespace(invocations=invocation_repository),
            memory=MemoryManager(chat=memory_port, persistence=memory_port), model=model, model_name=model_name,
            context_window=context_window, max_iterations=max_iterations,
        )
        for child in (False, True):
            executor = MoviePilotApiExecutor(
                context=ApiExecutionContext(user_id="1", username="evaluation", is_admin=True, session_id=agent.session_id),
                request_factory=factory,
            )
            tool = MoviePilotApiTool(session_id=agent.session_id, user_id="1", executor=executor)
            tool.set_message_attr(agent.channel, agent.source, agent.username)
            tool.set_agent_context({"is_admin": True, "should_dispatch_reply": False, "require_secret_confirmation": True}
                                   if child else agent._tool_context)
            (agent.evaluation_child_tools if child else agent.evaluation_tools).append(tool)
            # Skill 主体只返回相对 supporting_files；评测必须提供与生产一致的
            # read_file 能力，才能验证模型是否按需加载 api/*.md 合同。工具使用
            # 非管理员上下文，只能读取当前临时 CONFIG_DIR/agent 隔离目录。
            skill_file_tool = _EvaluationReadFileTool(
                allowed_root=directory / "agent", session_id=agent.session_id, user_id="1",
            )
            skill_file_tool.set_message_attr(agent.channel, agent.source, agent.username)
            skill_file_tool.set_agent_context({
                "is_admin": False,
                "should_dispatch_reply": False,
                "require_secret_confirmation": True,
            })
            (agent.evaluation_child_tools if child else agent.evaluation_tools).append(skill_file_tool)
        if world.scenario.kind in {"command", "terminal"}:
            command_tool = _EvaluationExecuteCommandTool(
                world=world, allowed_root=command_root, session_id=agent.session_id, user_id="1",
            )
            command_tool.set_message_attr(agent.channel, agent.source, agent.username)
            command_tool.set_agent_context(agent._tool_context)
            agent.evaluation_tools.append(command_tool)
        elif world.scenario.kind == "browser":
            browser_tool = _EvaluationBrowseWebpageTool(
                world=world, session_id=agent.session_id, user_id="1",
            )
            browser_tool.set_message_attr(agent.channel, agent.source, agent.username)
            browser_tool.set_agent_context(agent._tool_context)
            agent.evaluation_tools.append(browser_tool)
        try:
            result = await agent.process(world.model_input())
            bundle = agent.evaluation_bundle
            state = bundle.agent.get_state({"configurable": {"thread_id": agent.session_id}}).values if bundle else {}
            tool_catalog = bundle.tool_catalog.audit_payload() if bundle and bundle.tool_catalog else None
            child_tool_catalog = bundle.subagent_catalog.audit_payload() if bundle and bundle.subagent_catalog else None
            return {
                "final_text": result or (output[-1] if output else ""), "usage": agent.get_session_status(),
                "request_budgets": agent.evaluation_request_budgets,
                "execution_success": agent.execution_success, "raw_messages": messages_to_dict(state.get("messages", [])),
                "display_messages": agent.display_messages, "task_plan": state.get("task_plan"),
                "tool_catalog_scope": "controlled_moviepilot_api_and_production_internal_tools",
                "tool_names": sorted(tool.name for tool in bundle.tool_catalog.tools) if bundle else [],
                "child_tool_names": sorted(tool.name for tool in agent.evaluation_child_tools),
                "tool_catalog": tool_catalog,
                "child_tool_catalog": child_tool_catalog,
                "graph_nodes": sorted(bundle.agent.get_graph().nodes) if bundle else [],
            }
        finally:
            if not await agent.cleanup():
                raise RuntimeError("评测 Agent 子任务尚未完成清理")
