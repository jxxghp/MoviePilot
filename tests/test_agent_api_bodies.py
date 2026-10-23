"""
验证 API 请求体在 Agent、MCP 和直接工具入口实际发送时保持合同类型。
"""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent.api.executor import ApiExecutionContext, MoviePilotApiExecutor
from app.agent.tools.impl.api import MoviePilotApiTool
from app.agent.tools.manager import MoviePilotToolsManager


async def _invoke_gateway(
    entrypoint: str, gateway: MoviePilotApiTool, arguments: dict[str, Any],
) -> str:
    """
    从各公开入口进入同一真实网关，不跳过工具管理器的参数处理。

    :param entrypoint: 直接调用、LangChain 或 HTTP/MCP/CLI 共用管理器入口
    :param gateway: 仅替换 HTTP 传输的真实网关
    :param arguments: 模型或客户端提交的原始参数
    :return: 供调用方读取的 JSON 结果
    """
    if entrypoint == "langchain":
        return await gateway.ainvoke(arguments)
    if entrypoint == "manager":
        manager = MoviePilotToolsManager(session_id="body-test", user_id="1")
        manager.tools = [gateway]
        try:
            return await manager.call_tool(gateway.name, arguments)
        finally:
            await manager.close()
    return await gateway.run(**arguments)


@pytest.fixture
def api_transport(monkeypatch: pytest.MonkeyPatch) -> tuple[MoviePilotApiTool, AsyncMock]:
    """
    保留真实合同、权限和执行器，仅把令牌签发及 HTTP 出站替换为内存响应。
    """
    response = SimpleNamespace(
        status_code=200,
        headers={},
        json=lambda: {"success": True},
        aclose=AsyncMock(),
    )
    request = AsyncMock(return_value=response)
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(user_id="1", username="admin", is_admin=True),
        request_factory=Mock(return_value=SimpleNamespace(request=request)),
    )
    monkeypatch.setattr(executor, "_build_headers", lambda: {})
    monkeypatch.setattr(executor, "_resolve_base_url", lambda: "http://127.0.0.1:3001")
    gateway = MoviePilotApiTool(session_id="body-test", user_id="1", executor=executor)
    gateway.set_agent_context({"is_admin": True})
    return gateway, request


@pytest.mark.parametrize("entrypoint", ["direct", "langchain", "manager"])
@pytest.mark.parametrize("json_encoded", [False, True], ids=["native", "encoded"])
@pytest.mark.parametrize(
    ("operation_id", "body"),
    [
        ("download.add", {"torrent_in": {
            "title": "天气之子", "enclosure": "https://example.invalid/test.torrent",
            "description": "{\"原始副标题\": \"不应被递归解码\"}",
        }, "media_source": "tmdb", "media_id": "568160"}),
        ("transfer.history.delete", {"id": 7}),
        ("slash.run", {"command": "/help"}),
        ("media.classification.policy.preview", {"input": {
            "kind": "media", "media": {"title": "天气之子"},
        }}),
        ("site.priorities.update", [{"id": 3, "pri": 1}]),
        ("system.restart", None),
    ],
)
@pytest.mark.asyncio
async def test_gateway_sends_restored_body_to_http_transport(
    api_transport: tuple[MoviePilotApiTool, AsyncMock],
    entrypoint: str,
    json_encoded: bool,
    operation_id: str,
    body: Any,
) -> None:
    """
    对象、数组和空值必须实际按原生类型发送，不能只在校验副本中还原。
    """
    gateway, request = api_transport
    arguments = {
        "operation_id": operation_id,
        "body": json.dumps(body, ensure_ascii=False) if json_encoded else deepcopy(body),
    }
    original = deepcopy(arguments)

    result = await _invoke_gateway(entrypoint, gateway, arguments)

    assert json.loads(result)["success"] is True
    request.assert_awaited_once()
    assert request.call_args.kwargs["json"] == body
    assert arguments == original


@pytest.mark.parametrize("entrypoint", ["direct", "langchain", "manager"])
@pytest.mark.parametrize("json_encoded", [False, True], ids=["native", "encoded"])
@pytest.mark.asyncio
async def test_gateway_restores_get_body_before_moving_it_to_query(
    api_transport: tuple[MoviePilotApiTool, AsyncMock],
    entrypoint: str,
    json_encoded: bool,
) -> None:
    """
    兼容的 GET 对象请求体还原后才合并到查询参数，不得作为字符串发出。
    """
    gateway, request = api_transport
    body = {"page": 2, "count": 20}
    result = await _invoke_gateway(entrypoint, gateway, {
        "operation_id": "site.rss",
        "body": json.dumps(body) if json_encoded else body,
    })

    assert json.loads(result)["success"] is True
    request.assert_awaited_once()
    assert request.call_args.kwargs["json"] is None
    assert request.call_args.kwargs["params"] == body


@pytest.mark.parametrize("entrypoint", ["direct", "langchain", "manager"])
@pytest.mark.parametrize("body", [
    "not-json", "{", "true", "1", '"dev"', "null", "[]",
    json.dumps(json.dumps({"torrent_in": {"title": "双重编码"}})),
])
@pytest.mark.asyncio
async def test_gateway_rejects_invalid_bodies_before_http_request(
    api_transport: tuple[MoviePilotApiTool, AsyncMock], entrypoint: str, body: str,
) -> None:
    """
    兼容还原只处理一层，错误形状、标量和非法 JSON 仍须在出站前拒绝。
    """
    gateway, request = api_transport
    result = await _invoke_gateway(entrypoint, gateway, {
        "operation_id": "download.add", "body": body,
    })

    assert json.loads(result)["error"] == "invalid_input"
    request.assert_not_awaited()


@pytest.mark.parametrize("entrypoint", ["direct", "langchain", "manager"])
@pytest.mark.asyncio
async def test_gateway_preserves_exact_dev_literal(
    api_transport: tuple[MoviePilotApiTool, AsyncMock], entrypoint: str,
) -> None:
    """
    唯一合法的固定字符串请求体必须保持原值发送。
    """
    gateway, request = api_transport
    result = await _invoke_gateway(entrypoint, gateway, {
        "operation_id": "system.upgrade.dev", "body": "dev",
    })

    assert json.loads(result)["success"] is True
    request.assert_awaited_once()
    assert request.call_args.kwargs["json"] == "dev"
