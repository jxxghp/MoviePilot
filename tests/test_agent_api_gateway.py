import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

# pylint: disable=no-name-in-module  # 策略包根通过 __getattr__ 惰性导出，Pylint 无法静态解析。
from app.agent.policy import (
    DEFAULT_TOOL_POLICY_REGISTRY,
    ActionEffect,
    ConfirmationMode,
    PrincipalRole,
    RecoveryMode,
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
from app.agent.tools.impl.api import MoviePilotApiInput, MoviePilotApiTool
from app.agent.tools.impl.execute_command import ExecuteCommandTool
from app.agent.tools.manager import MoviePilotToolsManager
from app.db.adapters.user import TransactionalUserRepository
from app.db.models.user import User
from app.db.session import SessionFactory, async_session_scope
from app.schemas.types import NotificationChannel


def test_api_operation_registry_matches_migration_batches() -> None:
    """API 操作注册表必须覆盖各迁移批次且每项具有固定路由。"""
    assert len(API_FIRST_BATCH_OPERATION_SPECS) == 53
    assert len(API_PARITY_OPERATION_SPECS) == 17
    assert len(API_MUSIC_OPERATION_SPECS) == 10
    assert len(API_SYSTEM_OPERATION_SPECS) == 7
    assert len(API_EXTENDED_OPERATION_SPECS) == 144
    assert len(API_OPERATION_SPECS) == 231
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


def test_classification_operations_expose_versioned_policy_contract() -> None:
    """媒体自动分类必须只暴露新版查询、校验、预览和版本化写入合同。"""
    expected_routes = {
        "media.classification.fields": ("GET", "/api/v1/media/classification/fields"),
        "media.classification.policy.get": ("GET", "/api/v1/media/classification/policy"),
        "media.classification.policy.validate": ("POST", "/api/v1/media/classification/validate"),
        "media.classification.policy.preview": ("POST", "/api/v1/media/classification/preview"),
        "media.classification.policy.impact": ("POST", "/api/v1/media/classification/impact"),
        "media.classification.policy.history": ("GET", "/api/v1/media/classification/history"),
        "media.classification.policy.update": ("PUT", "/api/v1/media/classification/policy"),
        "media.classification.policy.rollback": (
            "POST",
            "/api/v1/media/classification/rollback/{revision}",
        ),
    }
    assert {
        operation_id: (route.method, route.path)
        for operation_id, route in API_OPERATION_ROUTES.items()
        if operation_id.startswith("media.classification.")
    } == expected_routes
    assert {"media.categories", "media.category.config.get"}.isdisjoint(API_OPERATION_ROUTES)

    specs = {spec.operation_id: spec for spec in API_OPERATION_SPECS}
    assert specs["media.classification.policy.update"].effect is ActionEffect.REVERSIBLE_WRITE
    assert specs["media.classification.policy.rollback"].effect is ActionEffect.REVERSIBLE_WRITE
    assert specs["media.classification.policy.update"].required_role is PrincipalRole.SYSTEM_ADMIN
    assert specs["media.classification.policy.rollback"].required_role is PrincipalRole.SYSTEM_ADMIN
    assert specs["media.classification.policy.update"].confirmation is ConfirmationMode.REQUIRED
    assert specs["media.classification.policy.rollback"].confirmation is ConfirmationMode.REQUIRED
    assert specs["media.classification.policy.update"].recovery is RecoveryMode.TRANSACTION
    assert specs["media.classification.policy.rollback"].recovery is RecoveryMode.TRANSACTION

    schema = MoviePilotApiTool(session_id="session", user_id="api_user").get_mcp_input_schema()
    branches = {
        item["properties"]["operation_id"]["const"]: item
        for item in schema["oneOf"]
    }
    assert branches["media.classification.policy.update"]["properties"]["body"]["$ref"].endswith(
        "/ClassificationPolicyPublishRequest"
    )
    assert branches["media.classification.policy.preview"]["properties"]["body"]["$ref"].endswith(
        "/ClassificationPreviewRequest"
    )
    assert branches["media.classification.policy.rollback"]["properties"]["path_params"]["required"] == [
        "revision"
    ]


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


def test_local_agent_tool_restores_json_encoded_object_bodies() -> None:
    """本地 Agent 工具 schema 不得把 JSON 对象字符串化作为合法请求体。

    部分模型在 body 声明为 anyOf 联合类型时会把整个请求体编码成 JSON 字符串；
    规范化阶段将其还原为原生对象，非 JSON 的非法字符串仍按合同报错。
    """
    tool = MoviePilotApiTool(session_id="session", user_id="api_user")
    schema = tool.tool_call_schema
    assert not isinstance(schema, dict)
    body_schema = schema.model_json_schema()["properties"]["body"]
    body_branches = body_schema["anyOf"]
    body_types = {branch.get("type") for branch in body_branches}

    assert {"object", "array", "string", "null"}.issubset(body_types)
    assert any(branch.get("const") == "dev" for branch in body_branches)
    assert all(
        branch.get("type") != "string" or branch.get("const") == "dev"
        for branch in body_branches
    )

    body = {
        "torrent_in": {
            "title": "示例种子",
            "enclosure": "https://example.com/test.torrent",
        },
    }
    canonical = tool.canonical_arguments({"operation_id": "download.add", "body": body})
    assert canonical["body"]["torrent_in"]["enclosure"] == body["torrent_in"]["enclosure"]

    # 模型把 body 编码为 JSON 字符串时，规范化阶段还原为原生对象。
    restored = tool.canonical_arguments(
        {
            "operation_id": "download.add",
            "body": json.dumps(body, ensure_ascii=False),
        }
    )
    assert isinstance(restored["body"], dict)
    assert restored["body"]["torrent_in"]["enclosure"] == body["torrent_in"]["enclosure"]

    # LangChain 入模同样必须还原，否则 canonical_arguments 通过后仍会在 Pydantic 阶段失败。
    restored_model = MoviePilotApiInput.model_validate(
        {
            "operation_id": "download.add",
            "body": json.dumps(body, ensure_ascii=False),
        }
    )
    assert isinstance(restored_model.body, dict)
    assert restored_model.body["torrent_in"]["enclosure"] == body["torrent_in"]["enclosure"]

    # 固定的 system.upgrade.dev 字面量继续作为唯一合法字符串请求体。
    dev_model = MoviePilotApiInput.model_validate(
        {"operation_id": "system.upgrade.dev", "body": "dev"}
    )
    assert dev_model.body == "dev"

    # 无法还原为对象/数组的非法字符串仍按合同报错。
    with pytest.raises(ValueError):
        tool.canonical_arguments(
            {
                "operation_id": "download.add",
                "body": "not-a-json-object",
            }
        )


def test_operation_body_shapes_fit_local_agent_input_schema() -> None:
    """所有 MCP operation 请求体必须可由内置 Agent 输入类型表达。"""
    tool = MoviePilotApiTool(session_id="session", user_id="api_user")
    api_schema = tool.get_mcp_input_schema()
    definitions = api_schema.get("$defs", {})
    string_bodies = set()

    for operation in api_schema["oneOf"]:
        properties = operation.get("properties", {})
        body_schema = properties.get("body")
        if body_schema is None:
            continue
        while "$ref" in body_schema:
            body_schema = definitions[body_schema["$ref"].rsplit("/", 1)[-1]]
        variants = body_schema.get("anyOf", body_schema.get("oneOf", [body_schema]))
        operation_id = properties["operation_id"]["const"]
        for variant in variants:
            while "$ref" in variant:
                variant = definitions[variant["$ref"].rsplit("/", 1)[-1]]
            if variant.get("type") == "string":
                string_bodies.add((operation_id, variant.get("const")))
            else:
                assert variant.get("type") in {"object", "array", "null"}

    assert string_bodies == {("system.upgrade.dev", "dev")}


def test_local_api_schema_forbids_flattened_arguments_and_examples_match_contracts() -> None:
    """模型实际接收的 schema 禁止额外顶层字段，内联示例均通过真实 operation 合同。"""
    tool = MoviePilotApiTool(session_id="session", user_id="1")
    definition = convert_to_openai_tool(tool)["function"]
    schema = definition["parameters"]
    assert definition["description"] == tool.description
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"operation_id", "path_params", "query", "body"}
    with pytest.raises(ValueError):
        MoviePilotApiInput.model_validate({"operation_id": "site.rss", "page": 1})

    examples = tool.description.split("Examples (replace sample IDs with IDs from prior results): ")[1]
    decoder = json.JSONDecoder()
    operations = []
    for _ in range(3):
        example, end = decoder.raw_decode(examples)
        operations.append(tool.canonical_arguments(example)["operation_id"])
        examples = examples[end:].lstrip("; ")
    assert operations == ["media.detail", "subscription.execution.list", "site.rss"]


@pytest.mark.parametrize(("arguments", "expected"), [
    ({"operation_id": "media.detail", "media_id": "secret-value", "media_source": "tmdb"},
     ["media_id 应移入 path_params.media_id", "media_source 应移入 query.media_source"]),
    ({"operation_id": "subscription.execution.list", "limit": 2}, ["limit 应移入 query.limit"]),
    ({"operation_id": "site.rss", "query": {"site_id": "secret-value"}}, ["query", "site_id"]),
    ({"operation_id": "media.detail"}, ["path_params.media_id"]),
    ({"operation_id": "media.detail", "path_params": {"media_id": "27205"}}, ["query.media_source"]),
    ({"operation_id": "subscription.execution.list", "query": {"limit": "secret-value"}}, ["query.limit"]),
    ({"operation_id": "site.list", "query": {"status": "secret-value"}}, ["query.status"]),
    ({"operation_id": "download.add", "torrent_in": {}}, ["torrent_in 应移入 body.torrent_in"]),
    ({"operation_id": "config.system.update", "body": {"value": "secret-value"}}, ["body.setting_key"]),
    ({"operation_id": "site.rss", "https://secret-value.invalid/": "secret-value"}, ["字段名已省略"]),
])
@pytest.mark.asyncio
async def test_gateway_raw_tool_inputs_return_actionable_errors_without_execution(arguments, expected) -> None:
    """覆盖 LangChain 和直接调用入口，错误字段不能静默丢弃、执行或泄露参数值。"""
    executor = AsyncMock()
    gateway = MoviePilotApiTool(session_id="session", user_id="1", executor=executor)
    message = await gateway.ainvoke({"name": gateway.name, "args": arguments, "id": "invalid", "type": "tool_call"})
    assert message.status == "error"
    for result in (message.content, await gateway.run(**arguments)):
        payload = json.loads(result)
        assert payload["error"] == "invalid_input"
        assert payload["input_contract"]["operation_id"] == arguments["operation_id"]
        assert all(item in payload["message"] for item in expected)
        assert "secret-value" not in result
    executor.execute.assert_not_awaited()


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
    assert delete_policy.required_role is PrincipalRole.USER
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
            query={"page": 1, "title": "示例"},
        )
    )

    assert json.loads(result)["success"] is True
    executor.execute.assert_awaited_once_with(
        "media.search",
        path_params=None,
        query={"page": 1, "title": "示例"},
        body=None,
    )


def test_gateway_rejects_unknown_operation() -> None:
    """未知 operation ID 必须在调用固定 API 执行器前稳定失败。"""
    gateway = MoviePilotApiTool(session_id="session", user_id="user")

    result = asyncio.run(gateway.run(operation_id="arbitrary.http", body={"path": "/admin"}))

    assert '"success": false' in result
    assert "unknown_operation" in result


def test_gateway_returns_operation_contract_when_input_is_invalid() -> None:
    """单个 operation 的参数错误必须返回可直接纠正调用的精确合同。"""
    executor = AsyncMock()
    gateway = MoviePilotApiTool(
        session_id="session",
        user_id="1",
        executor=executor,
    )

    result = asyncio.run(
        gateway.run(
            operation_id="site.list",
            query={"status": "enabled"},
        )
    )
    payload = json.loads(result)

    assert payload["error"] == "invalid_input"
    assert payload["operation_id"] == "site.list"
    assert payload["input_contract"]["allowed_arguments"] == ["query"]
    status = payload["input_contract"]["query"]["fields"]["status"]
    assert status["enum"] == ["active", "inactive", "all"]
    assert "enabled" not in json.dumps(payload, ensure_ascii=False)
    executor.execute.assert_not_awaited()


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


@pytest.mark.parametrize("channel", [NotificationChannel.QQ, NotificationChannel.Feishu])
@pytest.mark.parametrize("bound_suffix,other_suffix", [("openid", "userid"), ("userid", "openid")])
@pytest.mark.parametrize("other_value", [None, "", "different-id", "channel-id"])
def test_gateway_resolves_either_channel_binding(db, channel, bound_suffix, other_suffix, other_value) -> None:
    """任一候选字段匹配即可绑定真实用户，另一字段缺失、为空或不同时仍可使用。"""
    prefix = channel.name.lower()
    bindings = {f"{prefix}_{bound_suffix}": "channel-id"}
    if other_value is not None:
        bindings[f"{prefix}_{other_suffix}"] = other_value
    user = db.add(User(name="channel-owner", is_active=True, is_superuser=False, settings=bindings))
    users = TransactionalUserRepository(sync_session=SessionFactory, async_session=async_session_scope)
    gateway = MoviePilotApiTool(
        session_id="session", user_id="channel-id", data=SimpleNamespace(users=users),
    )
    gateway.set_message_attr(channel=channel.value, source="main-bot", username="channel-nickname")

    identity = asyncio.run(gateway._resolve_api_identity())

    assert identity == (str(user.id), "channel-owner", False)


@pytest.mark.parametrize("is_channel_admin", [False, True])
@pytest.mark.parametrize("user_id,username", [(11, "alice"), (12, "bob")])
@pytest.mark.parametrize(
    "operation_id,arguments",
    [
        ("subscription.add", {"body": {"name": "示例"}}),
        ("subscription.update", {"body": {"id": 1}}),
        ("subscription.delete", {"path_params": {"subscribe_id": 1}}),
    ],
)
def test_subscription_writes_keep_bound_channel_identity(
    is_channel_admin, user_id, username, operation_id, arguments,
) -> None:
    """不同飞书发送者的订阅写入使用各自绑定用户，不因渠道管理员资格提权。"""
    users = SimpleNamespace(
        find_name_by_bindings=Mock(return_value=username),
        async_get_by_name=AsyncMock(return_value=SimpleNamespace(
            id=user_id, name=username, is_active=True, is_superuser=False,
        )),
    )
    gateway = MoviePilotApiTool(
        session_id="session", user_id=f"ou_{username}", data=SimpleNamespace(users=users),
    )
    gateway.set_message_attr(channel=NotificationChannel.Feishu.value, source="main-bot", username="")
    gateway.set_agent_context({"is_admin": is_channel_admin})
    policy = DEFAULT_TOOL_POLICY_REGISTRY.resolve(
        tool_name="moviepilot_api", arguments={"operation_id": operation_id}, requires_admin=False,
    )
    assert policy.required_role is PrincipalRole.USER
    assert policy.confirmation is ConfirmationMode.REQUIRED
    with patch("app.agent.tools.impl.api.MoviePilotApiExecutor") as executor_type, patch(
        "app.application.security.auth.build_superuser_token_payload",
    ) as superuser:
        executor_type.return_value.execute = AsyncMock(return_value='{"success": true}')
        result = asyncio.run(gateway.run(operation_id=operation_id, **arguments))

    assert json.loads(result)["success"] is True
    context = executor_type.call_args.kwargs["context"]
    assert (context.user_id, context.username, context.is_admin) == (str(user_id), username, False)
    users.find_name_by_bindings.assert_called_once_with({
        "feishu_userid": f"ou_{username}", "feishu_openid": f"ou_{username}",
    }, match_any=True)
    superuser.assert_not_called()


@pytest.mark.parametrize("is_bound", [False, True])
def test_subscription_write_rejects_unbound_or_inactive_channel_admin(is_bound) -> None:
    """渠道管理员未绑定有效用户时不得回退超级管理员创建订阅。"""
    users = SimpleNamespace(
        find_name_by_bindings=Mock(return_value="disabled" if is_bound else None),
        async_get_by_name=AsyncMock(return_value=SimpleNamespace(is_active=False)),
    )
    gateway = MoviePilotApiTool(
        session_id="session", user_id="ou_user", data=SimpleNamespace(users=users),
    )
    gateway.set_message_attr(channel=NotificationChannel.Feishu.value, source="main-bot", username="")
    gateway.set_agent_context({"is_admin": True})
    with patch("app.agent.tools.impl.api.MoviePilotApiExecutor") as executor_type, patch(
        "app.application.security.auth.build_superuser_token_payload",
    ) as superuser:
        result = asyncio.run(gateway.run(operation_id="subscription.add", body={"name": "示例"}))

    assert json.loads(result)["error"] == "operation_unavailable"
    executor_type.assert_not_called()
    superuser.assert_not_called()


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
