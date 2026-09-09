"""验证多行工具参数在 Web 与外部消息渠道中的展示边界。"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.callback import StreamingHandler
from app.agent.tools.impl.execute_command import ExecuteCommandTool
from app.agent.web import _get_web_agent_streaming_handler_type
from app.api.endpoints.openai import _get_openai_streaming_handler_type
from app.application.messaging.agent import (
    apply_web_agent_display_event,
    build_web_agent_display_message,
    split_web_agent_output,
)
from app.runtime.config import settings
from app.schemas.types import NotificationChannel

COMMAND = 'python /config/agent/submit_feedback_issue.py \\\n  --payload-file "/config/feedback/payload.json"'


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", "\n\n", "\u2028"])
@pytest.mark.parametrize("middleware", [False, True])
def test_web_multiline_tool_parameters_stay_in_tool_snapshot(separator, middleware):
    """不同换行与工具入口都必须将完整参数保留在唯一工具事件及历史快照内。"""
    emitted = []
    handler = _get_web_agent_streaming_handler_type()(emitted.append)
    message = f'执行系统命令: python submit.py \\{separator}  --payload-file "payload.json"'
    handler.emit("准备提交。")
    with patch("app.agent.callback.get_runtime_setting", return_value=True):
        if middleware:
            handler.report_tool_call("execute_command", message)
        else:
            handler.emit_tool_message(message)
    handler.emit("提交成功。\n- Issue #6621")

    events = [event for chunk in emitted for event in split_web_agent_output(chunk)]
    tools = [event for event in events if event["type"] == "tool"]
    content = "".join(event["content"] for event in events if event["type"] == "delta")
    assert len(tools) == 1
    assert '--payload-file "payload.json"' in tools[0]["message"]
    assert content == "准备提交。\n\n提交成功。\n- Issue #6621"

    snapshot = build_web_agent_display_message(role="assistant", status="streaming")
    for event in events:
        apply_web_agent_display_event(event, snapshot)
    apply_web_agent_display_event({"type": "done"}, snapshot)
    assert snapshot["content"] == content
    assert snapshot["tools"][0]["message"] == tools[0]["message"]
    assert [segment["type"] for segment in snapshot["segments"]] == ["text", "tool", "text"]


def test_web_tool_display_keeps_original_command_and_separate_calls():
    """执行参数不被展示归一化改写，相邻多行工具调用也不会合并或泄入正文。"""
    emitted = []
    handler = _get_web_agent_streaming_handler_type()(emitted.append)
    tool = ExecuteCommandTool(session_id="display-test", user_id="1")
    tool.set_stream_handler(handler)
    tool.set_agent_context({"is_admin": True})

    async def scenario():
        """走真实工具包装入口，仅替换实际命令执行。"""
        await handler.start_streaming(channel=NotificationChannel.WebAgent.value)
        with (
            patch.object(settings, "AI_AGENT_VERBOSE", True),
            patch.object(ExecuteCommandTool, "run", new_callable=AsyncMock, return_value="ok") as run,
        ):
            await tool._arun(action="run", command=COMMAND)
            run.assert_awaited_once_with(action="run", command=COMMAND)
        handler.emit_tool_message('读取文件: preview.md\n编码: UTF-8')
        handler.emit("提交完成")
        await handler.stop_streaming()

    asyncio.run(scenario())
    events = [event for chunk in emitted for event in split_web_agent_output(chunk)]
    assert len([event for event in events if event["type"] == "tool"]) == 2
    assert "".join(event["content"] for event in events if event["type"] == "delta").strip() == "提交完成"


@pytest.mark.parametrize("channel", list(NotificationChannel))
@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", "\u2028"])
def test_channel_tool_buffer_preserves_multiline_parameters(channel, separator):
    """外部渠道继续使用完整文本；Telegram 的全部工具参数行保持引用格式。"""
    handler = StreamingHandler()
    handler._channel = channel.value
    command = COMMAND.replace("\n", separator)
    handler.emit("准备提交。")
    handler.emit_tool_message(f"执行系统命令: {command}")
    handler.emit("提交完成。")
    text = handler._buffer

    assert text == f"准备提交。\n\n⚙️ => 执行系统命令: {command}\n\n提交完成。"
    if channel == NotificationChannel.Telegram:
        assert handler._get_rich_message(text) == (
            f'准备提交。\n\n> ⚙️ => 执行系统命令: python /config/agent/submit_feedback_issue.py \\{separator}'
            '>   --payload-file "/config/feedback/payload.json"\n\n提交完成。'
        )
    else:
        assert handler._get_rich_message(text) is None


@pytest.mark.parametrize("channel", [NotificationChannel.Wechat, NotificationChannel.QQ, NotificationChannel.Slack])
def test_non_editing_channel_sends_complete_tool_message(channel):
    """不能编辑消息时，正文与多行参数仍通过独立通知完整发送到原渠道。"""
    handler = StreamingHandler()
    handler._streaming_enabled = True
    handler.emit("准备提交。")
    tool = ExecuteCommandTool(session_id="display-test", user_id="1")
    tool.set_stream_handler(handler)
    tool.set_agent_context({"is_admin": True})
    tool.set_message_attr(channel.value, "display-source", "tester")

    with (
        patch.object(settings, "AI_AGENT_VERBOSE", True),
        patch.object(ExecuteCommandTool, "run", new_callable=AsyncMock, return_value="ok"),
        patch.object(ExecuteCommandTool, "send_message", new_callable=AsyncMock) as send,
    ):
        asyncio.run(tool._arun(action="run", command=COMMAND))

    send.assert_awaited_once()
    message = send.await_args.args[0]
    assert message.channel == channel
    assert message.source == "display-source"
    assert message.userid == "1"
    assert message.text == f"准备提交。\n\n⚙️ => 执行系统命令: {COMMAND}"
    assert handler._buffer == ""


def test_openai_compatible_stream_preserves_multiline_tool_output():
    """兼容协议透传完整工具提示，消息队列与最终返回不得丢失续行参数。"""
    handler = _get_openai_streaming_handler_type()()
    queue = asyncio.Queue()
    handler.bind_queue(queue)

    async def scenario():
        """使用兼容协议流式生命周期收集完整输出。"""
        await handler.start_streaming()
        handler.emit_tool_message(f"执行系统命令: {COMMAND}")
        handler.emit("提交完成。")
        return await handler.stop_streaming()

    all_sent, final_text = asyncio.run(scenario())
    chunks = []
    while not queue.empty():
        chunks.append(queue.get_nowait())
    assert all_sent
    assert "".join(chunks) == final_text
    assert COMMAND in final_text
    assert final_text.endswith("\n\n提交完成。")
