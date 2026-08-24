import asyncio
import threading
from typing import Optional
from unittest.mock import AsyncMock

import pytest

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


class _BlockingTypingChannelStub:
    """模拟阻塞中的 Discord typing 请求。"""

    def __init__(self) -> None:
        """初始化请求进入与释放屏障。"""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def trigger_typing(self) -> None:
        """阻塞请求，直到测试释放或 owner 被取消。"""
        self.entered.set()
        await self.release.wait()


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
    instance._typing_lifecycle_lock = asyncio.Lock()
    instance._typing_accepting = True
    instance._typing_stop_timeout_seconds = 0.01
    return instance


def _typing_discord() -> Discord:
    """构造绑定当前测试循环且不连接外部服务的 typing client。"""
    instance = Discord.__new__(Discord)
    instance._loop = asyncio.get_running_loop()
    instance._typing_tasks = {}
    instance._typing_stop_events = {}
    instance._typing_lifecycle_lock = asyncio.Lock()
    instance._typing_accepting = True
    instance._typing_interval_seconds = 0.01
    instance._typing_initial_delay_seconds = 0
    instance._typing_max_duration_seconds = 1
    instance._typing_stop_timeout_seconds = 0.01
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


@pytest.mark.anyio
async def test_short_typing_task_can_stop_before_first_trigger(monkeypatch) -> None:
    """短响应在首发前结束时，不应留下 Discord 客户端 typing 状态。"""
    discord_client = _typing_discord()
    channel = AsyncMock()
    channel.trigger_typing = AsyncMock()
    monkeypatch.setattr(
        discord_client,
        "_resolve_channel",
        AsyncMock(return_value=channel),
    )

    started = await discord_client._start_typing_task(
        typing_key="chat:30003",
        chat_id="30003",
        max_duration_seconds=1,
        initial_delay_seconds=0.05,
    )
    stopped = await discord_client._stop_typing_task("chat:30003")
    await asyncio.sleep(0.08)

    assert started
    assert stopped
    channel.trigger_typing.assert_not_called()
    assert "chat:30003" not in discord_client._typing_tasks


@pytest.mark.anyio
async def test_typing_stop_retains_blocked_owner_until_terminal(monkeypatch) -> None:
    """typing 请求阻塞超过预算时，不得删除或覆盖仍运行的 task owner。"""
    discord_client = _typing_discord()
    channel = _BlockingTypingChannelStub()
    resolve_channel = AsyncMock(return_value=channel)
    monkeypatch.setattr(discord_client, "_resolve_channel", resolve_channel)

    try:
        assert await discord_client._start_typing_task(
            typing_key="chat:blocked",
            chat_id="blocked",
        )
        await asyncio.wait_for(channel.entered.wait(), timeout=1)
        owner = discord_client._typing_tasks["chat:blocked"]

        assert await discord_client._stop_typing_task("chat:blocked")
        assert discord_client._typing_tasks["chat:blocked"] is owner
        assert not owner.done()
        assert not await discord_client._start_typing_task(
            typing_key="chat:blocked",
            chat_id="blocked",
        )
        assert discord_client._typing_tasks["chat:blocked"] is owner

        channel.release.set()
        await asyncio.wait_for(owner, timeout=1)
        assert "chat:blocked" not in discord_client._typing_tasks
    finally:
        channel.release.set()
        await discord_client._stop_all_typing_tasks()


@pytest.mark.anyio
async def test_stop_all_seals_and_drains_typing_owners(monkeypatch) -> None:
    """client shutdown 必须封住新增任务并取消、等待既有 owner 进入终态。"""
    discord_client = _typing_discord()
    channel = _BlockingTypingChannelStub()
    resolve_channel = AsyncMock(return_value=channel)
    monkeypatch.setattr(discord_client, "_resolve_channel", resolve_channel)

    assert await discord_client._start_typing_task(
        typing_key="chat:shutdown",
        chat_id="shutdown",
    )
    await asyncio.wait_for(channel.entered.wait(), timeout=1)
    owner = discord_client._typing_tasks["chat:shutdown"]

    await discord_client._stop_all_typing_tasks()

    assert owner.done()
    assert owner.cancelled()
    assert discord_client._typing_tasks == {}
    assert discord_client._typing_stop_events == {}
    resolve_channel.reset_mock()
    assert not await discord_client._start_typing_task(
        typing_key="chat:after-stop",
        chat_id="after-stop",
    )
    resolve_channel.assert_not_awaited()
