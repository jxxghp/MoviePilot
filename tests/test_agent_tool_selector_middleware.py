import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from app.agent.middleware import tool_selection as tool_selector_module


class _FakeBoundModel:
    def __init__(self, content):
        self.content = content
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content=self.content)

    async def ainvoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content=self.content)


class _FakeModel:
    def __init__(
        self,
        *,
        content='{"tools": ["calendar", "search"]}',
        model_name="deepseek-reasoner",
        base_url="https://api.deepseek.com",
    ):
        self.model_name = model_name
        self.openai_api_base = base_url
        self.bind_calls = []
        self.bound_model = _FakeBoundModel(content)

    def bind(self, **kwargs):
        self.bind_calls.append(kwargs)
        return self.bound_model


class _FakeRequest:
    def __init__(self, *, tools, messages, model, state=None, runtime=None):
        self.tools = tools
        self.messages = messages
        self.model = model
        self.state = state if state is not None else {"messages": messages}
        self.runtime = runtime

    def override(self, **kwargs):
        data = {
            "tools": self.tools,
            "messages": self.messages,
            "model": self.model,
            "state": self.state,
            "runtime": self.runtime,
        }
        data.update(kwargs)
        return _FakeRequest(**data)


class ToolSelectorMiddlewareTest(unittest.TestCase):
    def test_awrap_model_call_uses_json_mode_for_deepseek(self):
        tools = [
            SimpleNamespace(name="search", description="Search for information"),
            SimpleNamespace(name="calendar", description="Manage events"),
            SimpleNamespace(name="translate", description="Translate text"),
        ]
        model = _FakeModel()
        middleware = tool_selector_module.ToolSelectorMiddleware(
            max_tools=2,
            selection_tools=tools,
        )
        middleware.model = model
        request = _FakeRequest(
            tools=tools,
            messages=[HumanMessage(content="帮我安排明天的行程并查天气")],
            model=model,
        )
        handled_requests = []

        async def handler(updated_request):
            handled_requests.append(updated_request)
            return updated_request

        state_update = asyncio.run(
            middleware.abefore_agent(request.state, runtime=None, config=None)
        )
        if state_update:
            request.state.update(state_update)
        result = asyncio.run(middleware.awrap_model_call(request, handler))

        self.assertEqual(
            model.bind_calls,
            [{"response_format": {"type": "json_object"}}],
        )
        self.assertEqual(
            [tool.name for tool in result.tools],
            ["search", "calendar"],
        )
        prompt = model.bound_model.messages[0]["content"]
        self.assertIn("Return the answer in JSON only.", prompt)
        self.assertIn('- search: Search for information', prompt)
        self.assertIn('- calendar: Manage events', prompt)
        self.assertEqual(len(handled_requests), 1)

    def test_awrap_model_call_reuses_first_selection_for_later_model_rounds(self):
        tools = [
            SimpleNamespace(name="search", description="Search for information"),
            SimpleNamespace(name="calendar", description="Manage events"),
            SimpleNamespace(name="translate", description="Translate text"),
        ]
        model = _FakeModel(content='{"tools": ["calendar", "search"]}')
        middleware = tool_selector_module.ToolSelectorMiddleware(
            max_tools=2,
            selection_tools=tools,
        )
        middleware.model = model
        request = _FakeRequest(
            tools=tools,
            messages=[HumanMessage(content="帮我安排明天的行程并查天气")],
            model=model,
        )
        handled_requests = []

        async def handler(updated_request):
            handled_requests.append(updated_request)
            return updated_request

        state_update = asyncio.run(
            middleware.abefore_agent(request.state, runtime=None, config=None)
        )
        if state_update:
            request.state.update(state_update)
        first_result = asyncio.run(middleware.awrap_model_call(request, handler))
        second_result = asyncio.run(middleware.awrap_model_call(request, handler))

        self.assertEqual(
            model.bind_calls,
            [{"response_format": {"type": "json_object"}}],
        )
        self.assertEqual(
            [tool.name for tool in first_result.tools],
            ["search", "calendar"],
        )
        self.assertEqual(
            [tool.name for tool in second_result.tools],
            ["search", "calendar"],
        )
        self.assertEqual(len(handled_requests), 2)

    def test_awrap_model_call_caches_non_deepseek_selection_too(self):
        tools = [
            SimpleNamespace(name="search", description="Search for information"),
            SimpleNamespace(name="calendar", description="Manage events"),
            SimpleNamespace(name="translate", description="Translate text"),
        ]
        model = _FakeModel(
            model_name="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
        )
        middleware = tool_selector_module.ToolSelectorMiddleware(
            max_tools=2,
            selection_tools=tools,
        )
        middleware.model = model
        request = _FakeRequest(
            tools=tools,
            messages=[HumanMessage(content="帮我安排明天的行程并查天气")],
            model=model,
        )

        async def handler(updated_request):
            return updated_request

        parent_calls = 0

        async def _fake_parent_awrap(self, request_arg, handler_arg):
            nonlocal parent_calls
            parent_calls += 1
            selected_request = request_arg.override(
                tools=[request_arg.tools[1], request_arg.tools[0]]
            )
            return await handler_arg(selected_request)

        with patch.object(
            tool_selector_module.LLMToolSelectorMiddleware,
            "awrap_model_call",
            _fake_parent_awrap,
        ):
            state_update = asyncio.run(
                middleware.abefore_agent(request.state, runtime=None, config=None)
            )
            if state_update:
                request.state.update(state_update)
            first_result = asyncio.run(middleware.awrap_model_call(request, handler))
            second_result = asyncio.run(middleware.awrap_model_call(request, handler))

        self.assertEqual(parent_calls, 1)
        self.assertEqual(
            [tool.name for tool in first_result.tools],
            ["calendar", "search"],
        )
        self.assertEqual(
            [tool.name for tool in second_result.tools],
            ["calendar", "search"],
        )

    def test_normalize_selection_response_accepts_code_fence_json(self):
        middleware = tool_selector_module.ToolSelectorMiddleware()
        response = SimpleNamespace(
            content=[
                {
                    "type": "text",
                    "text": '```json\n{"tools": ["search"]}\n```',
                }
            ]
        )

        normalized = middleware._normalize_selection_response(response)

        self.assertEqual(normalized, {"tools": ["search"]})
