import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import langchain.agents as langchain_agents

if not hasattr(langchain_agents, "create_agent"):
    langchain_agents.create_agent = lambda *args, **kwargs: None

from app.agent.callback import StreamingHandler
from app.agent.middleware.subagents import is_subagent_stream_metadata
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl.send_voice_message import SendVoiceMessageTool
from app.api.endpoints.openai import _OpenAIStreamingHandler
from app.core.config import settings
from app.schemas.message import MessageResponse
from app.schemas.types import MessageChannel, NotificationType


class DummyTool(MoviePilotTool):
    name: str = "dummy_tool"
    description: str = "Dummy tool for streaming tests."

    async def run(self, **kwargs) -> str:
        return "ok"


class TestAgentToolStreaming(unittest.TestCase):
    async def _run_tool(self, initial_buffer: str) -> tuple[str, str]:
        tool = DummyTool(session_id="session-1", user_id="10001")
        handler = StreamingHandler()
        await handler.start_streaming()
        if initial_buffer:
            handler.emit(initial_buffer)
        tool.set_stream_handler(handler)

        with patch.object(settings, "AI_AGENT_VERBOSE", False):
            result = await tool._arun(explanation="run test tool")

        buffered_message = await handler.take()
        return result, buffered_message

    def test_non_verbose_tool_call_flushes_summary_on_take(self):
        result, buffered_message = asyncio.run(self._run_tool("prefix"))

        self.assertEqual(result, "ok")
        self.assertEqual(buffered_message, "prefix\n\n（调用了 1 次工具）\n\n")

    def test_non_verbose_tool_call_reuses_existing_newline_before_summary(self):
        result, buffered_message = asyncio.run(self._run_tool("prefix\n"))

        self.assertEqual(result, "ok")
        self.assertEqual(buffered_message, "prefix\n（调用了 1 次工具）\n\n")

    def test_non_verbose_tool_call_emits_summary_even_when_buffer_was_empty(self):
        result, buffered_message = asyncio.run(self._run_tool(""))

        self.assertEqual(result, "ok")
        self.assertEqual(buffered_message, "（调用了 1 次工具）\n\n")

    def test_non_verbose_tool_summary_is_inserted_before_next_text(self):
        async def _run():
            tool = DummyTool(session_id="session-1", user_id="10001")
            handler = StreamingHandler()
            await handler.start_streaming()
            handler.emit("让我来检查一下：")
            tool.set_stream_handler(handler)

            with patch.object(settings, "AI_AGENT_VERBOSE", False):
                await tool._arun(explanation="run test tool")

            handler.emit("已经拿到结果")
            return await handler.take()

        buffered_message = asyncio.run(_run())

        self.assertEqual(
            buffered_message,
            "让我来检查一下：\n\n（调用了 1 次工具）\n\n已经拿到结果",
        )

    def test_non_verbose_tool_summary_aggregates_multiple_categories(self):
        async def _run():
            handler = StreamingHandler()
            await handler.start_streaming()
            handler.emit("处理中：")
            handler.record_tool_call(
                tool_name="search_web",
                tool_message="搜索网络内容: MoviePilot",
                tool_kwargs={"query": "MoviePilot"},
            )
            handler.record_tool_call(
                tool_name="search_web",
                tool_message="搜索网络内容: agent streaming",
                tool_kwargs={"query": "agent streaming"},
            )
            handler.record_tool_call(
                tool_name="read_file",
                tool_message="读取文件: a.py",
                tool_kwargs={"file_path": "/tmp/a.py"},
            )
            handler.record_tool_call(
                tool_name="read_file",
                tool_message="读取文件: b.py",
                tool_kwargs={"file_path": "/tmp/b.py"},
            )
            handler.emit("继续分析")
            return await handler.take()

        buffered_message = asyncio.run(_run())

        self.assertEqual(
            buffered_message,
            "处理中：\n\n（执行了 2 次搜索，读取了 2 个文件）\n\n继续分析",
        )

    def test_non_verbose_tool_summary_counts_subagents(self):
        async def _run():
            handler = StreamingHandler()
            await handler.start_streaming()
            handler.emit("处理中：")
            handler.record_tool_call(
                tool_name="task",
                tool_message="Subagent invoked",
                tool_kwargs={"subagent_type": "media-researcher"},
            )
            handler.record_tool_call(
                tool_name="task",
                tool_message="Subagent invoked",
                tool_kwargs={"subagent_type": "resource-searcher"},
            )
            return await handler.take()

        buffered_message = asyncio.run(_run())

        self.assertEqual(buffered_message, "处理中：\n\n（已调用 2 个子代理）\n\n")

    def test_subagent_stream_metadata_is_suppressed(self):
        self.assertTrue(
            is_subagent_stream_metadata(
                {"metadata": {"ls_agent_type": "subagent"}}
            )
        )
        self.assertTrue(is_subagent_stream_metadata({"lc_agent_name": "media-researcher"}))
        self.assertFalse(is_subagent_stream_metadata({"lc_agent_name": "main"}))

    def test_openai_streaming_handler_flushes_pending_summary_to_queue(self):
        async def _run():
            handler = _OpenAIStreamingHandler()
            queue: asyncio.Queue = asyncio.Queue()
            handler.bind_queue(queue)
            await handler.start_streaming()
            handler.record_tool_call(
                tool_name="read_file",
                tool_message="读取文件: app.py",
                tool_kwargs={"file_path": "/tmp/app.py"},
            )
            emitted = handler.flush_pending_tool_summary()
            queued = await queue.get()
            buffered_message = await handler.take()
            return emitted, queued, buffered_message

        emitted, queued, buffered_message = asyncio.run(_run())

        self.assertEqual(emitted, "（读取了 1 个文件）\n\n")
        self.assertEqual(queued, emitted)
        self.assertEqual(buffered_message, emitted)

    def test_flush_sends_direct_message_via_threadpool(self):
        handler = StreamingHandler()
        handler._channel = MessageChannel.Telegram.value
        handler._source = "telegram"
        handler._user_id = "10001"
        handler._username = "tester"
        handler._streaming_enabled = True
        handler.emit("hello")

        with patch(
            "app.agent.callback.run_in_threadpool", new_callable=AsyncMock
        ) as run_in_threadpool_mock:
            run_in_threadpool_mock.return_value = MessageResponse(
                message_id=1,
                chat_id=2,
                source="telegram",
                success=True,
            )

            asyncio.run(handler._flush())

        self.assertEqual(run_in_threadpool_mock.await_count, 1)
        self.assertEqual(
            run_in_threadpool_mock.await_args.args[0].__name__, "send_direct_message"
        )
        self.assertEqual(
            run_in_threadpool_mock.await_args.args[1].mtype,
            NotificationType.Agent,
        )
        self.assertTrue(handler.has_sent_message)

    def test_flush_edits_message_via_threadpool(self):
        handler = StreamingHandler()
        handler._channel = MessageChannel.Telegram.value
        handler._source = "telegram"
        handler._streaming_enabled = True
        handler._message_response = MessageResponse(
            message_id=1,
            chat_id=2,
            source="telegram",
            success=True,
        )
        handler._sent_text = "hello"
        handler.emit("hello world")

        with patch(
            "app.agent.callback.run_in_threadpool", new_callable=AsyncMock
        ) as run_in_threadpool_mock:
            run_in_threadpool_mock.return_value = True

            asyncio.run(handler._flush())

        self.assertEqual(run_in_threadpool_mock.await_count, 1)
        self.assertEqual(
            run_in_threadpool_mock.await_args.args[0].__name__, "edit_message"
        )
        self.assertEqual(handler._sent_text, "hello world")

    def test_stop_streaming_waits_inflight_initial_flush_before_final_edit(self):
        async def _run():
            handler = StreamingHandler()
            handler._channel = MessageChannel.Feishu.value
            handler._source = "feishu-main"
            handler._user_id = "ou_user"
            handler._streaming_enabled = True
            handler.emit("hello")

            send_started = asyncio.Event()
            allow_send_finish = asyncio.Event()
            calls = []

            async def fake_run_in_threadpool(func, *args, **kwargs):
                calls.append((func.__name__, args, kwargs))
                if func.__name__ == "send_direct_message":
                    send_started.set()
                    await allow_send_finish.wait()
                    return MessageResponse(
                        message_id="om_stream",
                        chat_id="oc_stream",
                        channel=MessageChannel.Feishu,
                        source="feishu-main",
                        success=True,
                    )
                return True

            with patch(
                "app.agent.callback.run_in_threadpool",
                new=fake_run_in_threadpool,
            ):
                # 模拟定时刷新已经开始发送首条消息，但飞书 API 尚未返回。
                handler._flush_task = asyncio.create_task(handler._flush())
                await send_started.wait()
                handler.emit(" world")

                stop_task = asyncio.create_task(handler.stop_streaming())
                await asyncio.sleep(0)
                self.assertFalse(stop_task.done())

                allow_send_finish.set()
                all_sent, final_text = await stop_task

            return all_sent, final_text, calls

        all_sent, final_text, calls = asyncio.run(_run())

        self.assertTrue(all_sent)
        self.assertEqual(final_text, "hello world")
        self.assertEqual(
            [call[0] for call in calls],
            ["send_direct_message", "edit_message", "finalize_message"],
        )
        edit_kwargs = calls[1][2]
        self.assertEqual(edit_kwargs["message_id"], "om_stream")
        self.assertEqual(edit_kwargs["text"], "hello world")

    def test_stop_streaming_uses_generic_finalize_message(self):
        handler = StreamingHandler()
        handler._message_response = MessageResponse(
            message_id="om_stream",
            chat_id="oc_stream",
            channel=MessageChannel.Feishu,
            source="feishu-main",
            metadata={"feishu_streaming": {"card_id": "card_stream", "sequence": 2}},
            success=True,
        )
        handler._sent_text = "hello"
        handler._buffer = "hello"
        handler._streaming_enabled = True

        with patch(
            "app.agent.callback.run_in_threadpool", new_callable=AsyncMock
        ) as run_in_threadpool_mock, patch.object(
            handler, "_cancel_flush_task", new_callable=AsyncMock
        ), patch.object(
            handler, "_flush", new_callable=AsyncMock
        ):
            asyncio.run(handler.stop_streaming())

        self.assertEqual(run_in_threadpool_mock.await_count, 1)
        self.assertEqual(
            run_in_threadpool_mock.await_args.args[0].__name__, "finalize_message"
        )
        self.assertEqual(
            run_in_threadpool_mock.await_args.args[1].message_id,
            "om_stream",
        )

    def test_flush_without_channel_context_does_not_send_direct_message(self):
        handler = StreamingHandler()
        handler._streaming_enabled = True
        handler.emit("hello")

        with patch(
            "app.agent.callback.run_in_threadpool", new_callable=AsyncMock
        ) as run_in_threadpool_mock:
            asyncio.run(handler._flush())

        run_in_threadpool_mock.assert_not_awaited()
        self.assertFalse(handler.has_sent_message)

    def test_flush_without_channel_context_dispatch_allowed_sends_direct_message(self):
        handler = StreamingHandler()
        handler._user_id = "10001"
        handler._username = "tester"
        handler._streaming_enabled = True
        handler.set_dispatch_policy(allow_dispatch_without_context=True)
        handler.emit("hello")

        with patch(
            "app.agent.callback.run_in_threadpool", new_callable=AsyncMock
        ) as run_in_threadpool_mock:
            run_in_threadpool_mock.return_value = MessageResponse(
                message_id=1,
                chat_id=2,
                source="telegram",
                success=True,
            )

            asyncio.run(handler._flush())

        self.assertEqual(run_in_threadpool_mock.await_count, 1)
        self.assertEqual(
            run_in_threadpool_mock.await_args.args[0].__name__, "send_direct_message"
        )
        self.assertTrue(handler.has_sent_message)

    def test_flush_passes_original_message_context_to_send_direct_message(self):
        handler = StreamingHandler()
        handler._channel = MessageChannel.Feishu.value
        handler._source = "feishu-main"
        handler._user_id = "ou_user"
        handler._username = "tester"
        handler._original_message_id = "om_origin"
        handler._original_chat_id = "oc_origin"
        handler._streaming_enabled = True
        handler.emit("hello")

        with patch(
            "app.agent.callback.run_in_threadpool", new_callable=AsyncMock
        ) as run_in_threadpool_mock:
            run_in_threadpool_mock.return_value = MessageResponse(
                message_id="om_stream",
                chat_id="oc_origin",
                source="feishu-main",
                success=True,
            )

            asyncio.run(handler._flush())

        notification = run_in_threadpool_mock.await_args.args[1]
        self.assertEqual(notification.original_message_id, "om_origin")
        self.assertEqual(notification.original_chat_id, "oc_origin")

    def test_verbose_background_tool_call_does_not_post_message(self):
        async def _run():
            tool = DummyTool(session_id="session-1", user_id="10001")
            handler = StreamingHandler()
            await handler.start_streaming()
            tool.set_stream_handler(handler)
            tool.set_message_attr(channel=None, source=None, username="tester")

            with (
                patch.object(settings, "AI_AGENT_VERBOSE", True),
                patch.object(
                    DummyTool, "send_tool_message", new_callable=AsyncMock
                ) as send_tool_message,
            ):
                result = await tool._arun(explanation="run test tool")
                buffered_message = await handler.take()
                return result, buffered_message, send_tool_message

        result, buffered_message, send_tool_message = asyncio.run(_run())

        self.assertEqual(result, "ok")
        send_tool_message.assert_not_awaited()
        self.assertEqual(buffered_message, "（调用了 1 次工具）\n\n")

    def test_verbose_background_dispatch_tool_call_can_post_message(self):
        async def _run():
            tool = DummyTool(session_id="session-1", user_id="10001")
            handler = StreamingHandler()
            await handler.start_streaming()
            handler.emit("前置内容")
            tool.set_stream_handler(handler)
            tool.set_message_attr(channel=None, source=None, username="tester")
            tool.set_agent_context({"should_dispatch_reply": True})

            with (
                patch.object(settings, "AI_AGENT_VERBOSE", True),
                patch.object(
                    DummyTool, "send_tool_message", new_callable=AsyncMock
                ) as send_tool_message,
            ):
                result = await tool._arun(explanation="run test tool")
                buffered_message = await handler.take()
                return result, buffered_message, send_tool_message

        result, buffered_message, send_tool_message = asyncio.run(_run())

        self.assertEqual(result, "ok")
        send_tool_message.assert_awaited_once_with("前置内容\n\n⚙️ => run test tool")
        self.assertEqual(buffered_message, "")

    def test_send_voice_message_uses_native_voice_for_supported_channels(self):
        """校验支持语音输出的渠道会发送原生语音消息。"""

        async def _run(channel: MessageChannel):
            """运行指定渠道的语音发送工具。"""
            tool = SendVoiceMessageTool(session_id="session-1", user_id="10001")
            tool.set_message_attr(
                channel=channel.value,
                source=f"{channel.name.lower()}-main",
                username="tester",
            )

            with (
                patch.object(settings, "LLM_SUPPORT_AUDIO_OUTPUT", True),
                patch.object(settings, "AUDIO_OUTPUT_INCLUDE_TEXT", True),
                patch(
                    "app.agent.tools.impl.send_voice_message.AgentCapabilityManager.is_audio_output_available",
                    return_value=True,
                ),
                patch(
                    "app.agent.tools.impl.send_voice_message.AgentCapabilityManager.synthesize_speech",
                    return_value=Path("/tmp/reply.opus"),
                ) as synthesize_speech,
                patch(
                    "app.agent.tools.impl.send_voice_message.ToolChain.async_post_message",
                    new_callable=AsyncMock,
                ) as async_post_message,
            ):
                result = await tool.run("你好")
            return result, synthesize_speech, async_post_message

        for channel in (MessageChannel.Telegram, MessageChannel.Feishu):
            result, synthesize_speech, async_post_message = asyncio.run(
                _run(channel)
            )
            notification = async_post_message.await_args.args[0]

            self.assertEqual(result, "语音回复已发送")
            synthesize_speech.assert_called_once_with("你好")
            self.assertEqual(notification.channel, channel)
            self.assertEqual(notification.voice_path, "/tmp/reply.opus")
            self.assertEqual(notification.voice_caption, "你好")
            voice_tool = SendVoiceMessageTool(session_id="session-1", user_id="10001")
            self.assertTrue(voice_tool.return_direct)
            self.assertIn("terminal response tool", voice_tool.description)

    def test_send_voice_message_falls_back_for_unsupported_channels(self):
        """校验不支持语音输出的渠道继续回退为文字消息。"""

        async def _run():
            """运行不支持语音输出渠道的语音发送工具。"""
            tool = SendVoiceMessageTool(session_id="session-1", user_id="10001")
            tool.set_message_attr(
                channel=MessageChannel.Slack.value, source="slack-main", username="tester"
            )

            with (
                patch.object(settings, "LLM_SUPPORT_AUDIO_OUTPUT", True),
                patch(
                    "app.agent.tools.impl.send_voice_message.AgentCapabilityManager.is_audio_output_available",
                    return_value=True,
                ),
                patch(
                    "app.agent.tools.impl.send_voice_message.AgentCapabilityManager.synthesize_speech"
                ) as synthesize_speech,
                patch(
                    "app.agent.tools.impl.send_voice_message.ToolChain.async_post_message",
                    new_callable=AsyncMock,
                ) as async_post_message,
            ):
                result = await tool.run("你好")
            return result, synthesize_speech, async_post_message

        result, synthesize_speech, async_post_message = asyncio.run(_run())
        notification = async_post_message.await_args.args[0]

        self.assertEqual(result, "当前渠道不支持语音回复，已自动回退为文字回复")
        synthesize_speech.assert_not_called()
        self.assertEqual(notification.text, "你好")
        self.assertIsNone(notification.voice_path)
