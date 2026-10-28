"""
验证 Agent API 执行器的本地调用、结果投影和分页总量语义。
"""

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

import httpx
import pytest

from app.agent.api.executor import ApiExecutionContext, MoviePilotApiExecutor
from app.agent.tools.base import format_tool_result_for_agent
from app.agent.tools.manager import MoviePilotToolsManager
from app.schemas.types import NotificationChannel


@pytest.mark.parametrize("app_domain", ["", "https://custom.domain.example:5443/moviepilot/"])
@pytest.mark.parametrize(
    ("host", "port", "base_url"),
    [
        (None, 3001, "http://127.0.0.1:3001"),
        ("", 3001, "http://127.0.0.1:3001"),
        ("0.0.0.0", 13001, "http://127.0.0.1:13001"),
        ("127.0.0.1", 3001, "http://127.0.0.1:3001"),
        ("192.0.2.10", 13001, "http://192.0.2.10:13001"),
        ("localhost", 13001, "http://localhost:13001"),
        ("::", 13001, "http://[::1]:13001"),
        ("[::]", 13001, "http://[::1]:13001"),
        ("::1", 3001, "http://[::1]:3001"),
        ("[::1]", 3001, "http://[::1]:3001"),
        ("2001:db8::10", 13001, "http://[2001:db8::10]:13001"),
        ("[2001:db8::10]", 13001, "http://[2001:db8::10]:13001"),
    ],
)
def test_executor_requests_local_listener_independently_of_public_domain(
    app_domain: str,
    host: Optional[str],
    port: int,
    base_url: str,
) -> None:
    """
    配置对外反代域名后，API 仍须按本机监听地址和端口发送且绕过环境代理。
    """
    runtime_settings = {"APP_DOMAIN": app_domain, "HOST": host, "PORT": port}
    response = SimpleNamespace(
        status_code=200,
        headers={},
        json=lambda: {"success": True, "data": []},
        aclose=AsyncMock(),
    )
    request = AsyncMock(return_value=response)
    request_factory = MagicMock(return_value=SimpleNamespace(request=request))
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(user_id="1", username="admin", is_admin=True),
        request_factory=request_factory,
    )

    with (
        patch("app.agent.api.executor.get_runtime_setting", side_effect=runtime_settings.get),
        patch("app.agent.api.executor.create_access_token", return_value="token"),
    ):
        result = asyncio.run(executor.execute("subscription.list", query={"page": 1}))

    assert json.loads(result) == {"success": True, "data": []}
    request.assert_awaited_once_with(
        method="GET",
        url=f"{base_url}/api/v1/subscribe/",
        params={"page": 1},
        json=None,
        raise_exception=True,
    )
    assert request_factory.call_args.kwargs["headers"]["Authorization"] == "Bearer token"
    assert request_factory.call_args.kwargs["trust_env"] is False
    response.aclose.assert_awaited_once()


def _execute_with_headers(headers: dict[str, str]) -> tuple[dict, AsyncMock]:
    """
    用内存 HTTP 响应执行一次固定 API operation。
    """
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
    """
    Agent 结果应把精确数量响应头变成可直接读取的集合元数据。
    """
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
    """
    外部接口未报告总数时，Agent 元数据只能包含当前窗口数量。
    """
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
    """
    非列表 API 未返回数量头时必须保持既有输出形状。
    """
    result, close = _execute_with_headers({})

    assert result == {"success": True, "message": "", "data": [{"id": 1}]}
    close.assert_awaited_once()


def _execute_and_get_request_timeout(
    operation_id: str,
    *,
    path_params: Optional[dict] = None,
    query: Optional[dict] = None,
) -> float:
    """
    执行一个内存 API 请求并返回构造 HTTP 客户端时采用的超时。
    """
    response = SimpleNamespace(
        status_code=200,
        headers={},
        json=lambda: {"success": True},
        aclose=AsyncMock(),
    )
    request_factory = MagicMock(
        return_value=SimpleNamespace(request=AsyncMock(return_value=response))
    )
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(user_id="1", username="admin", is_admin=True),
        request_factory=request_factory,
    )

    with patch("app.agent.api.executor.create_access_token", return_value="token"):
        asyncio.run(
            executor.execute(
                operation_id,
                path_params=path_params,
                query=query,
            )
        )

    return request_factory.call_args.kwargs["timeout"]


def test_executor_extends_request_timeout_only_for_search_torrents() -> None:
    """
    慢速种子搜索允许等待 290 秒，其它 operation 保持原有 30 秒。
    """
    assert _execute_and_get_request_timeout(
        "search.torrents",
        path_params={"media_id": "1423191"},
        query={"media_source": "themoviedb"},
    ) == 290
    assert _execute_and_get_request_timeout(
        "search.title",
        query={"keyword": "生化危机：爆发夜"},
    ) == 30
    assert _execute_and_get_request_timeout("subscription.list") == 30


def test_manager_default_session_id_is_http_header_compatible() -> None:
    """
    HTTP API 工具管理器生成的默认会话标识必须是可发送的字符串。
    """
    manager = MoviePilotToolsManager(user_id="1")
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(
            user_id="1",
            username="admin",
            is_admin=True,
            session_id=manager.session_id,
        )
    )

    with patch("app.agent.api.executor.create_access_token", return_value="token"):
        headers = executor._build_headers()

    session_header = headers["X-MoviePilot-Agent-Session"]
    assert isinstance(manager.session_id, str)
    assert isinstance(session_header, str)
    assert session_header == manager.session_id


def test_executor_keeps_collection_total_visible_in_truncated_tool_preview() -> None:
    """
    列表内容过大时，精确总数必须位于截断预览开头供 Agent 直接使用。
    """
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
    """
    中文通知渠道和来源必须以 ASCII 形式发送到 Agent API。
    """
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


def _build_context_headers(session_id: str) -> dict[str, str]:
    """
    用指定会话标识构造一次 Agent API 请求头。

    :param session_id: 待写入会话头的 Agent 会话标识
    :return: 执行器生成的完整请求头
    """
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(
            user_id="1",
            username="admin",
            is_admin=True,
            session_id=session_id,
        )
    )

    with patch("app.agent.api.executor.create_access_token", return_value="token"):
        return executor._build_headers()


def test_executor_serializes_non_ascii_session_header_as_ascii() -> None:
    """
    微信等渠道的中文会话标识必须编码为 ASCII 头值，并可还原回原标识。
    """
    session_id = 'user_message:["wechat","wc1","张三的微信"]_1793057820'

    headers = _build_context_headers(session_id)

    session_header = headers["X-MoviePilot-Agent-Session"]
    assert session_header.isascii()
    assert unquote(session_header) == session_id
    # httpx 在发送前按 ASCII 编码头值，非 ASCII 会话标识会直接抛 UnicodeEncodeError。
    httpx.Headers(headers)


def test_executor_keeps_ascii_session_header_verbatim() -> None:
    """
    默认随机生成的会话标识不含需转义字符，编码后必须保持原值以兼容既有读取方。
    """
    session_id = uuid.uuid4().hex

    headers = _build_context_headers(session_id)

    assert headers["X-MoviePilot-Agent-Session"] == session_id


@pytest.mark.parametrize("channel,channel_user_id", [
    (NotificationChannel.Telegram.value, "123456789"),
    (NotificationChannel.Wechat.value, "张三"),
    (NotificationChannel.Telegram.value, None),
    (None, "123456789"),
])
def test_executor_signs_channel_user_into_host_token(channel, channel_user_id) -> None:
    """
    渠道发起人只写入宿主令牌的签名声明，不再以可伪造的请求头透传。
    """
    executor = MoviePilotApiExecutor(
        context=ApiExecutionContext(
            user_id="1",
            username="admin",
            is_admin=True,
            channel=channel,
            channel_user_id=channel_user_id,
        ),
        request_factory=MagicMock(return_value=SimpleNamespace(request=AsyncMock())),
    )

    with patch("app.agent.api.executor.create_access_token", return_value="token") as create_token:
        headers = executor._build_headers()

    signed = bool(channel and channel_user_id)
    assert create_token.call_args.kwargs.get("agent_channel") == (channel if signed else None)
    assert create_token.call_args.kwargs.get("agent_channel_user") == (channel_user_id if signed else None)
    assert not any(name.lower() == "x-moviepilot-agent-user" for name in headers)


def test_public_executor_never_signs_channel_user() -> None:
    """
    公共只读执行器即使收到渠道上下文，也不能把个人发起人绑定到宿主身份。
    """
    executor = MoviePilotApiExecutor(context=ApiExecutionContext(
        user_id="1", username="admin", is_admin=True, public_read_only=True,
        channel=NotificationChannel.Telegram.value, channel_user_id="123456789",
    ))
    with patch("app.agent.api.executor.create_access_token", return_value="token") as create_token:
        executor._build_headers()

    create_token.assert_called_once_with(userid=1, username="admin", super_user=True)
