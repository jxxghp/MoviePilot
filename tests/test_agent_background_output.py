from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.contracts import ReplyMode
from app.agent.manager import AgentManager
from app.agent.memory import memory_manager
from app.agent.middleware.activity import QUERY_ACTIVITY_LOG_TOOL_NAME
from app.agent.middleware.skills import SKILL_TOOL_NAME
from app.agent.middleware.subagents import (
    SUBAGENT_CONTROL_TOOL_NAME,
    SUBAGENT_TASK_TOOL_NAME,
)
from app.agent.orchestrator import (
    HEARTBEAT_SESSION_PREFIX,
    UNSUPPORTED_IMAGE_INPUT_MESSAGE,
    MoviePilotAgent,
)
from app.agent.session import _MessageTask
from app.agent.tools.factory import MoviePilotToolFactory
from app.foundation.identity import SYSTEM_INTERNAL_USER_ID
from app.runtime.config import settings

pytestmark = pytest.mark.anyio


class _FakeGraphState:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeAgent:
    def __init__(self, messages):
        self._messages = messages

    async def ainvoke(self, _payload, config=None):
        return None

    def get_state(self, _config):
        return _FakeGraphState(self._messages)


class _FakeFailingAgent:
    def __init__(self, error):
        self._error = error

    async def ainvoke(self, _payload, config=None):
        raise self._error

    def get_state(self, _config):
        return _FakeGraphState([])


class _FakeStreamingFailingAgent(_FakeFailingAgent):
    async def astream(self, _messages, **_kwargs):
        if _kwargs.get("__keep_async_generator__"):
            yield None
        raise self._error


class _FakeStreamingAgent(_FakeAgent):
    async def astream(self, _messages, **_kwargs):
        if _kwargs.get("__keep_async_generator__"):
            yield None


class StreamChunkTimeoutError(RuntimeError):
    """模拟 langchain_openai 的流式分块超时异常。"""


def _fake_skills_middleware(tool=None):
    """构造带 tools 属性的 SkillsMiddleware 测试替身。"""
    return SimpleNamespace(name="skills", tools=[] if tool is None else [tool])


def _fake_activity_log_middleware(tool=None):
    """构造带 tools 属性的 ActivityLogMiddleware 测试替身。"""
    return SimpleNamespace(name="activity", tools=[] if tool is None else [tool])


class TestAgentBackgroundOutput:
    async def test_background_non_streaming_does_not_send_by_default(self):
        agent = MoviePilotAgent(session_id="bg-test", user_id="system")
        agent.channel = None
        agent.source = None
        agent.reply_mode = ReplyMode.CAPTURE_ONLY
        agent._tool_context = {"user_reply_sent": False}
        agent._streamed_output = ""
        agent.stream_handler = SimpleNamespace(
            stop_streaming=AsyncMock(return_value=(False, ""))
        )
        agent._should_stream = lambda: False
        agent._create_agent = AsyncMock(
            return_value=_FakeAgent([AIMessage(content="后台结果")])
        )
        agent.send_agent_message = AsyncMock()

        with patch.object(memory_manager, "save_agent_messages") as save_messages:
            await agent._execute_agent([])

        agent.send_agent_message.assert_not_awaited()
        save_messages.assert_not_called()
        assert agent._streamed_output == "后台结果"

    async def test_non_streaming_image_unsupported_error_sends_friendly_notice(self):
        agent = MoviePilotAgent(session_id="image-test", user_id="user-1")
        agent.channel = "Telegram"
        agent.source = "telegram-test"
        agent._tool_context = {"user_reply_sent": False}
        agent._streamed_output = ""
        agent.stream_handler = SimpleNamespace(
            stop_streaming=AsyncMock(return_value=(False, ""))
        )
        agent._should_stream = lambda: False
        agent._create_agent = AsyncMock(
            return_value=_FakeFailingAgent(
                RuntimeError("No endpoints found that support image input")
            )
        )
        agent.send_agent_message = AsyncMock()

        result, _ = await agent._execute_agent(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": "看看这张图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
                    ]
                )
            ]
        )

        assert result == UNSUPPORTED_IMAGE_INPUT_MESSAGE
        agent.send_agent_message.assert_awaited_once_with(
            UNSUPPORTED_IMAGE_INPUT_MESSAGE, title=""
        )
        assert agent._streamed_output == UNSUPPORTED_IMAGE_INPUT_MESSAGE

    async def test_streaming_image_unsupported_error_sends_friendly_notice(self):
        agent = MoviePilotAgent(session_id="image-test", user_id="user-1")
        agent.channel = "Telegram"
        agent.source = "telegram-test"
        agent._tool_context = {"user_reply_sent": False}
        agent._streamed_output = ""
        agent.stream_handler = SimpleNamespace(
            set_dispatch_policy=lambda allow_dispatch_without_context=False: None,
            start_streaming=AsyncMock(),
            flush_pending_tool_summary=lambda: "",
            stop_streaming=AsyncMock(return_value=(False, "")),
        )
        agent._should_stream = lambda: True
        agent._create_agent = AsyncMock(
            return_value=_FakeStreamingFailingAgent(
                RuntimeError("Error code: 404 - No endpoints found that support image input")
            )
        )
        agent.send_agent_message = AsyncMock()

        result, _ = await agent._execute_agent(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": "看看这张图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
                    ]
                )
            ]
        )

        assert result == UNSUPPORTED_IMAGE_INPUT_MESSAGE
        agent.send_agent_message.assert_awaited_once_with(
            UNSUPPORTED_IMAGE_INPUT_MESSAGE, title=""
        )

    async def test_streaming_model_chunk_timeout_sends_friendly_notice(self):
        """流式模型分块超时时应只把主错误信息发给用户。"""
        agent = MoviePilotAgent(session_id="timeout-test", user_id="user-1")
        agent.channel = "Telegram"
        agent.source = "telegram-test"
        agent._tool_context = {"user_reply_sent": False}
        agent._streamed_output = ""
        agent.stream_handler = SimpleNamespace(
            set_dispatch_policy=lambda allow_dispatch_without_context=False: None,
            start_streaming=AsyncMock(),
            flush_pending_tool_summary=lambda: "",
            stop_streaming=AsyncMock(return_value=(False, "")),
        )
        agent._should_stream = lambda: True
        raw_error = StreamChunkTimeoutError(
            "No streaming chunk received for 120.0s "
            "(model=mimo-v2.5-pro, chunks_received=1). "
            "Tune or disable via the `stream_chunk_timeout` constructor kwarg."
        )
        agent._create_agent = AsyncMock(
            return_value=_FakeStreamingFailingAgent(raw_error)
        )
        agent.send_agent_message = AsyncMock()

        result, _ = await agent._execute_agent([HumanMessage(content="测试超时")])

        expected = "智能助手执行失败，请稍后重试"
        assert result == expected
        agent.send_agent_message.assert_awaited_once_with(expected, title="")
        sent_message = agent.send_agent_message.await_args.args[0]
        assert "No streaming chunk received for 120.0s" not in sent_message
        assert agent._streamed_output == expected

    async def test_streaming_success_stops_streaming_once(self):
        """流式正常完成时不应在 finally 中重复停止流式输出。"""
        agent = MoviePilotAgent(session_id="stream-ok", user_id="user-1")
        agent.channel = "Telegram"
        agent.source = "telegram-test"
        agent._tool_context = {"user_reply_sent": False}
        agent._streamed_output = ""
        agent.stream_handler = SimpleNamespace(
            set_dispatch_policy=lambda allow_dispatch_without_context=False: None,
            start_streaming=AsyncMock(),
            flush_pending_tool_summary=lambda: "",
            stop_streaming=AsyncMock(return_value=(True, "已发送")),
        )
        agent._should_stream = lambda: True
        agent._create_agent = AsyncMock(
            return_value=_FakeStreamingAgent([AIMessage(content="已发送")])
        )
        agent.send_agent_message = AsyncMock()

        await agent._execute_agent([HumanMessage(content="测试")])

        agent.stream_handler.stop_streaming.assert_awaited_once()

    async def test_tool_sent_reply_persists_raw_agent_messages(self):
        """工具已发送用户回复时仍应保存可恢复的 Agent 原始消息。"""
        agent = MoviePilotAgent(session_id="tool-reply", user_id="user-1")
        agent.channel = "Telegram"
        agent.source = "telegram-test"
        agent._tool_context = {"user_reply_sent": True}
        agent._streamed_output = ""
        agent.stream_handler = SimpleNamespace(
            stop_streaming=AsyncMock(return_value=(False, ""))
        )
        agent._should_stream = lambda: False
        agent._create_agent = AsyncMock(
            return_value=_FakeAgent([AIMessage(content="消息已发送")])
        )
        agent.send_agent_message = AsyncMock()

        with patch.object(
            memory_manager,
            "async_save_agent_messages",
            new=AsyncMock(),
        ) as save_messages:
            await agent._execute_agent([HumanMessage(content="测试")])

        save_messages.assert_called_once()
        _, kwargs = save_messages.call_args
        assert kwargs["session_id"] == "tool-reply"
        assert kwargs["user_id"] == "user-1"
        assert kwargs["messages"][0].content == "消息已发送"

    async def test_process_does_not_mutate_cached_agent_messages(self):
        """处理新消息时不应直接修改记忆缓存中的历史消息列表。"""
        agent = MoviePilotAgent(
            session_id="cached-memory",
            user_id="user-1",
            channel="Telegram",
            source="telegram-test",
        )
        cached_messages = [HumanMessage(content="上一轮")]
        captured = {}

        async def _execute_agent(messages):
            captured["messages"] = messages
            return "消息已发送", {}

        agent._execute_agent = AsyncMock(side_effect=_execute_agent)

        with (
            patch.object(
                memory_manager,
                "async_get_agent_messages",
                new=AsyncMock(return_value=cached_messages),
            ),
            patch.object(agent, "prepare_chat_title", new=AsyncMock()),
            patch.object(agent, "_save_display_history_messages"),
        ):
            result = await agent.process("继续")

        assert result == "消息已发送"
        assert len(cached_messages) == 1
        assert cached_messages is not captured["messages"]
        assert len(captured["messages"]) == 2

    async def test_background_non_streaming_sends_when_reply_mode_dispatch(self):
        agent = MoviePilotAgent(session_id="bg-test", user_id="system")
        agent.channel = None
        agent.source = None
        agent.reply_mode = ReplyMode.DISPATCH
        agent._tool_context = {"user_reply_sent": False}
        agent._streamed_output = ""
        agent.stream_handler = SimpleNamespace(
            stop_streaming=AsyncMock(return_value=(False, ""))
        )
        agent._should_stream = lambda: False
        agent._create_agent = AsyncMock(
            return_value=_FakeAgent([AIMessage(content="后台结果")])
        )
        agent.send_agent_message = AsyncMock()

        with patch.object(
            memory_manager,
            "async_save_agent_messages",
            new=AsyncMock(),
        ) as save_messages:
            await agent._execute_agent([])

        agent.send_agent_message.assert_awaited_once_with(
            "后台结果", title="MoviePilot助手"
        )
        save_messages.assert_not_called()
        assert agent._streamed_output == "后台结果"

    async def test_background_non_streaming_captures_without_sending_when_capture_only(self):
        agent = MoviePilotAgent(session_id="bg-test", user_id="system")
        agent.channel = None
        agent.source = None
        agent.reply_mode = ReplyMode.CAPTURE_ONLY
        agent._tool_context = {"user_reply_sent": False}
        agent._streamed_output = ""
        agent.stream_handler = SimpleNamespace(
            stop_streaming=AsyncMock(return_value=(False, ""))
        )
        agent._should_stream = lambda: False
        agent._create_agent = AsyncMock(
            return_value=_FakeAgent([AIMessage(content="后台结果")])
        )
        agent.send_agent_message = AsyncMock()

        with patch.object(memory_manager, "save_agent_messages") as save_messages:
            await agent._execute_agent([])

        agent.send_agent_message.assert_not_awaited()
        save_messages.assert_not_called()
        assert agent._streamed_output == "后台结果"

    async def test_heartbeat_check_jobs_captures_final_reply_and_keeps_message_tools(self):
        manager = AgentManager()

        with (
            patch("app.agent.tasks.load_jobs_metadata", new=AsyncMock(return_value=[{
                "id": "job-1",
                "name": "测试任务",
                "description": "desc",
                "path": "/tmp/job-1/JOB.md",
                "schedule": "once",
                "status": "pending",
                "last_run": None,
            }])),
            patch.object(manager, "_build_heartbeat_prompt", return_value="HEARTBEAT"),
            patch.object(manager, "process_message", new=AsyncMock()) as process_message,
        ):
            await manager.heartbeat_check_jobs()

        process_message.assert_awaited_once()
        kwargs = process_message.await_args.kwargs
        assert kwargs["reply_mode"] == ReplyMode.CAPTURE_ONLY
        assert kwargs["allow_message_tools"] is True

    async def test_heartbeat_check_jobs_skips_when_no_active_jobs(self):
        manager = AgentManager()

        with (
            patch("app.agent.tasks.load_jobs_metadata", new=AsyncMock(return_value=[])),
            patch.object(manager, "process_message", new=AsyncMock()) as process_message,
        ):
            await manager.heartbeat_check_jobs()

        process_message.assert_not_awaited()

    async def test_agent_manager_preserves_voice_input_flag(self):
        """会话队列执行时应把语音输入标记继续传给 Agent。"""
        manager = AgentManager()
        agent = MoviePilotAgent(session_id="session-1", user_id="user-1")
        manager.active_agents["session-1"] = agent
        agent.process = AsyncMock(return_value="ok")
        task = _MessageTask(
            session_id="session-1",
            user_id="user-1",
            message="帮我推荐一部电影",
            has_audio_input=True,
        )

        await manager._process_message_internal(task)

        agent.process.assert_awaited_once_with(
            "帮我推荐一部电影",
            images=None,
            files=None,
            has_audio_input=True,
        )

    async def test_create_agent_excludes_activity_log_for_heartbeat_session(self):
        agent = MoviePilotAgent(
            session_id=f"{HEARTBEAT_SESSION_PREFIX}test__",
            user_id="system",
        )
        agent._initialize_tools = lambda: []
        agent._initialize_subagent_tools = lambda: []

        with (
            patch.object(settings, "LLM_MAX_TOOLS", 0),
            patch.object(agent, "_initialize_llm", new=AsyncMock(return_value=object())),
            patch("app.agent.orchestrator.prompt_manager.get_agent_prompt", return_value="PROMPT"),
            patch("app.agent.orchestrator.create_subagent_middlewares", return_value=([], [])),
            patch(
                "app.agent.tools.factory.MoviePilotToolFactory.get_tool_selector_always_include_names",
                return_value=[],
            ),
            patch(
                "app.agent.orchestrator.SkillsMiddleware",
                side_effect=lambda *args, **kwargs: _fake_skills_middleware(),
            ),
            patch("app.agent.orchestrator.JobsMiddleware", side_effect=lambda *args, **kwargs: "jobs"),
            patch("app.agent.orchestrator.RuntimeConfigMiddleware", side_effect=lambda *args, **kwargs: "runtime"),
            patch("app.agent.orchestrator.MemoryMiddleware", side_effect=lambda *args, **kwargs: "memory"),
            patch(
                "app.agent.orchestrator.ActivityLogMiddleware",
                side_effect=lambda *args, **kwargs: _fake_activity_log_middleware(),
            ),
            patch("app.agent.orchestrator.SummarizationMiddleware", side_effect=lambda *args, **kwargs: "summary"),
            patch("app.agent.orchestrator.PatchToolCallsMiddleware", side_effect=lambda *args, **kwargs: "patch"),
            patch("app.agent.orchestrator.UsageMiddleware", side_effect=lambda *args, **kwargs: "usage"),
            patch("app.agent.orchestrator.InMemorySaver", return_value="checkpointer"),
            patch("app.agent.orchestrator.create_agent", side_effect=lambda **kwargs: kwargs),
        ):
            created = await agent._create_agent(streaming=False)

        assert [getattr(item, "name", item) for item in created["middleware"]] == [
                "AgentPolicyMiddleware",
                "skills",
                "jobs",
                "runtime",
                "memory",
                "patch",
                "FinalRequestCompactionMiddleware",
                "usage",
        ]

    async def test_create_agent_registers_skill_tool_from_middleware(self):
        """SkillsMiddleware 暴露的 read_skill 应进入 Agent 工具和筛选候选。"""
        captured = {}
        skill_tool = SimpleNamespace(name=SKILL_TOOL_NAME)
        agent = MoviePilotAgent(session_id="normal-session", user_id="system")
        agent._initialize_tools = lambda: []
        agent._initialize_subagent_tools = lambda: []

        def _tool_selector(**kwargs):
            captured["selection_tools"] = kwargs["selection_tools"]
            captured["always_include"] = kwargs["always_include"]
            return "selector"

        with (
            patch.object(settings, "LLM_MAX_TOOLS", 5),
            patch.object(agent, "_initialize_llm", new=AsyncMock(return_value=object())),
            patch("app.agent.orchestrator.prompt_manager.get_agent_prompt", return_value="PROMPT"),
            patch("app.agent.orchestrator.create_subagent_middlewares", return_value=([], [])),
            patch(
                "app.agent.tools.factory.MoviePilotToolFactory.get_tool_selector_always_include_names",
                return_value=[],
            ),
            patch(
                "app.agent.orchestrator.SkillsMiddleware",
                side_effect=lambda *args, **kwargs: _fake_skills_middleware(skill_tool),
            ),
            patch("app.agent.orchestrator.JobsMiddleware", side_effect=lambda *args, **kwargs: "jobs"),
            patch(
                "app.agent.orchestrator.RuntimeConfigMiddleware",
                side_effect=lambda *args, **kwargs: "runtime",
            ),
            patch("app.agent.orchestrator.MemoryMiddleware", side_effect=lambda *args, **kwargs: "memory"),
            patch(
                "app.agent.orchestrator.ActivityLogMiddleware",
                side_effect=lambda *args, **kwargs: _fake_activity_log_middleware(),
            ),
            patch(
                "app.agent.orchestrator.SummarizationMiddleware",
                side_effect=lambda *args, **kwargs: "summary",
            ),
            patch(
                "app.agent.orchestrator.PatchToolCallsMiddleware",
                side_effect=lambda *args, **kwargs: "patch",
            ),
            patch("app.agent.orchestrator.UsageMiddleware", side_effect=lambda *args, **kwargs: "usage"),
            patch("app.agent.orchestrator.ToolSelectorMiddleware", side_effect=_tool_selector),
            patch("app.agent.orchestrator.InMemorySaver", return_value="checkpointer"),
            patch("app.agent.orchestrator.create_agent", side_effect=lambda **kwargs: kwargs),
        ):
            created = await agent._create_agent(streaming=False)

        assert skill_tool in created["tools"]
        assert skill_tool in captured["selection_tools"]
        assert SKILL_TOOL_NAME in captured["always_include"]

    async def test_create_agent_excludes_activity_log_without_message_context(self):
        """无渠道信息的后台捕获任务不应注入活动日志。"""
        agent = MoviePilotAgent(
            session_id="background-capture-session",
            user_id="system",
            output_callback=lambda _text: None,
        )
        agent._initialize_tools = lambda: []
        agent._initialize_subagent_tools = lambda: []

        with (
            patch.object(settings, "LLM_MAX_TOOLS", 0),
            patch.object(agent, "_initialize_llm", new=AsyncMock(return_value=object())),
            patch("app.agent.orchestrator.prompt_manager.get_agent_prompt", return_value="PROMPT"),
            patch("app.agent.orchestrator.create_subagent_middlewares", return_value=([], [])),
            patch(
                "app.agent.tools.factory.MoviePilotToolFactory.get_tool_selector_always_include_names",
                return_value=[],
            ),
            patch(
                "app.agent.orchestrator.SkillsMiddleware",
                side_effect=lambda *args, **kwargs: _fake_skills_middleware(),
            ),
            patch("app.agent.orchestrator.JobsMiddleware", side_effect=lambda *args, **kwargs: "jobs"),
            patch(
                "app.agent.orchestrator.RuntimeConfigMiddleware",
                side_effect=lambda *args, **kwargs: "runtime",
            ),
            patch("app.agent.orchestrator.MemoryMiddleware", side_effect=lambda *args, **kwargs: "memory"),
            patch(
                "app.agent.orchestrator.ActivityLogMiddleware",
                side_effect=lambda *args, **kwargs: _fake_activity_log_middleware(),
            ),
            patch(
                "app.agent.orchestrator.SummarizationMiddleware",
                side_effect=lambda *args, **kwargs: "summary",
            ),
            patch(
                "app.agent.orchestrator.PatchToolCallsMiddleware",
                side_effect=lambda *args, **kwargs: "patch",
            ),
            patch("app.agent.orchestrator.UsageMiddleware", side_effect=lambda *args, **kwargs: "usage"),
            patch("app.agent.orchestrator.InMemorySaver", return_value="checkpointer"),
            patch("app.agent.orchestrator.create_agent", side_effect=lambda **kwargs: kwargs),
        ):
            created = await agent._create_agent(streaming=False)

        assert [getattr(item, "name", item) for item in created["middleware"]] == [
                "AgentPolicyMiddleware",
                "skills",
                "jobs",
                "runtime",
                "memory",
                "patch",
                "FinalRequestCompactionMiddleware",
                "usage",
        ]

    def test_message_tool_is_not_always_included_by_tool_selector(self):
        """消息发送工具不应绕过工具筛选。"""
        send_message_tool = SimpleNamespace(name="send_message")

        always_include = MoviePilotToolFactory.get_tool_selector_always_include_names(
            [send_message_tool]
        )

        assert "send_message" not in always_include

    def test_activity_log_tool_is_not_registered_by_tool_factory(self):
        """活动日志查询工具不应再由全局工具工厂保留。"""
        activity_log_tool = SimpleNamespace(name=QUERY_ACTIVITY_LOG_TOOL_NAME)

        always_include = MoviePilotToolFactory.get_tool_selector_always_include_names(
            [activity_log_tool]
        )

        assert QUERY_ACTIVITY_LOG_TOOL_NAME not in always_include

    async def test_create_agent_registers_activity_log_tool_from_middleware(self):
        """ActivityLogMiddleware 暴露的工具应进入 Agent 工具和筛选候选。"""
        captured = {}
        activity_tool = SimpleNamespace(name=QUERY_ACTIVITY_LOG_TOOL_NAME)
        agent = MoviePilotAgent(
            session_id="normal-session",
            user_id="system",
            channel="Web",
            source="openai",
        )
        agent._initialize_tools = lambda: []
        agent._initialize_subagent_tools = lambda: []

        def _tool_selector(**kwargs):
            captured["selection_tools"] = kwargs["selection_tools"]
            captured["always_include"] = kwargs["always_include"]
            return "selector"

        with (
            patch.object(settings, "LLM_MAX_TOOLS", 5),
            patch.object(agent, "_initialize_llm", new=AsyncMock(return_value=object())),
            patch("app.agent.orchestrator.prompt_manager.get_agent_prompt", return_value="PROMPT"),
            patch("app.agent.orchestrator.create_subagent_middlewares", return_value=([], [])),
            patch(
                "app.agent.tools.factory.MoviePilotToolFactory.get_tool_selector_always_include_names",
                return_value=[],
            ),
            patch(
                "app.agent.orchestrator.SkillsMiddleware",
                side_effect=lambda *args, **kwargs: _fake_skills_middleware(),
            ),
            patch("app.agent.orchestrator.JobsMiddleware", side_effect=lambda *args, **kwargs: "jobs"),
            patch(
                "app.agent.orchestrator.RuntimeConfigMiddleware",
                side_effect=lambda *args, **kwargs: "runtime",
            ),
            patch("app.agent.orchestrator.MemoryMiddleware", side_effect=lambda *args, **kwargs: "memory"),
            patch(
                "app.agent.orchestrator.ActivityLogMiddleware",
                side_effect=lambda *args, **kwargs: _fake_activity_log_middleware(
                    activity_tool
                ),
            ),
            patch(
                "app.agent.orchestrator.SummarizationMiddleware",
                side_effect=lambda *args, **kwargs: "summary",
            ),
            patch(
                "app.agent.orchestrator.PatchToolCallsMiddleware",
                side_effect=lambda *args, **kwargs: "patch",
            ),
            patch("app.agent.orchestrator.UsageMiddleware", side_effect=lambda *args, **kwargs: "usage"),
            patch("app.agent.orchestrator.ToolSelectorMiddleware", side_effect=_tool_selector),
            patch("app.agent.orchestrator.InMemorySaver", return_value="checkpointer"),
            patch("app.agent.orchestrator.create_agent", side_effect=lambda **kwargs: kwargs),
        ):
            created = await agent._create_agent(streaming=False)

        assert activity_tool in created["tools"]
        assert activity_tool in captured["selection_tools"]
        assert QUERY_ACTIVITY_LOG_TOOL_NAME in captured["always_include"]

    async def test_create_agent_always_includes_subagent_tools(self):
        """工具筛选开启时应保留同步和异步子代理入口。"""
        captured = {}
        agent = MoviePilotAgent(session_id="normal-session", user_id="system")
        agent._initialize_tools = lambda: []
        agent._initialize_subagent_tools = lambda: []

        def _tool_selector(**kwargs):
            captured["always_include"] = kwargs["always_include"]
            return "selector"

        with (
            patch.object(settings, "LLM_MAX_TOOLS", 5),
            patch.object(agent, "_initialize_llm", new=AsyncMock(return_value=object())),
            patch("app.agent.orchestrator.prompt_manager.get_agent_prompt", return_value="PROMPT"),
            patch(
                "app.agent.orchestrator.create_subagent_middlewares",
                return_value=(
                    ["subagent"],
                    [
                        SimpleNamespace(name=SUBAGENT_TASK_TOOL_NAME),
                        SimpleNamespace(name=SUBAGENT_CONTROL_TOOL_NAME),
                    ],
                ),
            ),
            patch(
                "app.agent.tools.factory.MoviePilotToolFactory.get_tool_selector_always_include_names",
                return_value=[],
            ),
            patch(
                "app.agent.orchestrator.SkillsMiddleware",
                side_effect=lambda *args, **kwargs: _fake_skills_middleware(),
            ),
            patch("app.agent.orchestrator.JobsMiddleware", side_effect=lambda *args, **kwargs: "jobs"),
            patch(
                "app.agent.orchestrator.RuntimeConfigMiddleware",
                side_effect=lambda *args, **kwargs: "runtime",
            ),
            patch("app.agent.orchestrator.MemoryMiddleware", side_effect=lambda *args, **kwargs: "memory"),
            patch(
                "app.agent.orchestrator.ActivityLogMiddleware",
                side_effect=lambda *args, **kwargs: _fake_activity_log_middleware(),
            ),
            patch(
                "app.agent.orchestrator.SummarizationMiddleware",
                side_effect=lambda *args, **kwargs: "summary",
            ),
            patch(
                "app.agent.orchestrator.PatchToolCallsMiddleware",
                side_effect=lambda *args, **kwargs: "patch",
            ),
            patch("app.agent.orchestrator.UsageMiddleware", side_effect=lambda *args, **kwargs: "usage"),
            patch("app.agent.orchestrator.ToolSelectorMiddleware", side_effect=_tool_selector),
            patch("app.agent.orchestrator.InMemorySaver", return_value="checkpointer"),
            patch("app.agent.orchestrator.create_agent", side_effect=lambda **kwargs: kwargs),
        ):
            await agent._create_agent(streaming=False)

        assert SUBAGENT_TASK_TOOL_NAME in captured["always_include"]
        assert SUBAGENT_CONTROL_TOOL_NAME in captured["always_include"]

    async def test_create_agent_keeps_activity_log_for_normal_session(self):
        agent = MoviePilotAgent(
            session_id="normal-session",
            user_id="system",
            channel="Web",
            source="openai",
        )
        agent._initialize_tools = lambda: []
        agent._initialize_subagent_tools = lambda: []

        with (
            patch.object(settings, "LLM_MAX_TOOLS", 0),
            patch.object(agent, "_initialize_llm", new=AsyncMock(return_value=object())),
            patch("app.agent.orchestrator.prompt_manager.get_agent_prompt", return_value="PROMPT"),
            patch("app.agent.orchestrator.create_subagent_middlewares", return_value=([], [])),
            patch(
                "app.agent.tools.factory.MoviePilotToolFactory.get_tool_selector_always_include_names",
                return_value=[],
            ),
            patch(
                "app.agent.orchestrator.SkillsMiddleware",
                side_effect=lambda *args, **kwargs: _fake_skills_middleware(),
            ),
            patch("app.agent.orchestrator.JobsMiddleware", side_effect=lambda *args, **kwargs: "jobs"),
            patch(
                "app.agent.orchestrator.RuntimeConfigMiddleware",
                side_effect=lambda *args, **kwargs: "runtime",
            ),
            patch("app.agent.orchestrator.MemoryMiddleware", side_effect=lambda *args, **kwargs: "memory"),
            patch(
                "app.agent.orchestrator.ActivityLogMiddleware",
                side_effect=lambda *args, **kwargs: _fake_activity_log_middleware(),
            ),
            patch(
                "app.agent.orchestrator.SummarizationMiddleware",
                side_effect=lambda *args, **kwargs: "summary",
            ),
            patch(
                "app.agent.orchestrator.PatchToolCallsMiddleware",
                side_effect=lambda *args, **kwargs: "patch",
            ),
            patch("app.agent.orchestrator.UsageMiddleware", side_effect=lambda *args, **kwargs: "usage"),
            patch("app.agent.orchestrator.InMemorySaver", return_value="checkpointer"),
            patch("app.agent.orchestrator.create_agent", side_effect=lambda **kwargs: kwargs),
        ):
            created = await agent._create_agent(streaming=False)

        assert [getattr(item, "name", item) for item in created["middleware"]] == [
                "AgentPolicyMiddleware",
                "skills",
                "jobs",
                "runtime",
                "memory",
                "activity",
                "patch",
                "FinalRequestCompactionMiddleware",
                "usage",
        ]

    async def test_run_background_prompt_forces_disable_message_tools_when_capture_only(self):
        captured = {}
        manager = AgentManager()

        async def fake_process(task):
            captured["message"] = task.message
            captured["reply_mode"] = task.reply_mode
            captured["allow_message_tools"] = task.allow_message_tools
            captured["user_id"] = task.user_id

        manager._process_message_internal = fake_process
        await manager.initialize()
        try:
            await manager.run_background_prompt(
                message="background task",
                reply_mode=ReplyMode.CAPTURE_ONLY,
                allow_message_tools=True,
            )
        finally:
            await manager.close()

        assert captured["message"] == "background task"
        assert captured["reply_mode"] == ReplyMode.CAPTURE_ONLY
        assert captured["allow_message_tools"] is False
        assert captured["user_id"] == SYSTEM_INTERNAL_USER_ID
