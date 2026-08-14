"""Agent 本地工具目录身份、冲突与版本窗口测试。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from app.agent.tools.catalog import (
    ToolCatalogSnapshot,
    ToolIdentityAmbiguousError,
)
from app.agent.tools.factory import MoviePilotToolFactory
from app.runtime.extensions.plugin_manager import PluginManager


class _Arguments(BaseModel):
    """目录测试工具的参数契约。"""

    query: str = ""


def _tool(name: str, source: str = "builtin") -> SimpleNamespace:
    """构造带稳定 schema 与来源的最小工具替身。"""
    return SimpleNamespace(
        name=name,
        args_schema=_Arguments,
        _agent_tool_source=source,
    )


def test_catalog_preserves_order_and_resolves_exact_instance() -> None:
    """目录应保持构造顺序，并返回同一次构造的精确实例。"""
    first = _tool("first")
    second = _tool("second", "plugin:demo")

    catalog = ToolCatalogSnapshot.from_tools(
        [first, second],
        plugin_revision=7,
        factory_revision="factory-v1",
    )

    assert catalog.tools == [first, second]
    assert catalog.resolve_unique("second").tool is second
    assert catalog.resolve_unique("missing") is None
    assert catalog.signature[0:2] == ("factory-v1", 7)


def test_catalog_records_all_duplicate_names_and_strict_lookup_fails() -> None:
    """内置与插件同名时必须保留双方身份并拒绝隐式选胜者。"""
    builtin = _tool("query_system_settings")
    plugin = _tool("query_system_settings", "plugin:demo")
    catalog = ToolCatalogSnapshot.from_tools(
        [builtin, plugin],
        plugin_revision=3,
        factory_revision="factory-v1",
    )

    assert [entry.tool for entry in catalog.collisions["query_system_settings"]] == [
        builtin,
        plugin,
    ]
    with pytest.raises(
        ToolIdentityAmbiguousError,
        match="TOOL_IDENTITY_AMBIGUOUS",
    ):
        catalog.resolve_unique("query_system_settings")


def test_catalog_subset_keeps_all_identities_for_selected_name() -> None:
    """子图执行只选首个实例时，严格目录仍必须保留全部同名身份。"""
    first = _tool("shared", "mcp:one")
    second = _tool("shared", "mcp:two")
    catalog = ToolCatalogSnapshot.from_tools(
        [first, second],
        plugin_revision=0,
        factory_revision="factory-v1",
    )

    selected = catalog.select([first])

    assert [entry.tool for entry in selected.collisions["shared"]] == [first, second]


def test_catalog_signature_changes_with_schema_and_plugin_revision() -> None:
    """schema 或插件目录 revision 变化必须使图缓存签名失效。"""
    tool = _tool("demo")
    first = ToolCatalogSnapshot.from_tools(
        [tool], plugin_revision=1, factory_revision="factory-v1"
    )

    class _UpdatedArguments(BaseModel):
        """模拟热加载后的参数契约。"""

        query: str = ""
        limit: int = 10

    tool.args_schema = _UpdatedArguments
    schema_changed = ToolCatalogSnapshot.from_tools(
        [tool], plugin_revision=1, factory_revision="factory-v1"
    )
    revision_changed = ToolCatalogSnapshot.from_tools(
        [tool], plugin_revision=2, factory_revision="factory-v1"
    )

    assert schema_changed.signature != first.signature
    assert revision_changed.signature != schema_changed.signature


def test_catalog_signature_changes_with_json_schema_mapping() -> None:
    """MCP 使用的 dict JSON Schema 变化必须使目录签名失效。"""
    tool = _tool("mcp_demo", "mcp:demo")
    tool.args_schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    first = ToolCatalogSnapshot.from_tools(
        [tool], plugin_revision=1, factory_revision="factory-v1"
    )
    tool.args_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
    }
    second = ToolCatalogSnapshot.from_tools(
        [tool], plugin_revision=1, factory_revision="factory-v1"
    )

    assert second.signature != first.signature


def test_catalog_signature_changes_with_tool_description() -> None:
    """影响模型选择的工具描述变化必须使目录签名失效。"""
    tool = _tool("demo")
    tool.description = "first description"
    first = ToolCatalogSnapshot.from_tools(
        [tool], plugin_revision=1, factory_revision="factory-v1"
    )
    tool.description = "updated description"
    second = ToolCatalogSnapshot.from_tools(
        [tool], plugin_revision=1, factory_revision="factory-v1"
    )

    assert second.signature != first.signature


def test_factory_catalog_retries_plugin_revision_churn_with_bound() -> None:
    """插件构造期间持续 reload 时只能有界重试并失败关闭。"""
    revisions = iter([1, 2, 3, 4, 5, 6])
    plugin_manager = PluginManager()
    with patch.object(
        plugin_manager,
        "get_plugin_agent_tools_revision",
        side_effect=lambda: next(revisions),
    ), patch.object(MoviePilotToolFactory, "create_tools", return_value=[]):
        with pytest.raises(RuntimeError, match="持续变化"):
            MoviePilotToolFactory.create_catalog(
                session_id="session",
                user_id="user",
            )
