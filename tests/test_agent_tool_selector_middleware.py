import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.middleware import selection as tool_selector_module
from app.agent.tools.tags import ToolTag


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
            model_name="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            runtime=None,
    ):
        self.model_name = model_name
        self.model = model_name
        self.openai_api_base = base_url
        self.api_base = base_url
        self.base_url = base_url
        self._moviepilot_llm_runtime = runtime
        self._moviepilot_llm_base_url = base_url
        self.messages = None
        self.ainvoke_calls = []
        self.bind_calls = []
        self.bound_model = _FakeBoundModel(content)

    def bind(self, **kwargs):
        self.bind_calls.append(kwargs)
        return self.bound_model

    async def ainvoke(self, messages):
        self.messages = messages
        self.ainvoke_calls.append(messages)
        return SimpleNamespace(content=self.bound_model.content)


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


def _tool(name, description, tags=None):
    """构造测试用工具对象。"""
    return SimpleNamespace(name=name, description=description, tags=tags or [])


def test_awrap_model_call_uses_json_prompt_for_all_models():
    """工具筛选应统一使用 JSON 提示，不绑定 provider 专属参数。"""
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

    assert model.bind_calls == []
    assert [tool.name for tool in result.tools] == ["search", "calendar"]
    system_message = model.messages[0]
    assert isinstance(system_message, SystemMessage)
    prompt = system_message.content
    assert "Return the answer in JSON only." in prompt
    assert "- search: Search for information" in prompt
    assert "- calendar: Manage events" in prompt
    assert "MoviePilot tool-chain hints:" in prompt
    assert len(handled_requests) == 1


def test_awrap_model_call_uses_same_json_prompt_for_minimax():
    """MiniMax 工具筛选也应复用同一套 JSON 提示路径。"""
    tools = [
        SimpleNamespace(name="search", description="Search for information"),
        SimpleNamespace(name="calendar", description="Manage events"),
        SimpleNamespace(name="translate", description="Translate text"),
    ]
    model = _FakeModel(
        model_name="MiniMax-M2.7",
        base_url="https://api.minimaxi.com/anthropic/v1",
        runtime="anthropic_compatible",
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

    state_update = asyncio.run(
        middleware.abefore_agent(request.state, runtime=None, config=None)
    )

    assert state_update == {"selected_tool_names": ["search", "calendar"]}
    assert model.bind_calls == []
    system_message = model.messages[0]
    assert isinstance(system_message, SystemMessage)
    assert "Return the answer in JSON only." in system_message.content


def test_awrap_model_call_uses_prompt_json_for_anthropic_runtime():
    """Anthropic-compatible runtime 不应触发额外 provider 分支。"""
    tools = [
        SimpleNamespace(name="search", description="Search for information"),
        SimpleNamespace(name="calendar", description="Manage events"),
        SimpleNamespace(name="translate", description="Translate text"),
    ]
    model = _FakeModel(
        model_name="kimi-k2",
        base_url="https://example.com/anthropic/v1",
        runtime="anthropic_compatible",
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

    state_update = asyncio.run(
        middleware.abefore_agent(request.state, runtime=None, config=None)
    )

    assert state_update == {"selected_tool_names": ["search", "calendar"]}
    assert model.bind_calls == []
    system_message = model.messages[0]
    assert isinstance(system_message, SystemMessage)
    assert "Return the answer in JSON only." in system_message.content


def test_awrap_model_call_reuses_first_selection_for_later_model_rounds():
    """多轮模型回合应复用首轮筛选出的工具集合。"""
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

    assert model.bind_calls == []
    assert [tool.name for tool in first_result.tools] == ["search", "calendar"]
    assert [tool.name for tool in second_result.tools] == ["search", "calendar"]
    assert len(handled_requests) == 2
    assert len(model.ainvoke_calls) == 1


def test_awrap_model_call_caches_plain_json_prompt_selection_too():
    """普通模型也应只调用一次 JSON 提示筛选并缓存结果。"""
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

    state_update = asyncio.run(
        middleware.abefore_agent(request.state, runtime=None, config=None)
    )
    if state_update:
        request.state.update(state_update)
    first_result = asyncio.run(middleware.awrap_model_call(request, handler))
    second_result = asyncio.run(middleware.awrap_model_call(request, handler))

    assert model.bind_calls == []
    assert len(model.ainvoke_calls) == 1
    assert [tool.name for tool in first_result.tools] == ["search", "calendar"]
    assert [tool.name for tool in second_result.tools] == ["search", "calendar"]


def test_tool_selection_failure_falls_back_to_all_tools():
    """筛选模型返回空响应时不应中断 Agent 请求。"""
    tools = [
        SimpleNamespace(name="search", description="Search for information"),
        SimpleNamespace(name="calendar", description="Manage events"),
    ]
    model = _FakeModel(content=None)
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

    state_update = asyncio.run(
        middleware.abefore_agent(request.state, runtime=None, config=None)
    )

    assert state_update == {"selected_tool_names": ["search", "calendar"]}


def test_empty_tool_selection_keeps_empty_tool_list():
    """工具筛选返回空数组时应保持空工具列表。"""
    tools = [
        SimpleNamespace(name="search", description="Search for information"),
        SimpleNamespace(name="calendar", description="Manage events"),
    ]
    model = _FakeModel(content='{"tools": []}')
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

    with patch.object(tool_selector_module.logger, "info") as logger_info, \
            patch.object(tool_selector_module.logger, "warning") as logger_warning:
        state_update = asyncio.run(
            middleware.abefore_agent(request.state, runtime=None, config=None)
        )
        request.state.update(state_update)
        result = asyncio.run(middleware.awrap_model_call(request, handler))

    assert state_update == {"selected_tool_names": []}
    assert result.tools == []
    logger_info.assert_called_once_with("工具筛选结果: 无有效工具")
    logger_warning.assert_not_called()


def test_empty_tool_selection_keeps_always_included_tools():
    """工具筛选返回空数组时仍应保留必须包括的工具。"""
    tools = [
        SimpleNamespace(name="search", description="Search for information"),
        SimpleNamespace(name="skill", description="Run skill"),
    ]
    model = _FakeModel(content='{"tools": []}')
    middleware = tool_selector_module.ToolSelectorMiddleware(
        max_tools=2,
        selection_tools=tools,
        always_include=["skill"],
    )
    middleware.model = model
    request = _FakeRequest(
        tools=tools,
        messages=[HumanMessage(content="不用工具，直接回答")],
        model=model,
    )

    async def handler(updated_request):
        return updated_request

    state_update = asyncio.run(
        middleware.abefore_agent(request.state, runtime=None, config=None)
    )
    request.state.update(state_update)
    result = asyncio.run(middleware.awrap_model_call(request, handler))

    assert state_update == {"selected_tool_names": ["skill"]}
    assert [tool.name for tool in result.tools] == ["skill"]


def test_abefore_agent_logs_selected_tools():
    """工具筛选返回有效工具时应记录最终生效的工具名。"""
    tools = [
        SimpleNamespace(name="search", description="Search for information"),
        SimpleNamespace(name="calendar", description="Manage events"),
    ]
    model = _FakeModel(content='{"tools": ["calendar"]}')
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

    with patch.object(tool_selector_module.logger, "info") as logger_info:
        state_update = asyncio.run(
            middleware.abefore_agent(request.state, runtime=None, config=None)
        )

    assert state_update == {"selected_tool_names": ["calendar"]}
    logger_info.assert_called_once_with("工具筛选结果: calendar")


def test_abefore_agent_logs_skipped_selection():
    """工具筛选未启用时也应记录跳过原因。"""
    middleware = tool_selector_module.ToolSelectorMiddleware(selection_tools=[])
    request_state = {"messages": [HumanMessage(content="帮我安排明天的行程")]}

    with patch.object(tool_selector_module.logger, "info") as logger_info:
        state_update = asyncio.run(
            middleware.abefore_agent(request_state, runtime=None, config=None)
        )

    assert state_update == {"selected_tool_names": None}
    logger_info.assert_called_once_with("工具筛选跳过: 没有可筛选工具。")


def test_normalize_selection_response_accepts_code_fence_json():
    """工具筛选响应应兼容 Markdown 代码围栏包裹的 JSON。"""
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

    assert normalized == {"tools": ["search"]}


def test_json_prompt_selection_uses_recent_conversation_context():
    """多轮追问时工具筛选应看到上一轮用户需求和助手回复。"""
    tools = [
        _tool(
            "query_plugin_config",
            "Query plugin config",
            [ToolTag.Read, ToolTag.Plugin, ToolTag.Settings],
        ),
        _tool(
            "update_plugin_config",
            "Update plugin config",
            [ToolTag.Write, ToolTag.Plugin, ToolTag.Settings],
        ),
        _tool(
            "reload_plugin",
            "Reload plugin",
            [ToolTag.Write, ToolTag.Plugin],
        ),
    ]
    model = _FakeModel(content='{"tools": ["query_plugin_config"]}')
    middleware = tool_selector_module.ToolSelectorMiddleware(
        max_tools=3,
        selection_tools=tools,
    )
    middleware.model = model
    request = _FakeRequest(
        tools=tools,
        messages=[
            HumanMessage(content="帮我检查插件 DemoPlugin 的配置"),
            AIMessage(content="我建议先查询插件配置，然后根据结果决定是否重载插件。"),
            HumanMessage(content="按你说的来"),
        ],
        model=model,
    )

    state_update = asyncio.run(
        middleware.abefore_agent(request.state, runtime=None, config=None)
    )

    user_message = model.messages[1]
    assert state_update == {
        "selected_tool_names": [
            "query_plugin_config",
            "update_plugin_config",
            "reload_plugin",
        ]
    }
    assert isinstance(user_message, HumanMessage)
    assert "Recent conversation context for tool selection" in user_message.content
    assert "帮我检查插件 DemoPlugin 的配置" in user_message.content
    assert "我建议先查询插件配置" in user_message.content
    assert "按你说的来" in user_message.content


def test_single_turn_selection_keeps_original_user_message():
    """单轮对话不应额外包裹上下文提示。"""
    tools = [
        _tool("search", "Search for information", [ToolTag.Read, ToolTag.Web]),
        _tool("calendar", "Manage events", [ToolTag.Write]),
    ]
    model = _FakeModel(content='{"tools": ["search"]}')
    middleware = tool_selector_module.ToolSelectorMiddleware(
        max_tools=2,
        selection_tools=tools,
    )
    middleware.model = model
    original_message = HumanMessage(content="帮我查一下最近的更新")
    request = _FakeRequest(
        tools=tools,
        messages=[original_message],
        model=model,
    )

    asyncio.run(middleware.abefore_agent(request.state, runtime=None, config=None))

    user_message = model.messages[1]
    assert user_message is original_message
    assert "Recent conversation context for tool selection" not in user_message.content


def test_process_selection_response_completes_low_count_tool_group_by_tags():
    """筛选结果过少时应按已命中的工具标签组补齐同组工具。"""
    tools = [
        _tool(
            "search_media",
            "Search media",
            [ToolTag.Read, ToolTag.Media],
        ),
        _tool(
            "search_torrents",
            "Search torrents",
            [ToolTag.Read, ToolTag.Resource, ToolTag.Site, ToolTag.Media],
        ),
        _tool(
            "get_search_results",
            "Get results",
            [ToolTag.Read, ToolTag.Resource],
        ),
        _tool(
            "add_download_tasks",
            "Add downloads",
            [ToolTag.Write, ToolTag.Download, ToolTag.Resource],
        ),
        _tool(
            "query_download_tasks",
            "Query downloads",
            [ToolTag.Read, ToolTag.Download],
        ),
    ]
    middleware = tool_selector_module.ToolSelectorMiddleware(
        max_tools=4,
        selection_tools=tools,
    )
    request = _FakeRequest(
        tools=tools,
        messages=[HumanMessage(content="帮我下载流浪地球")],
        model=_FakeModel(),
    )

    result = middleware._process_selection_response(
        {"tools": ["search_torrents"]},
        available_tools=tools,
        valid_tool_names=[tool.name for tool in tools],
        request=request,
    )

    assert len(result.tools) == 4
    assert {tool.name for tool in result.tools} == {
        "search_media",
        "search_torrents",
        "get_search_results",
        "add_download_tasks",
    }


def test_process_selection_response_keeps_high_count_selection():
    """筛选结果数量足够时不应额外补齐工具。"""
    tools = [
        SimpleNamespace(name="search_media", description="Search media"),
        SimpleNamespace(name="search_torrents", description="Search torrents"),
        SimpleNamespace(name="get_search_results", description="Get results"),
        SimpleNamespace(name="query_sites", description="Query sites"),
    ]
    middleware = tool_selector_module.ToolSelectorMiddleware(
        max_tools=4,
        selection_tools=tools,
    )
    request = _FakeRequest(
        tools=tools,
        messages=[HumanMessage(content="帮我下载流浪地球")],
        model=_FakeModel(),
    )

    result = middleware._process_selection_response(
        {
            "tools": [
                "search_media",
                "search_torrents",
                "get_search_results",
                "query_sites",
            ]
        },
        available_tools=tools,
        valid_tool_names=[tool.name for tool in tools],
        request=request,
    )

    assert [tool.name for tool in result.tools] == [
        "search_media",
        "search_torrents",
        "get_search_results",
        "query_sites",
    ]


def test_process_selection_response_respects_max_tools_when_completing():
    """标签组补齐不应突破 max_tools 上限。"""
    tools = [
        _tool(
            "list_directory",
            "List directory",
            [ToolTag.Read, ToolTag.Directory, ToolTag.File],
        ),
        _tool(
            "query_directory_settings",
            "Query settings",
            [ToolTag.Read, ToolTag.Directory, ToolTag.Settings],
        ),
        _tool(
            "recognize_media",
            "Recognize media",
            [ToolTag.Read, ToolTag.Media],
        ),
        _tool(
            "transfer_file",
            "Transfer file",
            [ToolTag.Write, ToolTag.Transfer, ToolTag.Library, ToolTag.File],
        ),
    ]
    middleware = tool_selector_module.ToolSelectorMiddleware(
        max_tools=2,
        selection_tools=tools,
    )
    request = _FakeRequest(
        tools=tools,
        messages=[HumanMessage(content="帮我整理这个目录")],
        model=_FakeModel(),
    )

    result = middleware._process_selection_response(
        {"tools": ["transfer_file"]},
        available_tools=tools,
        valid_tool_names=[tool.name for tool in tools],
        request=request,
    )

    assert len(result.tools) == 2
    assert {tool.name for tool in result.tools} == {"transfer_file", "list_directory"}


def test_process_selection_response_ignores_generic_tags_when_completing():
    """通用权限标签不应被当作工具组使用。"""
    tools = [
        _tool("read_one", "Read one", [ToolTag.Read]),
        _tool("read_two", "Read two", [ToolTag.Read]),
        _tool("write_one", "Write one", [ToolTag.Write, ToolTag.Admin]),
    ]
    middleware = tool_selector_module.ToolSelectorMiddleware(
        max_tools=4,
        selection_tools=tools,
    )
    request = _FakeRequest(
        tools=tools,
        messages=[HumanMessage(content="查一下信息")],
        model=_FakeModel(),
    )

    result = middleware._process_selection_response(
        {"tools": ["read_one"]},
        available_tools=tools,
        valid_tool_names=[tool.name for tool in tools],
        request=request,
    )

    assert [tool.name for tool in result.tools] == ["read_one"]
