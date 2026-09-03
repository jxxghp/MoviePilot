import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.agent.api.executor import ApiExecutionContext, MoviePilotApiExecutor
from app.agent.tools.base import format_tool_result_for_agent
from app.schemas.types import NotificationChannel


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


def test_executor_keeps_collection_total_visible_in_truncated_tool_preview() -> None:
    """列表内容过大时，精确总数必须位于截断预览开头供 Agent 直接使用。"""
    response = SimpleNamespace(
        status_code=200,
        headers={"X-Result-Count": "200", "X-Total-Count": "357"},
        json=lambda: {
            "success": True,
            "message": "",
            "data": [{"title": "x" * 1024} for _ in range(100)],
        },
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
        raw_result = asyncio.run(executor.execute("subscription.list"))

    result = json.loads(
        format_tool_result_for_agent(raw_result, tool_name="moviepilot_api")
    )

    assert result["tool_result_truncated"] is True
    assert result["content_preview"].startswith(
        '{"collection": {"result_count": 200, "total_count": 357}'
    )
    assert result["content_preview"].index('"total_count": 357') < result[
        "content_preview"
    ].index('"data"')
    response.aclose.assert_awaited_once()


def test_executor_serializes_channel_and_source_headers_as_ascii() -> None:
    """中文通知渠道和来源必须以 ASCII 形式发送到 Agent API。"""
    request_factory = MagicMock(return_value=SimpleNamespace(request=AsyncMock()))
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(
            user_id="1",
            username="admin",
            is_admin=True,
            channel=NotificationChannel.Wechat.value,
            source="企业微信",
        ),
        request_factory=request_factory,
    )

    with patch("app.agent.api.executor.create_access_token", return_value="token"):
        headers = executor._build_headers()

    assert headers["X-MoviePilot-Agent-Channel"] == "%E5%BE%AE%E4%BF%A1"
    assert headers["X-MoviePilot-Agent-Channel"].isascii()
    assert headers["X-MoviePilot-Agent-Source"] == "%E4%BC%81%E4%B8%9A%E5%BE%AE%E4%BF%A1"
    assert headers["X-MoviePilot-Agent-Source"].isascii()
