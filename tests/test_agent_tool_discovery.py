"""工具按需发现的真实图执行与目录边界回归测试。"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, ValidationError

from app.agent.middleware.selection import (
    TOOL_DISCOVERY_DESCRIPTION_CHARS,
    TOOL_DISCOVERY_MAX_RESULTS,
    TOOL_DISCOVERY_NAME,
    ToolSelectorMiddleware,
)
from app.agent.tools.catalog import ToolCatalogSnapshot


class _RecordingModel(FakeMessagesListChatModel):
    """记录实际绑定工具的离线模型，驱动真实模型和工具循环。"""

    tool_history: list[list[Any]] = Field(default_factory=list)
    invocation_count: int = 0

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "_RecordingModel":
        """记录每轮精确工具实例，便于核实发现后的完整参数契约。"""
        self.tool_history.append(list(tools))
        return self

    def _generate(self, messages: list[Any], stop: Optional[list[str]] = None, run_manager: Any = None, **kwargs: Any) -> Any:
        """统计真实模型调用次数，确认发现工具不会增加额外筛选模型请求。"""
        self.invocation_count += 1
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _make_tool(name: str, description: str, tags: Optional[list[str]] = None) -> StructuredTool:
    """创建完全离线的测试能力，携带真实结构化输入参数。"""
    def invoke(record_id: int) -> str:
        """用传入标识生成观察结果，验证发现后可以实际调用工具。"""
        return f"{name}:{record_id}"

    return StructuredTool.from_function(
        func=invoke,
        name=name,
        description=description,
        tags=tags or [],
    )


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    """构造模型发出的标准工具调用。"""
    return {"name": name, "args": arguments, "id": call_id, "type": "tool_call"}


def test_discovery_enables_exact_schemas_merges_parallel_calls_and_resets_turns():
    """真实图并行发现遗漏能力后可调用，且同会话新请求与其他会话均重新筛选。"""
    status = _make_tool("status", "运行状态")
    transfer = _make_tool("transfer_history", "查询整理历史", ["transfer"])
    subscribe = _make_tool("subscribe_media", "管理媒体订阅", ["subscription"])
    hidden = _make_tool("delete_library", "删除媒体库")
    selection_model = _RecordingModel(responses=[AIMessage(content='{"tools": []}')])
    model = _RecordingModel(responses=[
        AIMessage(content="", tool_calls=[
            _tool_call(TOOL_DISCOVERY_NAME, {"search": "transfer_history", "limit": 1}, "find-transfer"),
            _tool_call(TOOL_DISCOVERY_NAME, {"search": "subscribe_media", "limit": 1}, "find-subscribe"),
        ]),
        AIMessage(content="", tool_calls=[
            _tool_call("transfer_history", {"record_id": 42}, "read-history"),
        ]),
        AIMessage(content="已查询整理历史"),
        AIMessage(content="新请求"),
        AIMessage(content="其他会话"),
    ])
    original_tools = [status, transfer, subscribe, hidden]
    middleware = ToolSelectorMiddleware(
        model=selection_model,
        selection_tools=original_tools,
        always_include=["status"],
        max_tools=1,
        enable_discovery=True,
    )
    graph = create_agent(
        model=model,
        tools=original_tools,
        middleware=[middleware],
        checkpointer=InMemorySaver(),
    )

    async def execute() -> None:
        """串行执行多个用户请求并读取持久化图状态。"""
        first_config = {"configurable": {"thread_id": "first-session"}}
        result = await graph.ainvoke({"messages": [HumanMessage(content="检查系统")]}, first_config)
        assert any(message.content == "transfer_history:42" for message in result["messages"])
        state = await graph.aget_state(first_config)
        assert set(state.values["discovered_tool_names"]) == {"transfer_history", "subscribe_media"}
        await graph.ainvoke({"messages": [HumanMessage(content="新的请求")]}, first_config)
        state = await graph.aget_state(first_config)
        assert state.values["discovered_tool_names"] == []
        await graph.ainvoke(
            {"messages": [HumanMessage(content="其他会话")]},
            {"configurable": {"thread_id": "second-session"}},
        )

    asyncio.run(execute())

    bound_names = [{tool.name for tool in tools} for tools in model.tool_history]
    assert bound_names[0] == {"status", TOOL_DISCOVERY_NAME}
    assert bound_names[1] == {"status", TOOL_DISCOVERY_NAME, "transfer_history", "subscribe_media"}
    assert bound_names[2] == bound_names[1]
    assert bound_names[3] == bound_names[4] == bound_names[0]
    assert transfer in model.tool_history[1]
    assert transfer.tool_call_schema.model_json_schema()["properties"]["record_id"]["type"] == "integer"
    assert "delete_library" not in set.union(*bound_names)
    assert selection_model.invocation_count == 3
    assert original_tools == [status, transfer, subscribe, hidden]


def test_discovery_catalog_is_bounded_and_matches_name_description_and_tags():
    """发现仅返回已授权目录的有界说明，并支持名称、中文说明和能力标签检索。"""
    exact = _make_tool("transfer_history", "查询失败整理历史", ["transfer"])
    subscriptions = [
        _make_tool(f"subscription_{index}", "媒体订阅" * 300, ["subscription"])
        for index in range(12)
    ]
    middleware = ToolSelectorMiddleware(selection_tools=[*subscriptions, exact], enable_discovery=True)
    discovery = middleware.tools[0]

    async def search(query: str, limit: int = TOOL_DISCOVERY_MAX_RESULTS) -> Any:
        """直接调用已注册工具以覆盖参数校验和状态增量结果。"""
        return await discovery.ainvoke({
            "search": query,
            "limit": limit,
            "runtime": SimpleNamespace(tool_call_id="discovery"),
        })

    result = asyncio.run(search("subscription"))
    payload = json.loads(result.update["messages"][0].content)
    assert len(payload["tools"]) == TOOL_DISCOVERY_MAX_RESULTS
    assert len(result.update["discovered_tool_names"]) == TOOL_DISCOVERY_MAX_RESULTS
    assert all(len(tool["description"]) <= TOOL_DISCOVERY_DESCRIPTION_CHARS for tool in payload["tools"])
    assert asyncio.run(search("TRANSFER_HISTORY", 1)).update["discovered_tool_names"] == ["transfer_history"]
    assert asyncio.run(search("失败整理", 1)).update["discovered_tool_names"] == ["transfer_history"]
    assert asyncio.run(search("transfer", 1)).update["discovered_tool_names"] == ["transfer_history"]
    assert asyncio.run(search("unavailable_secret_tool")).update["discovered_tool_names"] == []
    assert asyncio.run(search("  !!!  ")).update["discovered_tool_names"] == []


@pytest.mark.parametrize("arguments", [{"search": ""}, {"search": "x" * 161}, {"search": "media", "limit": 9}])
def test_discovery_rejects_unbounded_queries(arguments: dict[str, Any]):
    """超界请求被明确拒绝，避免模型通过参数扩大目录输出。"""
    middleware = ToolSelectorMiddleware(enable_discovery=True)
    with pytest.raises(ValidationError):
        middleware.tools[0].args_schema.model_validate(arguments)


def test_discovery_preserves_provider_and_mandatory_tools_without_fabricating_unknown_tools():
    """按需启用与首轮工具上限独立，保留强制工具和 provider 字典且拒绝不存在的名称。"""
    selected = _make_tool("status", "运行状态")
    mandatory = _make_tool("read_skill", "读取技能")
    discovered = _make_tool("transfer_history", "整理历史")
    provider = {"type": "web_search_preview"}
    middleware = ToolSelectorMiddleware(
        selection_tools=[selected, mandatory, discovered],
        always_include=["read_skill"],
        max_tools=1,
        enable_discovery=True,
    )
    request = ModelRequest(
        model=_RecordingModel(responses=[AIMessage(content="完成")]),
        tools=[selected, mandatory, discovered, *middleware.tools, provider],
        messages=[HumanMessage(content="查询记录")],
        state={"selected_tool_names": ["status"], "discovered_tool_names": ["transfer_history", "fabricated"]},
        runtime=None,
    )

    async def handler(updated: ModelRequest) -> ModelRequest:
        """捕获最终模型绑定请求。"""
        return updated

    result = asyncio.run(middleware.awrap_model_call(request, handler))
    assert result.tools == [selected, discovered, mandatory, middleware.tools[0], provider]


def test_discovery_has_serializable_private_catalog_identity_and_injected_runtime():
    """严格目录可以稳定签名发现工具，模型参数中不暴露宿主运行时状态。"""
    middleware = ToolSelectorMiddleware(selection_tools=[], enable_discovery=True)
    catalog = ToolCatalogSnapshot.from_tools(middleware.tools, plugin_revision=1, factory_revision="test")
    assert catalog.require_unique() is catalog
    assert catalog.entries[0].source == "middleware:selection"
    assert set(middleware.tools[0].tool_call_schema.model_json_schema()["properties"]) == {"search", "limit"}
