"""系统内部用户消息分发与异步立即发送的上下文传播测试。"""

# ruff: noqa: E402 - 可选三方模块必须在导入消息链前完成隔离。

import asyncio
from unittest.mock import patch

import pytest

from app.testing.bootstrap import ensure_optional_stub

# 仅在可选依赖缺失时补占位；已安装时保留真实模块，避免污染同一进程里导入真实类型的用例。
ensure_optional_stub("qbittorrentapi", TorrentFilesList=list)
ensure_optional_stub("transmission_rpc", File=object)
ensure_optional_stub("psutil")

from app.application.messaging.message import MessageQueueManager
from app.chain.message import MessageChain
from app.foundation.identity import (
    SYSTEM_INTERNAL_USER_ID,
    is_internal_user_id,
    normalize_internal_user_id,
)
from app.runtime.correlation import correlation_scope, get_correlation_id
from app.schemas.message import Message


def _internal_message() -> Message:
    """构造以系统内部用户为收件人的消息。"""
    return Message(
        userid=SYSTEM_INTERNAL_USER_ID,
        username="admin",
        title="后台报告",
        text="任务完成",
    )


def test_internal_userid_identity_helpers() -> None:
    """系统内部用户标识的判定与归一化。"""
    assert is_internal_user_id(SYSTEM_INTERNAL_USER_ID)
    assert is_internal_user_id(" System ")
    assert normalize_internal_user_id(SYSTEM_INTERNAL_USER_ID) is None
    assert normalize_internal_user_id("10001") == "10001"


def test_post_message_normalizes_internal_userid_before_queueing() -> None:
    """入队前应把系统内部用户归一化为空，不进入事件与队列载荷。"""
    chain = MessageChain()
    message = _internal_message()

    with patch("app.chain._messaging.MessageTemplateHelper.render", return_value=message), patch.object(
        chain.messagehelper, "put"
    ), patch.object(chain.messageoper, "add"), patch.object(
        chain.eventmanager, "send_event"
    ) as send_event, patch.object(
        chain.user_repository,
        "get_notification_settings",
        return_value={"telegram_userid": "admin-1"},
    ), patch.object(
        chain.messagequeue, "send_message"
    ) as send_message:
        chain.post_message(message)

    event_payload = send_event.call_args.kwargs["data"]
    queued_message = send_message.call_args.kwargs["message"]

    assert event_payload["userid"] is None
    assert queued_message.userid is None
    assert send_message.call_args.kwargs["immediately"] is False


def test_send_direct_message_normalizes_internal_userid() -> None:
    """直接发送时同样归一化系统内部用户。"""
    chain = MessageChain()

    with patch.object(chain, "run_module") as run_module:
        chain.send_direct_message(_internal_message())

    sent_message = run_module.call_args.kwargs["message"]
    assert sent_message.userid is None


class _FakeLoop:
    """同步模拟 asyncio 的 executor 提交，并记录提交时的上下文。"""

    def __init__(self, loop):
        self.called = False
        self.loop = loop
        self.submission_correlation_id = None

    def run_in_executor(self, _executor, func, *args):
        """立即执行 worker 调用并返回已完成 Future。"""
        self.called = True
        self.submission_correlation_id = get_correlation_id()
        future = self.loop.create_future()
        try:
            func(*args)
        except BaseException as error:  # pylint: disable=broad-exception-caught
            future.set_exception(error)
        else:
            future.set_result(None)
        return future


@pytest.mark.asyncio
async def test_async_send_message_uses_executor_for_immediate_send() -> None:
    """异步立即发送不能在事件循环里直接执行同步渠道回调。"""
    manager = MessageQueueManager()
    fake_loop = _FakeLoop(asyncio.get_running_loop())
    observed = []
    with patch(
        "asyncio.get_running_loop",
        return_value=fake_loop,
    ), patch.object(
        manager,
        "_send",
        side_effect=lambda *_args, **_kwargs: observed.append(get_correlation_id()),
    ) as send:
        with correlation_scope("message-request"):
            await manager.async_send_message("payload", immediately=True)

    assert fake_loop.called
    assert fake_loop.submission_correlation_id is None
    assert observed == ["message-request"]
    send.assert_called_once_with("payload")


@pytest.mark.asyncio
async def test_async_send_message_preserves_call_context() -> None:
    """异步立即发送的同步渠道回调应保留当前请求关联 ID。"""
    observed = []
    manager = MessageQueueManager()
    with patch.object(
        manager,
        "_send",
        side_effect=lambda *_args, **_kwargs: observed.append(get_correlation_id()),
    ):
        with correlation_scope("message-request"):
            await manager.async_send_message("payload", immediately=True)

    assert observed == ["message-request"]
