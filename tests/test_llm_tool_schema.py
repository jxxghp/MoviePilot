"""OpenAI 兼容请求中函数工具参数 schema 的规整契约测试。

Pydantic 为 `Optional[T]` 字段生成不带顶层 `type` 的 `anyOf`；把 OpenAI tools
转成 Gemini `functionDeclaration` 的网关不处理 `anyOf`，整次 Agent 请求会被 400
拒绝。这里用真实的 langchain-openai 请求构造与真实的 Agent 工具入参模型，固定
请求上线前的 schema 规整行为。
"""

import asyncio
import copy
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

from app.agent.llm import helper as llm_module
from app.agent.middleware.skills import SkillToolInput
from app.agent.middleware.subagents import _SubAgentControlInput
from app.agent.tools.impl.api import MoviePilotApiInput
from app.agent.tools.impl.view_image import ViewImageInput

_PATCH_MARKER = "_moviepilot_test_tool_schema_patched"
_TOOL_INPUTS = {
    "read_skill": SkillToolInput,
    "subagent": _SubAgentControlInput,
    "moviepilot_api": MoviePilotApiInput,
    "view_image": ViewImageInput,
}


def _build_model(model_name: str, **kwargs: Any) -> ChatOpenAI:
    """构造带独立补丁的 ChatOpenAI 子类实例，避免修补真实类或跨用例残留。"""

    class _PatchedChatOpenAI(ChatOpenAI):
        """承载本用例工具 schema 补丁的模型类。"""

    llm_module._patch_tool_schema_request_support(
        _PatchedChatOpenAI,
        patch_marker=_PATCH_MARKER,
    )
    return _PatchedChatOpenAI(
        model=model_name,
        api_key="sk-test",
        base_url="https://gateway.example/v1",
        **kwargs,
    )


def _build_tools() -> list[StructuredTool]:
    """用真实 Agent 工具入参模型构造 LangChain 工具。"""

    async def _noop(**_kwargs: Any) -> str:
        return ""

    return [
        StructuredTool.from_function(
            coroutine=_noop,
            name=name,
            description=f"{name} tool",
            args_schema=args_schema,
        )
        for name, args_schema in _TOOL_INPUTS.items()
    ]


def _request_tools(model: ChatOpenAI, **bind_kwargs: Any) -> tuple[list[dict], list[dict]]:
    """返回绑定后的原始工具定义与请求 payload 中的最终工具定义。"""
    bound = model.bind_tools(_build_tools(), **bind_kwargs)
    bound_tools = bound.kwargs["tools"]
    snapshot = copy.deepcopy(bound_tools)
    payload = model._get_request_payload([HumanMessage("hi")], **bound.kwargs)
    assert bound_tools == snapshot, "规整不得修改已绑定的工具定义"
    return bound_tools, payload["tools"]


def _parameters(tools: list[dict], name: str) -> dict:
    """按名称取 Chat Completions 或 Responses 格式工具的参数 schema。"""
    for tool in tools:
        function = tool.get("function", tool)
        if function.get("name") == name:
            return function["parameters"]
    raise AssertionError(f"缺少工具 {name}")


def _iter_schema_nodes(schema: dict, path: str = ""):
    """遍历 properties/items/additionalProperties 位置上的全部 schema 节点。"""
    for name, child in (schema.get("properties") or {}).items():
        yield f"{path}.{name}", child
        yield from _iter_schema_nodes(child, f"{path}.{name}")
    for keyword in ("items", "additionalProperties"):
        child = schema.get(keyword)
        if isinstance(child, dict):
            yield f"{path}[{keyword}]", child
            yield from _iter_schema_nodes(child, f"{path}[{keyword}]")


def test_optional_fields_collapse_to_single_type_for_any_model() -> None:
    """Optional[T] 字段折叠为 T，保留字段描述、默认值与分支约束。"""
    _, tools = _request_tools(_build_model("gpt-4o"))

    skill = _parameters(tools, "read_skill")
    assert skill["properties"]["file"] == {
        "type": "string",
        "default": None,
        "description": SkillToolInput.model_fields["file"].description,
    }
    assert skill["required"] == ["name"]

    subagent = _parameters(tools, "subagent")["properties"]
    assert subagent["description"]["type"] == "string"
    assert subagent["timeout_ms"]["type"] == "integer"
    assert subagent["tasks"]["type"] == "array"
    assert subagent["tasks"]["items"]["properties"]["description"]["type"] == "string"

    image_data = _parameters(tools, "view_image")["properties"]["image_data"]
    assert image_data["type"] == "string"
    assert "anyOf" not in image_data


def test_true_unions_keep_their_semantics_for_non_gemini_models() -> None:
    """非 Gemini 模型只做无损折叠，多类型联合保持原样。"""
    bound_tools, tools = _request_tools(_build_model("gpt-4o"))

    body = _parameters(tools, "moviepilot_api")["properties"]["body"]
    assert body == _parameters(bound_tools, "moviepilot_api")["properties"]["body"]
    assert {variant.get("type") for variant in body["anyOf"]} == {
        "object",
        "array",
        "string",
        "null",
    }


@pytest.mark.parametrize("model_name", ["gemini-2.5-flash", "google/Gemini-2.5-Pro"])
def test_every_schema_node_is_typed_for_gemini_models(model_name: str) -> None:
    """Gemini 模型的每个参数节点都必须带 type，且不再依赖 anyOf/oneOf。"""
    _, tools = _request_tools(_build_model(model_name))

    for name in _TOOL_INPUTS:
        for path, node in _iter_schema_nodes(_parameters(tools, name)):
            assert isinstance(node.get("type"), str), f"{name}{path} 缺少 type: {node}"
            assert "anyOf" not in node and "oneOf" not in node, f"{name}{path}"

    body = _parameters(tools, "moviepilot_api")["properties"]["body"]
    assert body["type"] == "object"
    assert "additionalProperties" not in body
    assert body["description"] == MoviePilotApiInput.model_fields["body"].description


def test_responses_api_function_tools_are_normalized() -> None:
    """Responses API 的扁平函数工具格式同样在上线前规整。"""
    model = _build_model("gemini-2.5-flash", use_responses_api=True)

    _, tools = _request_tools(model)

    assert tools[0]["type"] == "function"
    assert "function" not in tools[0]
    assert _parameters(tools, "read_skill")["properties"]["file"]["type"] == "string"
    assert _parameters(tools, "moviepilot_api")["properties"]["body"]["type"] == "object"


def test_strict_tools_are_left_unchanged() -> None:
    """strict 工具依赖 anyOf 表达可空必填字段，规整不得改变其语义。"""
    bound_tools, tools = _request_tools(_build_model("gemini-2.5-flash"), strict=True)

    assert tools == bound_tools


def test_normalization_is_idempotent_when_patched_twice() -> None:
    """重复修补同一模型类不会重复包装请求构造。"""

    class _PatchedChatOpenAI(ChatOpenAI):
        """验证修补标记的模型类。"""

    llm_module._patch_tool_schema_request_support(_PatchedChatOpenAI, patch_marker=_PATCH_MARKER)
    patched = _PatchedChatOpenAI._get_request_payload
    llm_module._patch_tool_schema_request_support(_PatchedChatOpenAI, patch_marker=_PATCH_MARKER)

    assert _PatchedChatOpenAI._get_request_payload is patched


def test_openai_runtime_applies_tool_schema_patch(monkeypatch) -> None:
    """OpenAI 兼容运行时构造的模型类必须挂上工具 schema 规整。"""

    class _FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.model_name = kwargs["model"]
            self.profile = {"tool_calling": True}

        def _get_request_payload(self, input_, *, stop=None, **kwargs):
            return {"tools": kwargs.get("tools", [])}

    monkeypatch.setitem(sys.modules, "langchain_openai", SimpleNamespace(ChatOpenAI=_FakeChatOpenAI))
    monkeypatch.setattr(llm_module, "_patch_openai_responses_instructions_support", lambda: None)

    model = asyncio.run(
        llm_module.LLMHelper.get_llm(
            provider="openai",
            model="gemini-2.5-flash",
            api_key="sk-test",
            base_url="https://gateway.example/v1",
        )
    )
    tool = {
        "type": "function",
        "function": {
            "name": "read_skill",
            "parameters": {
                "type": "object",
                "properties": {"file": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
            },
        },
    }

    payload = model._get_request_payload([], tools=[tool])

    assert payload["tools"][0]["function"]["parameters"]["properties"]["file"] == {"type": "string"}
