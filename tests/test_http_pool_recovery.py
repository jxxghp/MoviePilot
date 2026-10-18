"""
共享异步 HTTP 池的连续超时恢复、在途响应和关闭边界回归。
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx2
import pytest
import pytest_asyncio
from httpcore2 import AsyncMockBackend

from app.adapters.network import http as http_module
from app.adapters.network.http import AsyncRequestUtils

URL = "https://raw.githubusercontent.com/demo/repo/main/package.v3.json"


class _ResponseStream(httpx2.AsyncByteStream):
    """
    记录响应体的生命周期，以便发现旧池关闭早于响应退出。
    """

    def __init__(self, transport: "_ControlledTransport") -> None:
        """
        把当前响应登记为仍在读取的流。
        """
        self.transport = transport
        self.closed = False
        transport.streams += 1

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """
        返回离线索引载荷，保持真实 HTTPX 流式读取路径。
        """
        assert not self.transport.closed
        yield b'{"Demo":{"version":"1.0"}}'

    async def aclose(self) -> None:
        """
        记录响应流关闭，允许断言它先于退役池关闭。
        """
        if not self.closed:
            self.closed = True
            self.transport.streams -= 1


class _ControlledTransport(httpx2.AsyncBaseTransport):
    """
    在传输边界注入取池超时，不访问真实代理或网络。
    """

    def __init__(self, **kwargs: Any) -> None:
        """
        保存新池的完整配置和请求记录，默认模拟无法分配连接。
        """
        self.kwargs = kwargs
        self.error: Optional[type[httpx2.RequestError]] = httpx2.PoolTimeout
        self.requests: list[httpx2.Request] = []
        self.streams = 0
        self.closed = False
        self.close_calls = 0
        self.close_error = False
        self.release_requests: Optional[asyncio.Event] = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """
        失败时保留真实异常类别，成功时交给真实客户端消费响应体。
        """
        assert not self.closed
        self.requests.append(request)
        if self.release_requests is not None:
            await self.release_requests.wait()
        if self.error is not None:
            raise self.error("injected pool state", request=request)
        return httpx2.Response(200, stream=_ResponseStream(self), request=request)

    async def aclose(self) -> None:
        """
        验证活跃响应未被中途关闭，并允许注入一次关闭失败。
        """
        self.close_calls += 1
        if self.close_error:
            self.close_error = False
            raise RuntimeError("injected close failure")
        assert self.streams == 0
        self.closed = True


@pytest_asyncio.fixture
async def pools(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[_ControlledTransport]]:
    """
    每个事件循环创建独立受控池，并在用例结束后回收全部托管任务。
    """
    created: list[_ControlledTransport] = []

    def create(**kwargs: Any) -> _ControlledTransport:
        """
        首个池模拟故障，替换池默认可正常返回响应。
        """
        transport = _ControlledTransport(**kwargs)
        if created:
            transport.error = None
        created.append(transport)
        return transport

    monkeypatch.setattr(http_module.httpx2, "AsyncHTTPTransport", create)
    try:
        yield created
    finally:
        await http_module.aclose_shared_async_transports()
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        assert not http_module._retired_async_transports.get(loop)
        assert not any(task.get_loop() is loop for task in http_module._pending_eviction_tasks)


async def _timeout(request: AsyncRequestUtils, *, stream: bool = False, method: str = "GET") -> None:
    """
    通过公开请求入口触发一次取池超时，并核对异常传播。
    """
    with pytest.raises(httpx2.PoolTimeout):
        if stream:
            async with request.get_stream(URL, raise_exception=True):
                pytest.fail("故障池不应返回响应")
        else:
            await request.request(method, URL, raise_exception=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_repeated_pool_timeouts_replace_only_failed_pool(pools, stream: bool) -> None:
    """
    普通和流式请求均在连续三次取池超时后恢复，保留代理及池限制配置。
    """
    request = AsyncRequestUtils(proxies="http://proxy.example:7890", timeout=15)
    for _ in range(2):
        await _timeout(request, stream=stream)
        assert len(pools) == 1
    await _timeout(request, stream=stream)
    await asyncio.sleep(0)
    assert len(pools) == 2
    assert pools[0].closed
    assert len(pools[0].requests) == 3
    assert pools[0].kwargs.keys() == pools[1].kwargs.keys()
    for key in ("proxy", "verify", "http2", "trust_env"):
        assert pools[0].kwargs[key] == pools[1].kwargs[key]
    assert vars(pools[0].kwargs["limits"]) == vars(pools[1].kwargs["limits"])
    async with request.get_stream(URL, raise_exception=True) as response:
        assert await response.aread() == b'{"Demo":{"version":"1.0"}}'
    assert len(pools) == 2


@pytest.mark.asyncio
async def test_retired_pool_drains_stream_and_limits_generations(pools) -> None:
    """
    退役池保留在途流；旧代未退出时，新代再超时也不会增加第三代。
    """
    request = AsyncRequestUtils()
    await _timeout(request)
    pools[0].error = None
    async with request.get_stream(URL, raise_exception=True) as original:
        pools[0].error = httpx2.PoolTimeout
        for _ in range(3):
            await _timeout(request)
        await asyncio.sleep(0)
        assert len(pools) == 2
        assert not pools[0].closed
        pools[1].error = httpx2.PoolTimeout
        for _ in range(4):
            await _timeout(request)
        assert len(pools) == 2
        assert await original.aread() == b'{"Demo":{"version":"1.0"}}'
    await asyncio.sleep(0)
    assert pools[0].closed
    await _timeout(request)
    assert len(pools) == 3
    assert await request.get(URL) is not None


@pytest.mark.asyncio
async def test_success_resets_consecutive_pool_failures(pools) -> None:
    """
    有请求成功获得连接就清零失败计数，避免零散拥塞触发重建。
    """
    request = AsyncRequestUtils()
    await _timeout(request)
    await _timeout(request)
    pools[0].error = None
    assert await request.get(URL) is not None
    pools[0].error = httpx2.PoolTimeout
    await _timeout(request)
    await _timeout(request)
    assert len(pools) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx2.ConnectTimeout, httpx2.ReadTimeout])
async def test_other_timeouts_do_not_replace_pool(pools, error) -> None:
    """
    网络连接和读取超时维持既有语义，不作为池状态故障触发重建。
    """
    request = AsyncRequestUtils()
    await _timeout(request)
    pools[0].error = error
    for _ in range(4):
        assert await request.get(URL) is None
    pools[0].error = httpx2.PoolTimeout
    await _timeout(request)
    await _timeout(request)
    assert len(pools) == 1


@pytest.mark.asyncio
async def test_post_is_not_replayed_and_false_exception_contract_is_preserved(pools) -> None:
    """
    恢复只影响后续请求，POST 不重放，默认失败仍返回 None。
    """
    request = AsyncRequestUtils()
    for _ in range(3):
        assert await request.request("POST", URL) is None
    assert len(pools[0].requests) == 3
    assert all(request.method == "POST" for request in pools[0].requests)
    assert await request.request("POST", URL) is not None
    assert len(pools[1].requests) == 1


@pytest.mark.asyncio
async def test_caller_owned_client_is_not_replaced(pools) -> None:
    """
    调用方自管客户端的池和关闭权保持不变。
    """
    transport = _ControlledTransport()
    async with httpx2.AsyncClient(transport=transport) as client:
        request = AsyncRequestUtils(client=client)
        for _ in range(4):
            await _timeout(request)
        assert not transport.closed
        assert not pools
    assert transport.closed


@pytest.mark.asyncio
async def test_close_failure_retains_old_generation_until_shutdown_retry(pools) -> None:
    """
    退役关闭失败时保留 owner 阻止继续轮换，并由关闭入口重试回收。
    """
    request = AsyncRequestUtils()
    await _timeout(request)
    pools[0].close_error = True
    await _timeout(request)
    await _timeout(request)
    await asyncio.sleep(0)
    assert not pools[0].closed
    pools[1].error = httpx2.PoolTimeout
    for _ in range(3):
        await _timeout(request)
    assert len(pools) == 2
    await http_module.aclose_shared_async_transports()
    assert pools[0].closed
    assert pools[0].close_calls == 2


@pytest.mark.asyncio
async def test_registry_shutdown_keeps_retired_pool_for_explicit_close(pools, monkeypatch) -> None:
    """
    任务登记器停止接收后，归还借用仍保留原异常，并把旧池交给显式关闭入口。
    """
    from app.runtime.tasks import TaskRegistry

    registry = TaskRegistry()
    assert await registry.shutdown()
    monkeypatch.setattr(http_module, "get_task_registry", lambda: registry)
    request = AsyncRequestUtils()
    for _ in range(3):
        await _timeout(request)
    assert not pools[0].closed
    assert await request.get(URL) is not None
    await http_module.aclose_shared_async_transports()
    assert pools[0].closed


@pytest.mark.asyncio
async def test_plugin_index_recovers_through_real_streaming_entry(pools, monkeypatch) -> None:
    """
    插件索引调用真实异步读取路径，在共享池更换后下一次查询成功。
    """
    from app.adapters.external.plugin.client import PluginMarketTransport

    monkeypatch.setattr(
        PluginMarketTransport,
        "_build_github_request_strategies",
        lambda **_kwargs: [("代理", URL, {"proxies": "http://proxy.example:7890"})],
    )
    transport = PluginMarketTransport()
    for _ in range(3):
        with pytest.raises(RuntimeError, match="连接失败"):
            await transport._fetch_plugin_index_async(URL, None)
    assert await transport._fetch_plugin_index_async(URL, None) == {"Demo": {"version": "1.0"}}
    assert len(pools) == 2


@pytest.mark.asyncio
async def test_concurrent_old_generation_failures_do_not_rotate_replacement(pools) -> None:
    """
    同一波旧代请求延迟返回超时时只轮换一次，不误退役后继池。
    """
    request = AsyncRequestUtils()
    await _timeout(request)
    pools[0].release_requests = asyncio.Event()
    tasks = [asyncio.create_task(request.request("POST", URL)) for _ in range(8)]
    await asyncio.sleep(0)
    assert len(pools[0].requests) == 9
    pools[0].release_requests.set()
    assert await asyncio.gather(*tasks) == [None] * 8
    await asyncio.sleep(0)
    assert len(pools) == 2
    assert pools[0].closed
    assert len(pools[1].requests) == 0
    assert await request.get(URL) is not None


@pytest.mark.asyncio
async def test_cancelled_stream_releases_retired_pool(pools) -> None:
    """
    持有旧流的任务被取消后也会归还借用，退役池最终关闭。
    """
    request = AsyncRequestUtils()
    await _timeout(request)
    pools[0].error = None
    entered = asyncio.Event()

    async def hold_stream() -> None:
        """
        持有真实客户端流上下文直到测试取消任务。
        """
        async with request.get_stream(URL, raise_exception=True):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_stream())
    try:
        await entered.wait()
        pools[0].error = httpx2.PoolTimeout
        for _ in range(3):
            await _timeout(request)
        assert not pools[0].closed
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await asyncio.sleep(0)
    assert pools[0].closed


@pytest.mark.asyncio
async def test_recovery_isolated_by_transport_configuration(pools) -> None:
    """
    一个配置桶退役时，其他配置仍复用原池。
    """
    failing = AsyncRequestUtils(http2=True)
    healthy = AsyncRequestUtils(http2=False)
    await _timeout(failing)
    assert await healthy.get(URL) is not None
    await _timeout(failing)
    await _timeout(failing)
    assert len(pools) == 3
    assert await healthy.get(URL) is not None
    assert len(pools[1].requests) == 2
    assert not pools[1].closed


@pytest.mark.asyncio
async def test_real_connect_pool_recovers_without_interrupting_active_response(monkeypatch) -> None:
    """
    用真实 HTTPX/httpcore CONNECT 单连接池制造排队超时，验证新池恢复及旧流排空。

    只在网络后端回放字节，不替换池分配、超时异常或客户端关闭逻辑。
    """
    original_transport = httpx2.AsyncHTTPTransport
    created: list[httpx2.AsyncHTTPTransport] = []

    def create(**kwargs: Any) -> httpx2.AsyncHTTPTransport:
        """
        保留真实代理池，仅将 TCP/TLS I/O 替换为离线 CONNECT 响应。
        """
        transport = original_transport(**kwargs)
        transport._pool._network_backend = AsyncMockBackend([
            b"HTTP/1.1 200 Connection established\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello",
        ])
        created.append(transport)
        return transport

    monkeypatch.setattr(http_module.httpx2, "AsyncHTTPTransport", create)
    request = AsyncRequestUtils(
        proxies="http://proxy.example:7890", http2=False, max_connections=1,
    )
    try:
        async with request.get_stream(URL, raise_exception=True) as original:
            for _ in range(3):
                with pytest.raises(httpx2.PoolTimeout):
                    await request.request(
                        "GET", URL, raise_exception=True,
                        timeout=httpx2.Timeout(1, pool=0.01),
                    )
            assert len(created) == 2
            assert len(created[0]._pool.connections) == 1
            assert await request.get(URL) == "hello"
            assert await original.aread() == b"hello"
        await asyncio.sleep(0)
        assert created[0]._pool.connections == []
    finally:
        await http_module.aclose_shared_async_transports()
    assert created[1]._pool.connections == []
