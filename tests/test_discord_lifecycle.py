import asyncio
import threading
from typing import Optional

from app.modules.discord.discord import Discord


class _DiscordClientStub:
    """模拟 Discord 长连接，允许测试控制启动失败与正常关闭。"""

    def __init__(self, *, start_error: Optional[Exception] = None) -> None:
        self.started = threading.Event()
        self._release: Optional[asyncio.Event] = None
        self._start_error = start_error
        self.close_calls = 0

    async def start(self, _token: str) -> None:
        self._release = asyncio.Event()
        self.started.set()
        if self._start_error:
            raise self._start_error
        await self._release.wait()

    async def close(self) -> None:
        self.close_calls += 1
        if self._release:
            self._release.set()


class _YieldingCloseDiscordClientStub(_DiscordClientStub):
    """关闭协程至少让出一次执行权，用于覆盖启动窗口内的停止竞态。"""

    def __init__(self) -> None:
        super().__init__()
        self.closed = threading.Event()

    async def close(self) -> None:
        self.close_calls += 1
        await asyncio.sleep(0)
        self.closed.set()


def _discord(client: _DiscordClientStub) -> Discord:
    """构造只包含线程与事件循环生命周期状态的 Discord 实例。"""
    instance = Discord.__new__(Discord)
    instance._token = "test-token"
    instance._client = client
    instance._loop = asyncio.new_event_loop()
    instance._thread = None
    instance._stop_requested = threading.Event()
    instance._ready_event = threading.Event()
    instance._typing_tasks = {}
    instance._typing_stop_events = {}
    return instance


def _cleanup(instance: Discord) -> None:
    """即使用例断言失败也回收其线程和事件循环。"""
    thread = instance._thread
    loop = instance._loop
    if thread and thread.is_alive():
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
    if not loop.is_closed():
        loop.close()


def test_stop_waits_for_discord_thread_and_closes_loop() -> None:
    """正常停止返回时，Discord 线程与其事件循环必须已经结束。"""
    instance = _discord(_DiscordClientStub())
    instance._start()
    assert instance._client.started.wait(timeout=1)

    try:
        instance.stop()

        assert not instance._thread or not instance._thread.is_alive()
        assert instance._loop.is_closed()
        instance.stop()
    finally:
        _cleanup(instance)


def test_start_failure_closes_discord_thread_loop() -> None:
    """Discord 启动协程失败后不得留下空跑线程和未关闭循环。"""
    instance = _discord(_DiscordClientStub(start_error=RuntimeError("invalid token")))
    instance._start()
    assert instance._client.started.wait(timeout=1)

    try:
        assert instance._thread
        instance._thread.join(timeout=1)
        assert not instance._thread.is_alive()
        assert instance._loop.is_closed()
    finally:
        _cleanup(instance)


def test_stop_during_thread_bootstrap_preserves_runner_cleanup(monkeypatch) -> None:
    """线程已登记但循环尚未运行时，停止请求不得打断 runner 的关闭流程。"""
    client = _YieldingCloseDiscordClientStub()
    instance = _discord(client)
    runner_entered = threading.Event()
    release_runner = threading.Event()
    original_set_event_loop = asyncio.set_event_loop

    def block_runner(loop: asyncio.AbstractEventLoop | None) -> None:
        if loop is instance._loop:
            runner_entered.set()
            assert release_runner.wait(timeout=1)
        original_set_event_loop(loop)

    monkeypatch.setattr(
        "app.modules.discord.discord.asyncio.set_event_loop",
        block_runner,
    )
    instance._start()
    assert runner_entered.wait(timeout=1)
    stop_thread = threading.Thread(target=instance.stop)
    stop_thread.start()
    assert instance._stop_requested.wait(timeout=1)
    release_runner.set()

    try:
        stop_thread.join(timeout=2)
        assert not stop_thread.is_alive()
        assert client.closed.is_set()
        assert instance._loop.is_closed()
    finally:
        release_runner.set()
        _cleanup(instance)
