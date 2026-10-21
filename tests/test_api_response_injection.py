"""
验证旧直接调用默认值与 FastAPI 响应注入保持兼容。
"""

import importlib
import inspect
import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute


def _response_endpoints() -> list[tuple[str, str]]:
    """
    对每个已登记的响应默认值兼容入口执行真实路由检查。
    """
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / "tests/fixtures/architecture/mypy-policy.json").read_text())
    return [(entry["path"][:-3].replace("/", "."), entry["owner"])
            for entry in policy["ignores"] if entry["reason"] == "fastapi-response-default"]


@pytest.mark.parametrize(("module_name", "endpoint_name"), _response_endpoints())
def test_response_default_retains_framework_injection(module_name: str, endpoint_name: str) -> None:
    """
    None 默认值仍允许旧直接调用，HTTP 入口必须注入 Response 而不是生成请求字段。
    """
    module = importlib.import_module(module_name)
    endpoint = getattr(module, endpoint_name)
    assert inspect.signature(endpoint).parameters["response"].default is None
    routes = [route for route in module.router.routes
              if isinstance(route, APIRoute) and inspect.unwrap(route.endpoint) is inspect.unwrap(endpoint)]
    assert routes
    for route in routes:
        assert route.dependant.response_param_name == "response"
        assert all(field.name != "response" for field in [*route.dependant.query_params, *route.dependant.body_params])
