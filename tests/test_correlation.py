"""请求关联 ID 在 HTTP、线程、事件和外部请求边界的传播测试。"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import httpx2
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.adapters.web.correlation import CorrelationIdMiddleware
from app.runtime.correlation import (
    CORRELATION_ID_HEADER,
    call_with_correlation,
    correlation_scope,
    get_correlation_id,
    normalize_correlation_id,
)
from app.runtime.event.dispatch import EventDispatcher
from app.runtime.events import Event
from app.runtime.execution import run_in_threadpool
from app.runtime.log import CustomFormatter, configure_correlation_id_provider
from app.schemas.types import EventType


def _correlation_app() -> Starlette:
    """构造同时包含普通响应和 SSE 风格流式响应的最小应用。"""

    async def current_id(_request):
        """返回当前协程看到的关联 ID。"""
        await asyncio.sleep(0)
        return JSONResponse({"request_id": get_correlation_id()})

    async def stream_id(_request):
        """在响应开始后读取关联 ID，验证上下文覆盖完整流生命周期。"""
        async def content():
            """生成一条 SSE 数据。"""
            await asyncio.sleep(0)
            yield f"data: {get_correlation_id()}\n\n"

        return StreamingResponse(content(), media_type="text/event-stream")

    app = Starlette(
        routes=[
            Route("/id", current_id),
            Route("/stream", stream_id),
        ]
    )
    app.add_middleware(CorrelationIdMiddleware)
    return app


@pytest.mark.asyncio
async def test_concurrent_requests_keep_isolated_ids_and_stream_context() -> None:
    """并发请求及流式响应必须各自保留入口 ID。"""
    transport = httpx.ASGITransport(app=_correlation_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            client.get("/id", headers={CORRELATION_ID_HEADER: "request-one"}),
            client.get("/id", headers={CORRELATION_ID_HEADER: "request-two"}),
        )
        stream = await client.get(
            "/stream", headers={CORRELATION_ID_HEADER: "stream-request"}
        )

    assert first.json()["request_id"] == first.headers[CORRELATION_ID_HEADER]
    assert second.json()["request_id"] == second.headers[CORRELATION_ID_HEADER]
    assert first.headers[CORRELATION_ID_HEADER] != second.headers[CORRELATION_ID_HEADER]
    assert stream.headers[CORRELATION_ID_HEADER] == "stream-request"
    assert stream.text == "data: stream-request\n\n"
    assert get_correlation_id() is None


@pytest.mark.asyncio
async def test_invalid_request_id_is_replaced_and_threadpool_copies_context() -> None:
    """日志注入型入口值被替换，线程池仍读取替换后的安全 ID。"""
    transport = httpx.ASGITransport(app=_correlation_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/id", headers={CORRELATION_ID_HEADER: "bad id!"}
        )

    generated = response.headers[CORRELATION_ID_HEADER]
    assert generated != "bad id!"
    assert normalize_correlation_id(generated) == generated
    with correlation_scope("thread-request"):
        observed = await run_in_threadpool(get_correlation_id)
    assert observed == "thread-request"


def test_event_dispatch_restores_producer_correlation_id() -> None:
    """后台事件处理器使用事件生产时固化的 ID，而非消费者线程上下文。"""
    observed = []

    def handler(_event):
        """记录处理器实际看到的关联 ID。"""
        observed.append(get_correlation_id())

    resolver = MagicMock()
    resolver.resolve.return_value = (
        handler,
        SimpleNamespace(
            owner_name="test", run_sync_in_threadpool=False, instance_key=None
        ),
        "Handler",
        "handle",
    )
    registry = MagicMock()
    dispatcher = EventDispatcher(
        registry=registry,
        binding_resolver=resolver,
        executor=MagicMock(),
        event_loop=MagicMock(),
        event_factory=Event,
        error_handler=MagicMock(),
    )
    with correlation_scope("producer-request"):
        event = Event(EventType.SystemError, {})
    with correlation_scope("consumer-request"):
        dispatcher.invoke_sync(handler, event)

    assert event.correlation_id == "producer-request"
    assert observed == ["producer-request"]
    assert get_correlation_id() is None


def test_sync_external_request_and_formatter_receive_correlation_id() -> None:
    """同步外呼头和结构化日志字段使用同一个当前 ID。"""
    configure_correlation_id_provider(get_correlation_id)
    response = httpx.Response(200)
    session = MagicMock()
    session.request.return_value = response
    formatter = CustomFormatter("%(correlation_id)s %(message)s")
    record = logging.LogRecord("test", logging.INFO, "", 0, "message", (), None)

    with correlation_scope("outgoing-request"):
        RequestUtils(session=session).request("GET", "https://example.com")
        rendered = formatter.format(record)

    headers = session.request.call_args.kwargs["headers"]
    assert headers[CORRELATION_ID_HEADER] == "outgoing-request"
    assert rendered == "outgoing-request message"


def test_process_entry_restores_serialized_correlation_id() -> None:
    """多进程入口使用显式 payload 恢复 ID，不依赖 fork 偶然继承上下文。"""
    with correlation_scope("parent-request"):
        observed = call_with_correlation(
            "serialized-request",
            get_correlation_id,
            (),
            {},
        )

    assert observed == "serialized-request"


@pytest.mark.asyncio
async def test_async_external_request_preserves_explicit_header() -> None:
    """异步外呼默认传播当前 ID，但不得覆盖调用方显式 trace 边界。"""
    observed = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        """记录 MockTransport 收到的请求头。"""
        observed.append(request.headers[CORRELATION_ID_HEADER])
        return httpx2.Response(200)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        utils = AsyncRequestUtils(client=client)
        with correlation_scope("context-request"):
            await utils.request("GET", "https://example.com/default")
            await utils.request(
                "GET",
                "https://example.com/explicit",
                headers={CORRELATION_ID_HEADER: "explicit-request"},
            )

    assert observed == ["context-request", "explicit-request"]
