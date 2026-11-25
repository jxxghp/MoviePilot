"""
临时事件循环退出时共享异步 HTTP 连接池的回收回归。

复现 jxxghp/MoviePilot#7580：插件在临时循环（asyncio.run）里调用
AsyncRequestUtils 后，循环绑定的共享 transport 从不关闭，每次泄漏一个
连接；WeakKeyDictionary 的桶也因值链持有循环引用而无法被 GC 回收。
"""

import asyncio
import gc
import threading
from typing import Any, Callable

import httpx2
import pytest
import pytest_asyncio

from app.adapters.network import http as http_module
from app.adapters.network.http import AsyncRequestUtils

URL = "https://example.invalid/"


class _RecordingTransport(httpx2.AsyncBaseTransport):
    """
    记录请求与关闭次数的受控 transport，不访问真实网络。

    :param kwargs: 创建共享 transport 时传入的配置，仅记录不使用
    """

    def __init__(self, **kwargs: Any) -> None:
        """
        保存创建参数并初始化关闭计数。

        :param kwargs: 创建共享 transport 时传入的配置，仅记录不使用
        """
        self.kwargs = kwargs
        self.requests: list[httpx2.Request] = []
        self.closed = False
        self.close_calls = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """
        直接返回 200 空响应，不访问真实网络。

        :param request: 待处理的 HTTP 请求
        :return: 状态 200 的响应
        """
        self.requests.append(request)
        return httpx2.Response(200, content=b"ok", request=request)

    async def aclose(self) -> None:
        """
        记录一次关闭调用。
        """
        self.close_calls += 1
        self.closed = True


@pytest_asyncio.fixture
async def transports(monkeypatch: pytest.MonkeyPatch) -> Any:
    """
    把共享 transport 的创建替换为受控替身，并在用例结束时断言无残留桶。

    :param monkeypatch: 隔离 transport 创建并在用例结束后恢复的测试夹具
    :return: 按创建顺序记录的受控 transport 列表
    """
    created: list[_RecordingTransport] = []

    def create(**kwargs: Any) -> _RecordingTransport:
        """
        创建一个受控 transport 并记录。

        :param kwargs: 创建共享 transport 时传入的配置
        :return: 新建的受控 transport
        """
        transport = _RecordingTransport(**kwargs)
        created.append(transport)
        return transport

    monkeypatch.setattr(http_module.httpx2, "AsyncHTTPTransport", create)
    try:
        yield created
    finally:
        # 临时循环的桶应已被看门任务弹出；此处只做断言，不代替生产关闭路径。
        await http_module.aclose_shared_async_transports()
        await asyncio.sleep(0)
        assert not http_module._shared_async_transports
        loop = asyncio.get_running_loop()
        assert not http_module._retired_async_transports.get(loop)
        assert not any(
            task.get_loop() is loop for task in http_module._pending_eviction_tasks
        )


def _run_in_temp_loop(coro_factory: Callable[[], Any]) -> None:
    """
    在辅助线程的新事件循环里跑完一个协程（asyncio.run）。

    pytest 用例自身已在事件循环内，不能直接调用 asyncio.run，因此放到
    无运行循环的辅助线程里执行。

    :param coro_factory: 在辅助线程内构造待运行协程的工厂
    """
    thread = threading.Thread(target=lambda: asyncio.run(coro_factory()))
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive()


async def _fetch_once() -> httpx2.Response:
    """
    走生产路径发一次异步 GET。

    :return: 受控 transport 返回的 200 响应
    """
    response = await AsyncRequestUtils().get_res(URL)
    assert response is not None
    assert response.status_code == 200
    return response


def test_temp_loop_exit_closes_shared_transport(transports: list[_RecordingTransport]) -> None:
    """
    验证临时循环退出后共享 transport 被关闭且无残留桶。

    :param transports: 受控 transport 记录列表
    """
    _run_in_temp_loop(_fetch_once)
    assert len(transports) == 1
    assert transports[0].close_calls == 1
    assert transports[0].closed


def test_runner_loop_exit_closes_shared_transport(transports: list[_RecordingTransport]) -> None:
    """
    验证 asyncio.Runner 退出时共享 transport 同样被关闭（Issue 建议的第二条路径）。

    :param transports: 受控 transport 记录列表
    """
    def _run_with_runner() -> None:
        """
        在辅助线程里用 asyncio.Runner 跑完一次请求后退出。
        """
        with asyncio.Runner() as runner:
            runner.run(_fetch_once())

    thread = threading.Thread(target=_run_with_runner)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert len(transports) == 1
    assert transports[0].close_calls == 1
    assert transports[0].closed


def test_two_temp_loops_close_their_own_transports(transports: list[_RecordingTransport]) -> None:
    """
    验证两个临时循环各自关闭自己的 transport，看门任务不跨循环复用。

    :param transports: 受控 transport 记录列表
    """
    _run_in_temp_loop(_fetch_once)
    _run_in_temp_loop(_fetch_once)
    assert len(transports) == 2
    assert [transport.close_calls for transport in transports] == [1, 1]
    assert all(transport.closed for transport in transports)


def test_resident_loop_reuses_pool_and_explicit_close_cancels_watchdog(
    transports: list[_RecordingTransport],
) -> None:
    """
    验证常驻循环复用同一连接池，显式关闭后看门任务消失、循环可干净关闭。

    :param transports: 受控 transport 记录列表
    """
    loop = asyncio.new_event_loop()
    loop_errors: list[dict[str, Any]] = []

    def _record_loop_error(loop_arg: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        """
        收集常驻循环的异常处理回调（如被销毁的 pending 任务）。

        :param loop_arg: 报告异常的事件循环
        :param context: 异常上下文
        """
        loop_errors.append(context)

    loop.set_exception_handler(_record_loop_error)

    def _run() -> None:
        """
        在辅助线程里驱动常驻循环。
        """
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:

        async def _watchdog_present() -> bool:
            """
            检查当前循环上是否存在共享 transport 看门任务。

            :return: 看门任务存在时返回 True
            """
            return (
                http_module._find_shared_async_transport_watchdog(
                    asyncio.get_running_loop()
                )
                is not None
            )

        for _ in range(2):
            future = asyncio.run_coroutine_threadsafe(_fetch_once(), loop)
            response = future.result(timeout=30)
            assert response.status_code == 200
        # 两次请求复用同一个池，看门任务只挂一个且尚未关闭池。
        assert len(transports) == 1
        assert transports[0].close_calls == 0
        assert asyncio.run_coroutine_threadsafe(_watchdog_present(), loop).result(timeout=30)

        asyncio.run_coroutine_threadsafe(
            http_module.aclose_shared_async_transports(), loop
        ).result(timeout=30)
        assert transports[0].close_calls == 1
        assert transports[0].closed
        # 显式关闭后看门任务已取消，循环关闭时不再有 pending 任务。
        assert not asyncio.run_coroutine_threadsafe(_watchdog_present(), loop).result(timeout=30)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()
    gc.collect()
    assert not loop_errors
    assert not http_module._shared_async_transports


@pytest.mark.asyncio
async def test_ensure_watchdog_is_idempotent() -> None:
    """
    验证同一循环重复挂看门任务时只保留一个（_ensure 的早返回分支）。
    """
    loop = asyncio.get_running_loop()
    http_module._ensure_shared_async_transport_watchdog(loop)
    first = http_module._find_shared_async_transport_watchdog(loop)
    assert first is not None
    try:
        http_module._ensure_shared_async_transport_watchdog(loop)
        assert http_module._find_shared_async_transport_watchdog(loop) is first
    finally:
        # 看门任务挂在测试循环上且无对应桶，手动取消以免残留 pending 任务。
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first


def test_temp_loop_without_watchdog_leaks_but_explicit_close_still_works(
    transports: list[_RecordingTransport], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    对照用例：禁用看门任务时临时循环退出不关闭池（桶残留）。

    证明关闭动作确实由看门任务触发，而非 AsyncClient 退出时顺带关闭；
    池本身直接关闭仍可回收，漏的只是触发时机。

    :param transports: 受控 transport 记录列表
    :param monkeypatch: 隔离看门任务创建的测试夹具
    """
    monkeypatch.setattr(
        http_module, "_ensure_shared_async_transport_watchdog", lambda loop: None
    )
    created_loops: list[asyncio.AbstractEventLoop] = []

    async def _fetch_and_capture_loop() -> None:
        """
        发一次请求并记下当前临时循环，供用例结束时显式关闭。
        """
        created_loops.append(asyncio.get_running_loop())
        await _fetch_once()

    _run_in_temp_loop(_fetch_and_capture_loop)
    assert len(transports) == 1
    assert transports[0].close_calls == 0
    # 桶仍残留在全局字典中（看门任务被禁用，循环退出后无人弹出）。
    assert len(http_module._shared_async_transports) == 1
    assert created_loops[0] in http_module._shared_async_transports

    # 循环已关闭，无法再恢复执行；直接关闭池本身，证明池可关、漏的是触发时机。
    assert created_loops[0].is_closed()
    try:
        asyncio.run(transports[0].aclose())
        assert transports[0].close_calls == 1
    finally:
        # 清掉本用例故意留下的残留桶，不污染后续用例。
        with http_module._shared_async_transports_lock:
            http_module._shared_async_transports.pop(created_loops[0], None)
