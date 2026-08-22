import unittest
import asyncio
import sys
from dataclasses import replace
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("qbittorrentapi", ModuleType("qbittorrentapi"))
setattr(sys.modules["qbittorrentapi"], "TorrentFilesList", list)
sys.modules.setdefault("transmission_rpc", ModuleType("transmission_rpc"))
setattr(sys.modules["transmission_rpc"], "File", object)
sys.modules.setdefault("psutil", ModuleType("psutil"))

from app.application.orchestration.message import MessageChain
from app.application.orchestration.transfer import TransferChain
from app.application.messaging.interaction import InteractionContext
from app.runtime.config import settings
from app.schemas.types import NotificationChannel


class TestTransferFailedRetryButtons(unittest.TestCase):
    def test_build_failed_transfer_buttons(self):
        buttons = TransferChain.build_failed_transfer_buttons(12)

        self.assertEqual(
            buttons,
            [
                [
                    {"text": "重试", "callback_data": "transfer_retry_12"},
                    {
                        "text": "智能助手接管",
                        "callback_data": "transfer_ai_retry_12",
                    },
                ]
            ],
        )

    def test_remote_transfer_supports_history_only_retry(self):
        chain = TransferChain()

        with patch.object(chain, "redo_transfer_history", return_value=(True, "")) as redo:
            with patch.object(chain, "post_message") as post_message:
                chain.remote_transfer(
                    "12",
                    channel=NotificationChannel.Telegram,
                    userid="10001",
                    source="telegram-test",
                )

        redo.assert_called_once_with(12)
        post_message.assert_not_called()

    def test_message_chain_routes_transfer_callback_to_transfer_chain(self):
        """MessageChain 收到整理失败按钮回调时委托 TransferChain 处理。"""
        chain = MessageChain()

        with patch("app.application.orchestration.message.TransferChain") as transfer_cls:
            transfer_cls.return_value.handle_failed_transfer_callback.return_value = True
            chain._handle_callback(
                callback_data="transfer_retry_12",
                context=InteractionContext(
                    channel=NotificationChannel.Telegram,
                    source="telegram-test",
                    user_id="10001",
                    username="tester",
                ),
            )

        transfer_cls.return_value.handle_failed_transfer_callback.assert_called_once_with(
            callback_data="transfer_retry_12",
            channel=NotificationChannel.Telegram,
            source="telegram-test",
            userid="10001",
            username="tester",
        )

    def test_transfer_retry_callback_retries_history(self):
        chain = TransferChain()

        with patch.object(chain, "redo_transfer_history", return_value=(True, "")) as redo:
            with patch.object(chain, "post_message") as post_message:
                handled = chain.handle_failed_transfer_callback(
                    callback_data="transfer_retry_12",
                    channel=NotificationChannel.Telegram,
                    source="telegram-test",
                    userid="10001",
                    username="tester",
                )

        self.assertTrue(handled)
        redo.assert_called_once_with(12)
        self.assertEqual(post_message.call_count, 2)
        self.assertEqual(
            post_message.call_args_list[0].args[0].title,
            "开始重新整理记录 #12 ...",
        )
        self.assertEqual(
            post_message.call_args_list[1].args[0].title,
            "整理记录 #12 已重新整理",
        )

    def test_transfer_ai_retry_callback_schedules_agent_takeover(self):
        chain = TransferChain()
        chain.runtime_config = replace(chain.runtime_config, ai_agent_enable=True)
        history = SimpleNamespace(
            id=34,
            status=False,
            title="Test Show",
            type="电视剧",
            category=None,
            year="2024",
            seasons="S01",
            episodes="E01",
            src="/downloads/Test.Show.S01E01.mkv",
            src_storage="local",
            src_fileitem={"path": "/downloads/Test.Show.S01E01.mkv"},
            dest=None,
            dest_storage=None,
            dest_fileitem=None,
            mode="copy",
            tmdbid=123,
            doubanid=None,
            bangumiid=None,
            anilistid=None,
            media_source="themoviedb",
            media_id="123",
            errmsg="未识别到媒体信息",
        )

        def _close_pending_coro(coro, *args, **kwargs):
            """关闭被调度的协程：测试中事件循环未运行，不关闭会残留 never-awaited 警告。"""
            coro.close()

        with patch.object(settings, "AI_AGENT_ENABLE", True):
            with patch(
                "app.application.orchestration._transfer.TransferHistoryOper"
            ) as history_oper_cls, patch(
                "app.application.orchestration._transfer.build_manual_redo_prompt",
                return_value="retry transfer prompt",
            ), patch(
                "app.application.orchestration._transfer.asyncio.run_coroutine_threadsafe",
                side_effect=_close_pending_coro,
            ) as run_task:
                history_oper_cls.return_value.get.return_value = history
                with patch.object(chain, "post_message") as post_message:
                    chain.handle_failed_transfer_callback(
                        callback_data="transfer_ai_retry_34",
                        channel=NotificationChannel.Telegram,
                        source="telegram-test",
                        userid="10001",
                        username="tester",
                    )

        run_task.assert_called_once()
        self.assertEqual(post_message.call_count, 1)
        self.assertEqual(
            post_message.call_args_list[0].args[0].title,
            "已将整理记录 #34 交给智能助手处理",
        )

    def test_transfer_ai_retry_callback_uses_successful_move_dest_as_source(self):
        chain = TransferChain()
        chain.runtime_config = replace(chain.runtime_config, ai_agent_enable=True)
        captured = {}
        history = SimpleNamespace(
            id=35,
            status=True,
            title="Test Show",
            type="电视剧",
            category=None,
            year="2024",
            seasons="S01",
            episodes="E01",
            src="/downloads/Test.Show.S01E01.mkv",
            src_storage="local",
            src_fileitem={"path": "/downloads/Test.Show.S01E01.mkv"},
            dest="/library/Test Show (2024)/Season 1/Test.Show.S01E01.mkv",
            dest_storage="local",
            dest_fileitem={
                "storage": "local",
                "path": "/library/Test Show (2024)/Season 1/Test.Show.S01E01.mkv",
                "name": "Test.Show.S01E01.mkv",
                "type": "file",
            },
            mode="move",
            tmdbid=123,
            doubanid=None,
            bangumiid=None,
            anilistid=None,
            media_source="themoviedb",
            media_id="123",
            errmsg=None,
        )

        def _run_pending_coro(coro, *args, **kwargs):
            asyncio.run(coro)
            return SimpleNamespace()

        async def fake_run_background_prompt(**kwargs):
            captured["message"] = kwargs["message"]
            output_callback = kwargs.get("output_callback")
            if output_callback:
                output_callback("ok")

        async def fake_async_post_message(*args, **kwargs):
            return None

        from app.agent.prompt.transfer_redo import build_manual_redo_prompt

        manager = SimpleNamespace(run_background_prompt=fake_run_background_prompt)
        with patch.object(settings, "AI_AGENT_ENABLE", True):
            with patch(
                "app.application.orchestration._transfer.TransferHistoryOper"
            ) as history_oper_cls, patch(
                "app.application.orchestration._transfer.build_manual_redo_prompt",
                side_effect=build_manual_redo_prompt,
            ), patch(
                "app.application.orchestration._transfer.get_running_agent_manager",
                return_value=manager,
            ), patch(
                "app.application.orchestration._transfer.asyncio.run_coroutine_threadsafe",
                side_effect=_run_pending_coro,
            ):
                history_oper_cls.return_value.get.return_value = history
                with patch.object(chain, "post_message"), patch.object(
                    chain, "async_post_message", side_effect=fake_async_post_message
                ):
                    chain.handle_failed_transfer_callback(
                        callback_data="transfer_ai_retry_35",
                        channel=NotificationChannel.Telegram,
                        source="telegram-test",
                        userid="10001",
                        username="tester",
                    )

        self.assertIn(
            "- Source path: /library/Test Show (2024)/Season 1/Test.Show.S01E01.mkv",
            captured["message"],
        )
