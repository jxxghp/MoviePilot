"""
验证 Discord SDK 回调注册和同步发送回执的类型边界。
"""

import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from app.modules.discord.discord import Discord


def test_event_registration_preserves_sdk_dispatch_callbacks() -> None:
    """
    回调仍注册到真实 SDK 客户端，登录事件标记就绪且机器人消息不会进入宿主。
    """
    instance = Discord.__new__(Discord)
    instance._client = discord.Client(intents=discord.Intents.none())
    instance._ready_event = threading.Event()
    instance._registered_commands = None
    instance._post_to_ds = AsyncMock()
    instance._register_events()

    async def dispatch() -> None:
        """
        离线执行已注册的 SDK 事件并释放客户端资源。
        """
        try:
            await instance._client.on_ready()
            await instance._client.on_message(SimpleNamespace(author=SimpleNamespace(bot=True)))
            assert callable(instance._client.on_interaction)
        finally:
            await instance._client.close()

    asyncio.run(dispatch())
    assert instance._ready_event.is_set()
    assert instance._bot_user_id is None
    instance._post_to_ds.assert_not_awaited()


@pytest.mark.parametrize("method", ["send_msg", "send_file"])
def test_sync_send_preserves_message_receipt(monkeypatch, method: str) -> None:
    """
    同步入口应原样返回发送协程的消息回执，不把二元组转换成布尔值。
    """
    instance = Discord.__new__(Discord)
    instance._client = object()
    instance._loop = object()
    instance._ready_event = threading.Event()
    instance._ready_event.set()
    instance.get_state = Mock(return_value=True)
    receipt = (True, {"message_id": "123", "chat_id": "456"})

    def submit(coroutine, _loop):
        """
        关闭未调度协程并提供已完成回执，避免测试启动真实线程或网络请求。
        """
        coroutine.close()
        future = Future()
        future.set_result(receipt)
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    assert getattr(instance, method)("测试") is receipt
