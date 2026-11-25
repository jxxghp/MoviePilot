"""
Agent 渠道流式输出的 start/stop owner 生命周期测试。
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.callback import StreamingHandler
from app.agent.orchestrator import MoviePilotAgent
from app.agent.web import _get_web_agent_streaming_handler_type
from app.schemas.message import MessageResponse
from app.schemas.types import NotificationChannel


def test_thinking_status_is_replaced_by_first_answer_token() -> None:
    """
    思考占位消息应在首个正文 token 到达时被移除，避免污染最终回复。
    """
    handler = StreamingHandler()
    handler._can_stream = lambda: True

    handler.thinking_started()
    assert handler._thinking_active is True
    assert handler._buffer == "🤔 思考中 · 已用时 0 秒"

    handler.emit("最终答案")

    assert handler._thinking_active is False
    assert handler._buffer == "最终答案"


def test_thinking_status_stays_separate_from_message_channel_tool_summary() -> None:
    """
    消息渠道的思考状态应与工具统计保持独立段落，并在正文到达时移除。
    """
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._streaming_enabled = True

    handler.thinking_started()
    handler.record_tool_call("read_file", tool_kwargs={"file_path": "README.md"})

    assert handler._buffer.endswith("🤔 思考中 · 已用时 0 秒")
    assert "（读取了 1 个文件）\n\n" in handler._buffer

    handler.emit("最终答案")

    assert "思考中" not in handler._buffer
    assert handler._buffer.endswith("\n\n最终答案")


def test_tool_message_does_not_end_message_channel_thinking_status() -> None:
    """
    消息渠道的工具提示到达时，思考状态仍应继续计时展示。
    """
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._streaming_enabled = True

    handler.thinking_started()
    handler.emit_tool_message("执行检查")

    assert handler._thinking_active is True
    assert handler._buffer.endswith("🤔 思考中 · 已用时 0 秒")
    assert "⚙️ => 执行检查" in handler._buffer


def test_thinking_status_is_absent_for_a_non_thinking_model() -> None:
    """
    未返回 reasoning 协议的模型不应被误报为思考中。
    """
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._streaming_enabled = True

    handler.emit("普通回答")

    assert handler._thinking_active is False
    assert handler._thinking_started_at is None
    assert handler._buffer == "普通回答"


def test_thinking_waits_for_a_new_reasoning_signal_after_tools() -> None:
    """
    工具结束后只有模型再次发出 reasoning，思考状态才回到消息末尾。
    """
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._streaming_enabled = True

    handler.thinking_started()
    handler.tool_call_started("first", "第一条")
    handler.tool_call_started("second", "第二条")
    handler.emit_tool_message("第一条")
    handler.emit_tool_message("第二条")

    assert handler._thinking_active is False
    handler.tool_call_finished("first")
    assert handler._thinking_active is False
    handler.tool_call_finished("second")

    assert handler._thinking_active is False
    handler.thinking_started()
    assert handler._thinking_active is True
    assert handler._buffer.endswith("🤔 思考中 · 已用时 0 秒")
    assert handler._buffer.index("⚙️ => 第一条") < handler._buffer.index("思考中")

    handler.emit("最终答案")
    assert handler._thinking_active is False
    assert "思考中" not in handler._buffer


def test_web_agent_thinking_status_is_only_a_structured_event() -> None:
    """
    WebAgent 的思考状态不能进入正文，只能通过 SSE 结构化事件展示。
    """
    events: list[dict[str, object]] = []
    handler = _get_web_agent_streaming_handler_type()(lambda _text: None, events.append)

    handler.thinking_started()
    handler.thinking_finished()

    assert handler._buffer == ""
    assert events[0]["type"] == "thinking"
    assert events[0]["status"] == "running"
    assert events[1] == {"type": "thinking", "status": "done"}


def test_web_agent_thinking_event_follows_tool_completion() -> None:
    """
    WebAgent 应在工具完成事件之后重新发布 thinking，保持时间线末尾一致。
    """
    events: list[dict[str, object]] = []
    handler = _get_web_agent_streaming_handler_type()(lambda _text: None, events.append)
    handler._streaming_enabled = True
    handler._is_verbose_mode = lambda: True

    with patch("app.agent.callback.get_runtime_setting", return_value=True):
        handler.thinking_started()
        tool_id = handler.tool_call_started("search", "查询媒体")
        handler.tool_call_finished(tool_id)
        handler.thinking_started()

    assert [event["type"] for event in events] == [
        "thinking",
        "thinking",
        "tool",
        "tool",
        "thinking",
    ]
    assert [event.get("status") for event in events] == [
        "running",
        "done",
        "running",
        "done",
        "running",
    ]


def test_web_agent_does_not_duplicate_thinking_running_event() -> None:
    """
    重复收到 thinking 开始信号时，Web 时间线只保留一个运行事件。
    """
    events: list[dict[str, object]] = []
    handler = _get_web_agent_streaming_handler_type()(lambda _text: None, events.append)
    handler._streaming_enabled = True

    with patch("app.agent.callback.get_runtime_setting", return_value=True):
        handler.thinking_started()
        handler.thinking_started()

    assert events and len(events) == 1
    assert events[0]["type"] == "thinking"
    assert events[0]["status"] == "running"


@pytest.mark.asyncio
async def test_web_agent_stop_finishes_active_thinking() -> None:
    """
    Web 流结束时必须收口尚未产生正文的 thinking 生命周期。
    """
    events: list[dict[str, object]] = []
    handler = _get_web_agent_streaming_handler_type()(lambda _text: None, events.append)
    handler._streaming_enabled = True

    with patch("app.agent.callback.get_runtime_setting", return_value=True):
        handler.thinking_started()
        await handler.stop_streaming()

    assert [event["status"] for event in events] == ["running", "done"]


@pytest.mark.asyncio
async def test_repeated_streaming_start_retains_previous_flush_owner() -> None:
    """
    重复启动必须先等待旧 flush owner 结束，再发布新一轮上下文。
    """
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._source = "old-source"
    handler._streaming_enabled = True
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    observed_sources: list[str | None] = []

    async def old_flush_owner() -> None:
        """
        在退出前读取 handler 上下文，暴露被新一轮提前覆盖的风险。
        """
        old_started.set()
        await release_old.wait()
        observed_sources.append(handler._source)

    old_task = asyncio.create_task(old_flush_owner(), name="old.flush")
    handler._flush_task = old_task
    await old_started.wait()

    restart = asyncio.create_task(
        handler.start_streaming(
            channel=NotificationChannel.Feishu.value,
            source="new-source",
        ),
        name="agent.streaming.restart",
    )
    await asyncio.sleep(0)

    assert restart.done() is False
    assert handler._source == "old-source"
    assert handler._flush_task is old_task

    release_old.set()
    await asyncio.wait_for(restart, timeout=1)
    new_task = handler._flush_task

    assert old_task.done()
    assert observed_sources == ["old-source"]
    assert new_task is not None
    assert new_task is not old_task
    assert handler._source == "new-source"

    await asyncio.wait_for(handler.stop_streaming(), timeout=1)
    assert new_task.done()


@pytest.mark.asyncio
async def test_streaming_start_waits_for_concurrent_stop_final_flush() -> None:
    """
    停止阶段的最终刷新未完成时，新启动不得改写消息上下文。
    """
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._source = "stopping-source"
    handler._streaming_enabled = True
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()

    async def blocking_flush(*, final: bool = False) -> None:
        """
        把最终刷新停留在生命周期锁内，验证新启动等待。

        :param final: 是否执行停止阶段的最终刷新
        """
        assert final
        flush_started.set()
        await release_flush.wait()

    handler._flush = blocking_flush
    stop_task = asyncio.create_task(
        handler.stop_streaming(),
        name="agent.streaming.stop",
    )
    await flush_started.wait()
    start_task = asyncio.create_task(
        handler.start_streaming(
            channel=NotificationChannel.Feishu.value,
            source="next-source",
        ),
        name="agent.streaming.next",
    )
    await asyncio.sleep(0)

    assert start_task.done() is False
    assert handler._source == "stopping-source"

    release_flush.set()
    await asyncio.wait_for(stop_task, timeout=1)
    await asyncio.wait_for(start_task, timeout=1)
    next_flush_task = handler._flush_task

    assert handler._source == "next-source"
    assert next_flush_task is not None

    await asyncio.wait_for(handler.stop_streaming(), timeout=1)
    assert next_flush_task.done()


@pytest.fixture
def channel_stream(monkeypatch):
    """
    隔离渠道 I/O，保留真实缓冲、消息编辑和收口逻辑。
    """
    handler = StreamingHandler()
    handler._channel = NotificationChannel.Telegram.value
    handler._source = "bot"
    handler._original_message_id = "user-1"
    handler._streaming_enabled = True
    calls = []
    messages = {}

    async def dispatch(func, *args, **kwargs):
        """
        记录渠道消息身份与最终正文，不连接真实消息服务。
        """
        calls.append((func.__name__, args, kwargs))
        if func.__name__ == "send_direct_message":
            message_id = str(len(messages) + 1)
            messages[message_id] = args[0].text
            return MessageResponse(message_id=message_id, source="bot", success=True)
        if func.__name__ == "edit_message":
            messages[str(kwargs["message_id"])] = kwargs["text"]
        return True

    monkeypatch.setattr("app.agent.callback.run_in_threadpool", dispatch)
    return handler, calls, messages, dispatch


@pytest.mark.asyncio
async def test_applied_message_freezes_old_reply_and_opens_new_reply(channel_stream) -> None:
    """
    应用边界前的尾部可补齐旧回复，边界后的正文只能发送、编辑新消息。
    """
    handler, calls, messages, _ = channel_stream
    handler.emit("前段")
    await handler._flush_messages()
    handler.emit("尾部")
    handler.start_new_message("user-2")
    handler.emit("后段")
    await handler._flush_messages()
    handler.emit("完成")
    all_sent, final_text = await handler.stop_streaming()

    assert all_sent is True
    assert final_text == "前段尾部后段完成"
    assert messages == {"1": "前段尾部", "2": "后段完成"}
    assert [call[0] for call in calls] == [
        "send_direct_message", "edit_message", "finalize_message",
        "send_direct_message", "edit_message", "finalize_message",
    ]
    assert calls[3][1][0].original_message_id == "user-2"
    assert calls[4][2]["message_id"] == "2"


@pytest.mark.asyncio
async def test_message_boundary_waits_for_inflight_send_identity(channel_stream, monkeypatch) -> None:
    """
    首条消息仍在发送时收到补充输入，迟到的响应不得覆盖新回复的身份。
    """
    handler, calls, messages, dispatch = channel_stream
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_dispatch(func, *args, **kwargs):
        """
        将首条发送保持在途，模拟真实渠道网络延迟。
        """
        if func.__name__ == "send_direct_message" and not started.is_set():
            started.set()
            await release.wait()
        return await dispatch(func, *args, **kwargs)

    monkeypatch.setattr("app.agent.callback.run_in_threadpool", delayed_dispatch)
    handler.emit("前段")
    handler._flush_task = asyncio.create_task(handler._flush_messages())
    await asyncio.wait_for(started.wait(), 1)
    handler.start_new_message("user-2")
    handler.emit("后段")
    stopping = asyncio.create_task(handler.stop_streaming())
    await asyncio.sleep(0)
    assert stopping.done() is False
    release.set()
    all_sent, final_text = await asyncio.wait_for(stopping, 1)

    assert all_sent is True
    assert final_text == "前段后段"
    assert messages == {"1": "前段", "2": "后段"}
    assert [call[0] for call in calls] == [
        "send_direct_message", "finalize_message", "send_direct_message", "finalize_message",
    ]
    assert calls[2][1][0].original_message_id == "user-2"


@pytest.mark.asyncio
async def test_multiple_applied_messages_keep_tool_summaries_in_their_own_reply(channel_stream) -> None:
    """
    连续补充输入不产生空消息，新工具摘要不能重写旧回复的统计。
    """
    handler, calls, messages, _ = channel_stream
    handler.record_tool_call("read_file", tool_kwargs={"file_path": "first.txt"})
    await handler._flush_messages()
    first_text = messages["1"]
    handler.start_new_message("user-2")
    handler.start_new_message("user-3")
    handler.record_tool_call("read_file", tool_kwargs={"file_path": "second.txt"})
    handler.emit("完成")
    all_sent, _ = await handler.stop_streaming()

    assert all_sent is True
    assert len(messages) == 2
    assert messages["1"] == first_text
    assert "读取了 1 个文件" in messages["2"]
    assert "读取了 2 个文件" not in messages["2"]
    assert "完成" in messages["2"]
    sends = [call[1][0] for call in calls if call[0] == "send_direct_message"]
    assert sends[-1].original_message_id == "user-3"


@pytest.mark.asyncio
async def test_empty_continuation_does_not_resend_finished_reply(channel_stream) -> None:
    """
    应用后没有新增正文时，已收口的前段仍被视为交付完成。
    """
    handler, calls, messages, _ = channel_stream
    handler.emit("已完成前段")
    await handler._flush_messages()
    handler.start_new_message("user-2")
    all_sent, final_text = await handler.stop_streaming()
    assert all_sent is True
    assert final_text == "已完成前段"
    assert messages == {"1": "已完成前段"}
    assert [call[0] for call in calls] == ["send_direct_message", "finalize_message"]


@pytest.mark.asyncio
async def test_failed_continuation_falls_back_without_repeating_previous_reply(channel_stream, monkeypatch) -> None:
    """
    新消息发送失败后，常规发送回退只包含未交付的新段。
    """
    handler, _, messages, _ = channel_stream
    handler.emit("已交付")
    await handler._flush_messages()
    handler.start_new_message("user-2")
    handler.emit("尚未交付")

    async def failed_dispatch(func, *_args, **_kwargs):
        """
        旧消息可正常收口，但新消息发送失败。
        """
        return None if func.__name__ == "send_direct_message" else True

    monkeypatch.setattr("app.agent.callback.run_in_threadpool", failed_dispatch)
    await handler._flush_messages()
    all_sent, _ = await handler.stop_streaming()
    assert not all_sent
    assert messages == {"1": "已交付"}
    assert await handler.take() == "尚未交付"


@pytest.mark.asyncio
async def test_length_limit_rollover_does_not_cross_user_message_boundary(channel_stream) -> None:
    """
    长度限制与补充输入同时切分时，每段正文仍只进入所属消息。
    """
    handler, calls, messages, _ = channel_stream
    handler._max_message_length = 4
    handler.emit("abcd")
    await handler._flush_messages()
    handler.emit("ef")
    handler.start_new_message("user-2")
    handler.emit("gh")
    all_sent, final_text = await handler.stop_streaming()
    assert all_sent is True
    assert final_text == "abcdefgh"
    assert messages == {"1": "abcd", "2": "ef", "3": "gh"}
    sends = [call[1][0] for call in calls if call[0] == "send_direct_message"]
    assert [message.original_message_id for message in sends] == ["user-1", "user-1", "user-2"]


@pytest.fixture
def failed_channel_edits(channel_stream, monkeypatch):
    """
    模拟消息已发送，但后续编辑被渠道拒绝的场景。
    """
    handler, calls, messages, dispatch = channel_stream

    async def reject_edit(func, *args, **kwargs):
        """
        发送和收口保持成功，编辑失败时保留渠道旧文本。
        """
        if func.__name__ == "edit_message":
            calls.append((func.__name__, args, kwargs))
            return False
        return await dispatch(func, *args, **kwargs)

    monkeypatch.setattr("app.agent.callback.run_in_threadpool", reject_edit)
    return handler, calls, messages


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [NotificationChannel.Telegram, NotificationChannel.Feishu])
@pytest.mark.parametrize("verbose", [False, True])
async def test_failed_final_edit_does_not_repeat_delivered_tool_progress(
    failed_channel_edits, channel, verbose
) -> None:
    """
    工具进度与思考占位已送达时，最终编辑失败只回退尚未送达的答案。
    """
    handler, calls, messages = failed_channel_edits
    handler._channel = channel.value
    if verbose:
        handler.emit_tool_message("执行截图命令")
    else:
        handler.record_tool_call("execute_command", tool_kwargs={"command": "capture"})
    handler.thinking_started()
    await handler._flush_messages()
    delivered_progress = messages["1"]

    handler.emit("已发送。截图为站点首页。")
    all_sent, final_text = await handler.stop_streaming()

    assert not all_sent
    assert final_text == ""
    assert await handler.take() == "已发送。截图为站点首页。"
    assert await handler.take() == ""
    assert messages == {"1": delivered_progress}
    assert [call[0] for call in calls] == [
        "send_direct_message", "edit_message", "finalize_message",
    ]


@pytest.mark.asyncio
async def test_failed_edit_preserves_changed_summary_without_repeating_previous_body(
    failed_channel_edits,
) -> None:
    """
    统计在最后一次成功发送后发生变化时，必须保留新统计整行和新答案。
    """
    handler, _, messages = failed_channel_edits
    handler.emit("已打开站点。\n\n")
    handler.record_tool_call("read_file", tool_kwargs={"file_path": "first.txt"})
    await handler._flush_messages()
    handler.record_tool_call("read_file", tool_kwargs={"file_path": "second.txt"})
    handler.emit("核验完成。")

    all_sent, _ = await handler.stop_streaming()

    assert not all_sent
    assert messages == {"1": "已打开站点。\n\n（读取了 1 个文件）\n\n"}
    assert await handler.take() == "（读取了 2 个文件）\n\n核验完成。"


@pytest.mark.asyncio
async def test_failed_edit_keeps_a_later_identical_tool_batch(failed_channel_edits) -> None:
    """
    后续批次即使统计文案相同，也属于未送达的新内容，不能全局去重。
    """
    handler, _, _ = failed_channel_edits
    handler.record_tool_call("read_file", tool_kwargs={"file_path": "first.txt"})
    handler.emit("第一批完成。\n\n")
    await handler._flush_messages()
    handler.record_tool_call("read_file", tool_kwargs={"file_path": "second.txt"})
    handler.emit("第二批完成。")

    all_sent, _ = await handler.stop_streaming()

    assert not all_sent
    assert await handler.take() == "（读取了 1 个文件）\n\n第二批完成。"


@pytest.mark.asyncio
async def test_rollover_uses_delivered_body_without_thinking_placeholder(channel_stream) -> None:
    """
    思考占位被答案替换后，长度分段不能按旧占位长度裁掉答案开头。
    """
    handler, _, messages, _ = channel_stream
    handler.emit("已核验。\n\n")
    handler.thinking_started()
    await handler._flush_messages()
    handler._max_message_length = len(handler._sent_text)
    answer = "截图已经发送。" * 8
    handler.emit(answer)

    all_sent, final_text = await handler.stop_streaming()

    assert all_sent
    assert final_text == f"已核验。\n\n{answer}"
    assert messages["2"] == answer
    assert await handler.take() == ""


@pytest.mark.asyncio
async def test_inflight_progress_snapshot_survives_thinking_replacement(
    channel_stream, monkeypatch
) -> None:
    """
    发送在途时思考占位被移除，去重仍使用请求发出时的快照。
    """
    handler, _, _, dispatch = channel_stream
    handler.emit("已核验。\n\n")
    handler.thinking_started()
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_dispatch(func, *args, **kwargs):
        """
        首次发送延迟确认，之后的编辑均失败。
        """
        if func.__name__ == "send_direct_message":
            started.set()
            await release.wait()
        if func.__name__ == "edit_message":
            return False
        return await dispatch(func, *args, **kwargs)

    monkeypatch.setattr("app.agent.callback.run_in_threadpool", delayed_dispatch)
    handler._flush_task = asyncio.create_task(handler._flush_messages())
    await asyncio.wait_for(started.wait(), 1)
    handler.emit("截图已经发送。")
    release.set()

    all_sent, _ = await handler.stop_streaming()

    assert not all_sent
    assert await handler.take() == "截图已经发送。"


@pytest.mark.asyncio
async def test_failed_initial_send_keeps_complete_fallback_without_thinking(
    channel_stream, monkeypatch
) -> None:
    """
    首次发送未确认时保留全部正文和工具统计，临时思考状态不能进入回退消息。
    """
    handler, _, _, _ = channel_stream
    handler.record_tool_call("execute_command", tool_kwargs={"command": "capture"})
    handler.thinking_started()

    async def failed_dispatch(*_args, **_kwargs):
        """
        模拟渠道未确认首条消息发送成功。
        """
        return None

    monkeypatch.setattr("app.agent.callback.run_in_threadpool", failed_dispatch)
    await handler._flush_messages()
    all_sent, _ = await handler.stop_streaming()

    assert not all_sent
    assert await handler.take() == "（执行了 1 条命令）\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered_prefix", ["", "站点已打开。"])
@pytest.mark.parametrize("new_tool_after_send", [False, True])
async def test_agent_fallback_does_not_repeat_answer_in_output_or_history(
    failed_channel_edits, monkeypatch, delivered_prefix, new_tool_after_send
) -> None:
    """
    真实 Agent 收尾仅补发答案后缀，外部回调和历史文本中答案均只出现一次。
    """
    handler, _, _ = failed_channel_edits
    agent = MoviePilotAgent(session_id="fallback-progress", user_id="user-1")
    agent.channel = NotificationChannel.Telegram.value
    agent.source = "bot"
    agent.stream_handler = handler
    monkeypatch.setattr(agent, "_should_stream", lambda: True)
    monkeypatch.setattr(agent, "_create_agent", AsyncMock(return_value=object()))
    monkeypatch.setattr(agent, "_learning_enabled", lambda: False)
    save_display = AsyncMock()
    monkeypatch.setattr(agent, "_save_assistant_display_message_once", save_display)
    monkeypatch.setattr(agent, "_should_persist_agent_chat", lambda: False)
    monkeypatch.setattr(agent, "_send_agent_tokens_usage_event", lambda **_kwargs: None)
    monkeypatch.setattr(agent, "send_agent_message", AsyncMock())
    # 由测试显式刷新进度，避免依赖定时任务与墙钟时间。
    monkeypatch.setattr(handler, "start_streaming", AsyncMock())
    answer = "截图已经发送。"

    async def stream_tokens(**_kwargs):
        """
        仅模拟模型 token，使用真实缓冲区和发送回退逻辑。
        """
        handler.record_tool_call("execute_command", tool_kwargs={"command": "capture"})
        if delivered_prefix:
            agent._handle_stream_text(delivered_prefix)
        handler.thinking_started()
        await handler._flush_messages()
        if new_tool_after_send:
            handler.record_tool_call("execute_command", tool_kwargs={"command": "verify"})
        agent._handle_stream_text(answer)

    monkeypatch.setattr(agent, "_stream_agent_tokens", stream_tokens)

    await agent._execute_agent([])

    fallback = answer
    if new_tool_after_send:
        count = 1 if delivered_prefix else 2
        fallback = f"（执行了 {count} 条命令）\n\n{answer}"
    agent.send_agent_message.assert_awaited_once_with(fallback)
    assert agent._streamed_output == delivered_prefix + answer
    save_display.assert_awaited_once_with(delivered_prefix + answer)
