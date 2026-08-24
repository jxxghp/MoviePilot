import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import langchain.agents as langchain_agents

if not hasattr(langchain_agents, "create_agent"):
    langchain_agents.create_agent = lambda *args, **kwargs: None

from app.agent.orchestrator import _ThinkTagStripper
from app.agent.callback import StreamingHandler
from app.agent.middleware.subagents import is_subagent_stream_metadata
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.impl.send_voice_message import SendVoiceMessageTool
from app.api.endpoints.openai import _get_openai_streaming_handler_type
from app.runtime.config import settings
from app.schemas.message import Message, MessageResponse
from app.schemas.types import NotificationChannel, MessageType


def test_think_tag_stripper_waits_for_partial_open_tag():
    """普通文本后出现不完整 think 开始标签时不应进入死循环。"""
    stripper = _ThinkTagStripper()
    outputs = []

    emitted = stripper.process("你好<", outputs.append)
    emitted_next = stripper.process("世界", outputs.append)

    assert emitted is True
    assert emitted_next is True
    assert outputs == ["你好", "<世界"]


def test_think_tag_stripper_hides_split_think_tag_content():
    """think 标签被拆分到多个 token 时应继续隐藏思考内容。"""
    stripper = _ThinkTagStripper()
    outputs = []

    stripper.process("回答前<", outputs.append)
    stripper.process("thi", outputs.append)
    stripper.process("nk>隐藏内容</think>回答后", outputs.append)

    assert outputs == ["回答前", "回答后"]


class DummyTool(MoviePilotTool):
    """用于流式输出测试的固定结果工具。"""

    name: str = "dummy_tool"
    description: str = "Dummy tool for streaming tests."

    def get_tool_message(self, **kwargs) -> str:
        """返回固定工具执行提示。"""
        return "run test tool"

    async def run(self, **kwargs) -> str:
        """返回固定工具执行结果。"""
        return "ok"


class AdminOnlyDummyTool(MoviePilotTool):
    """仅管理员可用的工具，用于权限拦截场景的流式输出测试。"""

    name: str = "admin_only_tool"
    description: str = "Admin-only tool for streaming tests."
    require_admin: bool = True

    def get_tool_message(self, **kwargs) -> str:
        """返回固定工具执行提示。"""
        return "run admin only tool"

    async def run(self, **kwargs) -> str:
        """返回固定工具执行结果。"""
        return "ok"


class TestAgentToolStreaming:
    """Agent 工具流式输出测试。"""

    def test_web_message_callback_can_await_async_delivery(self):
        """WebAgent 通知回调支持异步附件准备并保持发送顺序。"""
        received = []

        async def scenario():
            tool = DummyTool(session_id="session-1", user_id="10001")
            tool.set_message_attr("WebAgent", "web-agent", "admin")

            async def callback(message):
                await asyncio.sleep(0)
                received.append(message.text)

            tool.set_agent_context({"message_callback": callback})
            await tool.send_message(
                Message(
                    text="异步通知",
                    channel=NotificationChannel.WebAgent,
                    mtype=MessageType.Agent,
                )
            )

        asyncio.run(scenario())

        assert received == ["异步通知"]

    async def _run_tool(self, initial_buffer: str) -> tuple[str, str]:
        """运行测试工具并返回工具结果与缓冲内容。"""
        tool = DummyTool(session_id="session-1", user_id="10001")
        handler = StreamingHandler()
        await handler.start_streaming()
        if initial_buffer:
            handler.emit(initial_buffer)
        tool.set_stream_handler(handler)

        with patch.object(settings, "AI_AGENT_VERBOSE", False):
            result = await tool._arun()

        buffered_message = await handler.take()
        return result, buffered_message

    def test_non_verbose_tool_call_flushes_summary_on_take(self):
        """校验非详细模式在读取时补充工具调用摘要。"""
        result, buffered_message = asyncio.run(self._run_tool("prefix"))

        assert result == "ok"
        assert buffered_message == "prefix\n\n（调用了 1 次工具）\n\n"

    def test_permission_failure_adds_boundary_newline(self):
        """校验权限拦截时在流式缓冲中补齐工具边界换行。"""
        async def _run():
            tool = AdminOnlyDummyTool(session_id="session-1", user_id="10001")
            handler = StreamingHandler()
            await handler.start_streaming()
            handler.emit("好的，我来帮您执行")
            tool.set_stream_handler(handler)
            tool.set_agent_context({"is_admin": False})

            result = await tool._arun()
            # 模拟模型在工具被拦截后继续输出失败说明
            handler.emit("抱歉，您没有执行此工具的权限")
            buffered_message = await handler.take()
            return result, buffered_message

        result, buffered_message = asyncio.run(_run())

        assert "没有执行此工具" in result
        assert buffered_message == "好的，我来帮您执行\n抱歉，您没有执行此工具的权限"

    def test_permission_failure_skips_newline_when_buffer_empty(self):
        """校验缓冲为空时权限拦截不会补多余的换行。"""
        async def _run():
            tool = AdminOnlyDummyTool(session_id="session-1", user_id="10001")
            handler = StreamingHandler()
            await handler.start_streaming()
            tool.set_stream_handler(handler)
            tool.set_agent_context({"is_admin": False})

            await tool._arun()
            return await handler.take()

        buffered_message = asyncio.run(_run())

        assert buffered_message == ""

    def test_permission_failure_reuses_existing_newline(self):
        """校验缓冲已以换行结尾时权限拦截不再补换行。"""
        async def _run():
            tool = AdminOnlyDummyTool(session_id="session-1", user_id="10001")
            handler = StreamingHandler()
            await handler.start_streaming()
            handler.emit("好的，我来帮您执行\n")
            tool.set_stream_handler(handler)
            tool.set_agent_context({"is_admin": False})

            await tool._arun()
            handler.emit("抱歉，您没有执行此工具的权限")
            return await handler.take()

        buffered_message = asyncio.run(_run())

        assert buffered_message == "好的，我来帮您执行\n抱歉，您没有执行此工具的权限"

    def test_non_verbose_tool_call_completes_blank_line_before_summary(self):
        """校验非详细模式在摘要前补足空行，保证工具摘要独立成段。"""
        result, buffered_message = asyncio.run(self._run_tool("prefix\n"))

        assert result == "ok"
        assert buffered_message == "prefix\n\n（调用了 1 次工具）\n\n"

    def test_non_verbose_tool_call_keeps_existing_blank_line_before_summary(self):
        """校验缓冲区已有空行时摘要不再追加多余换行。"""
        result, buffered_message = asyncio.run(self._run_tool("prefix\n\n"))

        assert result == "ok"
        assert buffered_message == "prefix\n\n（调用了 1 次工具）\n\n"

    def test_non_verbose_tool_call_emits_summary_even_when_buffer_was_empty(self):
        """校验空缓冲区仍会输出工具调用摘要。"""
        result, buffered_message = asyncio.run(self._run_tool(""))

        assert result == "ok"
        assert buffered_message == "（调用了 1 次工具）\n\n"

    def test_non_verbose_tool_summary_is_inserted_before_next_text(self):
        """校验工具摘要插入在后续文本之前。"""
        async def _run():
            tool = DummyTool(session_id="session-1", user_id="10001")
            handler = StreamingHandler()
            await handler.start_streaming()
            handler.emit("让我来检查一下：")
            tool.set_stream_handler(handler)

            with patch.object(settings, "AI_AGENT_VERBOSE", False):
                await tool._arun()

            handler.emit("已经拿到结果")
            return await handler.take()

        buffered_message = asyncio.run(_run())

        assert buffered_message == "让我来检查一下：\n\n（调用了 1 次工具）\n\n已经拿到结果"

    def test_non_verbose_tool_summary_aggregates_multiple_categories(self):
        """校验非详细模式按工具类别聚合摘要。"""
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

        assert buffered_message == "处理中：\n\n（执行了 2 次搜索，读取了 2 个文件）\n\n继续分析"

    def test_tool_summary_does_not_retain_secret_arguments(self):
        """流式聚合状态只能保留递归脱敏后的工具参数。"""
        secret_marker = "stream-secret-marker-1947"
        handler = StreamingHandler()

        handler.record_tool_call(
            tool_name="execute_command",
            tool_message=f"Authorization: Bearer {secret_marker}",
            tool_kwargs={
                "command": f"curl -H 'Authorization: Bearer {secret_marker}'",
                "headers": {"X-API-Key": secret_marker},
            },
        )

        pending_state = str(handler._pending_tool_stats)
        assert secret_marker not in pending_state
        assert "***" in pending_state

    def test_non_verbose_tool_summary_counts_subagents(self):
        """校验非详细模式统计子代理调用次数。"""
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

        assert buffered_message == "处理中：\n\n（已调用 2 个子代理）\n\n"

    def test_non_verbose_tool_summary_describes_skill_lookup(self):
        """校验非详细模式单独描述 Skill 说明查询。"""
        async def _run():
            handler = StreamingHandler()
            await handler.start_streaming()
            handler.emit("处理中：")
            handler.record_tool_call(
                tool_name="skill",
                tool_message="Loads the full instructions for a MoviePilot skill",
                tool_kwargs={"name": "moviepilot-cli"},
            )
            handler.record_tool_call(
                tool_name="skill",
                tool_message="Loads the full instructions for a MoviePilot skill",
                tool_kwargs={"name": "moviepilot-cli"},
            )
            handler.record_tool_call(
                tool_name="query_activity_log",
                tool_message="Query recent MoviePilot Agent activity logs",
                tool_kwargs={"keyword": "整理"},
            )
            return await handler.take()

        buffered_message = asyncio.run(_run())

        assert buffered_message == "处理中：\n\n（查询了 1 个技能说明，查询了 1 次活动日志）\n\n"

    def test_non_verbose_tool_summary_counts_subagent_batch_tasks(self):
        """校验批量子代理控制工具按子任务数统计。"""
        async def _run():
            handler = StreamingHandler()
            await handler.start_streaming()
            handler.emit("处理中：")
            handler.record_tool_call(
                tool_name="subagent_task",
                tool_message="Start and manage multiple MoviePilot subagent tasks",
                tool_kwargs={
                    "action": "start",
                    "tasks": [
                        {"subagent_type": "media-researcher"},
                        {"subagent_type": "download-diagnostician"},
                    ],
                },
            )
            return await handler.take()

        buffered_message = asyncio.run(_run())

        assert buffered_message == "处理中：\n\n（已调用 2 个子代理）\n\n"

    def test_subagent_stream_metadata_is_suppressed(self):
        """校验子代理流式元数据会被识别并抑制。"""
        assert is_subagent_stream_metadata(
            {"metadata": {"ls_agent_type": "subagent"}}
        )
        assert is_subagent_stream_metadata({"lc_agent_name": "media-researcher"})
        assert not is_subagent_stream_metadata({"lc_agent_name": "main"})

    def test_openai_streaming_handler_flushes_pending_summary_to_queue(self):
        """校验 OpenAI 流式处理器将待发送摘要推入队列。"""
        async def _run():
            handler = _get_openai_streaming_handler_type()()
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

        assert emitted == "（读取了 1 个文件）\n\n"
        assert queued == emitted
        assert buffered_message == emitted

    def test_flush_sends_direct_message_via_threadpool(self):
        """校验刷新时通过线程池发送首条直连消息。"""
        handler = StreamingHandler()
        handler._channel = NotificationChannel.Telegram.value
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

        assert run_in_threadpool_mock.await_count == 1
        assert run_in_threadpool_mock.await_args.args[0].__name__ == "send_direct_message"
        notification = run_in_threadpool_mock.await_args.args[1]
        assert notification.mtype == MessageType.Agent
        assert notification.text == "hello"
        assert notification.rich_message == "hello"
        assert handler.has_sent_message

    def test_rich_message_quotes_tool_summary_lines_for_telegram(self):
        """校验 Telegram 富文本将工具摘要行转换为引用块，与正文视觉分隔。"""
        handler = StreamingHandler()
        handler._channel = NotificationChannel.Telegram.value
        handler._source = "telegram"
        handler.record_tool_call(
            tool_name="list_directory",
            tool_message="查看目录",
            tool_kwargs={"path": "/tmp"},
        )
        handler.flush_pending_tool_summary()
        text = handler._buffer

        rich_message = handler._get_rich_message(text)

        assert text == "（查看了 1 个目录）\n\n"
        assert rich_message == "> （查看了 1 个目录）\n\n"

    def test_rich_message_keeps_body_text_unquoted_for_telegram(self):
        """校验 Telegram 富文本只转换工具摘要行，正文保持原样。"""
        handler = StreamingHandler()
        handler._channel = NotificationChannel.Telegram.value
        handler.emit("正文内容\n\n")
        handler.record_tool_call(
            tool_name="execute_command",
            tool_message="执行命令",
            tool_kwargs={},
        )
        handler.emit("后续结论")

        rich_message = handler._get_rich_message(handler._buffer)

        assert rich_message == "正文内容\n\n> （执行了 1 条命令）\n\n后续结论"

    def test_rich_message_returns_none_for_non_telegram_channels(self):
        """校验非 Telegram 渠道不启用富文本，摘要保持原有纯文本格式。"""
        handler = StreamingHandler()
        handler._channel = NotificationChannel.Feishu.value
        handler._source = "feishu-main"
        handler.record_tool_call(
            tool_name="execute_command",
            tool_message="执行命令",
            tool_kwargs={},
        )
        handler.flush_pending_tool_summary()

        assert handler._get_rich_message(handler._buffer) is None
        assert handler._buffer == "（执行了 1 条命令）\n\n"

    def test_flush_edits_message_via_threadpool(self):
        """校验刷新时通过线程池编辑已有消息。"""
        handler = StreamingHandler()
        handler._channel = NotificationChannel.Telegram.value
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

        assert run_in_threadpool_mock.await_count == 1
        assert run_in_threadpool_mock.await_args.args[0].__name__ == "edit_message"
        assert (
            run_in_threadpool_mock.await_args.kwargs["metadata"][
                "telegram_rich_message"
            ]
            == "hello world"
        )
        assert handler._sent_text == "hello world"

    def test_stop_streaming_waits_inflight_initial_flush_before_final_edit(self):
        """校验停止流式输出会等待首条消息发送完成再编辑。"""
        async def _run():
            handler = StreamingHandler()
            handler._channel = NotificationChannel.Feishu.value
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
                        channel=NotificationChannel.Feishu,
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
                assert not stop_task.done()

                allow_send_finish.set()
                all_sent, final_text = await stop_task

            return all_sent, final_text, calls

        all_sent, final_text, calls = asyncio.run(_run())

        assert all_sent
        assert final_text == "hello world"
        assert [call[0] for call in calls] == [
            "send_direct_message",
            "edit_message",
            "finalize_message",
        ]
        edit_kwargs = calls[1][2]
        assert edit_kwargs["message_id"] == "om_stream"
        assert edit_kwargs["text"] == "hello world"

    def test_stop_streaming_uses_generic_finalize_message(self):
        """校验停止流式输出会调用通用消息完成接口。"""
        handler = StreamingHandler()
        handler._message_response = MessageResponse(
            message_id="om_stream",
            chat_id="oc_stream",
            channel=NotificationChannel.Feishu,
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

        assert run_in_threadpool_mock.await_count == 1
        assert run_in_threadpool_mock.await_args.args[0].__name__ == "finalize_message"
        assert run_in_threadpool_mock.await_args.args[1].message_id == "om_stream"

    def test_flush_without_channel_context_does_not_send_direct_message(self):
        """校验缺少渠道上下文时不会发送直连消息。"""
        handler = StreamingHandler()
        handler._streaming_enabled = True
        handler.emit("hello")

        with patch(
            "app.agent.callback.run_in_threadpool", new_callable=AsyncMock
        ) as run_in_threadpool_mock:
            asyncio.run(handler._flush())

        run_in_threadpool_mock.assert_not_awaited()
        assert not handler.has_sent_message

    def test_flush_without_channel_context_dispatch_allowed_sends_direct_message(self):
        """校验允许后台派发时缺少渠道上下文也能发送消息。"""
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

        assert run_in_threadpool_mock.await_count == 1
        assert run_in_threadpool_mock.await_args.args[0].__name__ == "send_direct_message"
        assert handler.has_sent_message

    def test_flush_passes_original_message_context_to_send_direct_message(self):
        """校验刷新发送时保留原始消息上下文。"""
        handler = StreamingHandler()
        handler._channel = NotificationChannel.Feishu.value
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
        assert notification.original_message_id == "om_origin"
        assert notification.original_chat_id == "oc_origin"

    def test_verbose_background_tool_call_does_not_post_message(self):
        """校验详细模式后台工具调用不会主动发送工具消息。"""
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
                result = await tool._arun()
                buffered_message = await handler.take()
                return result, buffered_message, send_tool_message

        result, buffered_message, send_tool_message = asyncio.run(_run())

        assert result == "ok"
        send_tool_message.assert_not_awaited()
        assert buffered_message == "（调用了 1 次工具）\n\n"

    def test_verbose_background_dispatch_tool_call_can_post_message(self):
        """校验允许后台派发时详细模式工具调用可以发送消息。"""
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
                result = await tool._arun()
                buffered_message = await handler.take()
                return result, buffered_message, send_tool_message

        result, buffered_message, send_tool_message = asyncio.run(_run())

        assert result == "ok"
        send_tool_message.assert_awaited_once_with("前置内容\n\n⚙️ => run test tool")
        assert buffered_message == ""

    def test_send_voice_message_uses_native_voice_for_supported_channels(self):
        """校验支持语音输出的渠道会发送原生语音消息。"""

        async def _run(channel: NotificationChannel):
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
                patch.object(
                    SendVoiceMessageTool,
                    "send_message",
                    new_callable=AsyncMock,
                ) as send_message,
            ):
                result = await tool.run("你好")
            return result, synthesize_speech, send_message

        for channel in (NotificationChannel.Telegram, NotificationChannel.Feishu, NotificationChannel.WebAgent):
            result, synthesize_speech, send_message = asyncio.run(
                _run(channel)
            )
            notification = send_message.await_args.args[-1]

            assert result == "语音回复已发送"
            synthesize_speech.assert_called_once_with("你好")
            send_message.assert_awaited_once()
            assert notification.channel == channel
            assert notification.voice_path == "/tmp/reply.opus"
            assert notification.voice_caption == "你好"
            voice_tool = SendVoiceMessageTool(session_id="session-1", user_id="10001")
            assert voice_tool.return_direct
            assert "terminal response tool" in voice_tool.description

    def test_send_voice_message_falls_back_for_unsupported_channels(self):
        """校验不支持语音输出的渠道继续回退为文字消息。"""

        async def _run():
            """运行不支持语音输出渠道的语音发送工具。"""
            tool = SendVoiceMessageTool(session_id="session-1", user_id="10001")
            tool.set_message_attr(
                channel=NotificationChannel.Slack.value, source="slack-main", username="tester"
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
                patch.object(
                    SendVoiceMessageTool,
                    "send_message",
                    new_callable=AsyncMock,
                ) as send_message,
            ):
                result = await tool.run("你好")
            return result, synthesize_speech, send_message

        result, synthesize_speech, send_message = asyncio.run(_run())
        notification = send_message.await_args.args[-1]

        assert result == "当前渠道不支持语音回复，已自动回退为文字回复"
        synthesize_speech.assert_not_called()
        send_message.assert_awaited_once()
        assert notification.text == "你好"
        assert notification.voice_path is None
