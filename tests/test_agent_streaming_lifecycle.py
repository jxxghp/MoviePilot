"""Agent 渠道流式输出的 start/stop owner 生命周期测试。"""

import asyncio
from unittest.mock import patch

import pytest

from app.agent.callback import StreamingHandler
from app.agent.web import _get_web_agent_streaming_handler_type
from app.schemas.types import NotificationChannel


def test_thinking_status_is_replaced_by_first_answer_token() -> None:
    """思考占位消息应在首个正文 token 到达时被移除，避免污染最终回复。"""
    handler = StreamingHandler()
    handler._can_stream = lambda: True

    handler.thinking_started()
    assert handler._thinking_active is True
    assert handler._buffer == "🤔 思考中 · 已用时 0 秒"

    handler.emit("最终答案")

    assert handler._thinking_active is False
    assert handler._buffer == "最终答案"


def test_thinking_status_stays_separate_from_message_channel_tool_summary() -> None:
    """消息渠道的思考状态应与工具统计保持独立段落，并在正文到达时移除。"""
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
    """消息渠道的工具提示到达时，思考状态仍应继续计时展示。"""
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._streaming_enabled = True

    handler.thinking_started()
    handler.emit_tool_message("执行检查")

    assert handler._thinking_active is True
    assert handler._buffer.endswith("🤔 思考中 · 已用时 0 秒")
    assert "⚙️ => 执行检查" in handler._buffer


def test_thinking_status_is_absent_for_a_non_thinking_model() -> None:
    """未返回 reasoning 协议的模型不应被误报为思考中。"""
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._streaming_enabled = True

    handler.emit("普通回答")

    assert handler._thinking_active is False
    assert handler._thinking_started_at is None
    assert handler._buffer == "普通回答"


def test_thinking_waits_for_a_new_reasoning_signal_after_tools() -> None:
    """工具结束后只有模型再次发出 reasoning，思考状态才回到消息末尾。"""
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
    """WebAgent 的思考状态不能进入正文，只能通过 SSE 结构化事件展示。"""
    events: list[dict[str, object]] = []
    handler = _get_web_agent_streaming_handler_type()(lambda _text: None, events.append)

    handler.thinking_started()
    handler.thinking_finished()

    assert handler._buffer == ""
    assert events[0]["type"] == "thinking"
    assert events[0]["status"] == "running"
    assert events[1] == {"type": "thinking", "status": "done"}


def test_web_agent_thinking_event_follows_tool_completion() -> None:
    """WebAgent 应在工具完成事件之后重新发布 thinking，保持时间线末尾一致。"""
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
    """重复收到 thinking 开始信号时，Web 时间线只保留一个运行事件。"""
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
    """Web 流结束时必须收口尚未产生正文的 thinking 生命周期。"""
    events: list[dict[str, object]] = []
    handler = _get_web_agent_streaming_handler_type()(lambda _text: None, events.append)
    handler._streaming_enabled = True

    with patch("app.agent.callback.get_runtime_setting", return_value=True):
        handler.thinking_started()
        await handler.stop_streaming()

    assert [event["status"] for event in events] == ["running", "done"]


@pytest.mark.asyncio
async def test_repeated_streaming_start_retains_previous_flush_owner() -> None:
    """重复启动必须先等待旧 flush owner 结束，再发布新一轮上下文。"""
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._source = "old-source"
    handler._streaming_enabled = True
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    observed_sources: list[str | None] = []

    async def old_flush_owner() -> None:
        """在退出前读取 handler 上下文，暴露被新一轮提前覆盖的风险。"""
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
    """停止阶段的最终刷新未完成时，新启动不得改写消息上下文。"""
    handler = StreamingHandler()
    handler._can_stream = lambda: True
    handler._source = "stopping-source"
    handler._streaming_enabled = True
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()

    async def blocking_flush() -> None:
        """把最终刷新停留在生命周期锁内，验证新启动等待。"""
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
