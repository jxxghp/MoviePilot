import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from app.agent import (
    HEARTBEAT_SESSION_PREFIX,
    MoviePilotAgent,
    AgentManager,
    ReplyMode,
    UNSUPPORTED_IMAGE_INPUT_MESSAGE,
    _MessageTask,
)
from app.agent.memory import memory_manager
from app.agent.middleware.subagents import (
    SUBAGENT_CONTROL_TOOL_NAME,
    SUBAGENT_TASK_TOOL_NAME,
)
from app.agent.tools.factory import MoviePilotToolFactory
from app.core.config import settings
from app.utils.identity import SYSTEM_INTERNAL_USER_ID


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
        raise self._error
        # 保持 async generator 形态，避免测试替身变成普通 coroutine。
        yield None


class StreamChunkTimeoutError(RuntimeError):
    """模拟 langchain_openai 的流式分块超时异常。"""


class AgentBackgroundOutputTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_non_streaming_does_not_send_by_default(self):
        agent = MoviePilotAgent(session_id="bg-test", user_id="system")
        agent.channel = None
        agent.source = None
        agent.reply_mode = ReplyMode.CAPTURE_ONLY
        agent.persist_output_message = True
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
        agent._save_agent_message_to_db = AsyncMock()

        with patch.object(memory_manager, "save_agent_messages") as save_messages:
            await agent._execute_agent([])

        agent.send_agent_message.assert_not_awaited()
        agent._save_agent_message_to_db.assert_awaited_once_with(
            "后台结果", title="MoviePilot助手"
        )
        save_messages.assert_called_once()
        self.assertEqual("后台结果", agent._streamed_output)

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
        agent._save_agent_message_to_db = AsyncMock()

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

        self.assertEqual(UNSUPPORTED_IMAGE_INPUT_MESSAGE, result)
        agent.send_agent_message.assert_awaited_once_with(
            UNSUPPORTED_IMAGE_INPUT_MESSAGE, title=""
        )
        agent._save_agent_message_to_db.assert_not_awaited()
        self.assertEqual(UNSUPPORTED_IMAGE_INPUT_MESSAGE, agent._streamed_output)

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
        agent._save_agent_message_to_db = AsyncMock()

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

        self.assertEqual(UNSUPPORTED_IMAGE_INPUT_MESSAGE, result)
        agent.send_agent_message.assert_awaited_once_with(
            UNSUPPORTED_IMAGE_INPUT_MESSAGE, title=""
        )
        agent._save_agent_message_to_db.assert_not_awaited()

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
        agent._save_agent_message_to_db = AsyncMock()

        result, _ = await agent._execute_agent([HumanMessage(content="测试超时")])

        expected = (
            "智能助手执行失败: No streaming chunk received for 120.0s "
            "(model=mimo-v2.5-pro, chunks_received=1)."
        )
        self.assertEqual(expected, result)
        agent.send_agent_message.assert_awaited_once_with(expected, title="")
        sent_message = agent.send_agent_message.await_args.args[0]
        self.assertIn("No streaming chunk received for 120.0s", sent_message)
        self.assertNotIn("Tune or disable", sent_message)
        self.assertEqual(expected, agent._streamed_output)
        agent._save_agent_message_to_db.assert_not_awaited()

    async def test_background_non_streaming_sends_when_reply_mode_dispatch(self):
        agent = MoviePilotAgent(session_id="bg-test", user_id="system")
        agent.channel = None
        agent.source = None
        agent.reply_mode = ReplyMode.DISPATCH
        agent.persist_output_message = False
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
        agent._save_agent_message_to_db = AsyncMock()

        with patch.object(memory_manager, "save_agent_messages") as save_messages:
            await agent._execute_agent([])

        agent.send_agent_message.assert_awaited_once_with(
            "后台结果", title="MoviePilot助手"
        )
        agent._save_agent_message_to_db.assert_not_awaited()
        save_messages.assert_called_once()
        self.assertEqual("后台结果", agent._streamed_output)

    async def test_background_non_streaming_persists_without_sending_when_capture_only(self):
        agent = MoviePilotAgent(session_id="bg-test", user_id="system")
        agent.channel = None
        agent.source = None
        agent.reply_mode = ReplyMode.CAPTURE_ONLY
        agent.persist_output_message = True
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
        agent._save_agent_message_to_db = AsyncMock()

        with patch.object(memory_manager, "save_agent_messages") as save_messages:
            await agent._execute_agent([])

        agent.send_agent_message.assert_not_awaited()
        agent._save_agent_message_to_db.assert_awaited_once_with(
            "后台结果", title="MoviePilot助手"
        )
        save_messages.assert_called_once()
        self.assertEqual("后台结果", agent._streamed_output)

    async def test_heartbeat_check_jobs_captures_final_reply_and_keeps_message_tools(self):
        manager = AgentManager()

        with (
            patch("app.agent.load_jobs_metadata", new=AsyncMock(return_value=[{
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
        self.assertEqual(ReplyMode.CAPTURE_ONLY, kwargs["reply_mode"])
        self.assertFalse(kwargs["persist_output_message"])
        self.assertTrue(kwargs["allow_message_tools"])

    async def test_heartbeat_check_jobs_skips_when_no_active_jobs(self):
        manager = AgentManager()

        with (
            patch("app.agent.load_jobs_metadata", new=AsyncMock(return_value=[])),
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
            patch("app.agent.prompt_manager.get_agent_prompt", return_value="PROMPT"),
            patch("app.agent.create_subagent_middlewares", return_value=([], [])),
            patch(
                "app.agent.MoviePilotToolFactory.get_tool_selector_always_include_names",
                return_value=[],
            ),
            patch("app.agent.SkillsMiddleware", side_effect=lambda *args, **kwargs: "skills"),
            patch("app.agent.JobsMiddleware", side_effect=lambda *args, **kwargs: "jobs"),
            patch("app.agent.RuntimeConfigMiddleware", side_effect=lambda *args, **kwargs: "runtime"),
            patch("app.agent.MemoryMiddleware", side_effect=lambda *args, **kwargs: "memory"),
            patch("app.agent.ActivityLogMiddleware", side_effect=lambda *args, **kwargs: "activity"),
            patch("app.agent.SummarizationMiddleware", side_effect=lambda *args, **kwargs: "summary"),
            patch("app.agent.PatchToolCallsMiddleware", side_effect=lambda *args, **kwargs: "patch"),
            patch("app.agent.UsageMiddleware", side_effect=lambda *args, **kwargs: "usage"),
            patch("app.agent.InMemorySaver", return_value="checkpointer"),
            patch("app.agent.create_agent", side_effect=lambda **kwargs: kwargs),
        ):
            created = await agent._create_agent(streaming=False)

        self.assertEqual(
            ["skills", "jobs", "runtime", "memory", "summary", "patch", "usage"],
            created["middleware"],
        )

    def test_send_message_tool_is_always_included_by_tool_selector(self):
        send_message_tool = SimpleNamespace(name="send_message")

        always_include = MoviePilotToolFactory.get_tool_selector_always_include_names(
            [send_message_tool]
        )

        self.assertIn("send_message", always_include)

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
            patch("app.agent.prompt_manager.get_agent_prompt", return_value="PROMPT"),
            patch(
                "app.agent.create_subagent_middlewares",
                return_value=(
                    ["subagent"],
                    [
                        SimpleNamespace(name=SUBAGENT_TASK_TOOL_NAME),
                        SimpleNamespace(name=SUBAGENT_CONTROL_TOOL_NAME),
                    ],
                ),
            ),
            patch(
                "app.agent.MoviePilotToolFactory.get_tool_selector_always_include_names",
                return_value=[],
            ),
            patch("app.agent.SkillsMiddleware", side_effect=lambda *args, **kwargs: "skills"),
            patch("app.agent.JobsMiddleware", side_effect=lambda *args, **kwargs: "jobs"),
            patch("app.agent.RuntimeConfigMiddleware", side_effect=lambda *args, **kwargs: "runtime"),
            patch("app.agent.MemoryMiddleware", side_effect=lambda *args, **kwargs: "memory"),
            patch("app.agent.ActivityLogMiddleware", side_effect=lambda *args, **kwargs: "activity"),
            patch("app.agent.SummarizationMiddleware", side_effect=lambda *args, **kwargs: "summary"),
            patch("app.agent.PatchToolCallsMiddleware", side_effect=lambda *args, **kwargs: "patch"),
            patch("app.agent.UsageMiddleware", side_effect=lambda *args, **kwargs: "usage"),
            patch("app.agent.ToolSelectorMiddleware", side_effect=_tool_selector),
            patch("app.agent.InMemorySaver", return_value="checkpointer"),
            patch("app.agent.create_agent", side_effect=lambda **kwargs: kwargs),
        ):
            await agent._create_agent(streaming=False)

        self.assertIn(SUBAGENT_TASK_TOOL_NAME, captured["always_include"])
        self.assertIn(SUBAGENT_CONTROL_TOOL_NAME, captured["always_include"])

    async def test_create_agent_keeps_activity_log_for_normal_session(self):
        agent = MoviePilotAgent(session_id="normal-session", user_id="system")
        agent._initialize_tools = lambda: []
        agent._initialize_subagent_tools = lambda: []

        with (
            patch.object(settings, "LLM_MAX_TOOLS", 0),
            patch.object(agent, "_initialize_llm", new=AsyncMock(return_value=object())),
            patch("app.agent.prompt_manager.get_agent_prompt", return_value="PROMPT"),
            patch("app.agent.create_subagent_middlewares", return_value=([], [])),
            patch(
                "app.agent.MoviePilotToolFactory.get_tool_selector_always_include_names",
                return_value=[],
            ),
            patch("app.agent.SkillsMiddleware", side_effect=lambda *args, **kwargs: "skills"),
            patch("app.agent.JobsMiddleware", side_effect=lambda *args, **kwargs: "jobs"),
            patch("app.agent.RuntimeConfigMiddleware", side_effect=lambda *args, **kwargs: "runtime"),
            patch("app.agent.MemoryMiddleware", side_effect=lambda *args, **kwargs: "memory"),
            patch("app.agent.ActivityLogMiddleware", side_effect=lambda *args, **kwargs: "activity"),
            patch("app.agent.SummarizationMiddleware", side_effect=lambda *args, **kwargs: "summary"),
            patch("app.agent.PatchToolCallsMiddleware", side_effect=lambda *args, **kwargs: "patch"),
            patch("app.agent.UsageMiddleware", side_effect=lambda *args, **kwargs: "usage"),
            patch("app.agent.InMemorySaver", return_value="checkpointer"),
            patch("app.agent.create_agent", side_effect=lambda **kwargs: kwargs),
        ):
            created = await agent._create_agent(streaming=False)

        self.assertEqual(
            ["skills", "jobs", "runtime", "memory", "activity", "summary", "patch", "usage"],
            created["middleware"],
        )

    async def test_run_background_prompt_forces_disable_message_tools_when_capture_only(self):
        captured = {}

        async def fake_process(self, message, images=None, files=None):
            captured["message"] = message
            captured["reply_mode"] = self.reply_mode
            captured["allow_message_tools"] = self.allow_message_tools
            captured["user_id"] = self.user_id

        with (
            patch.object(MoviePilotAgent, "process", new=fake_process),
            patch.object(MoviePilotAgent, "cleanup", new=AsyncMock()),
            patch.object(memory_manager, "clear_memory"),
        ):
            await AgentManager.run_background_prompt(
                message="background task",
                reply_mode=ReplyMode.CAPTURE_ONLY,
                allow_message_tools=True,
            )

        self.assertEqual("background task", captured["message"])
        self.assertEqual(ReplyMode.CAPTURE_ONLY, captured["reply_mode"])
        self.assertFalse(captured["allow_message_tools"])
        self.assertEqual(SYSTEM_INTERNAL_USER_ID, captured["user_id"])
