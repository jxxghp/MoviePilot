import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.api.executor import ApiExecutionContext, MoviePilotApiExecutor


def _execute_with_headers(headers: dict[str, str]) -> tuple[dict, AsyncMock]:
    """用内存 HTTP 响应执行一次固定 API operation。"""
    response = SimpleNamespace(
        status_code=200,
        headers=headers,
        json=lambda: {"success": True, "message": "", "data": [{"id": 1}]},
        aclose=AsyncMock(),
    )
    request = AsyncMock(return_value=response)
    request_factory = MagicMock(return_value=SimpleNamespace(request=request))
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(
            user_id="1",
            username="admin",
            is_admin=True,
        ),
        request_factory=request_factory,
    )

    with patch("app.agent.api.executor.create_access_token", return_value="token"):
        result = asyncio.run(executor.execute("subscription.list"))

    return json.loads(result), response.aclose


def test_executor_exposes_exact_collection_total_when_api_reports_it() -> None:
    """Agent 结果应把精确数量响应头变成可直接读取的集合元数据。"""
    result, close = _execute_with_headers(
        {
            "X-Result-Count": "20",
            "X-Total-Count": "57",
            "X-Page": "2",
            "X-Page-Size": "20",
        }
    )

    assert result["data"] == [{"id": 1}]
    assert result["collection"] == {
        "result_count": 20,
        "total_count": 57,
        "page": 2,
        "count": 20,
    }
    close.assert_awaited_once()


def test_executor_does_not_invent_total_for_upstream_window() -> None:
    """外部接口未报告总数时，Agent 元数据只能包含当前窗口数量。"""
    result, close = _execute_with_headers(
        {
            "X-Result-Count": "20",
            "X-Page": "3",
            "X-Page-Size": "20",
        }
    )

    assert result["collection"] == {
        "result_count": 20,
        "page": 3,
        "count": 20,
    }
    assert "total_count" not in result["collection"]
    close.assert_awaited_once()


def test_executor_keeps_non_collection_payload_unchanged_without_headers() -> None:
    """非列表 API 未返回数量头时必须保持既有输出形状。"""
    result, close = _execute_with_headers({})

    assert result == {"success": True, "message": "", "data": [{"id": 1}]}
    close.assert_awaited_once()
