import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# pylint: disable=no-name-in-module  # 策略包根通过 __getattr__ 惰性导出，Pylint 无法静态解析。
from app.agent.policy import (
    DEFAULT_TOOL_POLICY_REGISTRY,
    ActionEffect,
    ConfirmationMode,
    PrincipalRole,
)
from app.agent.policy.api import (
    API_FIRST_BATCH_OPERATION_SPECS,
    API_OPERATION_ROUTES,
    API_OPERATION_SPECS,
    API_PARITY_OPERATION_SPECS,
)
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.agent_task import AgentTaskTool
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.execute_command import ExecuteCommandTool


def test_api_operation_registry_matches_migration_batches() -> None:
    """API 操作注册表必须覆盖两批 operation 且每项具有固定路由。"""
    assert len(API_FIRST_BATCH_OPERATION_SPECS) == 44
    assert len(API_PARITY_OPERATION_SPECS) == 15
    assert len(API_OPERATION_SPECS) == 59
    assert {spec.operation_id for spec in API_OPERATION_SPECS} == set(API_OPERATION_ROUTES)
    assert {
        "download.list",
        "download.update",
        "download.delete",
        "downloaders.list",
        "library.latest",
    }.isdisjoint(API_OPERATION_ROUTES)


def test_policy_classifies_api_operation_by_operation_id() -> None:
    """网关策略必须按 operation ID 分类，而不能把整个网关视为普通读取。"""
    delete_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="moviepilot_api",
        arguments={"operation_id": "subscription.delete"},
        requires_admin=False,
    )
    unknown_policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="moviepilot_api",
        arguments={"operation_id": "unknown.operation"},
        requires_admin=False,
    )

    assert delete_policy.effect is ActionEffect.DESTRUCTIVE_WRITE
    assert delete_policy.required_role is PrincipalRole.SYSTEM_ADMIN
    assert delete_policy.confirmation is ConfirmationMode.REQUIRED
    assert unknown_policy.machine_allowed is False


def test_gateway_forwards_structured_arguments_to_api_executor() -> None:
    """网关应只把结构化白名单调用转发给固定 API 执行器。"""
    executor = AsyncMock()
    executor.execute.return_value = json.dumps({"success": True})
    gateway = MoviePilotApiTool(
        session_id="session",
        user_id="1",
        executor=executor,
    )

    result = asyncio.run(
        gateway.run(
            operation_id="media.search",
            path_params={"media_type": "movie"},
            query={"page": 1},
            body={"title": "示例"},
        )
    )

    assert json.loads(result)["success"] is True
    executor.execute.assert_awaited_once_with(
        "media.search",
        path_params={"media_type": "movie"},
        query={"page": 1},
        body={"title": "示例"},
    )


def test_gateway_rejects_unknown_operation() -> None:
    """未知 operation ID 必须在调用固定 API 执行器前稳定失败。"""
    gateway = MoviePilotApiTool(session_id="session", user_id="user")

    result = asyncio.run(gateway.run(operation_id="arbitrary.http", body={"path": "/admin"}))

    assert '"success": false' in result
    assert "unknown_operation" in result


def test_gateway_resolves_http_manager_to_persisted_superuser() -> None:
    """MCP/HTTP 管理入口应绑定真实管理员，而不是伪造 api_user 身份。"""
    gateway = MoviePilotApiTool(session_id="session", user_id="api_user")
    gateway.set_message_attr(channel=None, source="api", username="API Client")
    gateway.set_agent_context({"is_admin": True})

    with patch(
        "app.application.security.auth.build_superuser_token_payload",
        return_value=SimpleNamespace(
            sub=7,
            username="admin",
            super_user=True,
        ),
    ):
        identity = asyncio.run(gateway._resolve_api_identity())

    assert identity == ("7", "admin", True)


def test_factory_uses_api_catalog_by_default(monkeypatch) -> None:
    """统一工具工厂默认只暴露原生能力和 API 网关。"""
    monkeypatch.setattr(
        MoviePilotToolFactory,
        "BUILTIN_TOOL_CLASSES",
        (),
    )
    monkeypatch.setattr(
        "app.agent.tools.factory._get_plugin_agent_tools",
        lambda: [],
    )

    tools = MoviePilotToolFactory.create_tools(
        session_id="session",
        user_id="user",
    )

    assert [tool.name for tool in tools] == [
        "send_local_file",
        "moviepilot_api",
    ]


def test_factory_keeps_native_tools_with_api_catalog(monkeypatch) -> None:
    """统一目录保留原生工具，并追加单一 API 网关。"""
    monkeypatch.setattr(
        MoviePilotToolFactory,
        "BUILTIN_TOOL_CLASSES",
        (AgentTaskTool, ExecuteCommandTool),
    )
    monkeypatch.setattr(
        "app.agent.tools.factory._get_plugin_agent_tools",
        lambda: [],
    )

    tools = MoviePilotToolFactory.create_tools(
        session_id="session",
        user_id="user",
    )

    assert [tool.name for tool in tools] == [
        "agent_task",
        "execute_command",
        "send_local_file",
        "moviepilot_api",
    ]
