"""用完整生产 Agent 驱动隔离业务世界；工具目录受控，不代表真实部署配置。"""

import os
import re
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Iterator, Optional
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pydantic import PrivateAttr

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
        memory_port = _MemoryPort()
        output: list[str] = []
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
        try:
            result = await agent.process(world.scenario.model_input())
            bundle = agent.evaluation_bundle
            state = bundle.agent.get_state({"configurable": {"thread_id": agent.session_id}}).values if bundle else {}
            tool_catalog = bundle.tool_catalog.audit_payload() if bundle and bundle.tool_catalog else None
            child_tool_catalog = bundle.subagent_catalog.audit_payload() if bundle and bundle.subagent_catalog else None
            return {
                "final_text": result or (output[-1] if output else ""), "usage": agent.get_session_status(),
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
