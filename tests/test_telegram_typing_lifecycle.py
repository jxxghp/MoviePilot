import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


from app.agent import AgentManager, _MessageTask, _async_start_processing_status
from app.chain.message import MessageChain
from app.command import Command, _finish_command_processing_status
from app.modules.telegram import TelegramModule
from app.modules.telegram.telegram import Telegram
from app.schemas.types import MessageChannel


class TestTelegramTypingLifecycle(unittest.TestCase):
    def setUp(self):
        self._cleanup_typing_tasks()

    def tearDown(self):
        self._cleanup_typing_tasks()

    @staticmethod
    def _cleanup_typing_tasks():
        helper = Telegram.__new__(Telegram)
        for chat_id in list(Telegram._typing_tasks.keys()):
            helper._stop_typing_task(chat_id)
        Telegram._typing_tasks.clear()
        Telegram._typing_stop_flags.clear()
        Telegram._user_chat_mapping.clear()

    @staticmethod
    def _telegram_client() -> Telegram:
        telegram = Telegram.__new__(Telegram)
        telegram._bot = Mock()
        telegram._telegram_token = "token"
        telegram._telegram_chat_id = "default-chat"
        # 缩短测试中的等待时间，不改变生产默认续发间隔。
        telegram._typing_interval_seconds = 0.01
        telegram._typing_max_duration_seconds = 1
        return telegram

    def test_start_typing_can_stop_by_chat_id(self):
        telegram = self._telegram_client()

        telegram._start_typing_task(
            "chat-1",
            max_duration_seconds=1,
            initial_delay_seconds=0,
        )
        time.sleep(0.03)

        self.assertIn("chat-1", Telegram._typing_tasks)
        self.assertTrue(telegram._bot.send_chat_action.called)
        self.assertTrue(telegram.stop_typing(chat_id="chat-1"))
        self.assertNotIn("chat-1", Telegram._typing_tasks)

    def test_start_typing_can_stop_by_user_mapping(self):
        telegram = self._telegram_client()
        Telegram._user_chat_mapping["10001"] = "chat-2"

        telegram._start_typing_task(
            "chat-2",
            max_duration_seconds=1,
            initial_delay_seconds=0,
        )
        time.sleep(0.03)

        self.assertTrue(telegram.stop_typing(userid="10001"))
        self.assertNotIn("chat-2", Telegram._typing_tasks)

    def test_typing_task_has_max_duration_guard(self):
        telegram = self._telegram_client()

        telegram._start_typing_task(
            "chat-3",
            max_duration_seconds=0.02,
            initial_delay_seconds=0,
        )
        time.sleep(0.08)

        self.assertNotIn("chat-3", Telegram._typing_tasks)

    def test_short_typing_task_can_stop_before_first_chat_action(self):
        """
        短响应在首次 typing 发出前结束时，不应留下客户端自然过期的残留状态。
        """
        telegram = self._telegram_client()

        telegram._start_typing_task(
            "chat-4",
            max_duration_seconds=1,
            initial_delay_seconds=0.05,
        )
        telegram.stop_typing(chat_id="chat-4")
        time.sleep(0.08)

        telegram._bot.send_chat_action.assert_not_called()
        self.assertNotIn("chat-4", Telegram._typing_tasks)

    def test_agent_managed_send_msg_keeps_typing_for_worker_cleanup(self):
        telegram = self._telegram_client()
        sent = SimpleNamespace(message_id=1, chat=SimpleNamespace(id="chat-1"))

        with patch.object(
                telegram, "_Telegram__send_request", return_value=sent
        ), patch.object(telegram, "_stop_typing_task") as stop_typing:
            result = telegram.send_msg(
                title="处理中",
                userid="10001",
                stop_typing=False,
            )

        self.assertTrue(result["success"])
        stop_typing.assert_not_called()

    def test_send_msg_does_not_stop_typing_by_default(self):
        """
        响应发送不再默认结束 typing，由处理状态统一收口。
        """
        telegram = self._telegram_client()
        sent = SimpleNamespace(message_id=1, chat=SimpleNamespace(id="chat-1"))

        with patch.object(
                telegram, "_Telegram__send_request", return_value=sent
        ), patch.object(telegram, "_stop_typing_task") as stop_typing:
            result = telegram.send_msg(title="处理中", userid="10001")

        self.assertTrue(result["success"])
        stop_typing.assert_not_called()

    def test_telegram_module_processing_status_starts_typing(self):
        """
        Telegram 通过模块处理状态接口启动 typing 保活。
        """
        module = TelegramModule()
        module._channel = MessageChannel.Telegram
        client = Mock()
        client.start_typing.return_value = True

        with patch.object(
                module, "get_config", return_value=SimpleNamespace(name="telegram-test")
        ), patch.object(module, "get_instance", return_value=client):
            status = module.mark_message_processing_started(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                chat_id="-100",
                text="hello",
            )

        client.start_typing.assert_called_once_with(chat_id="-100", userid="10001")
        self.assertEqual(status["metadata"]["kind"], "typing")

    def test_slash_command_defers_processing_status_to_command_handler(self):
        chain = MessageChain.__new__(MessageChain)
        chain.eventmanager = Mock()
        status = MessageChain._ProcessingStatus(
            channel=MessageChannel.Telegram,
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
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="/sites",
                original_chat_id="-100",
            )

        finish_status.assert_not_called()
        chain.eventmanager.send_event.assert_called_once()
        self.assertEqual(
            chain.eventmanager.send_event.call_args.args[1]["processing_status"],
            status.to_dict(),
        )

    def test_command_handler_finishes_processing_status_after_execute(self):
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
                "channel": MessageChannel.Telegram,
                "source": "telegram-test",
                "processing_status": {
                    "channel": MessageChannel.Telegram.value,
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

    def test_finish_command_processing_status_uses_module_interface(self):
        status = {
            "channel": MessageChannel.Telegram.value,
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

    def test_async_agent_leaves_processing_status_to_worker(self):
        chain = MessageChain.__new__(MessageChain)

        with patch.object(chain, "_record_user_message"), patch.object(
                chain, "_mark_message_processing_started"
        ) as start_status, patch(
                "app.chain.message.settings.AI_AGENT_ENABLE", True
        ), patch(
                "app.chain.message.agent_manager.process_message",
                new_callable=AsyncMock,
        ) as process_message, patch(
                "app.chain.message.asyncio.run_coroutine_threadsafe",
                side_effect=lambda coro, _loop: (coro.close(), Mock())[1],
        ), patch.object(
                chain, "_mark_message_processing_finished"
        ) as finish_status:
            chain.handle_message(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="/ai 搜索电影",
                original_chat_id="-100",
            )

        start_status.assert_not_called()
        finish_status.assert_not_called()
        process_message.assert_called_once()
        self.assertNotIn("processing_status", process_message.call_args.kwargs)
        self.assertEqual(
            process_message.call_args.kwargs["channel"],
            MessageChannel.Telegram.value,
        )
        self.assertEqual(process_message.call_args.kwargs["source"], "telegram-test")
        self.assertEqual(process_message.call_args.kwargs["original_chat_id"], "-100")

    def test_agent_manager_starts_processing_status_when_task_runs(self):
        async def _run():
            manager = AgentManager()
            task = _MessageTask(
                session_id="session-1",
                user_id="10001",
                message="第一条",
                channel=MessageChannel.Telegram.value,
                source="telegram-test",
                original_chat_id="-100",
            )
            status = {
                "channel": MessageChannel.Telegram.value,
                "source": "telegram-test",
                "userid": "10001",
                "chat_id": "-100",
                "metadata": {"kind": "typing"},
            }

            with patch(
                    "app.agent._async_start_processing_status",
                    new_callable=AsyncMock,
                    return_value=status,
            ) as start_status:
                await manager._start_task_processing_status(task)

            start_status.assert_awaited_once_with(task)
            self.assertEqual(task.processing_status, status)

        asyncio.run(_run())

    def test_agent_start_processing_status_uses_chain_interface(self):
        async def _run():
            task = _MessageTask(
                session_id="session-1",
                user_id="10001",
                message="第一条",
                channel=MessageChannel.Telegram.value,
                source="telegram-test",
                original_message_id="10",
                original_chat_id="-100",
            )
            status = {
                "channel": MessageChannel.Telegram.value,
                "source": "telegram-test",
                "userid": "10001",
                "message_id": "10",
                "chat_id": "-100",
                "metadata": {"kind": "typing"},
            }

            with patch("app.agent.AgentChain") as chain_cls:
                chain_cls.return_value.start_message_processing_status.return_value = status
                result = await _async_start_processing_status(task)

            chain_cls.return_value.start_message_processing_status.assert_called_once_with(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                message_id="10",
                chat_id="-100",
                text="第一条",
            )
            self.assertEqual(result, status)

        asyncio.run(_run())

    def test_callback_stops_typing_when_message_handler_returns(self):
        chain = MessageChain.__new__(MessageChain)
        status = MessageChain._ProcessingStatus(
            channel=MessageChannel.Telegram,
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
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                username="tester",
                text="CALLBACK:sites:req-1:refresh",
                original_chat_id="-100",
            )

        finish_status.assert_called_once_with(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            status=status,
            original_message_id=None,
            original_chat_id="-100",
        )

    def test_chain_finishes_processing_through_module_interface(self):
        chain = MessageChain.__new__(MessageChain)
        status = MessageChain._ProcessingStatus(
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            chat_id="-100",
            metadata={"kind": "typing"},
        )

        with patch.object(chain, "finish_message_processing_status") as finish_status:
            chain._mark_message_processing_finished(
                channel=MessageChannel.Telegram,
                source="telegram-test",
                userid="10001",
                status=status,
                original_chat_id="-100",
            )

        finish_status.assert_called_once_with(
            status=status.to_dict(),
            channel=MessageChannel.Telegram,
            source="telegram-test",
            userid="10001",
            message_id=None,
            chat_id="-100",
        )

    def test_agent_manager_finishes_processing_status_after_each_task(self):
        async def _run():
            manager = AgentManager()
            status = {
                "channel": MessageChannel.Telegram.value,
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
                    "app.agent._async_finish_processing_status",
                    new_callable=AsyncMock,
            ) as finish_status:
                await manager._finish_task_processing_status(task)

            finish_status.assert_awaited_once_with(status, "10001")
            self.assertIsNone(task.processing_status)

        asyncio.run(_run())

    def test_agent_worker_starts_and_finishes_each_queued_task(self):
        async def _run():
            manager = AgentManager()
            manager._session_queues["session-1"] = asyncio.Queue()
            first_status = {
                "channel": MessageChannel.Telegram.value,
                "source": "telegram-test",
                "userid": "10001",
                "chat_id": "-100",
                "metadata": {"kind": "typing", "seq": 1},
            }
            second_status = {
                "channel": MessageChannel.Telegram.value,
                "source": "telegram-test",
                "userid": "10001",
                "chat_id": "-100",
                "metadata": {"kind": "typing", "seq": 2},
            }
            await manager._session_queues["session-1"].put(_MessageTask(
                session_id="session-1",
                user_id="10001",
                message="第一条",
                channel=MessageChannel.Telegram.value,
                source="telegram-test",
                original_chat_id="-100",
            ))
            await manager._session_queues["session-1"].put(_MessageTask(
                session_id="session-1",
                user_id="10001",
                message="第二条",
                channel=MessageChannel.Telegram.value,
                source="telegram-test",
                original_chat_id="-100",
            ))

            with patch(
                    "app.agent._async_start_processing_status",
                    new_callable=AsyncMock,
                    side_effect=[first_status, second_status],
            ) as start_status, patch.object(
                    manager,
                    "_process_message_internal",
                    new_callable=AsyncMock,
            ), patch(
                    "app.agent._async_finish_processing_status",
                    new_callable=AsyncMock,
            ) as finish_status:
                manager._session_workers["session-1"] = asyncio.create_task(
                    manager._session_worker("session-1")
                )
                await manager._session_queues["session-1"].join()
                manager._session_workers["session-1"].cancel()
                await manager._session_workers["session-1"]

            self.assertEqual(start_status.await_count, 2)
            self.assertEqual(
                finish_status.await_args_list[0].args,
                (first_status, "10001"),
            )
            self.assertEqual(
                finish_status.await_args_list[1].args,
                (second_status, "10001"),
            )

        asyncio.run(_run())
