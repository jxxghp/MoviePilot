import asyncio
import json
import re
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
    API_EXTENDED_OPERATION_SPECS,
    API_FIRST_BATCH_OPERATION_SPECS,
    API_MUSIC_OPERATION_SPECS,
    API_OPERATION_ROUTES,
    API_OPERATION_SPECS,
    API_PARITY_OPERATION_SPECS,
    API_SYSTEM_OPERATION_SPECS,
)
from app.agent.tools.factory import MoviePilotToolFactory
from app.agent.tools.impl.agent_task import AgentTaskTool
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.impl.execute_command import ExecuteCommandTool
from app.agent.tools.manager import MoviePilotToolsManager


def test_api_operation_registry_matches_migration_batches() -> None:
    """API 操作注册表必须覆盖两批 operation 且每项具有固定路由。"""
    assert len(API_FIRST_BATCH_OPERATION_SPECS) == 52
    assert len(API_PARITY_OPERATION_SPECS) == 15
    assert len(API_MUSIC_OPERATION_SPECS) == 10
    assert len(API_SYSTEM_OPERATION_SPECS) == 7
    assert len(API_EXTENDED_OPERATION_SPECS) == 118
    assert len(API_OPERATION_SPECS) == 202
    assert {spec.operation_id for spec in API_OPERATION_SPECS} == set(API_OPERATION_ROUTES)
    assert {
        "download.list",
        "download.update",
        "download.delete",
        "downloaders.list",
    }.isdisjoint(API_OPERATION_ROUTES)
    assert {
        "plugin.source.options",
        "plugin.source.install",
        "plugin.source.change",
        "download.tasks.active",
        "download.clients",
        "download.paths",
        "download.history.list",
        "library.latest",
        "system.versions",
        "system.update.status",
        "system.update.check",
        "system.update.download",
        "system.restart",
        "system.update.install",
        "system.upgrade.dev",
        "dashboard.system",
        "media.sources",
        "search.title",
        "site.add",
        "subscription.get",
        "storage.rename",
        "transfer.manual_reviews",
        "workflow.create",
        "torrent.cache.get",
        "database.backups.list",
        "system.module.list",
        "plugin.clone",
    }.issubset(API_OPERATION_ROUTES)


def test_api_tool_message_displays_secret_safe_major_parameters() -> None:
    """啰嗦模式提示应展示 API 主要参数，同时对设置凭据值脱敏。"""
    tool = MoviePilotApiTool(session_id="session", user_id="api_user")

    list_message = tool.get_tool_message(
        operation_id="subscription.list",
        query={"page": 1, "count": 20},
    )
    secret_message = tool.get_tool_message(
        operation_id="config.system.update",
        body={
            "setting_key": "OPENAI_API_KEY",
            "value": "sk-secret-value",
            "operation": "replace",
        },
    )

    assert list_message == (
        '调用 MoviePilot API：subscription.list，主要参数：'
        '{"query": {"page": 1, "count": 20}}'
    )
    assert "OPENAI_API_KEY" in secret_message
    assert '"value": "***"' in secret_message
    assert "sk-secret-value" not in secret_message


def test_music_operations_expose_bidirectional_artist_album_navigation() -> None:
    """音乐 Skill 必须完整暴露作品到作者、作者到作品及关联浏览合同。"""
    schema = MoviePilotApiTool(session_id="session", user_id="api_user").get_mcp_input_schema()
    branches = {
        item["properties"]["operation_id"]["const"]: item
        for item in schema["oneOf"]
    }

    assert {
        "music.recognize",
        "music.explore",
        "music.album.get",
        "music.album.related",
        "music.artist.get",
        "music.artist.albums",
        "music.artist.related",
        "music.cache.get",
        "music.cache.delete",
        "music.cache.clear",
    }.issubset(branches)
    album_path = branches["music.album.get"]["properties"]["path_params"]
    assert album_path["required"] == ["album_id"]
    artist_albums = branches["music.artist.albums"]
    assert artist_albums["properties"]["path_params"]["required"] == ["artist_id"]
    album_type = artist_albums["properties"]["query"]["properties"]["album_type"]
    assert "single" in str(album_type)
    assert branches["music.cache.delete"]["properties"]["path_params"]["required"] == [
        "cache_key"
    ]


def test_mcp_tools_list_preserves_all_moviepilot_api_operation_branches() -> None:
    """外部 MCP tools/list 必须返回全部 operation 的精确 oneOf，而不是通用字典。"""
    manager = MoviePilotToolsManager(session_id="session", user_id="api_user")
    manager.tools = [MoviePilotApiTool(session_id="session", user_id="api_user")]

    definition = manager.list_tools()[0]
    operation_ids = {
        branch["properties"]["operation_id"]["const"]
        for branch in definition.input_schema["oneOf"]
    }

    assert definition.name == "moviepilot_api"
    assert operation_ids == set(API_OPERATION_ROUTES)


def test_mcp_collection_contract_distinguishes_exact_and_unavailable_totals() -> None:
    """MCP 必须说明缺省全量、精确总数和外部无总数三种集合语义。"""
    schema = MoviePilotApiTool(session_id="session", user_id="api_user").get_mcp_input_schema()
    branches = {
        item["properties"]["operation_id"]["const"]: item
        for item in schema["oneOf"]
    }

    subscription = branches["subscription.list"]
    subscription_query = subscription["properties"]["query"]["properties"]
    assert "default" not in subscription_query["page"]
    assert "default" not in subscription_query["count"]
    assert subscription["x-moviepilot-collection"] == {
        "body_shape": "list",
        "result_count_field": "collection.result_count",
        "total_count_field": "collection.total_count",
        "default_pagination": "unpaginated",
    }
    assert "page=1 and count=1" in subscription["description"]
    assert "do not query the database" in subscription["description"]

    storage = branches["storage.list"]
    assert {"page", "count"}.issubset(storage["properties"]["query"]["properties"])
    assert storage["x-moviepilot-collection"]["total_count_field"] == (
        "collection.total_count"
    )

    for operation_id in ("subscription.history", "download.history.list"):
        local_page = branches[operation_id]["x-moviepilot-collection"]
        assert local_page["total_count_field"] == "collection.total_count"
        assert local_page["default_pagination"] == "endpoint-defined"
        assert "defaults remain in effect" in branches[operation_id]["description"]

    for operation_id in ("plugin.installed", "plugin.market"):
        local_page = branches[operation_id]["x-moviepilot-collection"]
        assert local_page["total_count_field"] == "collection.total_count"
        assert local_page["default_pagination"] == "unpaginated"
        assert "omit both page and count" in branches[operation_id]["description"]
        assert "default" not in branches[operation_id]["properties"]["query"]["properties"]["max_results"]

    media_search = branches["media.search"]["x-moviepilot-collection"]
    assert media_search["result_count_field"] == "collection.result_count"
    assert media_search["total_count_field"] is None
    assert "does not expose a total" in branches["media.search"]["description"]

    transfer = branches["transfer.history"]["x-moviepilot-collection"]
    assert transfer["body_shape"] == "page_object"
    assert transfer["items_field"] == "data.list"
    assert transfer["total_count_field"] == "data.total"


def test_filter_read_parameters_are_query_fields_not_get_request_bodies() -> None:
    """规则读取筛选列表必须作为 query 参数公开，避免 Agent 构造无语义 GET body。"""
    schema = MoviePilotApiTool(session_id="session", user_id="api_user").get_mcp_input_schema()
    branches = {
        item["properties"]["operation_id"]["const"]: item
        for item in schema["oneOf"]
    }

    assert "body" not in branches["filter.builtin"]["properties"]
    assert "rule_ids" in branches["filter.builtin"]["properties"]["query"]["properties"]
    assert "body" not in branches["filter.custom"]["properties"]
    assert "rule_ids" in branches["filter.custom"]["properties"]["query"]["properties"]
    assert "body" not in branches["filter.groups"]["properties"]
    assert "group_names" in branches["filter.groups"]["properties"]["query"]["properties"]


def test_plugin_operations_expose_discovery_before_precise_writes() -> None:
    """插件配置和来源操作必须给 Agent 可先发现再精确写入的完整合同。"""
    schema = MoviePilotApiTool(session_id="session", user_id="api_user").get_mcp_input_schema()
    branches = {
        item["properties"]["operation_id"]["const"]: item
        for item in schema["oneOf"]
    }

    installed_query = branches["plugin.installed"]["properties"]["query"]
    assert installed_query["properties"]["state"]["const"] == "installed"
    assert "query" in installed_query["properties"]
    max_results = installed_query["properties"]["max_results"]
    integer_variant = next(item for item in max_results["anyOf"] if item.get("type") == "integer")
    assert integer_variant["maximum"] == 200

    config_get_path = API_OPERATION_ROUTES["plugin.config.get"].path
    assert config_get_path == "/api/v1/plugin/form/{plugin_id}"
    config_body = branches["plugin.config.update"]["properties"]["body"]
    assert config_body["minProperties"] == 1
    assert "First call plugin.config.get" in config_body["description"]

    assert branches["plugin.source.install"]["properties"]["body"]["$ref"].endswith(
        "/PluginSourceInstallRequest"
    )
    assert branches["plugin.source.change"]["properties"]["body"]["$ref"].endswith(
        "/PluginSourceChangeRequest"
    )


def test_dev_upgrade_mcp_schema_requires_exact_scalar_body() -> None:
    """Dev 更新必须暴露精确字符串 body，不能回退为任意 JSON。"""
    schema = MoviePilotApiTool(
        session_id="session",
        user_id="api_user",
    ).get_mcp_input_schema()
    branch = next(
        item
        for item in schema["oneOf"]
        if item["properties"]["operation_id"].get("const") == "system.upgrade.dev"
    )

    assert branch["properties"]["body"]["const"] == "dev"
    assert branch["properties"]["body"]["type"] == "string"
    assert "body" in branch["required"]


def test_gateway_forwards_exact_scalar_body() -> None:
    """网关必须原样传递少数固定 operation 声明的 JSON 标量请求体。"""
    executor = AsyncMock()
    executor.execute.return_value = json.dumps({"success": True})
    gateway = MoviePilotApiTool(
        session_id="session",
        user_id="api_user",
        executor=executor,
    )
    gateway.set_agent_context({"is_admin": True})

    result = asyncio.run(
        gateway.run(
            operation_id="system.upgrade.dev",
            body="dev",
        )
    )

    assert json.loads(result)["success"] is True
    executor.execute.assert_awaited_once_with(
        "system.upgrade.dev",
        path_params=None,
        query=None,
        body="dev",
    )


def test_system_settings_mcp_schema_explains_both_setting_sources() -> None:
    """外部 MCP Client 应直接看到系统设置发现与精确更新参数语义。"""
    schema = MoviePilotApiTool(
        session_id="session",
        user_id="api_user",
    ).get_mcp_input_schema()
    branches = {
        branch["properties"]["operation_id"]["const"]: branch
        for branch in schema["oneOf"]
    }
    query = branches["config.system.get"]["properties"]["query"]["properties"]
    update_ref = branches["config.system.update"]["properties"]["body"]["$ref"]
    update = schema["$defs"][update_ref.rsplit("/", 1)[-1]]["properties"]

    assert "Settings field names" in query["setting_key"]["description"]
    assert "systemconfig" in query["group"]["description"]
    assert "confirmation-protected" in query["show_secrets"]["description"]
    assert "config.system.get" in update["setting_key"]["description"]
    assert "upsert_list_item" in update["operation"]["description"]
    assert "NotificationSwitchs" in update["match_field"]["description"]


def test_api_mcp_schema_gives_every_field_concrete_english_guidance() -> None:
    """MCP 合同字段不得回退为抽象占位说明或中英文混排。"""
    schema = MoviePilotApiTool(
        session_id="session",
        user_id="api_user",
    ).get_mcp_input_schema()
    descriptions = []

    def collect(node) -> None:
        """递归收集对象字段说明并断言没有遗漏。"""
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                for field_name, field_schema in properties.items():
                    assert isinstance(field_schema, dict), field_name
                    description = field_schema.get("description")
                    assert isinstance(description, str) and description.strip(), field_name
                    descriptions.append(description)
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(schema)
    rendered = "\n".join(descriptions)
    assert "按接口模型语义传值" not in rendered
    assert "declared by the selected operation" not in rendered
    assert "use the matching oneOf branch for its exact type" not in rendered
    assert not re.search(r"[\u3400-\u9fff]", rendered)


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


def test_gateway_rejects_admin_operation_for_non_admin_before_http() -> None:
    """管理员 operation 必须在网关层拒绝普通用户，不能只依赖最终端点。"""
    executor = AsyncMock()
    gateway = MoviePilotApiTool(
        session_id="session",
        user_id="1",
        executor=executor,
    )
    gateway.set_agent_context({"is_admin": False})

    result = asyncio.run(
        gateway.run(
            operation_id="config.system.get",
            query={"group": "settings"},
        )
    )

    assert json.loads(result)["error"] == "permission_denied"
    executor.execute.assert_not_awaited()


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


def test_gateway_maps_verified_channel_admin_only_for_admin_operation() -> None:
    """通知渠道管理员执行管理员 operation 时应延续旧工具的管理员语义。"""
    gateway = MoviePilotApiTool(session_id="session", user_id="telegram-user")
    gateway.set_message_attr(
        channel="telegram",
        source="main-bot",
        username="channel-user",
    )
    gateway.set_agent_context({"is_admin": True})

    with patch(
        "app.application.security.auth.build_superuser_token_payload",
        return_value=SimpleNamespace(
            sub=7,
            username="admin",
            super_user=True,
        ),
    ):
        identity = asyncio.run(
            gateway._resolve_api_identity(require_system_admin=True)
        )

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
