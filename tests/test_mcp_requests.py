"""
MCP JSON-RPC 请求格式错误的协议回归。
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import mcp


@pytest.mark.parametrize(
    ("body", "request_id"),
    [
        (None, None),
        ([], None),
        ([{"jsonrpc": "2.0", "method": "ping", "id": 1}], None),
        ("invalid", None),
        (1, None),
        (True, None),
        ({}, None),
        ({"jsonrpc": "1.0", "id": 7}, 7),
    ],
)
def test_invalid_request_returns_jsonrpc_error(body: object, request_id: object) -> None:
    """
    非对象及错误版本的请求须返回 400 协议错误，不能在取 id 时崩溃。
    """
    app = FastAPI()
    app.include_router(mcp.router, prefix="/mcp")
    app.dependency_overrides[mcp.verify_apikey] = lambda: "test-api-key"

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            content=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
