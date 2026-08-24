import asyncio
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.agent import (  # pylint: disable=no-name-in-module
    AgentManager,
    _MessageTask,
    _async_start_processing_status,
)
from app.chain.message import MessageChain
from app.command import Command, _finish_command_processing_status
from app.modules.telegram import TelegramModule  # pylint: disable=no-name-in-module
from app.modules.telegram.telegram import Telegram
from app.runtime.config import global_vars
from app.schemas.types import NotificationChannel


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    """等待后台线程完成目标状态，避免用例依赖固定 sleep 时长。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _FakeTelegramBot:
    """记录 typing 调用的轻量 bot，避免后台线程与 Mock 内部锁交互。"""

    def __init__(self):
        """初始化调用记录和首个动作事件。"""
        self.chat_actions = []
        self.action_event = threading.Event()

    def send_chat_action(self, chat_id, action):
        """记录一次 typing 动作并唤醒等待者。"""
        self.chat_actions.append((chat_id, action))
        self.action_event.set()


class _BlockingTelegramBot:
    """模拟阻塞中的 Telegram SDK 请求。"""

    def __init__(self) -> None:
        """初始化进入和释放请求的事件屏障。"""
        self.entered = threading.Event()
        self.release = threading.Event()

    def send_chat_action(self, _chat_id, _action) -> None:
        """阻塞请求，直到测试显式释放。"""
        self.entered.set()
        self.release.wait(timeout=1)


def _telegram_client(bot=None) -> Telegram:
    """构造不连接外部服务且持有独立运行状态的 Telegram client。"""
    telegram = Telegram.__new__(Telegram)
    telegram._bot = bot or _FakeTelegramBot()
    telegram._telegram_token = "token"
    telegram._telegram_chat_id = "default-chat"
    telegram._user_chat_mapping = {}
    telegram._typing_tasks = {}
    telegram._typing_stop_flags = {}
    telegram._typing_lock = threading.RLock()
    telegram._typing_lifecycle_lock = threading.RLock()
    telegram._typing_accepting = True
    telegram._typing_join_timeout_seconds = 0.01
    # 缩短测试中的等待时间，不改变生产默认续发间隔。
    telegram._typing_interval_seconds = 0.01
    telegram._typing_max_duration_seconds = 1
    return telegram


def test_start_typing_can_stop_by_chat_id():
    telegram = _telegram_client()

    telegram._start_typing_task(
        "chat-1",
        max_duration_seconds=1,
        initial_delay_seconds=0,
    )

    assert "chat-1" in telegram._typing_tasks
    assert telegram._bot.action_event.wait(1.0)
    assert telegram.stop_typing(chat_id="chat-1")
    assert "chat-1" not in telegram._typing_tasks


def test_start_typing_can_stop_by_user_mapping():
    telegram = _telegram_client()
    telegram._user_chat_mapping["10001"] = "chat-2"

    telegram._start_typing_task(
        "chat-2",
        max_duration_seconds=1,
        initial_delay_seconds=0,
    )
    time.sleep(0.03)

    assert telegram.stop_typing(userid="10001")
    assert "chat-2" not in telegram._typing_tasks


def test_typing_task_has_max_duration_guard():
    telegram = _telegram_client()

    telegram._start_typing_task(
        "chat-3",
        max_duration_seconds=0.02,
        initial_delay_seconds=0,
    )

    assert _wait_until(lambda: "chat-3" not in telegram._typing_tasks)
    assert "chat-3" not in telegram._typing_tasks


def test_short_typing_task_can_stop_before_first_chat_action():
    """
    短响应在首次 typing 发出前结束时，不应留下客户端自然过期的残留状态。
    """
    telegram = _telegram_client()

    telegram._start_typing_task(
        "chat-4",
        max_duration_seconds=1,
        initial_delay_seconds=0.05,
    )
    telegram.stop_typing(chat_id="chat-4")
    time.sleep(0.08)

    assert telegram._bot.chat_actions == []
    assert "chat-4" not in telegram._typing_tasks


def test_typing_owner_is_isolated_between_config_instances():
    """不同 Telegram 配置即使 chat_id 相同也不得互相停止 typing。"""
    first = _telegram_client()
    second = _telegram_client()
    try:
        assert first._start_typing_task("shared-chat", initial_delay_seconds=0)
        assert second._start_typing_task("shared-chat", initial_delay_seconds=0)
        assert first._bot.action_event.wait(timeout=1)
        assert second._bot.action_event.wait(timeout=1)

        assert first.stop_typing(chat_id="shared-chat")

        assert "shared-chat" not in first._typing_tasks
        assert "shared-chat" in second._typing_tasks
    finally:
        first.stop_typing(chat_id="shared-chat")
        second.stop_typing(chat_id="shared-chat")


def test_typing_stop_keeps_blocked_thread_owner_until_terminal():
    """SDK 请求阻塞超过等待预算时，不得提前删除线程 owner。"""
    bot = _BlockingTelegramBot()
    telegram = _telegram_client(bot)
    try:
        assert telegram._start_typing_task("blocked-chat", initial_delay_seconds=0)
        assert bot.entered.wait(timeout=1)

        assert telegram._stop_typing_task("blocked-chat") is False
        owner = telegram._typing_tasks["blocked-chat"]
        assert owner.is_alive()
        assert telegram._start_typing_task(
            "blocked-chat", initial_delay_seconds=0
        ) is False

        bot.release.set()
        owner.join(timeout=1)
        assert not owner.is_alive()
        assert "blocked-chat" not in telegram._typing_tasks
    finally:
        bot.release.set()
        telegram.stop_typing(chat_id="blocked-chat")


def test_typing_start_failure_releases_registered_owner(monkeypatch):
    """线程启动失败时应清理已登记的 owner 和停止信号。"""
    telegram = _telegram_client()

    class FailingThread:
        """模拟登记成功后无法启动的线程对象。"""

        def __init__(self, **_kwargs) -> None:
            """接收真实 Thread 构造参数。"""

        def start(self) -> None:
            """模拟系统拒绝创建新线程。"""
            raise RuntimeError("thread start failed")

    monkeypatch.setattr("app.modules.telegram.telegram.threading.Thread", FailingThread)

    with pytest.raises(RuntimeError, match="thread start failed"):
        telegram._start_typing_task("failed-chat", initial_delay_seconds=0)

    assert telegram._typing_tasks == {}
    assert telegram._typing_stop_flags == {}


def test_typing_start_is_rejected_after_client_stop():
    """client 停止封口后不得再接受新的 typing 线程。"""
    telegram = _telegram_client()
    telegram._bot = None

    telegram.stop()

    assert telegram._start_typing_task("closed-chat", initial_delay_seconds=0) is False
    assert telegram._typing_tasks == {}


def test_agent_managed_send_msg_keeps_typing_for_worker_cleanup():
    telegram = _telegram_client()
    sent = SimpleNamespace(message_id=1, chat=SimpleNamespace(id="chat-1"))

    with patch.object(
            telegram, "_Telegram__send_request", return_value=sent
    ), patch.object(telegram, "_stop_typing_task") as stop_typing:
        result = telegram.send_msg(
            title="处理中",
            userid="10001",
            stop_typing=False,
        )

    assert result["success"]
    stop_typing.assert_not_called()


def test_send_msg_does_not_stop_typing_by_default():
    """
    响应发送不再默认结束 typing，由处理状态统一收口。
    """
    telegram = _telegram_client()
    sent = SimpleNamespace(message_id=1, chat=SimpleNamespace(id="chat-1"))

    with patch.object(
            telegram, "_Telegram__send_request", return_value=sent
    ), patch.object(telegram, "_stop_typing_task") as stop_typing:
        result = telegram.send_msg(title="处理中", userid="10001")

    assert result["success"]
    stop_typing.assert_not_called()


def test_telegram_module_processing_status_starts_typing():
    """
    Telegram 通过模块处理状态接口启动 typing 保活。
    """
    module = TelegramModule()
    module._channel = NotificationChannel.Telegram
    client = Mock()
    client.start_typing.return_value = True

    with patch.object(
            module, "get_config", return_value=SimpleNamespace(name="telegram-test")
    ), patch.object(module, "get_instance", return_value=client):
        status = module.mark_message_processing_started(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            chat_id="-100",
            text="hello",
        )

    client.start_typing.assert_called_once_with(chat_id="-100", userid="10001")
    assert status["metadata"]["kind"] == "typing"


def test_slash_command_defers_processing_status_to_command_handler():
    chain = MessageChain.__new__(MessageChain)
    chain.eventmanager = Mock()
    status = MessageChain._ProcessingStatus(
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        userid="10001",
        chat_id="-100",
        metadata={"kind": "typing"},
    )

    with patch.object(chain, "_record_user_message"), patch.object(
            chain, "_mark_message_processing_started", return_value=status
    ), patch.object(
            chain, "_mark_message_processing_finished"
    ) as finish_status:
        chain.handle_message(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="/sites",
            original_chat_id="-100",
        )

    finish_status.assert_not_called()
    chain.eventmanager.send_event.assert_called_once()
    assert (
        chain.eventmanager.send_event.call_args.args[1]["processing_status"]
        == status.to_dict()
    )


def test_command_handler_finishes_processing_status_after_execute():
    """
    传统命令响应完成后由命令处理器统一结束 processing status。
    """
    command = Command.__new__(Command)
    command.get = Mock(return_value={"func": Mock()})
    command.execute = Mock()
    event = SimpleNamespace(
        event_data={
            "cmd": "/sites",
            "user": "10001",
            "channel": NotificationChannel.Telegram,
            "source": "telegram-test",
            "processing_status": {
                "channel": NotificationChannel.Telegram.value,
                "source": "telegram-test",
                "userid": "10001",
                "chat_id": "-100",
                "metadata": {"kind": "typing"},
            },
        }
    )

    with patch("app.command._finish_command_processing_status") as finish_status:
        command.command_event(event)

    command.execute.assert_called_once()
    finish_status.assert_called_once_with(
        event.event_data["processing_status"],
        user_id="10001",
    )


def test_finish_command_processing_status_uses_module_interface():
    status = {
        "channel": NotificationChannel.Telegram.value,
        "source": "telegram-test",
        "userid": "10001",
        "chat_id": "-100",
        "metadata": {"kind": "typing"},
    }

    with patch("app.command.CommandChain") as chain_cls:
        _finish_command_processing_status(status, user_id="fallback")

    chain_cls.return_value.finish_message_processing_status.assert_called_once_with(
        status=status,
        userid="fallback",
    )


def test_async_agent_leaves_processing_status_to_worker():
    chain = MessageChain.__new__(MessageChain)
    chain.eventmanager = Mock()
    chain.runtime_config = replace(
        chain.runtime_config,
        ai_agent_enable=True,
    )

    loop = Mock(**{"is_running.return_value": True, "is_closed.return_value": False})
    with patch.object(global_vars, "CURRENT_EVENT_LOOP", loop), patch.object(
            chain, "_record_user_message"
    ), patch.object(
            chain, "_mark_message_processing_started"
    ) as start_status, patch(
            "app.chain.message.get_running_agent_manager",
    ) as get_running_manager, patch(
            "app.chain.message.asyncio.run_coroutine_threadsafe",
            side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
    ), patch.object(
            chain, "_mark_message_processing_finished"
    ) as finish_status:
        process_message = AsyncMock()
        get_running_manager.return_value.process_message = process_message
        chain.handle_message(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="/ai 搜索电影",
            original_chat_id="-100",
        )

    start_status.assert_not_called()
    finish_status.assert_not_called()
    process_message.assert_called_once()
    assert "processing_status" not in process_message.call_args.kwargs
    assert (
        process_message.call_args.kwargs["channel"]
        == NotificationChannel.Telegram.value
    )
    assert process_message.call_args.kwargs["source"] == "telegram-test"
    assert process_message.call_args.kwargs["original_chat_id"] == "-100"


def test_agent_manager_starts_processing_status_when_task_runs():
    async def _run():
        manager = AgentManager()
        task = _MessageTask(
            session_id="session-1",
            user_id="10001",
            message="第一条",
            channel=NotificationChannel.Telegram.value,
            source="telegram-test",
            original_chat_id="-100",
        )
        status = {
            "channel": NotificationChannel.Telegram.value,
            "source": "telegram-test",
            "userid": "10001",
            "chat_id": "-100",
            "metadata": {"kind": "typing"},
        }

        with patch(
                "app.agent.orchestrator._async_start_processing_status",
                new_callable=AsyncMock,
                return_value=status,
        ) as start_status:
            await manager._start_task_processing_status(task)

        start_status.assert_awaited_once_with(task)
        assert task.processing_status == status

    asyncio.run(_run())


def test_agent_start_processing_status_uses_chain_interface():
    async def _run():
        task = _MessageTask(
            session_id="session-1",
            user_id="10001",
            message="第一条",
            channel=NotificationChannel.Telegram.value,
            source="telegram-test",
            original_message_id="10",
            original_chat_id="-100",
        )
        status = {
            "channel": NotificationChannel.Telegram.value,
            "source": "telegram-test",
            "userid": "10001",
            "message_id": "10",
            "chat_id": "-100",
            "metadata": {"kind": "typing"},
        }
        calls = []

        class FakeAgentChain:
            """记录 processing status 请求的 Agent Chain 替身。"""

            def start_message_processing_status(self, **kwargs):
                """记录请求并返回预设状态。"""
                calls.append(kwargs)
                return status

        with patch("app.agent.orchestrator.AgentChain", FakeAgentChain):
            result = await _async_start_processing_status(task)

        assert calls == [{
            "channel": NotificationChannel.Telegram,
            "source": "telegram-test",
            "userid": "10001",
            "message_id": "10",
            "chat_id": "-100",
            "text": "第一条",
        }]
        assert result == status

    asyncio.run(_run())


def test_callback_stops_typing_when_message_handler_returns():
    chain = MessageChain.__new__(MessageChain)
    chain.eventmanager = Mock()
    status = MessageChain._ProcessingStatus(
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        userid="10001",
        chat_id="-100",
        metadata={"kind": "typing"},
    )

    with patch.object(chain, "_record_user_message"), patch.object(
            chain, "_mark_message_processing_started", return_value=status
    ), patch.object(chain, "_handle_message_core"), patch.object(
            chain, "_mark_message_processing_finished"
    ) as finish_status:
        chain.handle_message(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
            text="CALLBACK:sites:req-1:refresh",
            original_chat_id="-100",
        )

    finish_status.assert_called_once_with(
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        userid="10001",
        status=status,
        original_message_id=None,
        original_chat_id="-100",
    )


def test_chain_finishes_processing_through_module_interface():
    chain = MessageChain.__new__(MessageChain)
    status = MessageChain._ProcessingStatus(
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        userid="10001",
        chat_id="-100",
        metadata={"kind": "typing"},
    )

    with patch.object(chain, "finish_message_processing_status") as finish_status:
        chain._mark_message_processing_finished(
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            status=status,
            original_chat_id="-100",
        )

    finish_status.assert_called_once_with(
        status=status.to_dict(),
        channel=NotificationChannel.Telegram,
        source="telegram-test",
        userid="10001",
        message_id=None,
        chat_id="-100",
    )


def test_agent_manager_finishes_processing_status_after_each_task():
    async def _run():
        manager = AgentManager()
        status = {
            "channel": NotificationChannel.Telegram.value,
            "source": "telegram-test",
            "userid": "10001",
            "chat_id": "-100",
            "metadata": {"kind": "typing"},
        }
        task = _MessageTask(
            session_id="session-1",
            user_id="10001",
            message="第一条",
            processing_status=status,
        )

        with patch(
                "app.agent.orchestrator._async_finish_processing_status",
                new_callable=AsyncMock,
        ) as finish_status:
            await manager._finish_task_processing_status(task)

        finish_status.assert_awaited_once_with(status, "10001")
        assert task.processing_status is None

    asyncio.run(_run())


def test_agent_worker_starts_and_finishes_each_queued_task():
    async def _run():
        manager = AgentManager()
        manager._session_queues["session-1"] = asyncio.Queue()
        first_status = {
            "channel": NotificationChannel.Telegram.value,
            "source": "telegram-test",
            "userid": "10001",
            "chat_id": "-100",
            "metadata": {"kind": "typing", "seq": 1},
        }
        second_status = {
            "channel": NotificationChannel.Telegram.value,
            "source": "telegram-test",
            "userid": "10001",
            "chat_id": "-100",
            "metadata": {"kind": "typing", "seq": 2},
        }
        await manager._session_queues["session-1"].put(_MessageTask(
            session_id="session-1",
            user_id="10001",
            message="第一条",
            channel=NotificationChannel.Telegram.value,
            source="telegram-test",
            original_chat_id="-100",
        ))
        await manager._session_queues["session-1"].put(_MessageTask(
            session_id="session-1",
            user_id="10001",
            message="第二条",
            channel=NotificationChannel.Telegram.value,
            source="telegram-test",
            original_chat_id="-100",
        ))

        with patch(
                "app.agent.orchestrator._async_start_processing_status",
                new_callable=AsyncMock,
                side_effect=[first_status, second_status],
        ) as start_status, patch.object(
                manager,
                "_process_message_internal",
                new_callable=AsyncMock,
        ), patch(
                "app.agent.orchestrator._async_finish_processing_status",
                new_callable=AsyncMock,
        ) as finish_status:
            manager._session_workers["session-1"] = asyncio.create_task(
                manager._session_worker("session-1")
            )
            await manager._session_queues["session-1"].join()
            manager._session_workers["session-1"].cancel()
            await manager._session_workers["session-1"]

        assert start_status.await_count == 2
        assert finish_status.await_args_list[0].args == (first_status, "10001")
        assert finish_status.await_args_list[1].args == (second_status, "10001")

    asyncio.run(_run())
