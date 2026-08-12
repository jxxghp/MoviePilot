import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

import app.agent as agent_module
from app.agent.middleware.activity_log import ActivityLogMiddleware
from app.agent.middleware.memory import MemoryMiddleware
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.policy import (
    DEFAULT_TOOL_POLICY_ORCHESTRATOR,
    DEFAULT_TOOL_POLICY_REGISTRY,
    ActionEffect,
    AuthSource,
    MigrationState,
    PolicyPrincipal,
    PrincipalRole,
    PrincipalType,
    ResultSensitivity,
    ToolOrigin,
    ToolPolicyContext,
    call_policy_hook,
)
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.ask_user_choice import AskUserChoiceTool
from app.agent.tools.impl.query_system_settings import QuerySystemSettingsTool
from app.agent.tools.impl.send_local_file import SendLocalFileTool
from app.agent.tools.impl.send_voice_message import SendVoiceMessageTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.schemas.types import MessageChannel


class _EchoInput(BaseModel):
    """策略测试工具的输入契约。"""

    query: str = Field(description="待回显文本")


class _EchoTool(MoviePilotTool):
    """返回输入文本的测试工具。"""

    name: str = "policy_echo"
    description: str = "Echo policy test input."
    args_schema: type[BaseModel] = _EchoInput

    async def run(self, query: str) -> str:
        """返回输入文本。"""
        return query


class _OverrideArunTool(_EchoTool):
    """模拟插件覆盖宿主工具基类 `_arun` 的实现。"""

    name: str = "plugin_override_arun"

    def __init__(self, events: list[str], **kwargs):
        super().__init__(**kwargs)
        self._events = events

    async def _arun(self, *args, **kwargs) -> str:
        """绕过基类实现并记录真实执行顺序。"""
        self._events.append("tool")
        return str(kwargs.get("query") or "ok")


class _FailingTool(_EchoTool):
    """抛出固定异常以验证 direct manager 的 observation fail-open。"""

    name: str = "policy_failure"

    async def run(self, query: str) -> str:
        """模拟真实工具失败。"""
        raise ValueError(f"tool-error:{query}")


class _AdminSafeReadTool(_EchoTool):
    """复用 safe-read 名称并保留旧 require_admin 门禁的测试工具。"""

    name: str = "list_slash_commands"
    require_admin: bool = True

    def __init__(self, events: list[str], **kwargs):
        super().__init__(**kwargs)
        self._events = events

    async def run(self, query: str) -> str:
        """记录实际执行，区分旧门禁放行与拒绝。"""
        self._events.append("run")
        return query


def _tool_class_name(tool_class: type[MoviePilotTool]) -> str:
    """从 Pydantic 字段默认值读取工具类的稳定名称。"""
    return str(tool_class.model_fields["name"].default)


def _interactive_context(*, is_admin: bool = True) -> ToolPolicyContext:
    """构造会随本轮管理员上下文刷新的交互式策略上下文。"""
    return ToolPolicyContext(
        session_id="session-1",
        user_id="user-1",
        origin=ToolOrigin.AGENT_INTERACTIVE,
        principal_type=PrincipalType.HUMAN,
        auth_source=AuthSource.CHANNEL,
        channel="telegram",
        source="user",
        agent_context={"is_admin": is_admin},
    )


def test_builtin_policy_registry_covers_every_fixed_tool() -> None:
    """固定内置工具必须全部具有显式 migration registry 条目。"""
    fixed_tool_names = {
        _tool_class_name(tool_class)
        for tool_class in MoviePilotToolFactory.BUILTIN_TOOL_CLASSES
    }
    fixed_tool_names.update(
        {
            _tool_class_name(AskUserChoiceTool),
            _tool_class_name(SendLocalFileTool),
            _tool_class_name(SendVoiceMessageTool),
        }
    )

    assert DEFAULT_TOOL_POLICY_REGISTRY.builtin_tool_names == fixed_tool_names


def test_registry_separates_safe_read_from_legacy_shadow() -> None:
    """少量安全读取可直接迁移，其余高影响工具保持 shadow allow。"""
    safe_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="query_personas",
        arguments={},
        requires_admin=False,
    )
    admin_safe_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="list_slash_commands",
        arguments={},
        requires_admin=True,
    )
    shadow_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="update_system_settings",
        arguments={"updates": []},
        requires_admin=True,
    )
    dynamic_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="plugin_unknown_tool",
        arguments={"action": "custom"},
        requires_admin=False,
    )

    assert safe_policy.effect is ActionEffect.SAFE_READ
    assert safe_policy.result_sensitivity is ResultSensitivity.NORMAL
    assert safe_policy.migration_state is MigrationState.ENFORCED

    assert admin_safe_policy.effect is ActionEffect.SAFE_READ
    assert admin_safe_policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert admin_safe_policy.migration_state is MigrationState.LEGACY_SHADOW

    assert shadow_policy.migration_state is MigrationState.LEGACY_SHADOW
    assert shadow_policy.effect is ActionEffect.UNKNOWN
    assert shadow_policy.required_role is PrincipalRole.SYSTEM_ADMIN

    assert dynamic_policy.migration_state is MigrationState.LEGACY_SHADOW
    assert dynamic_policy.effect is ActionEffect.UNKNOWN
    assert dynamic_policy.result_sensitivity is ResultSensitivity.UNKNOWN


@pytest.mark.parametrize("show_secrets", [None, False])
def test_system_settings_without_secret_values_stays_legacy_shadow(
    show_secrets,
) -> None:
    """普通设置读取维持既有兼容路径，不增加确认。"""
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="query_system_settings",
        arguments={"show_secrets": show_secrets},
        requires_admin=True,
    )

    assert policy.migration_state is MigrationState.LEGACY_SHADOW
    assert policy.effect is ActionEffect.UNKNOWN


def test_system_settings_secret_read_has_enforced_sensitive_policy() -> None:
    """显式读取密钥只能进入宿主强制确认策略。"""
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="query_system_settings",
        arguments={"show_secrets": True},
        requires_admin=True,
    )

    assert policy.migration_state is MigrationState.ENFORCED
    assert policy.effect is ActionEffect.SENSITIVE_READ
    assert policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert policy.confirmation.value == "required"
    assert policy.result_sensitivity is ResultSensitivity.SECRET
    assert policy.background_allowed is False
    assert policy.subagent_allowed is False


def test_legacy_shadow_decision_allows_without_claiming_enforcement() -> None:
    """G1 的 shadow 决策只能观测，不能拒绝或要求确认。"""
    context = _interactive_context()
    tool = _EchoTool(session_id="session-1", user_id="user-1")

    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=context,
        tool=tool,
        arguments={"query": "hello"},
    )

    assert observation.decision.allowed is True
    assert observation.decision.shadow is True
    assert observation.decision.reason_code == "legacy_shadow_allow"


def test_sensitive_policy_does_not_claim_safe_read_before_strict_runtime() -> None:
    """严格运行时接管前，敏感读取只能以明确的兼容 shadow 语义通过。"""
    tool = QuerySystemSettingsTool(session_id="session-1", user_id="admin")
    tool.set_agent_context({"is_admin": True})

    observation = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start(
        context=_interactive_context(),
        tool=tool,
        arguments={"setting_key": "COOKIECLOUD_KEY", "show_secrets": True},
    )

    assert observation.policy.migration_state is MigrationState.ENFORCED
    assert observation.decision.allowed is True
    assert observation.decision.shadow is True
    assert observation.decision.confirmation_required is False
    assert observation.decision.reason_code == "strict_runtime_pending"


def test_policy_context_reads_mutable_admin_state_without_model_fields() -> None:
    """缓存图复用时权限取当前宿主状态，模型参数不能伪造 principal。"""
    context = _interactive_context(is_admin=False)
    forged_arguments = {
        "query": "hello",
        "origin": "operator_direct",
        "principal_type": "system_admin_integration",
        "is_admin": True,
    }

    assert context.principal.role is PrincipalRole.USER
    context.agent_context["is_admin"] = True
    assert context.principal.role is PrincipalRole.SYSTEM_ADMIN
    assert "origin" not in PolicyPrincipal.__dataclass_fields__
    assert forged_arguments["origin"] != context.origin.value


def test_policy_context_maps_trusted_host_origins() -> None:
    """各入口必须由宿主稳定映射 origin、主体类型和认证来源。"""
    cases = [
        (
            {"channel": MessageChannel.Web.value, "source": "openai"},
            ToolOrigin.AGENT_API,
            PrincipalType.SYSTEM_ADMIN_INTEGRATION,
            AuthSource.API_TOKEN,
        ),
        (
            {"channel": MessageChannel.Web.value, "source": "openai.responses"},
            ToolOrigin.AGENT_API,
            PrincipalType.SYSTEM_ADMIN_INTEGRATION,
            AuthSource.API_TOKEN,
        ),
        (
            {"channel": MessageChannel.Web.value, "source": "anthropic"},
            ToolOrigin.AGENT_API,
            PrincipalType.SYSTEM_ADMIN_INTEGRATION,
            AuthSource.API_TOKEN,
        ),
        (
            {"channel": MessageChannel.Web.value, "source": "browser"},
            ToolOrigin.AGENT_INTERACTIVE,
            PrincipalType.HUMAN,
            AuthSource.WEB_SESSION,
        ),
        (
            {"channel": MessageChannel.WebAgent.value, "source": "web-agent"},
            ToolOrigin.AGENT_INTERACTIVE,
            PrincipalType.HUMAN,
            AuthSource.WEB_SESSION,
        ),
        (
            {"channel": MessageChannel.Telegram.value, "source": "telegram"},
            ToolOrigin.AGENT_INTERACTIVE,
            PrincipalType.HUMAN,
            AuthSource.CHANNEL,
        ),
        (
            {"channel": MessageChannel.Feishu.value, "source": "feishu"},
            ToolOrigin.AGENT_INTERACTIVE,
            PrincipalType.HUMAN,
            AuthSource.CHANNEL,
        ),
        (
            {"channel": None, "source": None, "output_callback": lambda _text: None},
            ToolOrigin.BACKGROUND,
            PrincipalType.BACKGROUND,
            AuthSource.INTERNAL,
        ),
    ]

    for kwargs, expected_origin, expected_principal, expected_auth_source in cases:
        context = agent_module.MoviePilotAgent(
            session_id="origin-session",
            user_id="user-1",
            **kwargs,
        )._build_policy_context()

        assert context.origin is expected_origin
        assert context.principal_type is expected_principal
        assert context.auth_source is expected_auth_source

    subagent_context = agent_module.MoviePilotAgent(
        session_id="subagent-session",
        user_id="user-1",
        channel=MessageChannel.Telegram.value,
        source="telegram",
    )._build_policy_context().for_subagent()
    assert subagent_context.origin is ToolOrigin.SUBAGENT
    assert subagent_context.principal_type is PrincipalType.SUBAGENT
    assert subagent_context.auth_source is AuthSource.INTERNAL


def test_host_middleware_observes_plugin_before_overridden_arun() -> None:
    """插件覆盖 `_arun` 时，宿主 middleware 仍必须先执行策略。"""
    events: list[str] = []
    tool = _OverrideArunTool(
        events,
        session_id="session-1",
        user_id="user-1",
    )
    middleware = AgentPolicyMiddleware(context=_interactive_context())
    request = SimpleNamespace(
        tool=tool,
        tool_call={
            "id": "call-1",
            "name": tool.name,
            "args": {"query": "ok"},
        },
    )
    original_start = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start

    def _record_start(**kwargs):
        events.append("policy")
        return original_start(**kwargs)

    async def _handler(_request):
        result = await tool._arun(query="ok")
        return ToolMessage(content=result, tool_call_id="call-1")

    with patch.object(
        DEFAULT_TOOL_POLICY_ORCHESTRATOR,
        "start",
        side_effect=_record_start,
    ):
        result = asyncio.run(middleware.awrap_tool_call(request, _handler))

    assert result.content == "ok"
    assert events == ["policy", "tool"]


@pytest.mark.parametrize("failed_phase", ["start", "finish"])
def test_middleware_observation_failure_does_not_replace_success(
    failed_phase: str,
) -> None:
    """shadow start/finish 故障不能阻止 handler 或替换成功结果。"""
    orchestrator = MagicMock()
    orchestrator.start.return_value = object()
    getattr(orchestrator, failed_phase).side_effect = RuntimeError(
        f"policy-{failed_phase}-failure"
    )
    middleware = AgentPolicyMiddleware(
        context=_interactive_context(),
        orchestrator=orchestrator,
    )
    request = SimpleNamespace(
        tool=_EchoTool(session_id="session-1", user_id="user-1"),
        tool_call={"id": "call-1", "args": {"query": "same"}},
    )
    expected = ToolMessage(content="same", tool_call_id="call-1")
    handler_called = False

    async def _handler(_request):
        nonlocal handler_called
        handler_called = True
        return expected

    result = asyncio.run(middleware.awrap_tool_call(request, _handler))

    assert handler_called is True
    assert result is expected


def test_middleware_fail_observation_does_not_mask_tool_error() -> None:
    """shadow fail hook 故障后仍必须抛出原始工具异常。"""
    orchestrator = MagicMock()
    orchestrator.start.return_value = object()
    orchestrator.fail.side_effect = RuntimeError("policy-fail-hook-failure")
    middleware = AgentPolicyMiddleware(
        context=_interactive_context(),
        orchestrator=orchestrator,
    )
    request = SimpleNamespace(
        tool=_EchoTool(session_id="session-1", user_id="user-1"),
        tool_call={"id": "call-1", "args": {"query": "same"}},
    )
    tool_error = ValueError("original-tool-failure")

    async def _handler(_request):
        raise tool_error

    with pytest.raises(ValueError) as error_info:
        asyncio.run(middleware.awrap_tool_call(request, _handler))

    assert error_info.value is tool_error


def test_policy_hook_failure_logs_only_stable_type_information() -> None:
    """fail-open 诊断只记录阶段和异常类型，不读取可能含凭据的异常文本。"""
    mock_logger = MagicMock()

    def _fail() -> None:
        raise RuntimeError("DATABASE_PASSWORD=policy-secret-marker")

    with patch("app.agent.policy.orchestrator.logger", mock_logger):
        result = call_policy_hook("start", _fail)

    logged = "\n".join(str(call) for call in mock_logger.warning.call_args_list)
    assert result is None
    assert "phase=start" in logged
    assert "RuntimeError" in logged
    assert "policy-secret-marker" not in logged


def test_policy_hook_hostile_error_type_is_fail_open() -> None:
    """异常类型名协议不可信时，观测故障仍不得逃出 fail-open 边界。"""
    secret_marker = "hostile-policy-type-secret-6274"

    class _HostileMeta(type):
        def __getattribute__(cls, name):
            if name == "__name__":
                raise RuntimeError(f"DATABASE_PASSWORD={secret_marker}")
            return super().__getattribute__(name)

    class _HostilePolicyError(RuntimeError, metaclass=_HostileMeta):
        pass

    mock_logger = MagicMock()

    def _fail() -> None:
        raise _HostilePolicyError("visible policy failure")

    escaped = False
    with patch("app.agent.policy.orchestrator.logger", mock_logger):
        try:
            result = call_policy_hook("start", _fail)
        except BaseException:
            escaped = True
            result = None

    logged = "\n".join(str(call) for call in mock_logger.warning.call_args_list)
    assert escaped is False
    assert result is None
    assert "phase=start" in logged
    assert secret_marker not in logged


@pytest.mark.parametrize(
    ("legacy_admin", "expected_result", "expected_run_count"),
    [
        (True, "same", 1),
        (
            False,
            (
                "抱歉，您没有执行此工具的权限。"
                "只有渠道管理员或系统管理员才能执行工具操作。"
                "如需执行工具，请联系管理员将您的用户ID添加到渠道管理员列表中（设定 -> 通知 -> 对应渠道配置 -> 管理员名单），"
                "或联系系统管理员为您设置管理员权限。"
            ),
            0,
        ),
    ],
)
def test_agent_admin_safe_read_keeps_legacy_authorization_authority(
    legacy_admin: bool,
    expected_result: str,
    expected_run_count: int,
) -> None:
    """渠道管理员与普通用户结果保持旧门禁语义，policy 只做 shadow passthrough。"""
    events: list[str] = []
    observations = []
    original_start = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start
    tool = _AdminSafeReadTool(
        events,
        session_id="session-1",
        user_id="user-1",
    )
    tool.set_message_attr(
        channel=MessageChannel.Telegram.value,
        source="user",
        username="member",
    )
    tool.set_agent_context({"is_admin": False})
    middleware = AgentPolicyMiddleware(context=_interactive_context(is_admin=False))
    request = SimpleNamespace(
        tool=tool,
        tool_call={"id": "call-1", "args": {"query": "same"}},
    )

    def _capture_start(**kwargs):
        observation = original_start(**kwargs)
        observations.append(observation)
        return observation

    async def _handler(_request):
        content = await tool._arun(query="same")
        return ToolMessage(content=content, tool_call_id="call-1")

    with (
        patch.object(
            _AdminSafeReadTool,
            "is_admin_user",
            new=AsyncMock(return_value=legacy_admin),
        ),
        patch.object(
            DEFAULT_TOOL_POLICY_ORCHESTRATOR,
            "start",
            side_effect=_capture_start,
        ),
    ):
        result = asyncio.run(middleware.awrap_tool_call(request, _handler))

    assert result.content == expected_result
    assert events.count("run") == expected_run_count
    assert len(observations) == 1
    assert observations[0].policy.effect is ActionEffect.SAFE_READ
    assert observations[0].policy.migration_state is MigrationState.LEGACY_SHADOW
    assert observations[0].decision.allowed is True
    assert observations[0].decision.shadow is True
    assert observations[0].decision.reason_code == "legacy_shadow_allow"


def test_direct_non_admin_safe_read_rejects_before_policy_or_schema() -> None:
    """direct 未授权请求保持原 JSON 拒绝格式且不提前触发 policy/schema。"""
    events: list[str] = []
    orchestrator = MagicMock()
    tool = _AdminSafeReadTool(
        events,
        session_id="session-1",
        user_id="user-1",
    )
    manager = MoviePilotToolsManager(
        is_admin=False,
        policy_orchestrator=orchestrator,
    )
    manager.tools = [tool]

    result = json.loads(
        asyncio.run(manager.call_tool(tool.name, {"query": "same"}))
    )

    assert result == {
        "error": "抱歉，您没有执行此工具的权限。只有系统管理员才能执行工具操作。"
    }
    assert events == []
    orchestrator.start.assert_not_called()


@pytest.mark.parametrize("failed_phase", ["start", "finish"])
def test_direct_manager_observation_failure_does_not_replace_success(
    failed_phase: str,
) -> None:
    """direct manager 的 shadow start/finish 故障不能改写真实返回值。"""
    orchestrator = MagicMock()
    orchestrator.start.return_value = object()
    getattr(orchestrator, failed_phase).side_effect = RuntimeError(
        f"policy-{failed_phase}-failure"
    )
    tool = _EchoTool(session_id="session-1", user_id="user-1")
    manager = MoviePilotToolsManager(
        is_admin=True,
        policy_orchestrator=orchestrator,
    )
    manager.tools = [tool]

    result = asyncio.run(manager.call_tool(tool.name, {"query": "same"}))

    assert result == "same"


def test_direct_manager_fail_observation_does_not_mask_tool_error() -> None:
    """direct manager 的 fail hook 故障不能替换既有工具错误格式。"""
    orchestrator = MagicMock()
    orchestrator.start.return_value = object()
    orchestrator.fail.side_effect = RuntimeError("policy-fail-hook-failure")
    tool = _FailingTool(session_id="session-1", user_id="user-1")
    manager = MoviePilotToolsManager(
        is_admin=True,
        policy_orchestrator=orchestrator,
    )
    manager.tools = [tool]

    result = asyncio.run(manager.call_tool(tool.name, {"query": "same"}))

    assert "ValueError" in result
    assert "tool-error:same" in result
    assert "policy-fail-hook-failure" not in result


def test_direct_manager_and_agent_middleware_share_policy_resolution() -> None:
    """相同工具参数在 Agent 与 direct 入口应获得相同动作策略。"""
    observations = []
    original_start = DEFAULT_TOOL_POLICY_ORCHESTRATOR.start

    def _capture_start(**kwargs):
        observation = original_start(**kwargs)
        observations.append(observation)
        return observation

    tool = _EchoTool(session_id="session-1", user_id="user-1")
    middleware = AgentPolicyMiddleware(context=_interactive_context())
    request = SimpleNamespace(
        tool=tool,
        tool_call={
            "id": "call-agent",
            "name": tool.name,
            "args": {"query": "same"},
        },
    )

    async def _handler(_request):
        return ToolMessage(content="same", tool_call_id="call-agent")

    manager = MoviePilotToolsManager(
        user_id="api-user",
        session_id="api-session",
        is_admin=True,
        policy_orchestrator=DEFAULT_TOOL_POLICY_ORCHESTRATOR,
    )
    manager.tools = [tool]

    async def _run_both():
        await middleware.awrap_tool_call(request, _handler)
        return await manager.call_tool(tool.name, {"query": "same"})

    with patch.object(
        DEFAULT_TOOL_POLICY_ORCHESTRATOR,
        "start",
        side_effect=_capture_start,
    ):
        direct_result = asyncio.run(_run_both())

    assert direct_result == "same"
    assert [item.invocation.origin for item in observations] == [
        ToolOrigin.AGENT_INTERACTIVE,
        ToolOrigin.OPERATOR_DIRECT,
    ]
    assert observations[0].policy == observations[1].policy


def test_agent_middleware_secret_setting_result_stays_out_of_receipt_logs() -> None:
    """Agent ToolNode 可接收管理员请求的原值，但策略回执不得记录该值。"""
    secret_marker = "middleware-secret-setting-marker"
    tool = QuerySystemSettingsTool(session_id="session-1", user_id="admin")
    tool.set_agent_context({"is_admin": True})
    middleware = AgentPolicyMiddleware(context=_interactive_context())
    request = SimpleNamespace(
        tool=tool,
        tool_call={
            "id": "call-secret-setting",
            "name": tool.name,
            "args": {"setting_key": "COOKIECLOUD_KEY", "show_secrets": True},
        },
    )
    mock_logger = MagicMock()

    async def _handler(_request):
        result = await tool._arun(
            setting_key="COOKIECLOUD_KEY",
            show_secrets=True,
        )
        return ToolMessage(content=result, tool_call_id="call-secret-setting")

    with (
        patch.object(
            QuerySystemSettingsTool,
            "_load_setting_value",
            return_value=secret_marker,
        ),
        patch("app.agent.policy.orchestrator.logger", mock_logger),
    ):
        result = asyncio.run(middleware.awrap_tool_call(request, _handler))

    assert secret_marker in result.content
    logged = "\n".join(
        str(call)
        for call in (
            mock_logger.debug.call_args_list + mock_logger.info.call_args_list
        )
    )
    assert secret_marker not in logged
    assert '"value": "***"' in logged
    assert '"value_preview": "***"' in logged


def test_main_agent_registers_policy_middleware_as_outermost() -> None:
    """主 Agent 必须把宿主策略中间件放在 middleware 链最外层。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="user-1")
    fake_llm = SimpleNamespace(
        _llm_type="openai-chat",
        model="fake",
        profile={"max_input_tokens": 64000},
    )
    captured = {}

    def _fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    with (
        patch.object(agent, "_initialize_llm", return_value=fake_llm),
        patch.object(agent, "_initialize_tools", return_value=[]),
        patch.object(agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"),
        patch.object(agent_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
        patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
    ):
        asyncio.run(agent._create_agent(streaming=False))

    assert isinstance(captured["middleware"][0], AgentPolicyMiddleware)


def test_main_agent_preserves_activity_log_middleware_order() -> None:
    """策略层加入后，ActivityLog 仍应位于 Memory 后、摘要前。"""
    agent = agent_module.MoviePilotAgent(
        session_id="session-1",
        user_id="user-1",
        channel=MessageChannel.WebAgent.value,
        source="web-agent",
    )
    fake_llm = SimpleNamespace(
        _llm_type="openai-chat",
        model="fake",
        profile={"max_input_tokens": 64000},
    )
    captured = {}

    def _fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    with (
        patch.object(agent, "_initialize_llm", return_value=fake_llm),
        patch.object(agent, "_initialize_tools", return_value=[]),
        patch.object(agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"),
        patch.object(agent_module, "create_subagent_middlewares", return_value=([], [])),
        patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
        patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
    ):
        asyncio.run(agent._create_agent(streaming=False))

    middlewares = captured["middleware"]
    policy_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, AgentPolicyMiddleware)
    )
    memory_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, MemoryMiddleware)
    )
    activity_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, ActivityLogMiddleware)
    )
    summary_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, SummarizationMiddleware)
    )

    assert policy_index == 0
    assert activity_index == memory_index + 1
    assert summary_index == activity_index + 1
