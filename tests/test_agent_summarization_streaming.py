import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import app.agent as agent_module
from app.agent.memory import memory_manager
from app.agent.middleware.runtime_config import RuntimeConfigMiddleware
from app.agent.middleware.summarization import (
    ContextSummarizationError,
    ContextPreservingSummarizationMiddleware,
)


class _FakeLLM:
    _llm_type = "openai-chat"

    def __init__(self, model: str):
        self.model = model
        self.profile = {"max_input_tokens": 64000}


class _FailingSummaryLLM(_FakeLLM):
    """模拟摘要模型暂时不可用。"""

    async def ainvoke(self, *_args, **_kwargs):
        """摘要请求始终超时。"""
        raise TimeoutError("summary provider unavailable")

    def invoke(self, *_args, **_kwargs):
        """同步摘要请求始终超时。"""
        raise TimeoutError("summary provider unavailable")


class _SuccessfulSummaryLLM(_FakeLLM):
    """提供稳定摘要结果。"""

    async def ainvoke(self, *_args, **_kwargs):
        """返回异步摘要。"""
        return AIMessage(content="保留旧事实的摘要")

    def invoke(self, *_args, **_kwargs):
        """返回同步摘要。"""
        return AIMessage(content="保留旧事实的摘要")


class _FailingGraph:
    """模拟上下文压缩阶段失败的 Agent 图。"""

    async def ainvoke(self, _payload, config=None):
        """在提交图状态前终止执行。"""
        raise ContextSummarizationError(
            "会话上下文压缩失败，原有上下文已保留，请稍后重试"
        )


def test_streaming_agent_uses_non_streaming_llm_for_summary():
    """流式 Agent 的摘要中间件应使用非流式 LLM。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    main_llm = _FakeLLM("main")
    non_streaming_llm = _FakeLLM("non-streaming")
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
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
        if isinstance(middleware, ContextPreservingSummarizationMiddleware)
    )

    assert captured["model"] is main_llm
    assert summary_middleware.model is non_streaming_llm


def test_streaming_agent_uses_non_streaming_llm_for_model_middlewares():
    """流式 Agent 的模型型中间件应使用非流式 LLM。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    main_llm = _FakeLLM("main")
    non_streaming_llm = _FakeLLM("non-streaming")
    captured: dict = {}

    class _FakeToolSelectorMiddleware:
        """记录工具选择中间件初始化参数。"""

        def __init__(
            self,
            model,
            max_tools,
            always_include=None,
            selection_tools=None,
        ):
            """保存测试断言需要的参数。"""
            self.model = model
            self.max_tools = max_tools
            self.always_include = always_include or []
            self.selection_tools = selection_tools or []

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
        captured.update(kwargs)
        return object()

    class _FakeTool:
        """测试用工具占位对象。"""

        def __init__(self, name: str):
            """保存工具名。"""
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

    assert tool_selector_middleware.model is non_streaming_llm
    assert tool_selector_middleware.max_tools == 3
    assert tool_selector_middleware.always_include == [
        "list_directory",
        "write_file",
        "read_file",
        "edit_file",
        "execute_command",
        "skill",
    ]
    assert tool_selector_middleware.selection_tools[: len(fake_tools)] == fake_tools
    assert [
        getattr(tool, "name", None)
        for tool in tool_selector_middleware.selection_tools[len(fake_tools):]
    ] == ["skill"]


def test_non_streaming_agent_reuses_main_llm_for_summary():
    """非流式 Agent 的摘要中间件应复用主 LLM。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    main_llm = _FakeLLM("main")
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
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
        if isinstance(middleware, ContextPreservingSummarizationMiddleware)
    )

    assert captured["model"] is main_llm
    assert summary_middleware.model is main_llm


def test_summary_failure_does_not_replace_existing_context():
    """摘要模型失败时应中止压缩，避免错误文本替换既有上下文。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    failing_llm = _FailingSummaryLLM("summary")
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
        captured.update(kwargs)
        return object()

    with (
        patch.object(agent, "_initialize_llm", return_value=failing_llm),
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
        if isinstance(middleware, ContextPreservingSummarizationMiddleware)
    )
    messages = [
        HumanMessage(content=f"必须保留的旧上下文 {index} " * 200)
        for index in range(160)
    ]
    assert summary_middleware.token_counter(messages) >= 64000 * 0.85
    cutoff_index = summary_middleware._determine_cutoff_index(messages)
    assert summary_middleware._trim_messages_for_summary(messages[:cutoff_index])

    with pytest.raises(RuntimeError, match="会话上下文压缩失败"):
        asyncio.run(summary_middleware.abefore_model({"messages": messages}, None))

    with pytest.raises(RuntimeError, match="会话上下文压缩失败"):
        summary_middleware.before_model({"messages": messages}, None)


def test_summary_success_still_replaces_old_context():
    """摘要成功时仍应保留上游的压缩行为。"""
    middleware = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("messages", 25),
    )
    messages = [HumanMessage(content=f"消息 {index}") for index in range(25)]

    update = asyncio.run(middleware.abefore_model({"messages": messages}, None))

    assert update is not None
    assert "保留旧事实的摘要" in update["messages"][1].content
    assert len(update["messages"]) < len(messages)


def test_unsummarizable_message_requires_new_context_instead_of_retry():
    """确定性不可裁剪的消息应给出可前进路径，而非建议无效重试。"""
    middleware = ContextPreservingSummarizationMiddleware(
        model=_SuccessfulSummaryLLM("summary"),
        trigger=("messages", 21),
        keep=("messages", 20),
        trim_tokens_to_summarize=1000,
    )
    messages = [
        HumanMessage(content="无法裁剪的单条超长消息" * 4000),
        *[HumanMessage(content=f"后续消息 {index}") for index in range(20)],
    ]
    assert not middleware._trim_messages_for_summary(messages[:1])

    errors = []
    for _ in range(2):
        with pytest.raises(ContextSummarizationError) as error:
            middleware.before_model({"messages": messages}, None)
        errors.append(str(error.value))

    assert errors == [
        "会话历史中存在无法压缩的超长内容，原有上下文已保留，请新建或清空会话后继续"
    ] * 2
    assert all("稍后重试" not in error for error in errors)


def test_summary_failure_preserves_database_history():
    """上下文压缩失败时不得覆盖数据库中的上一轮消息。"""
    session_id = f"summary-failure-{uuid.uuid4().hex}"
    user_id = "10001"
    memory_manager.save_agent_messages(
        session_id=session_id,
        user_id=user_id,
        messages=[HumanMessage(content="数据库中的旧事实")],
    )
    memory_manager.clear_memory(session_id, user_id)
    restored_messages = memory_manager.get_agent_messages(session_id, user_id)
    agent = agent_module.MoviePilotAgent(session_id=session_id, user_id=user_id)
    agent._compiled_agent_bundle = object()
    agent._should_stream = lambda: False
    agent._create_agent = AsyncMock(return_value=_FailingGraph())
    agent.stream_handler = SimpleNamespace(
        stop_streaming=AsyncMock(return_value=(False, ""))
    )
    agent.send_agent_message = AsyncMock()

    with (
        patch("app.agent.eventmanager.send_event") as send_usage_event,
    ):
        result, _ = asyncio.run(
            agent._execute_agent(
                [*restored_messages, HumanMessage(content="继续原来的任务")]
            )
        )

    memory_manager.clear_memory(session_id, user_id)
    recovered_messages = memory_manager.get_agent_messages(session_id, user_id)
    assert result == "智能助手执行失败: 会话上下文压缩失败，原有上下文已保留，请稍后重试"
    assert agent._compiled_agent_bundle is None
    assert [message.content for message in recovered_messages] == ["数据库中的旧事实"]
    send_usage_event.assert_called_once()
    assert not send_usage_event.call_args.args[1].success


def test_agent_uses_runtime_config_middleware_instead_of_hooks():
    """Agent 应使用运行时配置中间件而不是旧 hooks。"""
    agent = agent_module.MoviePilotAgent(session_id="session-1", user_id="10001")
    main_llm = _FakeLLM("main")
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        """捕获 create_agent 参数。"""
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

    assert any(
        isinstance(middleware, RuntimeConfigMiddleware)
        for middleware in captured["middleware"]
    )
    assert not any(
        type(middleware).__name__ == "AgentHooksMiddleware"
        for middleware in captured["middleware"]
    )
