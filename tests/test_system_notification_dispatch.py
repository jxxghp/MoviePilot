import sys
import asyncio
import unittest
from types import ModuleType
from unittest.mock import patch

sys.modules.setdefault("qbittorrentapi", ModuleType("qbittorrentapi"))
setattr(sys.modules["qbittorrentapi"], "TorrentFilesList", list)
sys.modules.setdefault("transmission_rpc", ModuleType("transmission_rpc"))
setattr(sys.modules["transmission_rpc"], "File", object)
sys.modules.setdefault("psutil", ModuleType("psutil"))

from app.chain.message import MessageChain
from app.helper.message import MessageQueueManager
from app.schemas import Notification
from app.utils.identity import (
    SYSTEM_INTERNAL_USER_ID,
    is_internal_user_id,
    normalize_internal_user_id,
)


class TestSystemNotificationDispatch(unittest.TestCase):
    def test_internal_userid_identity_helpers(self):
        self.assertTrue(is_internal_user_id(SYSTEM_INTERNAL_USER_ID))
        self.assertTrue(is_internal_user_id(" System "))
        self.assertIsNone(normalize_internal_user_id(SYSTEM_INTERNAL_USER_ID))
        self.assertEqual(normalize_internal_user_id("10001"), "10001")

    def test_post_message_normalizes_internal_userid_before_queueing(self):
        chain = MessageChain()
        message = Notification(
            userid=SYSTEM_INTERNAL_USER_ID,
            username="admin",
            title="后台报告",
            text="任务完成",
        )

        with patch("app.chain.MessageTemplateHelper.render", return_value=message), patch.object(
            chain.messagehelper, "put"
        ), patch.object(chain.messageoper, "add"), patch.object(
            chain.eventmanager, "send_event"
        ) as send_event, patch.object(
            chain.messagequeue, "send_message"
        ) as send_message:
            chain.post_message(message)

        event_payload = send_event.call_args.kwargs["data"]
        queued_message = send_message.call_args.kwargs["message"]

        self.assertIsNone(event_payload["userid"])
        self.assertIsNone(queued_message.userid)
        self.assertFalse(send_message.call_args.kwargs["immediately"])

    def test_send_direct_message_normalizes_internal_userid(self):
        chain = MessageChain()
        message = Notification(
            userid=SYSTEM_INTERNAL_USER_ID,
            username="admin",
            title="后台报告",
            text="任务完成",
        )

        with patch.object(chain, "run_module") as run_module:
            chain.send_direct_message(message)

        sent_message = run_module.call_args.kwargs["message"]
        self.assertIsNone(sent_message.userid)

    def test_async_send_message_uses_executor_for_immediate_send(self):
        """异步立即发送不能在事件循环里直接执行同步渠道回调。"""

        class _FakeLoop:
            def __init__(self):
                self.called = False

            async def run_in_executor(self, executor, func):
                self.called = True
                func()

        async def _run():
            manager = MessageQueueManager()
            fake_loop = _FakeLoop()
            with patch("asyncio.get_running_loop", return_value=fake_loop), patch.object(
                manager, "_send"
            ) as send:
                await manager.async_send_message("payload", immediately=True)
            self.assertTrue(fake_loop.called)
            send.assert_called_once_with("payload")

        asyncio.run(_run())
