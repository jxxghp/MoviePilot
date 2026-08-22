"""Agent 图缓存行为测试。"""

from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import MoviePilotAgent, ReplyMode, _CompiledAgentBundle
from app.agent.mcp import AgentMcpToolSpec
from app.agent.middleware.policy import AgentPolicyMiddleware
from app.agent.tools.catalog import (
    ToolCatalogSnapshot,
    ToolIdentityAmbiguousError,
)
from app.agent.tools.impl.mcp import create_external_mcp_tools
from app.runtime.config import settings
from app.schemas.agent import AgentMcpServerConfig


@pytest.fixture
def anyio_backend():
    """使用 asyncio 后端运行 anyio 异步测试。"""
    return "asyncio"


class _FakeGraphState:
    """提供 LangGraph get_state 测试替身。"""

    def __init__(self, messages):
        """保存测试消息状态。"""
        self.values = {"messages": messages}


class _CapturingAgent:
    """捕获传入消息的非流式 Agent 测试替身。"""

    def __init__(self):
        """初始化捕获容器。"""
        self.payload = None

    async def ainvoke(self, payload, config=None):
        """记录 Agent 调用输入。"""
        self.payload = payload

    def get_state(self, _config):
        """返回包含最终 AI 回复的图状态。"""
        return _FakeGraphState([AIMessage(content="ok")])


@pytest.mark.anyio
async def test_create_agent_reuses_cached_graph_when_signature_matches():
    """构造签名一致时应直接复用已编译 Agent 图。"""
    cached_graph = object()
    catalog = ToolCatalogSnapshot.from_tools(
        [], plugin_revision=0, factory_revision="factory-v1"
    )
    agent = MoviePilotAgent(session_id="cache-hit", user_id="user-1")
    agent._compiled_agent_bundle = _CompiledAgentBundle(
        signature=("sig",),
        agent=cached_graph,
        streaming=False,
        created_at=datetime.now(),
        tool_catalog=catalog,
        subagent_catalog=catalog,
        plugin_revision=0,
        mcp_config_signature="mcp-config",
        catalog_checked_at=datetime.now(),
    )

    with patch.object(
        agent,
        "_agent_bundle_signature",
        new=AsyncMock(return_value=("sig",)),
    ), patch(
        "app.agent.orchestrator._get_plugin_tools_revision",
        return_value=0,
    ), patch(
        "app.agent.orchestrator.agent_mcp_manager.config_signature",
        return_value="mcp-config",
    ), patch("app.agent.orchestrator.create_agent") as create_agent:
        graph = await agent._create_agent(streaming=False)

    assert graph is cached_graph
    assert agent._last_agent_cache_hit is True
    create_agent.assert_not_called()


@pytest.mark.anyio
async def test_fresh_catalog_cache_hit_skips_tool_and_mcp_discovery() -> None:
    """目录仍在 freshness 窗口内时，缓存命中不得重建工具或访问 MCP。"""
    cached_graph = object()
    catalog = ToolCatalogSnapshot.from_tools(
        [], plugin_revision=0, factory_revision="factory-v1"
    )
    agent = MoviePilotAgent(session_id="catalog-cache-hit", user_id="user-1")
    agent._compiled_agent_bundle = _CompiledAgentBundle(
        signature=("sig",),
        agent=cached_graph,
        streaming=False,
        created_at=datetime.now(),
        tool_catalog=catalog,
        subagent_catalog=catalog,
        plugin_revision=0,
        mcp_config_signature="mcp-config",
        catalog_checked_at=datetime.now(),
    )

    with patch.object(
        agent,
        "_agent_bundle_signature",
        new=AsyncMock(return_value=("sig",)),
    ), patch.object(
        agent,
        "_initialize_local_tool_catalogs",
        side_effect=AssertionError("tool catalog rebuilt"),
    ), patch(
        "app.agent.orchestrator._get_plugin_tools_revision",
        return_value=0,
    ), patch(
        "app.agent.orchestrator.agent_mcp_manager.config_signature",
        return_value="mcp-config",
    ), patch(
        "app.agent.orchestrator.agent_mcp_manager.list_enabled_tool_specs",
        new=AsyncMock(side_effect=AssertionError("MCP discovery called")),
    ):
        graph = await agent._create_agent(streaming=False)

    assert graph is cached_graph
    assert agent._last_agent_cache_hit is True


@pytest.mark.anyio
async def test_expired_unchanged_catalog_renews_freshness() -> None:
    """过期目录复核后若签名未变，应续期缓存而不是每轮重复 discovery。"""
    cached_graph = object()
    catalog = ToolCatalogSnapshot.from_tools(
        [], plugin_revision=0, factory_revision="factory-v1"
    )
    expired_at = datetime.now() - timedelta(minutes=5)
    agent = MoviePilotAgent(session_id="catalog-refresh", user_id="user-1")
    agent._compiled_agent_bundle = _CompiledAgentBundle(
        signature=("sig",),
        agent=cached_graph,
        streaming=False,
        created_at=datetime.now(),
        tool_catalog=catalog,
        subagent_catalog=catalog,
        plugin_revision=0,
        mcp_config_signature="mcp-config",
        catalog_checked_at=expired_at,
    )
    fake_llm = SimpleNamespace(
        _llm_type="openai-chat",
        model="fake",
        profile={"max_input_tokens": 64000},
    )
    temporary_middleware = SimpleNamespace(close=AsyncMock())

    with patch.object(
        agent,
        "_agent_bundle_signature",
        new=AsyncMock(return_value=("sig",)),
    ), patch.object(
        agent,
        "_initialize_local_tool_catalogs",
        return_value=(catalog, catalog),
    ), patch.object(
        agent,
        "_initialize_mcp_tools",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        agent,
        "_initialize_subagent_mcp_tools",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        agent,
        "_initialize_llm",
        new=AsyncMock(return_value=fake_llm),
    ), patch.object(
        agent,
        "_sync_model_profile",
    ), patch(
        "app.agent.orchestrator.ServerToolRegistry.resolve_web_search",
        return_value=SimpleNamespace(use_local_web_search=True),
    ), patch(
        "app.agent.orchestrator.LLMHelper.get_server_tools",
        return_value=[],
    ), patch(
        "app.agent.orchestrator.prompt_manager.get_agent_prompt",
        return_value="prompt",
    ), patch(
        "app.agent.orchestrator.SkillsMiddleware",
        return_value=SimpleNamespace(name="skills", tools=[]),
    ), patch(
        "app.agent.orchestrator.create_subagent_middlewares",
        return_value=([temporary_middleware], []),
    ), patch(
        "app.agent.orchestrator._get_plugin_tools_revision",
        return_value=0,
    ), patch(
        "app.agent.orchestrator.agent_mcp_manager.config_signature",
        return_value="mcp-config",
    ), patch(
        "app.agent.orchestrator.agent_mcp_manager.list_enabled_tool_specs",
        new=AsyncMock(return_value=[]),
    ):
        graph = await agent._create_agent(streaming=False)

    assert graph is cached_graph
    assert agent._compiled_agent_bundle.catalog_checked_at > expired_at
    temporary_middleware.close.assert_awaited_once()


@pytest.mark.anyio
async def test_create_agent_cancellation_closes_temporary_subagent_middleware() -> None:
    """构图取消时必须释放尚未被缓存接管的子代理控制器。"""
    catalog = ToolCatalogSnapshot.from_tools(
        [], plugin_revision=0, factory_revision="factory-v1"
    )
    fake_llm = SimpleNamespace(
        _llm_type="openai-chat",
        model="fake",
        profile={"max_input_tokens": 64000},
    )
    temporary_middleware = SimpleNamespace(close=AsyncMock())
    signature_started = __import__("asyncio").Event()
    agent = MoviePilotAgent(session_id="cancel-create", user_id="user-1")

    async def _wait_for_signature(*_args, **_kwargs):
        signature_started.set()
        await __import__("asyncio").Future()

    with patch.object(
        agent,
        "_resolve_llm_runtime_config",
        new=AsyncMock(return_value={"provider": "openai", "model": "fake"}),
    ), patch.object(
        agent,
        "_initialize_local_tool_catalogs",
        return_value=(catalog, catalog),
    ), patch.object(
        agent,
        "_initialize_mcp_tools",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        agent,
        "_initialize_subagent_mcp_tools",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        agent,
        "_initialize_llm",
        new=AsyncMock(return_value=fake_llm),
    ), patch.object(
        agent,
        "_sync_model_profile",
    ), patch.object(
        agent,
        "_agent_bundle_signature",
        new=_wait_for_signature,
    ), patch(
        "app.agent.orchestrator._get_plugin_tools_revision",
        return_value=0,
    ), patch(
        "app.agent.orchestrator.agent_mcp_manager.config_signature",
        return_value="mcp-config",
    ), patch(
        "app.agent.orchestrator.agent_mcp_manager.list_enabled_tool_specs",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.agent.orchestrator.ServerToolRegistry.resolve_web_search",
        return_value=SimpleNamespace(use_local_web_search=True),
    ), patch(
        "app.agent.orchestrator.LLMHelper.get_server_tools",
        return_value=[],
    ), patch(
        "app.agent.orchestrator.prompt_manager.get_agent_prompt",
        return_value="prompt",
    ), patch(
        "app.agent.orchestrator.SkillsMiddleware",
        return_value=SimpleNamespace(name="skills", tools=[]),
    ), patch(
        "app.agent.orchestrator.create_subagent_middlewares",
        return_value=([temporary_middleware], []),
    ):
        create_task = __import__("asyncio").create_task(
            agent._create_agent(streaming=False)
        )
        await signature_started.wait()
        create_task.cancel()
        with pytest.raises(__import__("asyncio").CancelledError):
            await create_task

    temporary_middleware.close.assert_awaited_once()


@pytest.mark.anyio
async def test_agent_bundle_signature_changes_with_temperature(monkeypatch) -> None:
    """温度配置变化时应使会话内 Agent 图缓存失效。"""
    agent = MoviePilotAgent(session_id="temperature-change", user_id="user-1")
    runtime_config = {
        "provider": "openai",
        "model": "gpt-test",
        "api_key": "test-key",
        "base_url": "https://llm.example.com/v1",
        "base_url_preset": None,
        "user_agent": None,
        "use_proxy": False,
        "thinking_level": "off",
        "api_protocol": "auto",
    }

    with patch.object(
        agent,
        "_resolve_llm_runtime_config",
        new=AsyncMock(return_value=runtime_config),
    ):
        monkeypatch.setattr(settings, "LLM_TEMPERATURE", 0.3)
        initial_signature = await agent._agent_bundle_signature(streaming=False)
        monkeypatch.setattr(settings, "LLM_TEMPERATURE", 1.0)
        updated_signature = await agent._agent_bundle_signature(streaming=False)

    assert updated_signature != initial_signature


@pytest.mark.anyio
async def test_agent_bundle_signature_changes_with_context_cap(monkeypatch) -> None:
    """有效窗口配置变化时应使会话内 Agent 图缓存失效。"""
    agent = MoviePilotAgent(session_id="context-cap-change", user_id="user-1")
    runtime_config = {
        "provider": "openai",
        "model": "gpt-test",
        "api_key": "test-key",
        "base_url": "https://llm.example.com/v1",
    }

    with patch.object(
        agent,
        "_resolve_llm_runtime_config",
        new=AsyncMock(return_value=runtime_config),
    ):
        monkeypatch.setattr(settings, "LLM_MAX_CONTEXT_TOKENS", 32)
        initial_signature = await agent._agent_bundle_signature(streaming=False)
        monkeypatch.setattr(settings, "LLM_MAX_CONTEXT_TOKENS", 64)
        updated_signature = await agent._agent_bundle_signature(streaming=False)

    assert updated_signature != initial_signature


@pytest.mark.anyio
async def test_agent_bundle_signature_changes_with_tool_catalog() -> None:
    """工具目录 revision 必须参与会话内 Agent 图缓存签名。"""
    agent = MoviePilotAgent(session_id="tool-revision", user_id="user-1")
    runtime_config = {"provider": "openai", "model": "gpt-test"}
    first_catalog = ToolCatalogSnapshot.from_tools(
        [], plugin_revision=1, factory_revision="factory-v1"
    )
    second_catalog = ToolCatalogSnapshot.from_tools(
        [], plugin_revision=2, factory_revision="factory-v1"
    )

    with patch.object(
        agent,
        "_resolve_llm_runtime_config",
        new=AsyncMock(return_value=runtime_config),
    ):
        first_signature = await agent._agent_bundle_signature(
            streaming=False,
            tool_catalog=first_catalog,
            subagent_catalog=first_catalog,
        )
        second_signature = await agent._agent_bundle_signature(
            streaming=False,
            tool_catalog=second_catalog,
            subagent_catalog=second_catalog,
        )

    assert second_signature != first_signature


@pytest.mark.anyio
@pytest.mark.parametrize("max_tools", [0, 5])
async def test_graph_keeps_mcp_first_winner_and_catalogs_all_collisions(
    max_tools: int,
) -> None:
    """主图与子图保持 MCP first-wins，严格目录覆盖全部客户端工具。"""
    servers = [
        AgentMcpServerConfig(
            id=server_id,
            name="Shared Name",
            transport="stdio",
            command=server_id,
        )
        for server_id in ("one", "two")
    ]
    specs = [
        AgentMcpToolSpec(
            server=server,
            name="echo",
            agent_tool_name="shared_echo",
            description="echo",
            input_schema={"type": "object", "properties": {}},
        )
        for server in servers
    ]
    main_tools = await create_external_mcp_tools(
        session_id="session",
        user_id="user",
        specs=specs,
    )
    subagent_tools = await create_external_mcp_tools(
        session_id="session",
        user_id="user",
        specs=specs,
    )
    empty_catalog = ToolCatalogSnapshot.from_tools(
        [], plugin_revision=0, factory_revision="factory-v1"
    )
    fake_llm = SimpleNamespace(
        _llm_type="openai-chat",
        model="fake",
        profile={"max_input_tokens": 64000},
    )
    skill_tool = SimpleNamespace(
        name="shared_echo",
        description="skill collision",
        args_schema={"type": "object", "properties": {}},
        _agent_tool_source="middleware:skills",
    )
    activity_tool = SimpleNamespace(
        name="query_activity_log",
        description="activity log",
        args_schema={"type": "object", "properties": {}},
        _agent_tool_source="middleware:activity_log",
    )
    subagent_task_tool = SimpleNamespace(
        name="task",
        description="subagent task",
        args_schema={"type": "object", "properties": {}},
        _agent_tool_source="middleware:subagents",
    )
    captured = {}
    agent = MoviePilotAgent(
        session_id="mcp-collision",
        user_id="user",
        channel="web",
        source="test",
    )

    def _capture_subagents(**kwargs):
        captured["subagent_tools"] = kwargs["tools"]
        captured["subagent_catalog"] = kwargs["catalog"]
        return [], [subagent_task_tool]

    def _capture_selector(**kwargs):
        captured["selection_tools"] = kwargs["selection_tools"]
        return SimpleNamespace(name="selector")

    def _capture_agent(**kwargs):
        captured["agent_tools"] = kwargs["tools"]
        captured["middlewares"] = kwargs["middleware"]
        return object()

    patchers = [
        patch.object(
            agent,
            "_resolve_llm_runtime_config",
            new=AsyncMock(return_value={"provider": "openai", "model": "fake"}),
        ),
        patch.object(
            agent,
            "_initialize_local_tool_catalogs",
            return_value=(empty_catalog, empty_catalog),
        ),
        patch.object(
            agent,
            "_initialize_mcp_tools",
            new=AsyncMock(return_value=main_tools),
        ),
        patch.object(
            agent,
            "_initialize_subagent_mcp_tools",
            new=AsyncMock(return_value=subagent_tools),
        ),
        patch.object(
            agent,
            "_agent_bundle_signature",
            new=AsyncMock(return_value=("mcp-collision", max_tools)),
        ),
        patch.object(
            agent,
            "_initialize_llm",
            new=AsyncMock(return_value=fake_llm),
        ),
        patch.object(agent, "_sync_model_profile"),
        patch(
            "app.agent.orchestrator._get_plugin_tools_revision",
            return_value=0,
        ),
        patch(
            "app.agent.orchestrator.agent_mcp_manager.config_signature",
            return_value="mcp-config",
        ),
        patch(
            "app.agent.orchestrator.agent_mcp_manager.list_enabled_tool_specs",
            new=AsyncMock(return_value=specs),
        ),
        patch(
            "app.agent.orchestrator.ServerToolRegistry.resolve_web_search",
            return_value=SimpleNamespace(use_local_web_search=True),
        ),
        patch("app.agent.orchestrator.LLMHelper.get_server_tools", return_value=[]),
        patch("app.agent.orchestrator.prompt_manager.get_agent_prompt", return_value="prompt"),
        patch(
            "app.agent.orchestrator.create_subagent_middlewares",
            side_effect=_capture_subagents,
        ),
        patch(
            "app.agent.tools.factory.MoviePilotToolFactory.get_tool_selector_always_include_names",
            return_value=[],
        ),
        patch(
            "app.agent.orchestrator.SkillsMiddleware",
            return_value=SimpleNamespace(name="skills", tools=[skill_tool]),
        ),
        patch(
            "app.agent.orchestrator.ActivityLogMiddleware",
            return_value=SimpleNamespace(name="activity", tools=[activity_tool]),
        ),
        patch(
            "app.agent.orchestrator.JobsMiddleware",
            return_value=SimpleNamespace(name="jobs"),
        ),
        patch(
            "app.agent.orchestrator.RuntimeConfigMiddleware",
            return_value=SimpleNamespace(name="runtime"),
        ),
        patch(
            "app.agent.orchestrator.MemoryMiddleware",
            return_value=SimpleNamespace(name="memory"),
        ),
        patch(
            "app.agent.orchestrator.SummarizationMiddleware",
            return_value=SimpleNamespace(name="summary"),
        ),
        patch(
            "app.agent.orchestrator.PatchToolCallsMiddleware",
            return_value=SimpleNamespace(name="patch"),
        ),
        patch(
            "app.agent.orchestrator.UsageMiddleware",
            return_value=SimpleNamespace(name="usage"),
        ),
        patch(
            "app.agent.orchestrator.ToolSelectorMiddleware",
            side_effect=_capture_selector,
        ),
        patch("app.agent.orchestrator.InMemorySaver", return_value=object()),
        patch("app.agent.orchestrator.create_agent", side_effect=_capture_agent),
        patch.object(settings, "LLM_MAX_TOOLS", max_tools),
    ]
    with ExitStack() as stack:
        for patcher in patchers:
            stack.enter_context(patcher)
        await agent._create_agent(streaming=False)

    assert captured["agent_tools"] == [main_tools[0], skill_tool, activity_tool]
    assert captured["subagent_tools"] == [subagent_tools[0]]
    if max_tools:
        assert captured["selection_tools"] == [
            main_tools[0],
            skill_tool,
            activity_tool,
            subagent_task_tool,
        ]
        assert captured["middlewares"][-3].name == "selector"
    else:
        assert "selection_tools" not in captured
    assert captured["middlewares"][-2].name == "FinalRequestCompactionMiddleware"
    assert captured["middlewares"][-1].name == "usage"
    policy_middleware = next(
        middleware
        for middleware in captured["middlewares"]
        if isinstance(middleware, AgentPolicyMiddleware)
    )
    assert [
        entry.source
        for entry in policy_middleware.catalog.collisions["shared_echo"]
    ] == ["mcp:one", "mcp:two", "middleware:skills"]
    assert (
        policy_middleware.catalog.resolve_unique("query_activity_log").tool
        is activity_tool
    )
    assert policy_middleware.catalog.resolve_unique("task").tool is subagent_task_tool
    with pytest.raises(ToolIdentityAmbiguousError, match="TOOL_IDENTITY_AMBIGUOUS"):
        policy_middleware.catalog.resolve_unique("shared_echo")
    assert [
        entry.source
        for entry in captured["subagent_catalog"].collisions["shared_echo"]
    ] == ["mcp:one", "mcp:two"]


@pytest.mark.anyio
async def test_execute_agent_sends_only_latest_message_on_cache_hit():
    """缓存命中时只把本轮新消息交给 LangGraph，避免重复提交历史。"""
    fake_graph = _CapturingAgent()
    agent = MoviePilotAgent(session_id="cache-hit", user_id="user-1")
    agent.reply_mode = ReplyMode.CAPTURE_ONLY
    agent._tool_context = {"user_reply_sent": False}
    agent._streamed_output = ""
    agent._should_stream = lambda: False
    agent.stream_handler = SimpleNamespace(
        stop_streaming=AsyncMock(return_value=(False, ""))
    )

    async def _create_agent(streaming=False):
        """模拟缓存命中后的 Agent 创建结果。"""
        agent._last_agent_cache_hit = True
        return fake_graph

    agent._create_agent = _create_agent
    messages = [HumanMessage(content="上一轮"), HumanMessage(content="本轮")]

    with patch("app.agent.orchestrator.eventmanager.send_event"):
        await agent._execute_agent(messages)

    assert agent._streamed_output == "ok"
    assert fake_graph.payload["messages"] == [messages[-1]]
