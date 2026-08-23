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
