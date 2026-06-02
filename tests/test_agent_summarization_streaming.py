import asyncio
import unittest
from unittest.mock import patch

from langchain.agents.middleware import SummarizationMiddleware

import app.agent as agent_module
from app.agent.middleware.runtime_config import RuntimeConfigMiddleware


class _FakeLLM:
    _llm_type = "openai-chat"

    def __init__(self, model: str):
        self.model = model
        self.profile = {"max_input_tokens": 64000}


class TestAgentSummarizationStreaming(unittest.TestCase):
    def test_streaming_agent_uses_non_streaming_llm_for_summary(self):
        agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
        main_llm = _FakeLLM("main")
        non_streaming_llm = _FakeLLM("non-streaming")
        captured: dict = {}

        def _fake_create_agent(**kwargs):
            captured.update(kwargs)
            return object()

        with (
            patch.object(
                agent, "_initialize_llm", side_effect=[main_llm, non_streaming_llm]
            ),
            patch.object(agent, "_initialize_tools", return_value=[]),
            patch.object(
                agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"
            ),
            patch.object(
                agent_module, "create_subagent_middlewares", return_value=([], [])
            ),
            patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
            patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
        ):
            asyncio.run(agent._create_agent(streaming=True))

        summary_middleware = next(
            middleware
            for middleware in captured["middleware"]
            if isinstance(middleware, SummarizationMiddleware)
        )

        self.assertIs(captured["model"], main_llm)
        self.assertIs(summary_middleware.model, non_streaming_llm)

    def test_streaming_agent_uses_non_streaming_llm_for_model_middlewares(self):
        agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
        main_llm = _FakeLLM("main")
        non_streaming_llm = _FakeLLM("non-streaming")
        captured: dict = {}

        class _FakeToolSelectorMiddleware:
            def __init__(
                self,
                model,
                max_tools,
                always_include=None,
                selection_tools=None,
            ):
                self.model = model
                self.max_tools = max_tools
                self.always_include = always_include or []
                self.selection_tools = selection_tools or []

        def _fake_create_agent(**kwargs):
            captured.update(kwargs)
            return object()

        class _FakeTool:
            def __init__(self, name: str):
                self.name = name

        fake_tools = [
            _FakeTool("list_directory"),
            _FakeTool("write_file"),
            _FakeTool("read_file"),
            _FakeTool("edit_file"),
            _FakeTool("execute_command"),
            _FakeTool("search_media"),
        ]

        with (
            patch.object(
                agent, "_initialize_llm", side_effect=[main_llm, non_streaming_llm]
            ),
            patch.object(agent, "_initialize_tools", return_value=fake_tools),
            patch.object(
                agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"
            ),
            patch.object(
                agent_module, "create_subagent_middlewares", return_value=([], [])
            ),
            patch.object(
                agent_module,
                "ToolSelectorMiddleware",
                _FakeToolSelectorMiddleware,
            ),
            patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
            patch.object(agent_module.settings, "LLM_MAX_TOOLS", 3),
        ):
            asyncio.run(agent._create_agent(streaming=True))

        tool_selector_middleware = next(
            middleware
            for middleware in captured["middleware"]
            if isinstance(middleware, _FakeToolSelectorMiddleware)
        )

        self.assertIs(tool_selector_middleware.model, non_streaming_llm)
        self.assertEqual(tool_selector_middleware.max_tools, 3)
        self.assertEqual(
            tool_selector_middleware.always_include,
            [
                "list_directory",
                "write_file",
                "read_file",
                "edit_file",
                "execute_command",
            ],
        )
        self.assertEqual(tool_selector_middleware.selection_tools, fake_tools)

    def test_non_streaming_agent_reuses_main_llm_for_summary(self):
        agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
        main_llm = _FakeLLM("main")
        captured: dict = {}

        def _fake_create_agent(**kwargs):
            captured.update(kwargs)
            return object()

        with (
            patch.object(agent, "_initialize_llm", return_value=main_llm),
            patch.object(agent, "_initialize_tools", return_value=[]),
            patch.object(
                agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"
            ),
            patch.object(
                agent_module, "create_subagent_middlewares", return_value=([], [])
            ),
            patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
            patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
        ):
            asyncio.run(agent._create_agent(streaming=False))

        summary_middleware = next(
            middleware
            for middleware in captured["middleware"]
            if isinstance(middleware, SummarizationMiddleware)
        )

        self.assertIs(captured["model"], main_llm)
        self.assertIs(summary_middleware.model, main_llm)

    def test_agent_uses_runtime_config_middleware_instead_of_hooks(self):
        agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
        main_llm = _FakeLLM("main")
        captured: dict = {}

        def _fake_create_agent(**kwargs):
            captured.update(kwargs)
            return object()

        with (
            patch.object(agent, "_initialize_llm", return_value=main_llm),
            patch.object(agent, "_initialize_tools", return_value=[]),
            patch.object(
                agent_module.prompt_manager, "get_agent_prompt", return_value="prompt"
            ),
            patch.object(
                agent_module, "create_subagent_middlewares", return_value=([], [])
            ),
            patch.object(agent_module, "create_agent", side_effect=_fake_create_agent),
            patch.object(agent_module.settings, "LLM_MAX_TOOLS", 0),
        ):
            asyncio.run(agent._create_agent(streaming=False))

        self.assertTrue(
            any(
                isinstance(middleware, RuntimeConfigMiddleware)
                for middleware in captured["middleware"]
            )
        )
        self.assertFalse(
            any(type(middleware).__name__ == "AgentHooksMiddleware" for middleware in captured["middleware"])
        )
